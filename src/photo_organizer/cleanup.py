"""Remove source folders emptied by transfers."""

import os

from photo_organizer.constants import JUNK_NAMES
from photo_organizer.scanner import should_skip_dir


def _is_junk(name):
    return name in JUNK_NAMES or name.startswith('._')


def prune_empty_dirs(root, verbose=False):
    """Delete folders under root that hold nothing but OS junk files, deepest first.
    Never enters NAS snapshot/recycle folders or app bundles; root itself is kept.
    Returns the number of folders removed."""

    def visit(path):
        removed = 0
        try:
            entries = list(os.scandir(path))
        except OSError:
            return 0, False
        for e in entries:
            if e.is_dir(follow_symlinks=False) and not should_skip_dir(e.name):
                n, gone = visit(e.path)
                removed += n
        try:
            remaining = os.listdir(path)
        except OSError:
            return removed, False
        if path == root or any(not _is_junk(name) for name in remaining):
            return removed, False
        try:
            for name in remaining:
                os.unlink(os.path.join(path, name))
            os.rmdir(path)
        except OSError:
            return removed, False
        if verbose:
            print(f"  [pruned] {path}")
        return removed + 1, True

    return visit(root)[0]
