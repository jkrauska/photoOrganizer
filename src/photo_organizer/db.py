import sqlite3


def init_db(db_path):
    """Create database and tables if they don't exist."""
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            original_path TEXT NOT NULL UNIQUE,
            dest_path TEXT,
            sha256 TEXT,
            exif_creation_date TEXT,
            file_modified_date TEXT,
            file_size INTEGER,
            date_source TEXT,
            status TEXT DEFAULT 'pending',
            error_message TEXT,
            group_key TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sha256 ON files(sha256)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON files(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_original_path ON files(original_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_group_key ON files(group_key)")
    conn.commit()
    return conn
