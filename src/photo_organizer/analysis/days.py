"""Day summaries: what a day folder's photo shoot was about, from a few sampled frames.

A day folder (YYYY/MM/YYYY-MM-DD) is split into bursts wherever the gap between shots is longer
than --gap minutes. A few evenly spaced frames from each burst are captioned by Gemma (stored in
images like any other caption, so they are reused), then Gemma writes one summary for the day
from those captions, the burst times and any named faces. Days are visited in random order so
an interrupted run still covers the whole collection evenly; summarized days are skipped.
"""

import json
import os
import random
import sqlite3
import tempfile
import time
from datetime import datetime

from photo_organizer.analysis.ark import _list_json, _store_root, analyze_one
from photo_organizer.constants import PHOTO_EXTENSIONS

SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    day TEXT PRIMARY KEY,           -- the day folder in the store
    date TEXT,
    n_images INTEGER, n_bursts INTEGER,
    bursts TEXT,                    -- JSON [{start, end, n, samples: [sha256]}]
    title TEXT, summary TEXT, event_type TEXT, setting TEXT,
    keywords TEXT, people TEXT,     -- JSON arrays
    vlm_model TEXT, vlm_raw TEXT,   -- vlm_raw: the reply when it wasn't valid JSON
    summarized_at TEXT
);
"""

DAY_PROMPT = """These are notes on one day of personal photos, taken {date} ({weekday}).
{n} photos were taken in {nb} burst(s) of shooting. For each burst: the time range, how many
photos, and descriptions of a few sample photos from it.

{bursts}
{people}
Describe the day as a whole: what the outing, event or photo shoot was, where, and who was there.
Don't describe individual photos. Respond with ONLY a valid JSON object, no other text:
{{"title": "short title, at most 8 words",
  "summary": "2-4 sentences about the day",
  "event_type": "one or two words, e.g. hike, birthday, travel, product shots, everyday",
  "setting": "where it took place, as specific as the photos allow",
  "keywords": ["5-10 keywords"]}}"""


def init(conn):
    conn.executescript(SCHEMA)


def _ts(s):
    """Wall-clock time of the shot; an offset, when the catalog has one, is dropped."""
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None) if s else None
    except ValueError:
        return None


def select_days(catalog_path, year=None, prefix=None, dest=None):
    """{day folder: [(sha256, dest_path, taken_at datetime | None)]}, each day in shooting order."""
    if not prefix:
        root = dest or _store_root(catalog_path)
        prefix = os.path.join(root, str(year)) if year else root
    prefix = prefix.rstrip('/') + '/'
    cat = sqlite3.connect(f"file:{catalog_path}?mode=ro", uri=True, timeout=30)
    try:
        rows = cat.execute(
            "SELECT sha256, dest_path, taken_at FROM files WHERE status = 'done' AND sha256 IS NOT NULL "
            "AND substr(dest_path, 1, ?) = ?", (len(prefix), prefix)).fetchall()
    finally:
        cat.close()
    days, seen = {}, set()
    for sha, path, taken in rows:
        if os.path.splitext(path)[1].lower() in PHOTO_EXTENSIONS and sha not in seen:
            seen.add(sha)
            days.setdefault(os.path.dirname(path), []).append((sha, path, _ts(taken)))
    for items in days.values():
        items.sort(key=lambda r: (r[2] is None, r[2] or datetime.min, r[1]))
    return days


def split_bursts(items, gap_minutes=45):
    """Split time-ordered items wherever the gap between shots exceeds gap_minutes. Undated
    items form their own burst at the end."""
    dated = [r for r in items if r[2]]
    bursts = []
    for r in dated:
        if bursts and (r[2] - bursts[-1][-1][2]).total_seconds() <= gap_minutes * 60:
            bursts[-1].append(r)
        else:
            bursts.append([r])
    undated = [r for r in items if not r[2]]
    return bursts + ([undated] if undated else [])


def _spread(n, k):
    """k evenly spaced indexes into range(n), centered in their slices."""
    return sorted({min(n - 1, int((i + 0.5) * n / k)) for i in range(k)})


def plan_samples(bursts, per_burst=3, max_samples=9):
    """How many frames to caption from each burst: one each (largest bursts first when there are
    more bursts than max_samples), then extra frames to the bursts with the most photos per
    sample, at most per_burst each (per two hours of a long burst). Returns a list of sample-index lists, one per burst."""
    def cap(b):                     # long bursts (a game, a party) get per_burst per two hours
        hours = (b[-1][2] - b[0][2]).total_seconds() / 3600 if b[0][2] and b[-1][2] else 0
        return min(len(b), per_burst * (1 + int(hours // 2)))

    alloc = [0] * len(bursts)
    for i in sorted(range(len(bursts)), key=lambda i: -len(bursts[i]))[:max_samples]:
        alloc[i] = 1
    for _ in range(max_samples - sum(alloc)):
        open_ = [i for i, a in enumerate(alloc) if a and a < cap(bursts[i])]
        if not open_:
            break
        alloc[max(open_, key=lambda i: len(bursts[i]) / (alloc[i] + 1))] += 1
    return [_spread(len(b), a) if a else [] for b, a in zip(bursts, alloc)]


def _hm(t):
    return t.strftime('%H:%M') if t else '?'


def _people(conn, shas):
    """Names on faces in these images, most frequent first (rejected suggestions excluded)."""
    if not shas:
        return []
    q = ','.join('?' * len(shas))
    return [r[0] for r in conn.execute(
        f"SELECT p.name, COUNT(DISTINCT f.sha256) n FROM faces f JOIN persons p ON p.id = f.person_id "
        f"WHERE f.sha256 IN ({q}) GROUP BY p.name ORDER BY n DESC", shas)]


def day_prompt(conn, day, items, bursts, samples):
    dates = [r[2] for r in items if r[2]]
    date = dates[0] if dates else _ts(os.path.basename(day))
    lines = []
    for i, (b, idx) in enumerate(zip(bursts, samples), 1):
        lines.append(f"Burst {i}: {_hm(b[0][2])}-{_hm(b[-1][2])}, {len(b)} photo(s)")
        for j in idx:
            r = conn.execute("SELECT caption, setting, keywords FROM images WHERE sha256 = ?",
                             (b[j][0],)).fetchone()
            if r and r['caption']:
                kw = ', '.join(json.loads(r['keywords'] or '[]')[:6])
                lines.append(f"  - {_hm(b[j][2])} {r['caption']}" + (f" [{kw}]" if kw else ''))
    people = _people(conn, [r[0] for r in items])
    return DAY_PROMPT.format(
        date=date.strftime('%Y-%m-%d') if date else os.path.basename(day),
        weekday=date.strftime('%A') if date else 'unknown weekday',
        n=len(items), nb=len(bursts), bursts='\n'.join(lines),
        people=f"People recognized in the day's photos: {', '.join(people)}.\n" if people else ''), people


def summarize_day(conn, day, items, vlm_fn, summary_fn, vlm_name=None, gap_minutes=45,
                  per_burst=3, max_samples=9, tmpdir=None, log=print):
    bursts = split_bursts(items, gap_minutes)
    samples = plan_samples(bursts, per_burst, max_samples)
    for b, idx in zip(bursts, samples):
        for j in idx:
            analyze_one(conn, b[j][0], b[j][1], None, vlm_fn, vlm_name, tmpdir)
    prompt, people = day_prompt(conn, day, items, bursts, samples)
    r = summary_fn(prompt)
    raw = r.get('_raw_response') if r.get('_parse_error') else None
    dated = [x[2] for x in items if x[2]]
    conn.execute(
        "INSERT OR REPLACE INTO days (day, date, n_images, n_bursts, bursts, title, summary, event_type, "
        "setting, keywords, people, vlm_model, vlm_raw, summarized_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%S', 'now'))",
        (day, dated[0].date().isoformat() if dated else os.path.basename(day), len(items), len(bursts),
         json.dumps([{'start': _hm(b[0][2]), 'end': _hm(b[-1][2]), 'n': len(b),
                      'samples': [b[j][0] for j in idx]} for b, idx in zip(bursts, samples)]),
         r.get('title'), r.get('summary'), r.get('event_type'), r.get('setting'),
         _list_json(r.get('keywords')), json.dumps(people), vlm_name, raw))
    conn.commit()
    return len(bursts), sum(map(len, samples)), r


def run_days(conn, days, vlm_fn, summary_fn, vlm_name=None, seed=None, limit=0, log=print, **kw):
    """Summarize days not yet summarized, in random order."""
    init(conn)
    done = {r[0] for r in conn.execute("SELECT day FROM days WHERE summarized_at IS NOT NULL")}
    todo = sorted(d for d in days if d not in done)
    random.Random(seed).shuffle(todo)
    if limit:
        todo = todo[:limit]
    log(f"{len(days):,} days, {len(done & set(days)):,} already summarized, {len(todo):,} to do (random order)")
    start = time.time()
    with tempfile.TemporaryDirectory(prefix='ark-days-') as tmpdir:
        for i, day in enumerate(todo, 1):
            t = time.time()
            try:
                nb, ns, r = summarize_day(conn, day, days[day], vlm_fn, summary_fn, vlm_name,
                                          tmpdir=tmpdir, log=log, **kw)
            except Exception as e:
                conn.rollback()
                log(f"[{i}/{len(todo)}] ERROR {day}: {type(e).__name__}: {e}")
                continue
            per = (time.time() - start) / i
            log(f"[{i}/{len(todo)}] {os.path.basename(day)}  {len(days[day])} photos, {nb} bursts, "
                f"{ns} sampled  {time.time() - t:.0f}s  ({(len(todo) - i) * per / 3600:.1f} h left)\n"
                f"    {r.get('title') or '(no title)'}: {r.get('summary') or r.get('_raw_response', '')[:200]}")
    return len(todo)
