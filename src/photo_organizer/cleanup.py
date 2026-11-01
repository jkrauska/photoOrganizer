"""Remove source folders emptied by transfers."""

import os

from photo_organizer.constants import JUNK_NAMES, THUMB_CACHE_DIRS
from photo_organizer.scanner import should_skip_dir


def _is_junk(name):
    return name in JUNK_NAMES or name in THUMB_CACHE_DIRS or name.startswith('._')


def prune_empty_dirs(root, verbose=False, fs=None):
    """Delete folders under root that hold nothing but OS junk files and NAS thumbnail caches
    (.@__thumb, @eaDir), deepest first, including root itself once it is emptied. Never removes
    a mount point, and never enters NAS snapshot/recycle folders or app bundles. Returns the
    number of folders removed. fs is a transfer.LocalFS or nas.NasFS: after deletes made on the
    NAS, this Mac's SMB directory cache can still list the files, so pruning runs there too."""
    if fs is None:
        from photo_organizer.transfer import LocalFS
        fs = LocalFS()

    def visit(path):
        removed = 0
        try:
            entries = fs.entries(path)
        except OSError:
            return 0, False
        for name, is_dir in entries:
            if is_dir and not should_skip_dir(name):
                n, _ = visit(os.path.join(path, name))
                removed += n
        try:
            remaining = fs.entries(path)
        except OSError:
            return removed, False
        if any(not _is_junk(name) for name, _ in remaining) or fs.ismount(path):
            return removed, False
        try:
            for name, is_dir in remaining:
                p = os.path.join(path, name)
                if is_dir and name in THUMB_CACHE_DIRS:
                    fs.rmtree(p)
                else:
                    fs.unlink(p)
            fs.rmdir(path)
        except OSError:
            return removed, False
        if verbose:
            print(f"  [pruned] {path}")
        return removed + 1, True

    return visit(root)[0]
