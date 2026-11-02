import fcntl
import hashlib
import os

CHUNK = 8 << 20
# Files this big report live progress while hashing, copying and verifying.
LARGE_FILE = 1 << 30
# macOS: per-descriptor cache bypass. Over SMB, a normal read of a just-written file is served
# from this Mac's memory cache, so verification must use this to read what the NAS stored.
F_NOCACHE = getattr(fcntl, 'F_NOCACHE', 48)


def _open_read(path, nocache):
    fd = os.open(path, os.O_RDONLY)
    if nocache:
        try:
            fcntl.fcntl(fd, F_NOCACHE, 1)
        except OSError:
            pass
    return os.fdopen(fd, 'rb', buffering=0)


def sha256_file(filepath, nocache=False, progress=None):
    """SHA256 of a file; nocache=True reads from the storage itself, not the client cache.
    progress(bytes_done) is called after each chunk when given."""
    h = hashlib.sha256()
    done = 0
    with _open_read(filepath, nocache) as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            if progress:
                done += len(chunk)
                progress(done)
    return h.hexdigest()


QUICK_SAMPLE = 1 << 20


def quick_offsets(size):
    """Start, middle and end samples. nas.HELPER repeats this exactly; a test keeps them equal."""
    return sorted({0, max(size // 2 - QUICK_SAMPLE // 2, 0), max(size - QUICK_SAMPLE, 0)})


def quick_hash(path, nocache=False):
    """SHA256 of the size and three 1 MB samples: different values prove different content."""
    size = os.path.getsize(path)
    h = hashlib.sha256(str(size).encode())
    with _open_read(path, nocache) as f:
        for off in quick_offsets(size):
            h.update(os.pread(f.fileno(), QUICK_SAMPLE, off))
    return h.hexdigest()


def copy_and_hash(src, dst, progress=None):
    """Copy src to a new file dst in one read pass, returning the SHA256 of the bytes written.
    Fails if dst already exists. Only timestamps are carried over: SMB rejects local BSD flags
    such as UF_TRACKED (shutil.copystat raised EINVAL), and flags/xattrs don't matter in the store."""
    # Overlapping reads with writes was benchmarked for NAS-to-NAS copies of large files and was
    # no faster under load (the shared SMB link is the limit), so all sizes use this one loop.
    h = hashlib.sha256()
    done = 0
    report = progress if progress and os.path.getsize(src) >= LARGE_FILE else None
    with open(src, 'rb', buffering=0) as fi, open(dst, 'xb', buffering=CHUNK) as fo:
        while chunk := fi.read(CHUNK):
            h.update(chunk)
            fo.write(chunk)
            if report:
                done += len(chunk)
                report(done)
    digest = h.hexdigest()
    st = os.stat(src)
    try:
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
        pass  # a timestamp the destination won't take is no reason to discard a verified copy
    return digest
