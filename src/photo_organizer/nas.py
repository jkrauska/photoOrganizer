"""Run transfers on the NAS itself when source and store are SMB shares of the same server.

Over SMB every file crosses the network three times (read source, write copy, read copy back
to verify). On the NAS the same move is a ZFS block clone (copy_file_range shares the blocks
across datasets of one pool, so nothing is rewritten) plus two local hashes, the second of
which is served from the ARC. The Mac keeps the catalog, dates and duplicate decisions; a
small stdlib-only helper started over ssh does the file operations.

Safety is unchanged: a source is deleted only after the copy's bytes, read on the NAS, hash
equal to the source's. Files are created by the ssh user, so they inherit the store's ACL
the same way SMB-created files do.
"""

import json
import os
import re
import shlex
import subprocess
import threading

from photo_organizer.constants import THUMB_CACHE_DIRS

# Runs on the NAS (python3, stdlib only). Protocol: one JSON request per line on stdin;
# zero or more {"p": bytes_done} progress lines, then {"ok": result} or {"err", "errno"}.
HELPER = r'''
import errno, hashlib, json, os, sys
CHUNK = 8 << 20
REPORT = 64 << 20

def out(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()

def sha(path, report=False, base=0, scale=1.0):
    h = hashlib.sha256()
    done = last = 0
    with open(path, "rb", buffering=0) as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            done += len(b)
            if report and done - last >= REPORT:
                out({"p": int(base + done * scale)})
                last = done
    return h.hexdigest()

def clone(src, dst, size):
    """Copy src to the new file dst: a block clone where the filesystem allows, else a copy."""
    st = os.stat(src)
    fi = os.open(src, os.O_RDONLY)
    try:
        fo = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
    except BaseException:
        os.close(fi)
        raise                   # dst exists or can't be made: it isn't ours to remove
    try:
        try:
            off, cfr = 0, hasattr(os, "copy_file_range")
            while off < size:
                if cfr:
                    try:
                        k = os.copy_file_range(fi, fo, size - off, off, off)
                    except OSError as e:
                        if e.errno not in (errno.EXDEV, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL):
                            raise
                        cfr = False
                        continue
                else:
                    b = os.pread(fi, CHUNK, off)
                    k = os.pwrite(fo, b, off) if b else 0
                if k == 0:
                    break
                off += k
            if off != size:
                raise OSError(errno.EIO, "short copy: %d of %d bytes" % (off, size))
            os.fsync(fo)
        finally:
            os.close(fo)
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    except BaseException:
        try:
            os.unlink(dst)
        except OSError:
            pass
        raise
    finally:
        os.close(fi)

def op_copy(src, dst, size, report):
    if os.path.getsize(src) != size:
        raise OSError(errno.EIO, "source size %d != catalog size %d" % (os.path.getsize(src), size))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    clone(src, dst, size)
    try:
        s = sha(src, report, 0, 0.5)
        d = sha(dst, report, size / 2, 0.5)
        if s != d:
            raise OSError(errno.EIO, "copy hash mismatch on NAS")
    except BaseException:
        os.unlink(dst)
        raise
    return s

def handle(r):
    op = r["op"]
    if op == "ping":
        return {"pid": os.getpid(), "copy_file_range": hasattr(os, "copy_file_range")}
    if op == "stat":
        try:
            st = os.stat(r["path"])
        except FileNotFoundError:
            return None
        return {"size": st.st_size, "file": not os.path.isdir(r["path"])}
    if op == "hash":
        return sha(r["path"], r.get("report"))
    if op == "copy":
        return op_copy(r["src"], r["dst"], r["size"], r.get("report"))
    if op == "unlink":
        if os.path.isdir(r["path"]) or os.path.islink(r["path"]):
            raise OSError(errno.EISDIR, "not a regular file: " + r["path"])
        os.unlink(r["path"])
        return True
    if op == "entries":
        with os.scandir(r["path"]) as it:
            return [[e.name, e.is_dir(follow_symlinks=False)] for e in it]
    if op == "ismount":
        return os.path.ismount(r["path"])
    if op == "rmdir":
        os.rmdir(r["path"])
        return True
    if op == "rmtree":
        import shutil
        if os.path.basename(r["path"]) not in r["allowed"] or os.path.islink(r["path"]):
            raise OSError(errno.EPERM, "rmtree refused: " + r["path"])
        shutil.rmtree(r["path"])
        return True
    if op == "listdir":
        return sorted(n for n in os.listdir(r["path"]) if not n.startswith("."))
    if op == "candidates":
        import glob
        found = []
        for pat in ("/mnt/*/%s", "/mnt/*/*/%s", "/mnt/*/*/*/%s"):
            found += [p for p in glob.glob(pat % glob.escape(r["share"])) if os.path.isdir(p)]
        return [[p, sorted(n for n in os.listdir(p) if not n.startswith("."))] for p in found]
    raise ValueError("unknown op " + op)

for line in sys.stdin:
    try:
        out({"ok": handle(json.loads(line))})
    except Exception as e:
        out({"err": "%s: %s" % (type(e).__name__, e), "errno": getattr(e, "errno", None)})
'''


class NasError(OSError):
    pass


class NasFS:
    """File operations executed on the NAS. Paths are this Mac's SMB paths, mapped per share."""

    verifies_copy = True    # copy_and_hash already compared source and copy hashes on the NAS

    def __init__(self, target, mapping, command=None):
        self.target = target                        # ssh destination, e.g. user@newnas.local
        self.mapping = sorted(mapping, key=lambda m: -len(m[0]))   # [(local_prefix, nas_prefix)]
        self.command = command or ['ssh', '-o', 'BatchMode=yes', '-o', 'ServerAliveInterval=30',
                                   target, 'python3 -u -c ' + shlex.quote(HELPER)]
        self.label = f"on NAS {target.split('@')[-1]}"
        self._local = threading.local()
        self._procs = []
        self._procs_lock = threading.Lock()

    def remote(self, path):
        for local, nas in self.mapping:
            if path == local or path.startswith(local + '/'):
                return nas + path[len(local):]
        raise ValueError(f"not on a mapped NAS share: {path}")

    def _proc(self):
        p = getattr(self._local, 'proc', None)
        if p is None or p.poll() is not None:
            # Own session: a Ctrl-C meant for the organizer must not kill an in-flight operation.
            p = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 text=True, bufsize=1, start_new_session=True)
            self._local.proc = p
            with self._procs_lock:
                self._procs.append(p)
        return p

    def call(self, op, progress=None, **args):
        p = self._proc()
        try:
            p.stdin.write(json.dumps(dict(args, op=op, report=progress is not None)) + '\n')
            p.stdin.flush()
            while True:
                line = p.stdout.readline()
                if not line:
                    raise NasError(5, f"NAS helper exited during {op}")
                msg = json.loads(line)
                if 'p' in msg:
                    progress(msg['p'])
                elif 'ok' in msg:
                    return msg['ok']
                else:
                    raise OSError(msg.get('errno') or 5, f"on NAS: {msg['err']}")
        except (BrokenPipeError, ValueError) as e:
            p.kill()
            raise NasError(5, f"NAS helper failed during {op}: {e}")

    # --- the interface transfer.py uses (LocalFS has the same methods) ---

    def exists(self, path):
        return self.call('stat', path=self.remote(path)) is not None

    def getsize(self, path):
        st = self.call('stat', path=self.remote(path))
        if st is None:
            raise FileNotFoundError(2, 'No such file', path)
        return st['size']

    def sha256(self, path, nocache=False, progress=None):
        return self.call('hash', progress, path=self.remote(path))

    def copy_and_hash(self, src, dst, size, progress=None):
        return self.call('copy', progress, src=self.remote(src), dst=self.remote(dst), size=size)

    def unlink(self, path):
        self.call('unlink', path=self.remote(path))

    def remove_source(self, path):
        self.call('unlink', path=self.remote(path))

    def entries(self, path):
        return [tuple(e) for e in self.call('entries', path=self.remote(path))]

    def ismount(self, path):
        return self.call('ismount', path=self.remote(path))

    def rmdir(self, path):
        self.call('rmdir', path=self.remote(path))

    def rmtree(self, path):
        # Only thumbnail caches are ever removed recursively.
        self.call('rmtree', path=self.remote(path), allowed=sorted(THUMB_CACHE_DIRS))

    def close(self):
        with self._procs_lock:
            procs, self._procs = self._procs, []
        for p in procs:
            try:
                p.stdin.close()
                p.wait(timeout=10)
            except Exception:
                p.kill()


_SMB_MOUNT = re.compile(r'^//(?:([^@/]+)@)?([^/]+)/(\S+) on (.+) \(smbfs')


def smb_mounts():
    """{mountpoint: (user, host, share)} for mounted SMB shares."""
    out = subprocess.run(['mount'], capture_output=True, text=True).stdout
    mounts = {}
    for line in out.splitlines():
        m = _SMB_MOUNT.match(line)
        if m:
            user, host, share, point = m.groups()
            from urllib.parse import unquote
            mounts[point] = (unquote(user) if user else None, host.lower(), unquote(share))
    return mounts


def _mount_of(path, mounts):
    best = None
    for point in mounts:
        if (path == point or path.startswith(point + '/')) and (best is None or len(point) > len(best)):
            best = point
    return best


def _visible_names(path):
    return sorted(n for n in os.listdir(path) if not n.startswith('.'))


def detect(sources, dest, mounts=None, make_fs=NasFS):
    """A NasFS when dest and every source are SMB shares of one server reachable over ssh,
    with each share's server path identified by matching its listing; otherwise None."""
    mounts = smb_mounts() if mounts is None else mounts
    points = [_mount_of(p, mounts) for p in [dest, *sources]]
    if None in points or len({mounts[p][1] for p in points}) != 1:
        return None
    user, host, _ = mounts[points[0]]
    fs = make_fs(f"{user}@{host}" if user else host, [])
    try:
        fs.call('ping')
        mapping = []
        for point in sorted(set(points)):
            local = _visible_names(point)
            matches = [nas for nas, names in fs.call('candidates', share=mounts[point][2]) if names == local]
            if len(matches) != 1 or not local:
                print(f"  NAS mode off: can't identify the server path of {point} (candidates: {matches})")
                fs.close()
                return None
            mapping.append((point, matches[0]))
    except OSError as e:
        print(f"  NAS mode off: {e}")
        fs.close()
        return None
    fs.mapping = sorted(mapping, key=lambda m: -len(m[0]))
    return fs
