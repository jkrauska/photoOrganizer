"""Archive summary for the status server: what's in the store by year, month and day."""

import os
import re
import sqlite3
from collections import defaultdict

from photo_organizer.constants import RAW_EXTENSIONS, VIDEO_EXTENSIONS

DAY_RE = re.compile(r'/(\d{4})/(\d{2})/(\d{4}-\d{2}-\d{2})/[^/]+$')
KINDS = ('raw', 'photo', 'video', 'other')
PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.heic', '.heif', '.png', '.tif', '.tiff', '.gif', '.webp', '.psd'}


def _kind(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext in RAW_EXTENSIONS:
        return 'raw'
    if ext in PHOTO_EXTENSIONS:
        return 'photo'
    if ext in VIDEO_EXTENSIONS:
        return 'video'
    return 'other'


def _empty():
    return {'files': 0, 'bytes': 0, **{k: 0 for k in KINDS}}


def _add(bucket, kind, size):
    bucket['files'] += 1
    bucket['bytes'] += size
    bucket[kind] += 1


def _connect(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def archive_snapshot(db_path):
    conn = _connect(db_path)
    try:
        years, months, total = defaultdict(_empty), defaultdict(_empty), _empty()
        for r in conn.execute("SELECT dest_path, filename, file_size FROM files WHERE status = 'done'"):
            m = DAY_RE.search(r['dest_path'] or '')
            if not m:
                continue
            kind, size = _kind(r['filename']), r['file_size'] or 0
            _add(years[m.group(1)], kind, size)
            _add(months[f"{m.group(1)}-{m.group(2)}"], kind, size)
            _add(total, kind, size)
        left = {r['status']: r['n'] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM files WHERE status IN ('pending', 'review', 'ignored', 'error') "
            "GROUP BY status")}
        dest = None
        row = conn.execute("SELECT value FROM progress WHERE key = 'run'").fetchone()
        if row:
            import json
            dest = json.loads(row[0]).get('dest')
        return {
            'total': total, 'dest': dest, 'left': left,
            'years': [{'year': y, **v} for y, v in sorted(years.items())],
            'months': {k: v for k, v in sorted(months.items())},
        }
    finally:
        conn.close()


def month_detail(db_path, ym):
    if not re.fullmatch(r'\d{4}-\d{2}', ym or ''):
        return {'month': ym, 'days': []}
    conn = _connect(db_path)
    try:
        days = defaultdict(_empty)
        folders = {}
        like = f"%/{ym[:4]}/{ym[5:7]}/{ym}-%"
        for r in conn.execute(
            "SELECT dest_path, filename, file_size FROM files WHERE status = 'done' AND dest_path LIKE ?", (like,)
        ):
            m = DAY_RE.search(r['dest_path'])
            if not m:
                continue
            _add(days[m.group(3)], _kind(r['filename']), r['file_size'] or 0)
            folders[m.group(3)] = os.path.dirname(r['dest_path'])
        return {'month': ym, 'days': [{'day': d, 'folder': folders[d], **v} for d, v in sorted(days.items())]}
    finally:
        conn.close()


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Photo Archive</title>
<style>
:root { --bg:#f6f7f9; --card:#fcfcfb; --fg:#0b0b0b; --muted:#52514e; --line:#e3e6ec; --empty:#eef0f3;
  --s-raw:#2a78d6; --s-photo:#eb6834; --s-video:#1baf7a; --s-other:#eda100;
  --q1:#4b03a1; --q2:#7d03a8; --q3:#a82296; --q4:#cb4679; --q5:#e56b5d; --q6:#f89441; --q7:#fdc328; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#12151b; --card:#1a1a19; --fg:#f0efec; --muted:#c3c2b7; --line:#2a303b; --empty:#22262e;
  --s-raw:#3987e5; --s-photo:#d95926; --s-video:#199e70; --s-other:#c98500;
  --q1:#4b03a1; --q2:#7d03a8; --q3:#a82296; --q4:#cb4679; --q5:#e56b5d; --q6:#f89441; --q7:#fdc328; } }
:root[data-theme="dark"] {
  --bg:#12151b; --card:#1a1a19; --fg:#f0efec; --muted:#c3c2b7; --line:#2a303b; --empty:#22262e;
  --s-raw:#3987e5; --s-photo:#d95926; --s-video:#199e70; --s-other:#c98500;
  --q1:#4b03a1; --q2:#7d03a8; --q3:#a82296; --q4:#cb4679; --q5:#e56b5d; --q6:#f89441; --q7:#fdc328; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width:2400px; margin:0 auto; padding:20px 16px 40px; }
@media (min-width:1200px) { main { padding:24px 32px 40px; } }
header { display:flex; align-items:baseline; gap:16px; flex-wrap:wrap; margin-bottom:16px; }
h1 { font-size:20px; margin:0; }
nav a { color:var(--muted); text-decoration:none; margin-right:12px; } nav a.on { color:var(--fg); font-weight:600; }
.grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(160px, 1fr)); gap:12px; margin-bottom:12px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; min-width:0; margin-bottom:12px; }
.grid .card { margin-bottom:0; }
.k { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
.v { font-size:26px; font-weight:650; font-variant-numeric:tabular-nums; margin-top:2px; }
.s { font-size:12px; color:var(--muted); font-variant-numeric:tabular-nums; }
h2 { font-size:13px; margin:0 0 10px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
.legend { display:flex; gap:14px; flex-wrap:wrap; font-size:12px; color:var(--muted); margin-bottom:10px; }
.legend i { display:inline-block; width:10px; height:10px; border-radius:3px; margin-right:5px; vertical-align:-1px; }
.years { display:grid; grid-template-columns:44px 1fr minmax(150px, auto); gap:6px 10px; align-items:center; }
.years .y { font-variant-numeric:tabular-nums; color:var(--muted); font-size:13px; }
.bar { display:flex; gap:2px; height:16px; }
.bar span { height:100%; min-width:2px; }
.bar span:first-child { border-radius:4px 0 0 4px; } .bar span:last-child { border-radius:0 4px 4px 0; }
.bar span:only-child { border-radius:4px; }
.years .n { font-size:12px; font-variant-numeric:tabular-nums; white-space:nowrap; }
.heat { display:grid; grid-template-columns:44px repeat(12, minmax(22px, 1fr)); gap:3px; }
.heat .h { font-size:11px; color:var(--muted); text-align:center; }
.heat .y { font-size:12px; color:var(--muted); font-variant-numeric:tabular-nums; align-self:center; }
.heat .c { height:26px; border-radius:4px; background:var(--empty); cursor:default; }
.heat .c.has { cursor:pointer; } .heat .c.sel { outline:2px solid var(--fg); outline-offset:1px; }
.scale { display:flex; align-items:center; gap:3px; font-size:11px; color:var(--muted); margin-top:10px; }
.scale i { width:22px; height:10px; border-radius:3px; display:inline-block; }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
td, th { text-align:left; padding:5px 6px; border-top:1px solid var(--line); }
th { color:var(--muted); font-weight:500; border-top:none; } td.n, th.n { text-align:right; }
.path { font-family:ui-monospace, Menlo, monospace; font-size:12px; word-break:break-all; color:var(--muted); }
.two { display:grid; grid-template-columns:1fr; gap:12px; }
@media (min-width:1400px) { .two { grid-template-columns:2fr 3fr; align-items:start; } .two .card { margin-bottom:0; } }
#tip { position:fixed; pointer-events:none; background:var(--card); color:var(--fg); border:1px solid var(--line);
  border-radius:8px; padding:6px 9px; font-size:12px; box-shadow:0 4px 14px rgba(0,0,0,.18); display:none; z-index:10;
  font-variant-numeric:tabular-nums; }
details summary { cursor:pointer; color:var(--muted); font-size:12px; margin-top:10px; }
</style></head><body><main>
<header><h1>Photo Archive</h1><nav><a href="/">Status</a><a href="/archive" class="on">Archive</a></nav>
<span id="dest" class="s"></span></header>
<div class="grid" id="tiles"></div>
<div class="two">
  <div class="card"><h2>Files by year</h2><div class="legend" id="legend"></div><div class="years" id="years"></div>
    <details><summary>Show as table</summary><table id="ytable"></table></details></div>
  <div class="card"><h2>Files by month</h2><div class="heat" id="heat"></div><div class="scale" id="scale"></div>
    <p class="s">Click a month to list its days.</p></div>
</div>
<div class="card" id="monthcard" style="display:none"><h2 id="mtitle"></h2><table id="mtable"></table></div>
<div id="tip"></div>
</main>
<script>
const KINDS = [['raw','RAW'], ['photo','Photo'], ['video','Video'], ['other','Other']];
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const $ = id => document.getElementById(id);
const num = n => (n ?? 0).toLocaleString();
function bytes(b) { const u=['B','KB','MB','GB','TB']; let i=0; b=b||0; while (b>=1000 && i<u.length-1) { b/=1000; i++; }
  return b.toFixed(b<10&&i?1:0)+' '+u[i]; }
function el(tag, cls, text) { const e=document.createElement(tag); if (cls) e.className=cls; if (text!=null) e.textContent=text; return e; }
const tip = $('tip');
function showTip(ev, lines) { tip.replaceChildren(...lines.map((l, i) => el('div', i ? 's' : '', l)));
  tip.style.display='block'; const x=Math.min(ev.clientX+14, innerWidth-tip.offsetWidth-8);
  tip.style.left=x+'px'; tip.style.top=(ev.clientY+14)+'px'; }
function hideTip() { tip.style.display='none'; }
function kindsLine(v) { return KINDS.filter(([k]) => v[k]).map(([k, l]) => `${num(v[k])} ${l}`).join(' · '); }

let selected = null;
async function load() {
  const d = await (await fetch('/api/archive')).json();
  $('dest').textContent = d.dest ? d.dest : '';
  const t = d.total, ys = d.years;
  const tiles = [
    ['Files', num(t.files), bytes(t.bytes)],
    ['Years', ys.length ? `${ys[0].year}–${ys[ys.length-1].year}` : '–', `${ys.length} years with photos`],
    ['RAW', num(t.raw), ''], ['Photos', num(t.photo), 'JPEG, HEIC, PNG, TIFF…'], ['Videos', num(t.video), ''],
    ['Still in sources', num((d.left.pending||0)+(d.left.review||0)+(d.left.ignored||0)+(d.left.error||0)),
     `${num(d.left.review||0)} no date · ${num(d.left.ignored||0)} other types · ${num(d.left.error||0)} errors`],
  ];
  $('tiles').replaceChildren(...tiles.map(([k, v, s]) => { const c=el('div','card'); c.append(el('div','k',k), el('div','v',v), el('div','s',s)); return c; }));

  $('legend').replaceChildren(...KINDS.map(([k, l]) => { const s=el('span'); const i=el('i'); i.style.background=`var(--s-${k})`; s.append(i, l); return s; }));
  const max = Math.max(1, ...ys.map(y => y.files));
  const yb = $('years'); yb.replaceChildren();
  ys.slice().reverse().forEach(y => {
    const bar = el('div', 'bar');
    bar.style.width = (100 * y.files / max) + '%';
    KINDS.forEach(([k, l]) => { if (!y[k]) return; const s=el('span'); s.style.flex = y[k]; s.style.background=`var(--s-${k})`;
      s.addEventListener('mousemove', ev => showTip(ev, [`${y.year} · ${l}`, `${num(y[k])} of ${num(y.files)} files`]));
      s.addEventListener('mouseleave', hideTip); bar.append(s); });
    yb.append(el('div','y',y.year), bar, el('div','n',`${num(y.files)} · ${bytes(y.bytes)}`));
  });
  const yt = $('ytable'); yt.replaceChildren();
  const hr = el('tr'); ['Year','Files','RAW','Photo','Video','Other','Size'].forEach((h, i) => hr.append(el('th', i ? 'n' : '', h))); yt.append(hr);
  ys.forEach(y => { const tr=el('tr'); [y.year, num(y.files), num(y.raw), num(y.photo), num(y.video), num(y.other), bytes(y.bytes)]
    .forEach((v, i) => tr.append(el('td', i ? 'n' : '', v))); yt.append(tr); });

  const counts = Object.values(d.months).map(m => m.files).filter(Boolean).sort((a, b) => a - b);
  const q = p => counts.length ? counts[Math.min(counts.length - 1, Math.floor(p * counts.length))] : 0;
  const cuts = [1/7, 2/7, 3/7, 4/7, 5/7, 6/7].map(q);
  const step = n => 1 + cuts.filter(c => n > c).length;
  const heat = $('heat'); heat.replaceChildren(el('div'));
  MONTHS.forEach(m => heat.append(el('div','h',m)));
  ys.slice().reverse().forEach(y => {
    heat.append(el('div','y',y.year));
    MONTHS.forEach((mn, i) => {
      const key = `${y.year}-${String(i + 1).padStart(2, '0')}`, m = d.months[key];
      const c = el('div', 'c' + (m ? ' has' : '') + (key === selected ? ' sel' : ''));
      if (m) {
        c.style.background = `var(--q${step(m.files)})`;
        c.addEventListener('mousemove', ev => showTip(ev, [`${mn} ${y.year} · ${num(m.files)} files · ${bytes(m.bytes)}`, kindsLine(m)]));
        c.addEventListener('mouseleave', hideTip);
        c.addEventListener('click', () => { selected = key; document.querySelectorAll('.heat .c.sel').forEach(x => x.classList.remove('sel')); c.classList.add('sel'); showMonth(key); });
      }
      heat.append(c);
    });
  });
  const sc = $('scale'); sc.replaceChildren(el('span', '', 'fewer'));
  for (let i = 1; i <= 7; i++) { const s=el('i'); s.style.background=`var(--q${i})`; sc.append(s); }
  sc.append(el('span', '', 'more files per month'));
}

async function showMonth(ym) {
  const d = await (await fetch('/api/archive/month?ym=' + ym)).json();
  const [y, m] = ym.split('-');
  $('monthcard').style.display = 'block';
  $('mtitle').textContent = `${MONTHS[+m - 1]} ${y} · ${d.days.length} days`;
  const t = $('mtable'); t.replaceChildren();
  const hr = el('tr'); ['Day','Files','RAW','Photo','Video','Size','Folder'].forEach((h, i) => hr.append(el('th', i && i < 6 ? 'n' : '', h))); t.append(hr);
  d.days.forEach(x => { const tr=el('tr');
    const wd = new Date(x.day + 'T12:00:00').toLocaleDateString([], {weekday:'short', month:'short', day:'numeric'});
    [wd, num(x.files), num(x.raw), num(x.photo), num(x.video), bytes(x.bytes)].forEach((v, i) => tr.append(el('td', i ? 'n' : '', v)));
    tr.append(el('td', 'path', x.folder)); t.append(tr); });
  $('monthcard').scrollIntoView({behavior:'smooth', block:'nearest'});
}
load(); setInterval(load, 15000);
</script></body></html>
"""
