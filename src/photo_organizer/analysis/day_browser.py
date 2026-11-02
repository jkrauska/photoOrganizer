"""Local web page to browse and search the Gemma day summaries (days table in analysis.db).

Run:   make ark-days-ui   (or: uv run photo-day-browser)
Then:  http://127.0.0.1:8079/

Search matches every word against the day's title, summary, keywords, setting, event type,
names and folder, and against the captions of the day's sampled photos. "Open folder" opens the
day folder in Finder on this Mac. It binds 127.0.0.1 unless --host says otherwise (--host 0.0.0.0
serves the LAN, with no login); Open folder and Show in Finder work only from this Mac itself, and
only for folders listed in the days table.
"This is wrong" queues the day for re-analysis with more frames (days.mark_wrong); the running
photo-ark-analyze days job picks it up before its next day.
The Context box under each day stores what the person knows about it (days.set_context): it goes
into every summary of that day, and changing it has the days job redo the day.
"""

import argparse
import io
import json
import os
import socket
import sqlite3
import subprocess
import webbrowser
from contextlib import closing
from pathlib import Path
from threading import Timer

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from photo_organizer.analysis import days as days_mod
from photo_organizer.analysis.ark import DEFAULT_ANALYSIS_DB, load_image, open_db

THUMB_SIDE = 360
PHOTO_SIDE = 1800
DEFAULT_THUMBS = '~/.photo-organizer/day-thumbs'

SEARCH_FIELDS = ("d.day || ' ' || coalesce(d.title,'') || ' ' || coalesce(d.summary,'') || ' ' || "
                 "coalesce(d.keywords,'') || ' ' || coalesce(d.setting,'') || ' ' || "
                 "coalesce(d.event_type,'') || ' ' || coalesce(d.people,'') || ' ' || coalesce(c.context,'')")
FROM = "days d LEFT JOIN day_context c ON c.day = d.day"
ORDERS = {'newest': 'd.date DESC, d.day DESC', 'oldest': 'd.date, d.day',
          'recent': 'd.summarized_at DESC', 'biggest': 'd.n_images DESC'}


def connect(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _captions(conn, shas):
    if not shas:
        return {}
    q = ','.join('?' * len(shas))
    return {r['sha256']: r['caption'] for r in conn.execute(
        f"SELECT sha256, caption FROM images WHERE sha256 IN ({q})", shas)}


def search_days(conn, q='', year=None, order='newest', limit=60, offset=0):
    """(total, [day dicts]) for days whose text matches every word of q."""
    where, args = ["d.summarized_at IS NOT NULL"], []
    for w in q.split():
        where.append(f"(({SEARCH_FIELDS}) LIKE ? OR EXISTS (SELECT 1 FROM json_each(d.bursts) b, "
                     "json_each(b.value, '$.samples') s JOIN images i ON i.sha256 = s.value "
                     "WHERE i.caption LIKE ?))")
        args += [f'%{w}%'] * 2
    if year:
        where.append("substr(d.date, 1, 4) = ?")
        args.append(str(year))
    sql = " WHERE " + " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM {FROM}{sql}", args).fetchone()[0]
    rows = conn.execute(f"SELECT d.*, c.context FROM {FROM}{sql} ORDER BY {ORDERS.get(order, ORDERS['newest'])} "
                        "LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    out = []
    for r in rows:
        bursts = json.loads(r['bursts'] or '[]')
        caps = _captions(conn, [s for b in bursts for s in b['samples']])
        out.append({
            'day': r['day'], 'date': r['date'], 'n_images': r['n_images'], 'title': r['title'],
            'summary': r['summary'] or (r['vlm_raw'] or '')[:300], 'event_type': r['event_type'],
            'setting': r['setting'], 'keywords': json.loads(r['keywords'] or '[]'),
            'people': json.loads(r['people'] or '[]'),
            'wrong_at': r['wrong_at'], 'wrong_note': r['wrong_note'], 'attempt': r['attempt'] or 0,
            'context': r['context'] or '', 'context_pending': r['context'] != r['context_used'],
            'bursts': [{'start': b['start'], 'end': b['end'], 'n': b['n'],
                        'samples': [{'sha': s, 'caption': caps.get(s)} for s in b['samples']]}
                       for b in bursts]})
    return total, out


def years(conn):
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(date, 1, 4) FROM days WHERE summarized_at IS NOT NULL ORDER BY 1")]


def _jpeg(path, side):
    im, _ = load_image(path)
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=85)
    return buf.getvalue()


def _local(request):
    return bool(request.client) and request.client.host in ('127.0.0.1', '::1')


class WrongIn(BaseModel):
    day: str
    note: str = ''


class ContextIn(BaseModel):
    day: str
    context: str = ''


class OpenIn(BaseModel):
    day: str
    sha: str | None = None


def make_app(db_path, thumb_dir, opener=None):
    """opener(args) runs `open` (tests pass a fake)."""
    with closing(open_db(db_path)) as rw:
        days_mod.init(rw)               # the redo columns, on a DB from before they existed
    conn = connect(db_path)
    thumb_dir = Path(thumb_dir)
    opener = opener or (lambda args: subprocess.run(['open', *args], check=False))
    app = FastAPI()

    def image_path(sha):
        r = conn.execute("SELECT path FROM images WHERE sha256 = ?", (sha,)).fetchone()
        if not r or not os.path.exists(r['path']):
            raise HTTPException(404)
        return r['path']

    @app.get("/api/days")
    def api_days(q: str = '', year: str = '', order: str = 'newest', offset: int = 0, limit: int = 60):
        total, items = search_days(conn, q, year or None, order, min(limit, 200), offset)
        return {'total': total, 'items': items}

    @app.get("/api/years")
    def api_years():
        return years(conn)

    @app.get("/thumb/{sha}.jpg")
    def thumb(sha: str):
        if not sha.isalnum():
            raise HTTPException(404)
        cached = thumb_dir / f"{sha}.jpg"
        if not cached.exists():
            data = _jpeg(image_path(sha), THUMB_SIDE)
            thumb_dir.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(data)
        return Response(cached.read_bytes(), media_type='image/jpeg',
                        headers={'Cache-Control': 'max-age=86400'})

    @app.get("/photo/{sha}.jpg")
    def photo(sha: str):
        return Response(_jpeg(image_path(sha), PHOTO_SIDE), media_type='image/jpeg')

    @app.post("/api/wrong")
    def wrong(body: WrongIn):
        with closing(open_db(db_path)) as rw:
            if not days_mod.mark_wrong(rw, body.day, body.note):
                raise HTTPException(404)
        return {'ok': True}

    @app.post("/api/context")
    def context(body: ContextIn):
        if not conn.execute("SELECT 1 FROM days WHERE day = ?", (body.day,)).fetchone():
            raise HTTPException(404)
        with closing(open_db(db_path)) as rw:
            days_mod.set_context(rw, body.day, body.context)
        return {'ok': True}

    @app.get("/api/local")
    def is_local(request: Request):
        return {'local': _local(request)}

    @app.post("/api/open")
    def open_folder(body: OpenIn, request: Request):
        """Open the day folder in Finder, or reveal one of its photos (from this Mac only: Finder
        would open on this Mac, not on the machine viewing the page)."""
        if not _local(request):
            raise HTTPException(403, f"Finder opens on {socket.gethostname()} only")
        if not conn.execute("SELECT 1 FROM days WHERE day = ?", (body.day,)).fetchone():
            raise HTTPException(404)
        if body.sha:
            path = image_path(body.sha)
            if os.path.dirname(path) != body.day:
                raise HTTPException(404)
            opener(['-R', path])
        else:
            if not os.path.isdir(body.day):
                raise HTTPException(404, "folder not reachable (is photoArk mounted?)")
            opener([body.day])
        return {'ok': True}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE

    return app


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>photoArk days</title>
<style>
:root { --bg:#f6f5f2; --card:#fff; --ink:#1d1d1f; --muted:#6b6b70; --line:#e3e1dc; --accent:#2f6fde; --chip:#eef1f7; }
@media (prefers-color-scheme: dark) { :root { --bg:#161618; --card:#1f1f22; --ink:#ececee; --muted:#9a9aa1;
  --line:#2e2e33; --accent:#6ea0ff; --chip:#2a2e38; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.45 -apple-system, system-ui, sans-serif; }
header { position:sticky; top:0; z-index:2; background:var(--bg); border-bottom:1px solid var(--line);
  padding:12px 16px; display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
header h1 { font-size:17px; margin:0 8px 0 0; }
input[type=search] { flex:1 1 260px; font:inherit; padding:7px 10px; border:1px solid var(--line);
  border-radius:8px; background:var(--card); color:var(--ink); }
select { font:inherit; padding:6px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--ink); }
#count { color:var(--muted); font-size:13px; }
main { padding:16px; display:grid; gap:14px; grid-template-columns:repeat(auto-fill, minmax(min(100%, 720px), 1fr));
  align-items:start; }
.day { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
.top { display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; }
.date { font-weight:600; font-variant-numeric:tabular-nums; }
.title { font-size:17px; font-weight:600; }
.meta { color:var(--muted); font-size:13px; }
.summary { margin:6px 0 8px; }
.chips { display:flex; gap:6px; flex-wrap:wrap; margin:6px 0; }
.chip { background:var(--chip); border:0; border-radius:999px; padding:2px 10px; font:inherit; font-size:13px;
  color:var(--ink); cursor:pointer; }
.chip.person { outline:1px solid var(--accent); }
.bursts { display:flex; gap:14px; flex-wrap:wrap; margin-top:8px; }
.burst .meta { margin-bottom:4px; }
.thumbs { display:flex; gap:6px; }
.thumbs { flex-wrap:wrap; }
.thumbs img { height:120px; border-radius:6px; cursor:zoom-in; background:var(--line); }
.path { display:flex; gap:8px; align-items:center; margin-top:8px; flex-wrap:wrap; }
.badge { font-size:12px; padding:2px 8px; border-radius:999px; background:#f6d38a; color:#3a2a00; }
.badge.redone { background:var(--chip); color:var(--muted); }
button.wrong { border-color:#c4542d; color:#c4542d; }
textarea.ctx { width:100%; font:inherit; font-size:13px; padding:5px 8px; border:1px solid var(--line);
  border-radius:8px; background:var(--bg); color:var(--ink); resize:vertical; min-height:32px; margin-top:6px; }
.path code { font-size:12px; color:var(--muted); word-break:break-all; }
button.open { font:inherit; font-size:13px; padding:4px 10px; border-radius:8px; border:1px solid var(--accent);
  background:transparent; color:var(--accent); cursor:pointer; }
#more { grid-column:1 / -1; justify-self:center; }
#viewer { position:fixed; inset:0; background:rgba(0,0,0,.88); display:none; align-items:center;
  justify-content:center; flex-direction:column; gap:10px; z-index:5; padding:16px; }
#viewer img { max-width:100%; max-height:82vh; border-radius:6px; }
#viewer p { color:#ddd; max-width:900px; text-align:center; margin:0; }
#viewer .bar { display:flex; gap:10px; }
</style></head><body>
<header>
  <h1>photoArk days</h1>
  <input id="q" type="search" placeholder="Search titles, summaries, keywords, captions, names…" autofocus>
  <select id="year"><option value="">All years</option></select>
  <select id="order"><option value="newest">Newest first</option><option value="oldest">Oldest first</option>
    <option value="recent">Recently summarized</option><option value="biggest">Most photos</option></select>
  <span id="count"></span>
</header>
<main id="list"></main>
<div id="viewer"><img id="vimg" alt=""><p id="vcap"></p>
  <div class="bar"><button class="open" id="vreveal">Show in Finder</button>
  <button class="open" id="vclose">Close (Esc) · ← → step</button></div></div>
<script>
const $ = s => document.querySelector(s);
let offset = 0, total = 0, timer, viewing = null;
const esc = s => (s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const params = () => new URLSearchParams({q: $('#q').value, year: $('#year').value, order: $('#order').value});

async function load(reset) {
  if (reset) { offset = 0; $('#list').innerHTML = ''; }
  const p = params(); p.set('offset', offset);
  const r = await (await fetch('/api/days?' + p)).json();
  total = r.total; offset += r.items.length;
  $('#count').textContent = `${total.toLocaleString()} day${total == 1 ? '' : 's'}`;
  $('#more')?.remove();
  for (const d of r.items) $('#list').insertAdjacentHTML('beforeend', card(d));
  if (offset < total) $('#list').insertAdjacentHTML('beforeend', '<button class="open" id="more">More</button>');
  try { localStorage.setItem('days-ui', JSON.stringify(Object.fromEntries(params()))); } catch (e) {}
}

function card(d) {
  const chips = d.people.map(n => `<button class="chip person" data-q="${esc(n)}">${esc(n)}</button>`)
    .concat(d.keywords.map(k => `<button class="chip" data-q="${esc(k)}">${esc(k)}</button>`)).join('');
  const bursts = d.bursts.map(b => `<div class="burst"><div class="meta">${esc(b.start)}–${esc(b.end)} · ${b.n} photo${b.n == 1 ? '' : 's'}</div>
    <div class="thumbs">${b.samples.map(s => `<img loading="lazy" src="/thumb/${s.sha}.jpg" title="${esc(s.caption)}"
      data-sha="${s.sha}" data-day="${esc(d.day)}" data-cap="${esc(s.caption)}">`).join('')}</div></div>`).join('');
  return `<article class="day"><div class="top"><span class="date">${esc(d.date)}</span>
    <span class="title">${esc(d.title || '(untitled)')}</span>
    <span class="meta">${d.n_images} photos${d.event_type ? ' · ' + esc(d.event_type) : ''}${d.setting ? ' · ' + esc(d.setting) : ''}</span></div>
    ${d.wrong_at ? `<span class="badge">Queued for re-analysis${d.wrong_note ? ': ' + esc(d.wrong_note) : ''}</span>`
      : d.attempt ? `<span class="badge redone">Re-analyzed${d.wrong_note ? ' after: ' + esc(d.wrong_note) : ''}</span>` : ''}
    <div class="summary">${esc(d.summary)}</div>
    <textarea class="ctx" rows="1" data-day="${esc(d.day)}" data-prev="${esc(d.context)}"
      placeholder="Context: who, where, what the occasion was… (the day is re-analyzed with it)">${esc(d.context)}</textarea>
    ${d.context_pending ? '<span class="badge">Queued for re-analysis with this context</span>' : ''}<div class="chips">${chips}</div><div class="bursts">${bursts}</div>
    <div class="path"><button class="open" data-open="${esc(d.day)}">${local ? 'Open folder' : 'Copy path'}</button>
    ${d.wrong_at ? '' : `<button class="open wrong" data-wrong="${esc(d.day)}">This is wrong</button>`}<code>${esc(d.day)}</code></div></article>`;
}

let local = true;
async function openIn(day, sha) {
  if (!local) { try { await navigator.clipboard.writeText(day); } catch (e) { prompt('Folder path:', day); } return; }
  const r = await fetch('/api/open', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                      body: JSON.stringify({day, sha})});
  if (!r.ok) alert((await r.json()).detail || 'Could not open it');
}

async function markWrong(btn) {
  const note = prompt('What is wrong? (optional, e.g. "St. Louis, not San Francisco")\\n' +
                      'The day will be re-analyzed with more photos.');
  if (note === null) return;
  const r = await fetch('/api/wrong', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                       body: JSON.stringify({day: btn.dataset.wrong, note})});
  if (!r.ok) { alert('Could not mark it'); return; }
  btn.insertAdjacentHTML('afterend', `<span class="badge">Queued for re-analysis${note ? ': ' + esc(note) : ''}</span>`);
  btn.remove();
}

// A context box saves when it loses focus (or on Cmd/Ctrl-Enter) if its text changed.
async function saveContext(ta) {
  const context = ta.value.trim();
  if (context === ta.dataset.prev) return;
  const r = await fetch('/api/context', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                         body: JSON.stringify({day: ta.dataset.day, context})});
  if (!r.ok) { alert('Could not save the context'); return; }
  ta.dataset.prev = context;
  if (!ta.nextElementSibling?.classList.contains('badge'))
    ta.insertAdjacentHTML('afterend', '<span class="badge">Queued for re-analysis with this context</span>');
}
document.addEventListener('focusout', e => { if (e.target.classList?.contains('ctx')) saveContext(e.target); });
document.addEventListener('keydown', e => {
  if (e.target.classList?.contains('ctx') && e.key == 'Enter' && (e.metaKey || e.ctrlKey)) e.target.blur();
});

document.addEventListener('click', e => {
  const t = e.target;
  if (t.id == 'more') load(false);
  else if (t.dataset.open) openIn(t.dataset.open);
  else if (t.dataset.wrong) markWrong(t);
  else if (t.dataset.q) { $('#q').value = t.dataset.q; load(true); window.scrollTo(0, 0); }
  else if (t.dataset.sha) show(t);
  else if (t.id == 'vreveal') openIn(viewing.dataset.day, viewing.dataset.sha);
  else if (t.id == 'vclose' || t.id == 'viewer') $('#viewer').style.display = 'none';
});
// The viewer steps through every sample photo on the page with the arrow keys, across days.
function show(img) {
  viewing = img;
  const d = img.closest('.day');
  $('#vimg').src = `/photo/${img.dataset.sha}.jpg`;
  $('#vcap').textContent = `${d.querySelector('.date').textContent} · ${d.querySelector('.title').textContent}` +
    (img.dataset.cap ? ` — ${img.dataset.cap}` : '');
  $('#viewer').style.display = 'flex';
}
function step(by) {
  const all = [...document.querySelectorAll('.thumbs img')];
  const next = all[all.indexOf(viewing) + by];
  if (next) { show(next); next.scrollIntoView({block: 'nearest'}); }
  else if (by > 0 && offset < total) load(false).then(() => step(by));
}
document.addEventListener('keydown', e => {
  if ($('#viewer').style.display != 'flex') return;
  if (e.key == 'Escape') $('#viewer').style.display = 'none';
  else if (e.key == 'ArrowRight') { e.preventDefault(); step(1); }
  else if (e.key == 'ArrowLeft') { e.preventDefault(); step(-1); }
});
$('#q').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => load(true), 250); });
$('#year').addEventListener('change', () => load(true));
$('#order').addEventListener('change', () => load(true));

(async () => {
  local = (await (await fetch('/api/local')).json()).local;
  if (!local) { document.body.classList.add('remote'); $('#vreveal').textContent = 'Copy folder path'; }
  for (const y of await (await fetch('/api/years')).json())
    $('#year').insertAdjacentHTML('beforeend', `<option>${esc(y)}</option>`);
  try { const s = JSON.parse(localStorage.getItem('days-ui') || '{}');
        for (const k of ['q', 'year', 'order']) if (s[k] != null) $('#' + k).value = s[k]; } catch (e) {}
  load(true);
})();
</script></body></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', default=DEFAULT_ANALYSIS_DB)
    parser.add_argument('--port', type=int, default=8079)
    parser.add_argument('--host', default='127.0.0.1', help='0.0.0.0 to serve the LAN (no login)')
    parser.add_argument('--thumbs', default=DEFAULT_THUMBS, help='thumbnail cache')
    parser.add_argument('--no-open', action='store_true', help="don't open a browser")
    args = parser.parse_args()
    db_path = os.path.expanduser(args.db)
    if not os.path.exists(db_path):
        raise SystemExit(f"{db_path} not found. Run photo-ark-analyze days first.")
    with closing(sqlite3.connect(db_path)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM days WHERE summarized_at IS NOT NULL").fetchone()[0]
    url = f"http://127.0.0.1:{args.port}/"
    print(f"{n:,} day summaries in {db_path} → {url}")
    if args.host != '127.0.0.1':
        print(f"(also on the LAN at http://{socket.gethostname()}:{args.port}/, no login)")
    if not args.no_open:
        Timer(0.6, lambda: webbrowser.open(url)).start()
    uvicorn.run(make_app(db_path, Path(args.thumbs).expanduser()), host=args.host, port=args.port,
                log_level="warning")


if __name__ == '__main__':
    main()
