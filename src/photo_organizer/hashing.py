import fcntl
import hashlib
import os
import shutil

CHUNK = 8 << 20
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


def sha256_file(filepath, nocache=False):
    """SHA256 of a file; nocache=True reads from the storage itself, not the client cache."""
    h = hashlib.sha256()
    with _open_read(filepath, nocache) as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def copy_and_hash(src, dst):
    """Copy src to a new file dst in one read pass, returning the SHA256 of the bytes written.
    Fails if dst already exists; preserves timestamps like shutil.copy2."""
    h = hashlib.sha256()
    with open(src, 'rb', buffering=0) as fi, open(dst, 'xb', buffering=CHUNK) as fo:
        while chunk := fi.read(CHUNK):
            h.update(chunk)
            fo.write(chunk)
    shutil.copystat(src, dst)
    return h.hexdigest()
