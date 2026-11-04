"""Face Finder: the photos that have every one of up to four named people in them.

Served by photo-ui at /finder/ (make ui). Pick people from the menus; the grid shows the photos
where all of them were recognized (faces.person_id), newest first, with a count per year to
narrow it. "Nobody else" keeps only photos with no other faces, named or not. Click a photo to
see it large (← → step, Esc closes); double-click it, or "Score in Shuffle", to score it on the
shuffle page.
"""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from photo_organizer.analysis.ark import open_db
from photo_organizer.analysis.day_browser import PHOTO_SIDE, THUMB_SIDE, _jpeg
from photo_organizer.analysis.shuffle import _is_photo, day_year, init_scores, persons

MAX_PEOPLE = 4


def connect(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def find_photos(conn, person_ids, alone=False):
    """[{sha, path, day, year, caption, score}] of the photos with all of person_ids in them, newest
    first; alone: and no other faces."""
    ids = sorted(set(person_ids))[:MAX_PEOPLE]
    if not ids:
        return []
    q = ','.join('?' * len(ids))
    other = (f"AND NOT EXISTS (SELECT 1 FROM faces o WHERE o.sha256 = i.sha256 "
             f"AND (o.person_id IS NULL OR o.person_id NOT IN ({q})))") if alone else ""
    rows = conn.execute(
        f"SELECT i.sha256, i.path, i.caption, s.score FROM images i LEFT JOIN photo_scores s ON s.sha256 = i.sha256 "
        f"WHERE i.sha256 IN (SELECT sha256 FROM faces WHERE person_id IN ({q}) GROUP BY sha256 "
        f"HAVING COUNT(DISTINCT person_id) = ?) {other} ORDER BY i.path DESC",
        ids + [len(ids)] + (ids if alone else [])).fetchall()
    return [{'sha': r['sha256'], 'path': r['path'], 'day': os.path.basename(os.path.dirname(r['path'])),
             'year': day_year(os.path.dirname(r['path'])), 'caption': r['caption'], 'score': r['score']}
            for r in rows if _is_photo(r['path'])]


def make_app(db_path, thumb_dir):
    """thumb_dir: shared with the days page (same size, same names)."""
    with closing(open_db(db_path)) as rw:
        init_scores(rw)                 # the scores shown on the photos, on a DB from before they existed
    thumb_dir = Path(thumb_dir)
    app = FastAPI()

    def image_path(sha):
        with closing(connect(db_path)) as conn:
            r = conn.execute("SELECT path FROM images WHERE sha256 = ?", (sha,)).fetchone()
        if not r or not os.path.exists(r['path']):
            raise HTTPException(404)
        return r['path']

    @app.get("/api/persons")
    def api_persons():
        with closing(connect(db_path)) as conn:
            return persons(conn)

    @app.get("/api/find")
    def api_find(p: list[int] = Query([]), year: int | None = None, alone: bool = False,
                 offset: int = 0, limit: int = 120):
        with closing(connect(db_path)) as conn:
            photos = find_photos(conn, p, alone)
        years = {}
        for ph in photos:
            years[ph['year']] = years.get(ph['year'], 0) + 1
        shown = [ph for ph in photos if year is None or ph['year'] == year]
        return {'total': len(shown), 'all': len(photos),
                'years': sorted(([y, n] for y, n in years.items() if y), reverse=True),
                'items': shown[offset:offset + min(limit, 500)]}

    @app.get("/thumb/{sha}.jpg")
    def thumb(sha: str):
        if not sha.isalnum():
            raise HTTPException(404)
        cached = thumb_dir / f"{sha}.jpg"
        if not cached.exists():
            data = _jpeg(image_path(sha), THUMB_SIDE)
            thumb_dir.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(data)
        return Response(cached.read_bytes(), media_type='image/jpeg', headers={'Cache-Control': 'max-age=86400'})

    @app.get("/photo/{sha}.jpg")
    def photo(sha: str):
        if not sha.isalnum():
            raise HTTPException(404)
        return Response(_jpeg(image_path(sha), PHOTO_SIDE), media_type='image/jpeg')

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE

    return app


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>photoArk face finder</title>
<style>
:root { --bg:#f6f5f2; --card:#fff; --ink:#1d1d1f; --muted:#6b6b70; --line:#e3e1dc; --accent:#2f6fde; --chip:#eef1f7; }
@media (prefers-color-scheme: dark) { :root { --bg:#161618; --card:#1f1f22; --ink:#ececee; --muted:#9a9aa1;
  --line:#2e2e33; --accent:#6ea0ff; --chip:#2a2e38; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.45 -apple-system, system-ui, sans-serif; }
header { position:sticky; top:0; z-index:2; background:var(--bg); border-bottom:1px solid var(--line);
  padding:12px 16px; display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
header h1 { font-size:17px; margin:0 8px 0 0; }
select { font:inherit; padding:6px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--ink);
  max-width:15em; }
select.set { border-color:var(--accent); }
label { font-size:14px; color:var(--muted); display:flex; gap:5px; align-items:center; cursor:pointer; }
#count { color:var(--muted); font-size:13px; }
#years { display:flex; gap:6px; flex-wrap:wrap; padding:10px 16px 0; }
.chip { background:var(--chip); border:0; border-radius:999px; padding:2px 10px; font:inherit; font-size:13px;
  color:var(--ink); cursor:pointer; font-variant-numeric:tabular-nums; }
.chip.on { background:var(--accent); color:#fff; }
.chip small { color:var(--muted); } .chip.on small { color:#dde6ff; }
main { padding:16px; display:grid; gap:8px; grid-template-columns:repeat(auto-fill, minmax(170px, 1fr)); }
.ph { position:relative; background:var(--line); border-radius:8px; overflow:hidden; aspect-ratio:1; cursor:zoom-in; }
.ph img { width:100%; height:100%; object-fit:cover; display:block; }
.ph .d { position:absolute; left:0; right:0; bottom:0; padding:14px 7px 4px; font-size:12px; color:#fff;
  background:linear-gradient(transparent, rgba(0,0,0,.65)); font-variant-numeric:tabular-nums; }
.ph .s { position:absolute; top:5px; right:6px; font-size:12px; color:#fd6; text-shadow:0 1px 2px #000; }
.empty { grid-column:1 / -1; color:var(--muted); text-align:center; padding:40px 0; }
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
  <h1>Face finder</h1>
  <span id="pick"></span>
  <label title="Only photos with no other faces, named or not"><input type="checkbox" id="alone"> Nobody else</label>
  <span id="count"></span>
</header>
<div id="years"></div>
<main id="grid"></main>
<div id="viewer"><img id="vimg" alt=""><p id="vcap"></p>
  <div class="bar"><button class="open" id="vscore" title="Or double-click the photo">Score in Shuffle</button>
  <button class="open" id="vclose">Close (Esc) · ← → step</button></div></div>
<script>
const $ = s => document.querySelector(s);
const esc = s => (s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const N = 4;
let people = [], year = null, offset = 0, total = 0, viewing = null;

// The choice lives in the URL (?p=1&p=7&year=2015&alone=1), so Back and links keep it.
function readUrl() {
  const u = new URLSearchParams(location.search);
  const ps = u.getAll('p').slice(0, N);
  document.querySelectorAll('#pick select').forEach((s, i) => { s.value = ps[i] || ''; });
  year = u.get('year') ? +u.get('year') : null;
  $('#alone').checked = u.get('alone') == '1';
}
function chosen() { return [...new Set([...document.querySelectorAll('#pick select')].map(s => s.value).filter(Boolean))]; }
function query() {
  const u = new URLSearchParams();
  for (const p of chosen()) u.append('p', p);
  if (year) u.set('year', year);
  if ($('#alone').checked) u.set('alone', '1');
  return u;
}
function writeUrl() { const q = query().toString(); window.history.replaceState(null, '', q ? '?' + q : location.pathname); }

function pickers() {
  // Each menu offers everyone, but not a person another menu already has.
  const sels = [...document.querySelectorAll('#pick select')], taken = sels.map(s => s.value);
  sels.forEach((s, i) => {
    const v = s.value;
    s.innerHTML = `<option value="">${i ? '+ person' : 'Pick a person'}</option>` + people
      .filter(p => String(p.id) == v || !taken.includes(String(p.id)))
      .map(p => `<option value="${p.id}">${esc(p.name)} (${p.photos.toLocaleString()})</option>`).join('');
    s.value = v;
    s.classList.toggle('set', !!v);
    // Show the next empty menu only once the ones before it are filled.
    s.style.display = i == 0 || sels[i - 1].value ? '' : 'none';
  });
}

async function load(reset) {
  if (reset) { offset = 0; $('#grid').innerHTML = ''; }
  writeUrl();
  const ps = chosen();
  if (!ps.length) {
    $('#count').textContent = ''; $('#years').innerHTML = '';
    $('#grid').innerHTML = '<div class="empty">Pick up to four people to see the photos they are all in.</div>';
    return;
  }
  const u = query(); u.set('offset', offset);
  const r = await (await fetch('api/find?' + u)).json();
  total = r.total; offset += r.items.length;
  const names = ps.map(id => (people.find(p => String(p.id) == id) || {}).name).join(', ');
  $('#count').textContent = `${total.toLocaleString()} photo${total == 1 ? '' : 's'}` +
    (year ? ` in ${year}` : '') + (ps.length > 1 ? ` with all of ${names}` : '');
  $('#years').innerHTML = r.years.length > 1 || year ?
    `<button class="chip${year ? '' : ' on'}" data-year="">All <small>${r.all.toLocaleString()}</small></button>` +
    r.years.map(([y, n]) => `<button class="chip${y == year ? ' on' : ''}" data-year="${y}">${y} <small>${n}</small></button>`).join('') : '';
  $('#more')?.remove();
  if (reset && !r.items.length) $('#grid').innerHTML = '<div class="empty">No photos with all of them together.</div>';
  for (const p of r.items) $('#grid').insertAdjacentHTML('beforeend',
    `<div class="ph" data-sha="${p.sha}" data-day="${esc(p.day)}" data-cap="${esc(p.caption)}" title="${esc(p.caption)}">
     <img loading="lazy" src="thumb/${p.sha}.jpg" alt=""><span class="d">${esc(p.day)}</span>
     ${p.score ? `<span class="s">${'★'.repeat(p.score)}</span>` : ''}</div>`);
  if (offset < total) $('#grid').insertAdjacentHTML('beforeend', '<button class="open" id="more">More</button>');
}

function show(el) {
  viewing = el;
  $('#vimg').src = `photo/${el.dataset.sha}.jpg`;
  $('#vcap').textContent = el.dataset.day + (el.dataset.cap ? ' — ' + el.dataset.cap : '');
  $('#viewer').style.display = 'flex';
}
function step(by) {
  const all = [...document.querySelectorAll('.ph')];
  const next = all[all.indexOf(viewing) + by];
  if (next) { show(next); next.scrollIntoView({block: 'nearest'}); }
  else if (by > 0 && offset < total) load(false).then(() => step(by));
}
// The shuffle page sits next to this one under photo-ui; it opens paused on the photo, to score.
function scoreInShuffle(sha) { location.href = '../shuffle/?sha=' + encodeURIComponent(sha); }

document.addEventListener('click', e => {
  const t = e.target, ph = t.closest('.ph');
  if (t.id == 'more') load(false);
  else if (t.closest('[data-year]')) { const y = t.closest('[data-year]').dataset.year; year = y ? +y : null; load(true); }
  else if (ph) show(ph);
  else if (t.id == 'vscore') scoreInShuffle(viewing.dataset.sha);
  else if (t.id == 'vclose' || t.id == 'viewer') $('#viewer').style.display = 'none';
});
document.addEventListener('dblclick', e => {
  const ph = e.target.closest('.ph');
  if (ph) scoreInShuffle(ph.dataset.sha);
  else if ((e.target.id == 'vimg' || e.target.id == 'viewer') && viewing) scoreInShuffle(viewing.dataset.sha);
});
document.addEventListener('keydown', e => {
  if ($('#viewer').style.display != 'flex') return;
  if (e.key == 'Escape') $('#viewer').style.display = 'none';
  else if (e.key == 'ArrowRight') { e.preventDefault(); step(1); }
  else if (e.key == 'ArrowLeft') { e.preventDefault(); step(-1); }
});
$('#alone').addEventListener('change', () => load(true));

(async () => {
  people = await (await fetch('api/persons')).json();
  $('#pick').innerHTML = Array.from({length: N}, () => '<select></select> ').join('');
  pickers(); readUrl(); pickers();
  document.querySelectorAll('#pick select').forEach(s => s.addEventListener('change', () => {
    // An emptied menu closes the gap: the people after it move up.
    const ps = chosen();
    document.querySelectorAll('#pick select').forEach((x, i) => { x.value = ps[i] || ''; });
    year = null; pickers(); load(true);
  }));
  load(true);
})();
</script></body></html>"""
