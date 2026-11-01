"""Process-wide graceful-shutdown flag, toggled by the CLI's SIGINT handler."""

import sys

_shutdown_requested = False


def shutdown_requested():
    return _shutdown_requested


def signal_handler(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        # Second Ctrl-C: abort immediately
        sys.exit(1)
    _shutdown_requested = True
    print("\nShutdown requested, finishing current file... (Ctrl-C again to force quit)")
