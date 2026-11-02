import fcntl
import os
import sqlite3


class RunLocked(RuntimeError):
    pass


def acquire_run_lock(db_path):
    """Only one run may use a catalog at a time; the lock is released when the process exits."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    fd = os.open(db_path + '.lock', os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RunLocked(f"another run is using {db_path}")
    return fd

# SQLite locking is unreliable over SMB/NFS, so the live catalog stays on local disk
# and a copy is backed up to the destination after each batch.
DEFAULT_DB_PATH = os.path.expanduser('~/.photo-organizer/catalog.db')
BACKUP_NAME = 'photo_organizer_catalog.db'

# status: pending -> copying -> done | duplicate_removed | error,
# plus review (no reliable date) and ignored (unhandled file type); both are left in place.
SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    original_path TEXT NOT NULL UNIQUE,
    source_root TEXT,
    origin_path TEXT,
    dest_path TEXT,
    sha256 TEXT,
    quick_hash TEXT,
    photos_uuid TEXT,
    camera_make TEXT,
    camera_model TEXT,
    lens_model TEXT,
    camera TEXT,
    camera_read_at TEXT,
    taken_at TEXT,
    exif_creation_date TEXT,
    file_modified_date TEXT,
    file_size INTEGER,
    date_source TEXT,
    status TEXT DEFAULT 'pending',
    error_message TEXT,
    group_key TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sha256 ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_group_key ON files(group_key);
CREATE INDEX IF NOT EXISTS idx_dest_path ON files(dest_path);
CREATE INDEX IF NOT EXISTS idx_updated_at ON files(updated_at);
CREATE INDEX IF NOT EXISTS idx_created_at ON files(created_at);
CREATE INDEX IF NOT EXISTS idx_file_size ON files(file_size);
CREATE INDEX IF NOT EXISTS idx_photos_uuid ON files(photos_uuid);
CREATE INDEX IF NOT EXISTS idx_camera ON files(camera);

-- 'run' and 'worker' rows holding live state and heartbeats as JSON, read by the status page.
CREATE TABLE IF NOT EXISTS progress (key TEXT PRIMARY KEY, value TEXT);

-- Source folders waiting for the queue worker: queued -> running -> done | stopped | failed.
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    added_at TEXT DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT,
    result TEXT,
    keep_sources INTEGER NOT NULL DEFAULT 0
);
"""

# camera_*/lens_model: EXIF Make/Model/LensModel; camera: friendly name ('Sony A1'); see cameras.py.
# photos_uuid: the Apple Photos asset a row was copied from (photos_library.py).
# quick_hash: SHA256 of size + three 1 MB samples (hashing.quick_hash); rules out duplicates cheaply.
ADDED_COLUMNS = {'source_root': 'TEXT', 'origin_path': 'TEXT', 'taken_at': 'TEXT', 'updated_at': 'TEXT',
                 'quick_hash': 'TEXT', 'photos_uuid': 'TEXT',
                 'camera_make': 'TEXT', 'camera_model': 'TEXT', 'lens_model': 'TEXT', 'camera': 'TEXT',
                 'camera_read_at': 'TEXT'}

# UTC timestamp with milliseconds, comparable as text.
NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%f', 'now')"


def init_db(db_path):
    """Open (creating if needed) the catalog and bring an older schema up to date."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    existing = {r['name'] for r in conn.execute("PRAGMA table_info(files)")}
    if existing:
        for col, typ in ADDED_COLUMNS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE files ADD COLUMN {col} {typ}")
    conn.executescript(SCHEMA)
    qcols = {r['name'] for r in conn.execute("PRAGMA table_info(queue)")}
    if 'keep_sources' not in qcols:
        conn.execute("ALTER TABLE queue ADD COLUMN keep_sources INTEGER NOT NULL DEFAULT 0")
    if 'transfer' not in qcols:     # how the item's files moved: 'NAS <host>' or 'Mac'
        conn.execute("ALTER TABLE queue ADD COLUMN transfer TEXT")
    conn.commit()
    return conn


def backup_db(conn, dest_root):
    """Snapshot the live catalog onto the destination volume."""
    target = os.path.join(dest_root, BACKUP_NAME)
    tmp = target + '.tmp'
    with sqlite3.connect(tmp) as out:
        conn.backup(out)
    out.close()
    os.replace(tmp, target)
    return target
