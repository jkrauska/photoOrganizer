"""Places: a map of where photos were taken, filtered by place, people and years.

Served by photo-ui at /places/ (make ui). Reads the catalog's locations table (filled by
photo-organizer-locations) and, for the people filter, the named faces in analysis.db. The map
groups photos into clusters on a grid that follows the zoom; click one to zoom in. The grid beside
it shows the matching photos, newest first, only those in the map's view unless "Map area only"
is off (then photos placed from Gemma text, which have no coordinates, show too).
How sure: GPS only, GPS plus photos borrowing a same-day GPS position, or any source.
The map tiles come from OpenStreetMap in the browser; photo positions are never sent anywhere.
"""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from photo_organizer.analysis.day_browser import PHOTO_SIDE, THUMB_SIDE, _jpeg
from photo_organizer.analysis.shuffle import _is_photo, persons
from photo_organizer.constants import PHOTO_EXTENSIONS

MAX_PEOPLE = 4
THUMBS = 60                     # clusters drawn with a photo; the rest are counts
PLACE_COLUMNS = ('region', 'country', 'state', 'city')
SURE = {'gps': "l.source = 'gps'", 'measured': "l.source IN ('gps', 'day_gps')", 'any': "l.source IS NOT NULL"}
PHOTO_SQL = '(' + ' OR '.join(f"lower(f.dest_path) LIKE '%{e}'" for e in sorted(PHOTO_EXTENSIONS)) + ')'


def connect(catalog, analysis):
    """The catalog read-only, with analysis.db attached as `a` for the faces."""
    conn = sqlite3.connect(f"file:{catalog}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS a", (f"file:{analysis}?mode=ro",))
    return conn


def has_locations(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'locations'").fetchone())


def where(f, skip=None, bbox=True):
    """(SQL, args) for the filters f (a dict of the query's values); skip: one place column left out,
    so its menu lists every choice the other filters allow."""
    sql = [SURE.get(f.get('sure') or 'measured', SURE['measured']), "f.status = 'done'", PHOTO_SQL]
    args = []
    for col in PLACE_COLUMNS:
        if col != skip and f.get(col):
            sql.append(f"l.{col} = ?")
            args.append(f[col])
    if f.get('start'):
        sql.append("substr(f.taken_at, 1, 4) >= ?")
        args.append(str(f['start']))
    if f.get('end'):
        sql.append("substr(f.taken_at, 1, 4) <= ?")
        args.append(str(f['end']))
    people = sorted(set(f.get('p') or []))[:MAX_PEOPLE]
    if people:
        q = ','.join('?' * len(people))
        sql.append(f"f.sha256 IN (SELECT sha256 FROM a.faces WHERE person_id IN ({q}) GROUP BY sha256 "
                   f"HAVING COUNT(DISTINCT person_id) = ?)")
        args += people + [len(people)]
    if bbox and f.get('bbox'):
        s, w, n, e = f['bbox']
        sql.append("l.lat BETWEEN ? AND ?")
        args += [s, n]
        if w <= e:
            sql.append("l.lon BETWEEN ? AND ?")
            args += [w, e]
        else:                                      # the view crosses the date line
            sql.append("(l.lon >= ? OR l.lon <= ?)")
            args += [w, e]
    return ' AND '.join(sql), args


FROM = "locations l CROSS JOIN files f ON f.id = l.file_id"   # CROSS: locations drive the join


def facets(conn, f):
    """Choices for each place menu and the years, with counts, under the other filters."""
    out = {}
    for col in PLACE_COLUMNS:
        sql, args = where(f, skip=col, bbox=False)
        out[col] = [[r[0], r[1]] for r in conn.execute(
            f"SELECT l.{col}, COUNT(*) n FROM {FROM} WHERE {sql} AND l.{col} IS NOT NULL "
            f"GROUP BY 1 ORDER BY n DESC LIMIT 300", args)]
    sql, args = where({**f, 'start': None, 'end': None}, bbox=False)
    out['years'] = [r[0] for r in conn.execute(
        f"SELECT DISTINCT substr(f.taken_at, 1, 4) FROM {FROM} WHERE {sql} AND f.taken_at IS NOT NULL ORDER BY 1",
        args)]
    sql, args = where(f, bbox=False)
    r = conn.execute(f"SELECT COUNT(*) n, COUNT(l.lat) mapped, MIN(l.lat), MIN(l.lon), MAX(l.lat), MAX(l.lon) "
                     f"FROM {FROM} WHERE {sql}", args).fetchone()
    out['total'], out['mapped'] = r[0], r[1]
    out['bounds'] = [r[2], r[3], r[4], r[5]] if r[1] else None
    return out


def clusters(conn, f, zoom):
    """[{lat, lon, n, sha, cell: [s, w, n, e]}]: photos grouped on a grid about 48 px wide at this zoom."""
    cell = 360 / 2 ** max(0, min(int(zoom), 20)) * 48 / 256
    sql, args = where(f)
    rows = conn.execute(
        f"SELECT CAST(floor(l.lat / ?) AS INT) gy, CAST(floor(l.lon / ?) AS INT) gx, COUNT(*) n, AVG(l.lat) lat, "
        f"AVG(l.lon) lon, MAX(f.taken_at || '|' || f.sha256) last FROM {FROM} WHERE {sql} AND l.lat IS NOT NULL "
        f"GROUP BY gy, gx ORDER BY n DESC LIMIT 4000", [cell, cell] + args).fetchall()
    # Thumbnails only on the biggest clusters: an uncached RAW preview costs a NAS read each.
    return [{'lat': r['lat'], 'lon': r['lon'], 'n': r['n'], 'sha': (r['last'] or '|').split('|')[-1], 'thumb': k < THUMBS,
             'cell': [r['gy'] * cell, r['gx'] * cell, (r['gy'] + 1) * cell, (r['gx'] + 1) * cell]}
            for k, r in enumerate(rows)]


def photos(conn, f, offset=0, limit=120):
    """(total, [photo dicts]) newest first."""
    sql, args = where(f)
    total = conn.execute(f"SELECT COUNT(*) FROM {FROM} WHERE {sql}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT f.sha256, f.dest_path, f.taken_at, l.city, l.state, l.region, l.country, l.source, l.confidence, "
        f"l.lat, l.lon FROM {FROM} WHERE {sql} ORDER BY f.taken_at DESC, f.id DESC LIMIT ? OFFSET ?",
        args + [limit, offset]).fetchall()
    return total, [{'sha': r['sha256'], 'day': os.path.basename(os.path.dirname(r['dest_path'])),
                    'taken': r['taken_at'], 'place': ', '.join(x for x in (r['city'], r['region'] if r['region'] != r['city']
                                                                          else None, r['state'], r['country']) if x),
                    'source': r['source'], 'confidence': r['confidence'], 'lat': r['lat'], 'lon': r['lon']}
                   for r in rows]


def make_app(catalog, analysis, thumb_dir):
    """thumb_dir: shared with the days page (same size, same names)."""
    thumb_dir = Path(thumb_dir)
    app = FastAPI()

    def filters(region, country, state, city, p, start, end, sure, bbox):
        b = None
        if bbox:
            try:
                b = [float(x) for x in bbox.split(',')]
            except ValueError:
                raise HTTPException(400, 'bbox is s,w,n,e')
            if len(b) != 4:
                raise HTTPException(400, 'bbox is s,w,n,e')
        return {'region': region, 'country': country, 'state': state, 'city': city, 'p': p, 'start': start,
                'end': end, 'sure': sure if sure in SURE else 'measured', 'bbox': b}

    def run(fn, *a):
        with closing(connect(catalog, analysis)) as conn:
            if not has_locations(conn):
                return None
            return fn(conn, *a)

    @app.get("/api/persons")
    def api_persons():
        with closing(sqlite3.connect(f"file:{analysis}?mode=ro", uri=True, timeout=30)) as a:
            a.row_factory = sqlite3.Row
            return persons(a)

    @app.get("/api/facets")
    def api_facets(region: str = '', country: str = '', state: str = '', city: str = '', p: list[int] = Query([]),
                   start: int | None = None, end: int | None = None, sure: str = 'measured'):
        out = run(facets, filters(region, country, state, city, p, start, end, sure, ''))
        return out or {c: [] for c in PLACE_COLUMNS} | {'years': [], 'total': 0, 'mapped': 0, 'bounds': None,
                                                         'missing': True}

    @app.get("/api/clusters")
    def api_clusters(zoom: int = 3, region: str = '', country: str = '', state: str = '', city: str = '',
                     p: list[int] = Query([]), start: int | None = None, end: int | None = None,
                     sure: str = 'measured', bbox: str = ''):
        return run(clusters, filters(region, country, state, city, p, start, end, sure, bbox), zoom) or []

    @app.get("/api/photos")
    def api_photos(region: str = '', country: str = '', state: str = '', city: str = '', p: list[int] = Query([]),
                   start: int | None = None, end: int | None = None, sure: str = 'measured', bbox: str = '',
                   offset: int = 0, limit: int = 120):
        out = run(photos, filters(region, country, state, city, p, start, end, sure, bbox), offset, min(limit, 500))
        total, items = out or (0, [])
        return {'total': total, 'items': items}

    def image_path(sha):
        if not sha.isalnum():
            raise HTTPException(404)
        with closing(sqlite3.connect(f"file:{catalog}?mode=ro", uri=True, timeout=30)) as conn:
            for (path,) in conn.execute("SELECT dest_path FROM files WHERE sha256 = ? AND status = 'done'", (sha,)):
                if _is_photo(path) and os.path.exists(path):
                    return path
        raise HTTPException(404)

    @app.get("/thumb/{sha}.jpg")
    def thumb(sha: str):
        cached = thumb_dir / f"{sha}.jpg"
        if not sha.isalnum():
            raise HTTPException(404)
        if not cached.exists():
            data = _jpeg(image_path(sha), THUMB_SIDE)
            thumb_dir.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(data)
        return Response(cached.read_bytes(), media_type='image/jpeg', headers={'Cache-Control': 'max-age=86400'})

    @app.get("/photo/{sha}.jpg")
    def photo(sha: str):
        return Response(_jpeg(image_path(sha), PHOTO_SIDE), media_type='image/jpeg')

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE

    return app


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>photoArk places</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root { --bg:#f6f5f2; --card:#fff; --ink:#1d1d1f; --muted:#6b6b70; --line:#e3e1dc; --accent:#2f6fde; --chip:#eef1f7; }
@media (prefers-color-scheme: dark) { :root { --bg:#161618; --card:#1f1f22; --ink:#ececee; --muted:#9a9aa1;
  --line:#2e2e33; --accent:#6ea0ff; --chip:#2a2e38; } }
* { box-sizing: border-box; }
html, body { height:100%; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.45 -apple-system, system-ui, sans-serif;
  display:flex; flex-direction:column; }
header { background:var(--bg); border-bottom:1px solid var(--line); padding:10px 16px; display:flex; gap:8px;
  flex-wrap:wrap; align-items:center; }
header h1 { font-size:17px; margin:0 6px 0 0; }
select { font:inherit; font-size:14px; padding:5px; border:1px solid var(--line); border-radius:8px;
  background:var(--card); color:var(--ink); max-width:13em; }
select.set { border-color:var(--accent); }
label { font-size:14px; color:var(--muted); display:flex; gap:5px; align-items:center; cursor:pointer; }
.sep { width:1px; height:22px; background:var(--line); margin:0 4px; }
#count { color:var(--muted); font-size:13px; }
button.open { font:inherit; font-size:13px; padding:4px 10px; border-radius:8px; border:1px solid var(--accent);
  background:transparent; color:var(--accent); cursor:pointer; }
#split { flex:1; display:flex; min-height:0; }
#map { flex:1.3; min-width:0; }
#side { flex:1; min-width:0; overflow-y:auto; }
@media (max-width: 800px) { #split { flex-direction:column; } #map { flex:none; height:55vh; } #side { overflow:visible; } }
#grid { padding:10px; display:grid; gap:6px; grid-template-columns:repeat(auto-fill, minmax(140px, 1fr)); }
.ph { position:relative; background:var(--line); border-radius:8px; overflow:hidden; aspect-ratio:1; cursor:zoom-in; }
.ph img { width:100%; height:100%; object-fit:cover; display:block; }
.ph .d { position:absolute; left:0; right:0; bottom:0; padding:14px 6px 4px; font-size:11px; color:#fff;
  background:linear-gradient(transparent, rgba(0,0,0,.7)); line-height:1.25; }
.ph .src { position:absolute; top:5px; left:5px; font-size:10px; color:#fff; background:rgba(0,0,0,.55);
  border-radius:4px; padding:0 4px; }
.empty { grid-column:1 / -1; color:var(--muted); text-align:center; padding:40px 10px; }
#more { grid-column:1 / -1; justify-self:center; }
.cl { border-radius:50%; background:rgba(47,111,222,.85); color:#fff; font:600 12px -apple-system, system-ui;
  display:flex; align-items:center; justify-content:center; border:2px solid #fff; box-sizing:border-box; box-shadow:0 1px 4px rgba(0,0,0,.4); }
.leaflet-container .leaflet-marker-pane .cl img { flex:none; width:100%; height:100%; border-radius:50%;
  object-fit:cover; display:block; }   /* Leaflet sets marker images to width:auto */
.cl span { position:absolute; bottom:-6px; right:-8px; background:#2f6fde; border-radius:9px; padding:0 5px;
  border:1px solid #fff; font-size:11px; }
#viewer { position:fixed; inset:0; background:rgba(0,0,0,.88); display:none; align-items:center;
  justify-content:center; flex-direction:column; gap:10px; z-index:2000; padding:16px; }
#viewer img { max-width:100%; max-height:82vh; border-radius:6px; }
#viewer p { color:#ddd; max-width:900px; text-align:center; margin:0; }
#viewer .bar { display:flex; gap:10px; }
#viewer a { color:#9cf; }
</style></head><body>
<header>
  <h1>Places</h1>
  <select id="region" data-label="Any region"></select>
  <select id="country" data-label="Any country"></select>
  <select id="state" data-label="Any state"></select>
  <select id="city" data-label="Any city"></select>
  <span class="sep"></span>
  <span id="pick"></span>
  <span class="sep"></span>
  <select id="start"></select>–<select id="end"></select>
  <select id="sure" title="How the place was found">
    <option value="gps">GPS only</option>
    <option value="measured" selected>GPS + same day</option>
    <option value="any">Any (incl. Gemma text)</option>
  </select>
  <label title="Off: every matching photo, also those placed from Gemma text that have no position">
    <input type="checkbox" id="inview" checked> Map area only</label>
  <button class="open" id="reset">Clear</button>
  <span id="count"></span>
</header>
<div id="split"><div id="map"></div><div id="side"><div id="grid"></div></div></div>
<div id="viewer"><img id="vimg" alt=""><p id="vcap"></p>
  <div class="bar"><button class="open" id="vclose">Close (Esc) · ← → step</button></div></div>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const PLACES = ['region', 'country', 'state', 'city'], N = 4;
const SOURCE = {gps: 'GPS', day_gps: 'same day', image_setting: 'Gemma', day_setting: 'Gemma day'};
let people = [], offset = 0, total = 0, viewing = null, fitNext = true, timer = null;

const map = L.map('map', {worldCopyJump: true}).setView([30, -40], 2);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'}).addTo(map);
const layer = L.layerGroup().addTo(map);
new ResizeObserver(() => map.invalidateSize()).observe($('#map'));   // the header wraps on narrow windows

function chosen() { return [...new Set([...document.querySelectorAll('#pick select')].map(s => s.value).filter(Boolean))]; }
function query(withView) {
  const u = new URLSearchParams();
  // A menu not filled yet holds the URL's choice in data-want.
  const val = id => $('#' + id).value || $('#' + id).dataset.want || '';
  for (const c of PLACES) if (val(c)) u.set(c, val(c));
  for (const p of chosen()) u.append('p', p);
  if (val('start')) u.set('start', val('start'));
  if (val('end')) u.set('end', val('end'));
  u.set('sure', $('#sure').value);
  if (withView) {
    const b = map.getBounds(), w = ((b.getWest() + 540) % 360) - 180, e = ((b.getEast() + 540) % 360) - 180;
    u.set('bbox', [Math.max(-90, b.getSouth()), b.getEast() - b.getWest() >= 360 ? -180 : w,
                   Math.min(90, b.getNorth()), b.getEast() - b.getWest() >= 360 ? 180 : e].map(x => x.toFixed(5)).join(','));
  }
  return u;
}
// The choice lives in the URL, so Back and shared links keep it.
function writeUrl() {
  const u = query(false); if (!$('#inview').checked) u.set('all', '1');
  history.replaceState(null, '', '?' + u);
}
function readUrl() {
  const u = new URLSearchParams(location.search);
  for (const c of PLACES) $('#' + c).dataset.want = u.get(c) || '';
  $('#start').dataset.want = u.get('start') || ''; $('#end').dataset.want = u.get('end') || '';
  if (u.get('sure')) $('#sure').value = u.get('sure');
  $('#inview').checked = u.get('all') != '1';
  return u.getAll('p');
}

function fill(sel, items, label) {
  const v = sel.value || sel.dataset.want || '';
  sel.innerHTML = `<option value="">${label}</option>` + items.map(([name, n]) =>
    `<option value="${esc(name)}">${esc(name)} (${n.toLocaleString()})</option>`).join('');
  if (v && !items.some(([name]) => name == v)) sel.insertAdjacentHTML('beforeend', `<option value="${esc(v)}">${esc(v)} (0)</option>`);
  sel.value = v; sel.dataset.want = ''; sel.classList.toggle('set', !!v);
}
function pickers() {
  const sels = [...document.querySelectorAll('#pick select')], taken = sels.map(s => s.value);
  sels.forEach((s, i) => {
    const v = s.value;
    s.innerHTML = `<option value="">${i ? '+ person' : 'Anyone'}</option>` + people
      .filter(p => String(p.id) == v || !taken.includes(String(p.id)))
      .map(p => `<option value="${p.id}">${esc(p.name)} (${p.photos.toLocaleString()})</option>`).join('');
    s.value = v; s.classList.toggle('set', !!v);
    s.style.display = i == 0 || sels[i - 1].value ? '' : 'none';
  });
}

async function refresh() {
  writeUrl();
  const f = await (await fetch('api/facets?' + query(false))).json();
  if (f.missing) {
    $('#grid').innerHTML = '<div class="empty">No locations yet: run <code>make locations</code>.</div>'; return;
  }
  for (const c of PLACES) fill($('#' + c), f[c], $('#' + c).dataset.label);
  for (const [id, label] of [['start', 'From'], ['end', 'To']]) {
    const s = $('#' + id), v = s.value || s.dataset.want || '';
    s.innerHTML = `<option value="">${label}</option>` + f.years.map(y => `<option>${y}</option>`).join('');
    s.value = v; s.dataset.want = ''; s.classList.toggle('set', !!v);
  }
  $('#count').dataset.all = `${f.total.toLocaleString()} photos, ${f.mapped.toLocaleString()} on the map`;
  if (fitNext && f.bounds) {
    fitNext = false;
    const [s, w, n, e] = f.bounds;
    map.fitBounds([[s, w], [n, e]], {maxZoom: 14, padding: [20, 20]});
  }
  clearTimeout(timer); timer = setTimeout(view, 150);    // once, also when fitting moved the map (moveend)
}

async function drawMap() {
  const u = query(true); u.set('zoom', map.getZoom());
  const cl = await (await fetch('api/clusters?' + u)).json();
  layer.clearLayers();
  for (const c of cl) {
    const size = c.thumb ? (c.n == 1 ? 44 : Math.round(34 + Math.min(30, Math.log2(c.n) * 3)))
                         : Math.round(18 + Math.min(20, Math.log2(c.n) * 2.5));
    const icon = L.divIcon({className: '', iconSize: [size, size], html:
      c.thumb ? `<div class="cl" style="width:${size}px;height:${size}px;position:relative"><img src="thumb/${c.sha}.jpg" alt="">` +
      (c.n > 1 ? `<span>${c.n.toLocaleString()}</span>` : '') + '</div>'
      : `<div class="cl" style="width:${size}px;height:${size}px">${c.n > 1 ? c.n.toLocaleString() : ''}</div>`});
    L.marker([c.lat, c.lon], {icon, title: `${c.n.toLocaleString()} photo${c.n == 1 ? '' : 's'}`})
      .on('click', () => c.n > 1 && map.getZoom() < 18
          ? map.fitBounds([[c.cell[0], c.cell[1]], [c.cell[2], c.cell[3]]], {padding: [30, 30]})
          : openSha(c.sha))
      .addTo(layer);
  }
}

async function loadPhotos(reset) {
  if (reset) { offset = 0; $('#grid').innerHTML = ''; }
  const u = query($('#inview').checked); u.set('offset', offset);
  const r = await (await fetch('api/photos?' + u)).json();
  if (reset) $('#grid').innerHTML = '';
  total = r.total; offset += r.items.length;
  $('#count').textContent = `${total.toLocaleString()} ${$('#inview').checked ? 'in view' : 'matching'} · ` + ($('#count').dataset.all || '');
  $('#more')?.remove();
  if (reset && !r.items.length) $('#grid').innerHTML = '<div class="empty">No photos here with these filters.</div>';
  for (const p of r.items) $('#grid').insertAdjacentHTML('beforeend',
    `<div class="ph" data-sha="${p.sha}" data-day="${esc(p.day)}" data-place="${esc(p.place)}" data-src="${esc(p.source)}"
      data-conf="${p.confidence ?? ''}" data-lat="${p.lat ?? ''}" data-lon="${p.lon ?? ''}" title="${esc(p.place)}">
     <img loading="lazy" src="thumb/${p.sha}.jpg" alt=""><span class="src">${SOURCE[p.source] || ''}</span>
     <span class="d">${esc(p.day)}<br>${esc(p.place)}</span></div>`);
  if (offset < total) $('#grid').insertAdjacentHTML('beforeend', '<button class="open" id="more">More</button>');
}

function view() { writeUrl(); drawMap(); loadPhotos(true); }
map.on('moveend', () => { clearTimeout(timer); timer = setTimeout(view, 150); });

function show(el) {
  viewing = el;
  $('#vimg').src = `photo/${el.dataset.sha}.jpg`;
  const d = el.dataset, src = SOURCE[d.src] || d.src;
  $('#vcap').innerHTML = `${esc(d.day)} — ${esc(d.place)} <small>(${esc(src)}, ${d.conf ? Math.round(d.conf * 100) + '%' : ''})</small>` +
    ` · <a href="../days/?q=${encodeURIComponent(d.day)}">the day</a>`;
  $('#viewer').style.display = 'flex';
}
function openSha(sha) {
  const el = document.querySelector(`.ph[data-sha="${sha}"]`);
  if (el) show(el); else { viewing = null; $('#vimg').src = `photo/${sha}.jpg`; $('#vcap').textContent = ''; $('#viewer').style.display = 'flex'; }
}
function step(by) {
  if (!viewing) return;
  const all = [...document.querySelectorAll('.ph')], next = all[all.indexOf(viewing) + by];
  if (next) { show(next); next.scrollIntoView({block: 'nearest'}); }
  else if (by > 0 && offset < total) loadPhotos(false).then(() => step(by));
}

document.addEventListener('click', e => {
  const t = e.target, ph = t.closest('.ph');
  if (t.id == 'more') loadPhotos(false);
  else if (ph) show(ph);
  else if (t.id == 'vclose' || t.id == 'viewer') $('#viewer').style.display = 'none';
  else if (t.id == 'reset') {
    for (const c of PLACES) $('#' + c).value = '';
    document.querySelectorAll('#pick select').forEach(s => { s.value = ''; });
    $('#start').value = $('#end').value = ''; pickers(); fitNext = true; refresh();
  }
});
document.addEventListener('keydown', e => {
  if ($('#viewer').style.display != 'flex') return;
  if (e.key == 'Escape') $('#viewer').style.display = 'none';
  else if (e.key == 'ArrowRight') { e.preventDefault(); step(1); }
  else if (e.key == 'ArrowLeft') { e.preventDefault(); step(-1); }
});
for (const c of [...PLACES, 'start', 'end', 'sure'])
  $('#' + c).addEventListener('change', () => { fitNext = PLACES.includes(c) || c == 'sure'; refresh(); });
$('#inview').addEventListener('change', () => { writeUrl(); loadPhotos(true); });

(async () => {
  const ps = readUrl();
  people = await (await fetch('api/persons')).json();
  $('#pick').innerHTML = Array.from({length: N}, () => '<select></select> ').join('');
  document.querySelectorAll('#pick select').forEach((s, i) => { s.innerHTML = `<option value="${ps[i] || ''}"></option>`; s.value = ps[i] || ''; });
  pickers();
  document.querySelectorAll('#pick select').forEach(s => s.addEventListener('change', () => {
    const v = chosen();
    document.querySelectorAll('#pick select').forEach((x, i) => { x.value = v[i] || ''; });
    pickers(); fitNext = true; refresh();
  }));
  refresh();
})();
</script></body></html>"""
