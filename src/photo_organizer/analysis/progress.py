"""Face detection and Gemma day-summary progress, for the status page.

Counts come from analysis.db (and the catalog, for how many photos and day folders the store
holds); whether a job is running and its pace come from the newest log of each kind in the
logs folder. Everything here is read-only.
"""

import os
import re
import sqlite3
import subprocess
import time

from photo_organizer.analysis.ark import DEFAULT_ANALYSIS_DB
from photo_organizer.constants import PHOTO_EXTENSIONS

LOG_DIR = os.path.expanduser('~/.photo-organizer/logs')
JOBS = {     # job: (log file prefixes, newest first wins; the command line of a running job)
    'faces': (('ark-faces-spread-', 'ark-faces-'), 'photo-ark-analyze run'),
    'days': (('ark-days-',), 'photo-ark-analyze days'),
}
FACE_LINE = re.compile(r'^\[(\d+)/(\d+)\].*\(([\d.]+)/s, (\d+) min left\)')
DAY_LINE = re.compile(r'^\[(\d+), (\d+) left\] (\S+).*\(([\d.]+) h left\)')
_store_cache = {}


def _store_counts(catalog_path, ttl=120):
    """(photos, day folders) in the store, one per content hash; cached, it reads every row."""
    hit = _store_cache.get(catalog_path)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    cat = sqlite3.connect(f"file:{catalog_path}?mode=ro", uri=True, timeout=5)
    try:
        rows = cat.execute("SELECT sha256, dest_path FROM files WHERE status = 'done' "
                           "AND sha256 IS NOT NULL AND dest_path IS NOT NULL").fetchall()
    finally:
        cat.close()
    seen, days = set(), set()
    for sha, path in rows:
        if os.path.splitext(path)[1].lower() in PHOTO_EXTENSIONS and sha not in seen:
            seen.add(sha)
            days.add(os.path.dirname(path))
    counts = (len(seen), len(days))
    _store_cache[catalog_path] = (time.time(), counts)
    return counts


def _running(pattern):
    try:
        out = subprocess.run(['pgrep', '-f', pattern], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(out.split())


def _last_lines(path, n=40, block=16384):
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - block))
        return f.read().decode('utf-8', 'replace').splitlines()[-n:]


def newest_log(prefixes, log_dir=LOG_DIR):
    try:
        names = [n for n in os.listdir(log_dir) if n.endswith('.log') and n.startswith(prefixes)]
    except OSError:
        return None
    paths = [os.path.join(log_dir, n) for n in names]
    return max(paths, key=os.path.getmtime) if paths else None


def job_status(job, log_dir=LOG_DIR, running=None):
    """{running, log, updated (s ago), and from the last progress line: done/todo, rate, eta_s}."""
    prefixes, cmd = JOBS[job]
    out = {'running': _running(cmd) if running is None else running, 'log': None}
    log = newest_log(prefixes, log_dir)
    if not log:
        return out
    out['log'] = log
    out['updated'] = time.time() - os.path.getmtime(log)
    for line in reversed(_last_lines(log)):
        if job == 'faces' and (m := FACE_LINE.match(line)):
            out.update(done=int(m[1]), todo=int(m[2]), rate=float(m[3]), eta_s=int(m[4]) * 60)
            break
        if job == 'days' and (m := DAY_LINE.match(line)):
            out.update(done=int(m[1]), left=int(m[2]), last_day=m[3], eta_s=float(m[4]) * 3600)
            break
    return out


def snapshot(catalog_path, analysis_db=DEFAULT_ANALYSIS_DB, log_dir=LOG_DIR, running=None):
    """Progress of both jobs, or {'available': False} before any analysis has run."""
    if not os.path.exists(analysis_db):
        return {'available': False}
    conn = sqlite3.connect(f"file:{analysis_db}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        q = lambda sql: conn.execute(sql).fetchone()[0]
        faces = {
            'images_done': q("SELECT COUNT(*) FROM images WHERE faces_at IS NOT NULL"),
            'faces': q("SELECT COUNT(*) FROM faces"),
            'named': q("SELECT COUNT(*) FROM faces WHERE person_id IS NOT NULL"),
            'people': q("SELECT COUNT(DISTINCT person_id) FROM faces WHERE person_id IS NOT NULL"),
            'errors': q("SELECT COUNT(*) FROM images WHERE error IS NOT NULL"),
        }
        has_days = q("SELECT COUNT(*) FROM sqlite_master WHERE name = 'days'")
        days = {'summarized': 0, 'redo': 0, 'latest': []}
        if has_days:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(days)")}
            days['summarized'] = q("SELECT COUNT(*) FROM days WHERE summarized_at IS NOT NULL")
            if 'wrong_at' in cols:
                days['redo'] = q("SELECT COUNT(*) FROM days WHERE wrong_at IS NOT NULL")
            days['latest'] = [dict(r) for r in conn.execute(
                "SELECT date, title, n_images, summarized_at FROM days WHERE summarized_at IS NOT NULL "
                "ORDER BY summarized_at DESC LIMIT 5")]
        days['captioned'] = q("SELECT COUNT(*) FROM images WHERE vlm_at IS NOT NULL")
    finally:
        conn.close()
    try:
        faces['images_total'], days['days_total'] = _store_counts(catalog_path)
    except sqlite3.Error:
        faces['images_total'] = days['days_total'] = None
    faces['job'] = job_status('faces', log_dir, running and running.get('faces'))
    days['job'] = job_status('days', log_dir, running and running.get('days'))
    return {'available': True, 'faces': faces, 'days': days}
