"""Live run state (phase, batch, heartbeat) kept in the catalog for the status page."""

import datetime
import json
import os


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def start_run(conn, sources, dest, execute):
    state = {
        'pid': os.getpid(), 'started_at': _now(), 'sources': sources, 'dest': dest,
        'mode': 'execute' if execute else 'dry-run', 'phase': 'starting', 'batch': 0,
    }
    _write(conn, state)
    return state


def set_progress(conn, lock=None, **fields):
    """Merge fields into the run state and refresh the heartbeat."""
    def write():
        row = conn.execute("SELECT value FROM progress WHERE key = 'run'").fetchone()
        state = json.loads(row[0]) if row else {}
        state.update(fields)
        _write(conn, state)

    if lock:
        with lock:
            write()
    else:
        write()


def finish_run(conn, phase):
    set_progress(conn, phase=phase, finished=True, finished_at=_now())


def _write(conn, state):
    state['heartbeat'] = _now()
    conn.execute("INSERT OR REPLACE INTO progress (key, value) VALUES ('run', ?)", (json.dumps(state),))
    conn.commit()
