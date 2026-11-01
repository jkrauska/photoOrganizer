"""Process-wide graceful-shutdown flag.

A stop is requested by Ctrl-C, SIGTERM, or creating the stop file (`make stop`). In-flight
files finish and are verified; nothing new starts. A second Ctrl-C aborts immediately.
"""

import os
import sys

DEFAULT_STOP_FILE = os.path.expanduser('~/.photo-organizer/STOP')

_shutdown_requested = False
_stop_file = DEFAULT_STOP_FILE


def configure_stop_file(path):
    """Use path as the stop file and clear any stale one from an earlier stop."""
    global _stop_file, _shutdown_requested
    _stop_file = path
    _shutdown_requested = False
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def request_stop(reason):
    global _shutdown_requested
    if not _shutdown_requested:
        _shutdown_requested = True
        print(f"\n{reason}: finishing in-flight files, then stopping...", flush=True)


def shutdown_requested():
    if not _shutdown_requested and os.path.exists(_stop_file):
        request_stop(f"Stop file {_stop_file} found")
    return _shutdown_requested


def signal_handler(signum, frame):
    if _shutdown_requested and signum == 2:  # second Ctrl-C
        sys.exit(1)
    request_stop("Ctrl-C" if signum == 2 else f"Signal {signum}")
