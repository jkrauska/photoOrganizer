"""Live status page for an organizer run: throughput, ETA, per-source progress, errors.

Reads the catalog read-only, so it can run alongside (or after) `photo-organizer`.
"""

import argparse
import datetime
import json
import os
import sqlite3
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Timer

from urllib.parse import parse_qs, urlparse

from photo_organizer.archive import PAGE as ARCHIVE_PAGE, archive_snapshot, month_detail
from photo_organizer.db import DEFAULT_DB_PATH, init_db
from photo_organizer.hashing import LARGE_FILE
from photo_organizer.nas import same_server, smb_mounts
from photo_organizer.work_queue import enqueue, enqueue_photos, enqueue_split

MOVED = "('done', 'duplicate_removed')"


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def _rel(path, root):
    if path and root and path.startswith(root.rstrip('/') + '/'):
        return path[len(root.rstrip('/')) + 1:]
    return path


def _activity(row, dest_root):
    """One recent-activity line: where the file came from and where it ended up (or why it stayed)."""
    status, msg = row['status'], row['error_message'] or ''
    target = row['dest_path']
    if status == 'duplicate_removed' and msg.startswith('duplicate of '):
        target = msg[len('duplicate of '):].split('; source removed')[0]
    return {
        'updated_at': row['updated_at'],
        'status': status,
        'source_root': row['source_root'],
        'source': _rel(row['original_path'], row['source_root']),
        'dest': _rel(target, dest_root) if status in ('done', 'duplicate_removed', 'copying') else None,
        'note': msg if status in ('review', 'ignored', 'error') else None,
    }


def snapshot(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT value FROM progress WHERE key = 'run'").fetchone()
        run = json.loads(row[0]) if row else {}
        now = datetime.datetime.now(datetime.timezone.utc)
        age = elapsed = None
        if run.get('heartbeat'):
            age = (now - datetime.datetime.fromisoformat(run['heartbeat'])).total_seconds()
        if run.get('started_at'):
            elapsed = (now - datetime.datetime.fromisoformat(run['started_at'])).total_seconds()
        run['heartbeat_age'] = age
        run['elapsed'] = elapsed
        run['alive'] = bool(run) and not run.get('finished') and _pid_alive(run.get('pid'))

        started = None
        if run.get('started_at'):
            started = datetime.datetime.fromisoformat(run['started_at']).astimezone(datetime.timezone.utc)

        def window(seconds):
            # Count only this run's moves, so a previous run's burst doesn't inflate the rate.
            lower = now - datetime.timedelta(seconds=seconds)
            if started and started > lower:
                lower = started
            r = conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(file_size), 0) FROM files WHERE status IN {MOVED} "
                f"AND updated_at >= ?", (lower.strftime('%Y-%m-%dT%H:%M:%S'),),
            ).fetchone()
            span = max((now - lower).total_seconds(), 1)
            return {'files_per_s': r[0] / span, 'bytes_per_s': r[1] / span, 'files': r[0]}

        rates = {'1m': window(60), '10m': window(600), 'run': {'files_per_s': 0, 'bytes_per_s': 0, 'files': 0}}
        # Live bytes/s: small files as they finish, plus large files' in-progress rate (a 40 GB clip
        # would otherwise read as zero for minutes, then as a one-minute spike when it completes).
        lower = max(now - datetime.timedelta(seconds=60), started) if started else now - datetime.timedelta(seconds=60)
        small = conn.execute(
            f"SELECT COALESCE(SUM(file_size), 0) FROM files WHERE status IN {MOVED} AND file_size < ? "
            f"AND updated_at >= ?", (LARGE_FILE, lower.strftime('%Y-%m-%dT%H:%M:%S')),
        ).fetchone()[0]
        large = run.get('large') or [] if run.get('alive') else []
        rates['live'] = {'bytes_per_s': small / max((now - lower).total_seconds(), 1)
                         + sum(f.get('bytes_per_s', 0) for f in large)}
        if started:
            ended = datetime.datetime.fromisoformat(run['finished_at']) if run.get('finished_at') else now
            span = max((ended - started).total_seconds(), 1)
            r = conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(file_size), 0) FROM files WHERE status IN {MOVED} "
                f"AND updated_at >= ?", (started.strftime('%Y-%m-%dT%H:%M:%S'),),
            ).fetchone()
            rates['run'] = {'files_per_s': r[0] / span, 'bytes_per_s': r[1] / span, 'files': r[0]}
        scanned_1m = conn.execute(
            "SELECT COUNT(*) FROM files WHERE created_at >= datetime('now', '-60 seconds')"
        ).fetchone()[0]
        rates['scan_files_per_s'] = scanned_1m / max(min(60, elapsed or 60), 1)

        pending = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(file_size), 0) FROM files WHERE status = 'pending'"
        ).fetchone()
        rate = rates['10m'] if rates['10m']['files'] >= 20 else rates['1m']
        eta = None
        if run.get('alive') and rate['bytes_per_s'] > 0 and pending[1]:
            eta = pending[1] / rate['bytes_per_s']

        statuses = [dict(r) for r in conn.execute(
            "SELECT status, COUNT(*) AS files, COALESCE(SUM(file_size), 0) AS bytes FROM files "
            "GROUP BY status ORDER BY files DESC")]
        sources = {}
        for r in conn.execute(
            "SELECT source_root, status, COUNT(*) AS n FROM files GROUP BY source_root, status"
        ):
            sources.setdefault(r['source_root'] or '?', {})[r['status']] = r['n']
        per_minute = [dict(r) for r in conn.execute(
            f"SELECT substr(updated_at, 1, 16) AS minute, COUNT(*) AS files, "
            f"COALESCE(SUM(file_size), 0) AS bytes FROM files WHERE status IN {MOVED} "
            f"AND updated_at >= strftime('%Y-%m-%dT%H:%M', 'now', '-60 minutes') "
            f"GROUP BY minute ORDER BY minute")]
        recent = [_activity(dict(r), run.get('dest')) for r in conn.execute(
            "SELECT original_path, source_root, dest_path, status, error_message, updated_at FROM files "
            "WHERE updated_at IS NOT NULL ORDER BY updated_at DESC LIMIT 25")]
        errors = [dict(r) for r in conn.execute(
            "SELECT original_path, error_message, updated_at FROM files WHERE status = 'error' "
            "ORDER BY updated_at DESC LIMIT 15")]
        wrow = conn.execute("SELECT value FROM progress WHERE key = 'worker'").fetchone()
        worker = json.loads(wrow[0]) if wrow else {}
        worker['alive'] = bool(worker) and not worker.get('stopped') and _pid_alive(worker.get('pid'))
        worker['hash_pending'] = conn.execute(
            "SELECT COUNT(*) FROM files WHERE status = 'done' AND sha256 IS NULL").fetchone()[0]
        try:
            queue = [dict(r) for r in conn.execute(
                "SELECT id, status, source, added_at, finished_at, result, keep_sources, transfer FROM queue ORDER BY "
                "CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, "
                "CASE status WHEN 'queued' THEN id ELSE -id END LIMIT 20")]
        except sqlite3.OperationalError:
            queue = []
        for q in queue:
            if q['status'] == 'running' and run.get('alive') and run.get('transfer'):
                q['transfer'] = run['transfer']
            elif q['status'] == 'queued' and run.get('dest'):
                host = None if q['source'].startswith('photos:') else _expected_server(q['source'], run['dest'])
                q['transfer_expected'] = f"NAS {host}" if host else 'Mac'
        return {
            'worker': worker, 'queue': queue,
            'run': run, 'rates': rates, 'eta_seconds': eta,
            'pending': {'files': pending[0], 'bytes': pending[1]},
            'statuses': statuses, 'sources': sources, 'per_minute': per_minute,
            'recent': recent, 'errors': errors,
        }
    finally:
        conn.close()


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Organizer Status</title>
<style>
:root { --bg:#f6f7f9; --card:#fff; --fg:#1d2330; --muted:#6b7385; --line:#e3e6ec;
        --accent:#2f7d62; --warn:#b26b00; --bad:#b3261e; --bar:#2f7d62; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#12151b; --card:#1b1f27; --fg:#e7eaf0; --muted:#9aa3b5; --line:#2a303b;
  --accent:#5cc49b; --warn:#e0a040; --bad:#ef6b62; --bar:#5cc49b; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width:2400px; margin:0 auto; padding:20px 16px 40px; }
@media (min-width:1200px) { main { padding:24px 32px 40px; } }
header { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; margin-bottom:16px; }
h1 { font-size:20px; margin:0; }
.pill { padding:2px 10px; border-radius:999px; font-size:12px; font-weight:600; border:1px solid var(--line); }
.pill.live { color:var(--accent); border-color:var(--accent); }
.pill.stale { color:var(--warn); border-color:var(--warn); }
.pill.nas, td.xfer.nas { color:#c026d3; border-color:#c026d3; font-weight:600; }
.pill.mac, td.xfer.mac { color:#0ea5e9; border-color:#0ea5e9; }
td.xfer { white-space:nowrap; font-size:12px; }
.muted { color:var(--muted); }
.grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:12px; margin-bottom:12px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; min-width:0; }
.k { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
.v { font-size:26px; font-weight:650; font-variant-numeric:tabular-nums; margin-top:2px; }
.s { font-size:12px; color:var(--muted); font-variant-numeric:tabular-nums; }
h2 { font-size:13px; margin:0 0 8px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
td, th { text-align:left; padding:5px 6px; border-top:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:500; border-top:none; }
td.n, th.n { text-align:right; }
.path { font-family:ui-monospace, Menlo, monospace; font-size:12px; word-break:break-all; }
.qform { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:10px; }
.qform input { flex:1 1 420px; min-width:0; padding:8px 10px; font:13px ui-monospace, Menlo, monospace;
  color:var(--fg); background:var(--bg); border:1px solid var(--line); border-radius:6px; }
.qform input:focus { outline:none; border-color:var(--accent); }
.qform button, .qbtn { padding:8px 14px; border-radius:6px; border:1px solid var(--accent); background:var(--accent);
  color:#fff; font-weight:600; cursor:pointer; }
.qbtn { padding:2px 8px; font-size:12px; font-weight:500; background:transparent; color:var(--muted); border-color:var(--line); }
.qbtn:hover { color:var(--bad); border-color:var(--bad); }
#qmsg.ok { color:var(--accent); } #qmsg.err { color:var(--bad); }
.path .to { color:var(--accent); margin-top:2px; }
.lf { margin-bottom:10px; } .lf:last-child { margin-bottom:0; }
.lft { display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; }
.path .to.st-review, .path .to.st-ignored { color:var(--warn); } .path .to.st-error { color:var(--bad); }
.st-error { color:var(--bad); } .st-review, .st-ignored { color:var(--warn); } .st-done, .st-duplicate_removed { color:var(--accent); }
.progress { height:8px; background:var(--line); border-radius:4px; overflow:hidden; margin-top:8px; }
.progress > div { height:100%; background:var(--bar); }
svg { width:100%; height:90px; display:block; }
.two { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:12px; }
@media (max-width:760px) { .two { grid-template-columns:1fr; } }
.activity { display:grid; grid-template-columns:1fr; gap:12px; }
@media (min-width:1600px) { .activity { grid-template-columns:3fr 2fr; align-items:start; } }
</style></head><body><main>
<header><h1>Photo Organizer</h1><nav><a href="/" style="color:var(--fg);font-weight:600;text-decoration:none;margin-right:12px">Status</a><a href="/archive" style="color:var(--muted);text-decoration:none">Archive</a></nav><span id="pill" class="pill">loading</span><span id="xfer" class="pill" style="display:none"></span><span id="phase" class="muted"></span></header>
<div class="grid">
  <div class="card"><div class="k">Images / s</div><div class="v" id="fps">–</div><div class="s" id="fps2"></div></div>
  <div class="card"><div class="k">Throughput</div><div class="v" id="mbps">–</div><div class="s" id="mbps2"></div></div>
  <div class="card"><div class="k">ETA (scanned so far)</div><div class="v" id="eta">–</div><div class="s" id="pend"></div></div>
  <div class="card"><div class="k">Batch</div><div class="v" id="batch">–</div><div class="s" id="batchs"></div>
    <div class="progress"><div id="bar" style="width:0"></div></div></div>
</div>
<div class="card" id="largecard" style="margin-bottom:12px;display:none"><h2>Large files in flight</h2><div id="large"></div></div>
<div class="card" style="margin-bottom:12px"><h2>Queue <span id="worker" class="s" style="text-transform:none;letter-spacing:0"></span></h2>
  <form id="qform" class="qform" autocomplete="off">
    <input id="qpath" list="qdirs" placeholder="/Volumes/Photowork/Masters — type a path (or photos:2016); subfolders are suggested" spellcheck="false">
    <datalist id="qdirs"></datalist>
    <label class="s" title="Verify copies into the store but never delete the originals (always on for Time Machine backups)"><input type="checkbox" id="qkeep"> copy only (keep originals)</label>
    <label class="s" title="Queue each subfolder as its own item, then the folder itself: for large folders"><input type="checkbox" id="qsplit"> split into subfolders</label>
    <button type="submit">Add to queue</button>
    <button type="button" id="qphotos" title="Queue every year of the Apple Photos library, oldest first, copy-only (the library is never changed). One year: type photos:2016">Queue Apple Photos years</button>
    <span id="qmsg" class="s"></span>
  </form>
  <table id="queue"></table></div>
<div class="card" style="margin-bottom:12px"><h2>Moved per minute (last hour)</h2><svg id="chart" preserveAspectRatio="none"></svg></div>
<div class="two">
  <div class="card"><h2>Catalog</h2><table id="statuses"></table></div>
  <div class="card"><h2>Sources</h2><table id="sources"></table></div>
</div>
<div class="activity">
  <div class="card"><h2>Recent activity</h2><table id="recent"></table></div>
  <div class="card"><h2>Errors</h2><table id="errors"></table></div>
</div>
<p class="s" id="meta"></p>
</main>
<script>
const LABEL = {done:'moved', duplicate_removed:'duplicate removed', duplicate_kept:'duplicate (kept)', pending:'pending', review:'left: no date',
               ignored:'left: unhandled type', error:'left: error', copying:'copying'};
const $ = id => document.getElementById(id);
const num = n => (n ?? 0).toLocaleString();
function bytes(b) { const u=['B','KB','MB','GB','TB']; let i=0; b=b||0; while (b>=1000 && i<u.length-1) { b/=1000; i++; }
  return b.toFixed(b<10&&i?1:0)+' '+u[i]; }
function dur(s) { if (s==null) return '–'; s=Math.round(s); const d=Math.floor(s/86400), h=Math.floor(s%86400/3600), m=Math.floor(s%3600/60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : m ? `${m}m ${s%60}s` : `${s}s`; }
// Catalog timestamps are UTC (some without a zone suffix); show them in the browser's local time.
function toDate(s) { if (!s) return null; s = s.replace(' ', 'T'); return new Date(/Z|[+-]\d\d:\d\d$/.test(s) ? s : s + 'Z'); }
function localTime(s) { const d = toDate(s); return d ? d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'}) : ''; }
function localDateTime(s) { const d = toDate(s); return d ? d.toLocaleString([], {dateStyle:'medium', timeStyle:'medium'}) : ''; }
function cell(tr, text, cls) { const td=document.createElement('td'); td.textContent=text; if (cls) td.className=cls; tr.appendChild(td); return td; }
function head(t, cols) { t.replaceChildren(); const tr=document.createElement('tr');
  cols.forEach(([c, cls]) => { const th=document.createElement('th'); th.textContent=c; if (cls) th.className=cls; tr.appendChild(th); }); t.appendChild(tr); }
function row(t) { const tr=document.createElement('tr'); t.appendChild(tr); return tr; }

function render(d) {
  const r = d.run || {};
  const pill = $('pill');
  if (r.alive) { pill.textContent = (r.mode || '') + ' running'; pill.className = 'pill live'; }
  else if (r.finished) { pill.textContent = r.phase || 'finished'; pill.className = 'pill'; }
  else if (r.pid) { pill.textContent = 'not running'; pill.className = 'pill stale'; }
  else { pill.textContent = 'no run yet'; pill.className = 'pill'; }
  const xf = $('xfer');
  xf.style.display = r.alive && r.transfer ? '' : 'none';
  if (r.transfer) { xf.textContent = r.transfer.startsWith('NAS') ? '⚡ ' + r.transfer.split(' (')[0] : 'via Mac (SMB)';
    xf.className = 'pill ' + (r.transfer.startsWith('NAS') ? 'nas' : 'mac'); xf.title = r.transfer; }
  $('phase').textContent = r.alive ? `${r.phase || ''}${r.current_dir ? ' · ' + r.current_dir : ''}` : '';

  const a = d.rates['1m'], b = d.rates['10m'], ra = d.rates.run;
  const main = r.alive ? a : ra;
  document.querySelector('#fps').previousElementSibling.textContent = r.alive ? 'Images / s' : 'Images / s (run avg)';
  $('fps').textContent = main.files_per_s.toFixed(1);
  $('fps2').textContent = r.alive
    ? `10-min ${b.files_per_s.toFixed(1)} · run ${ra.files_per_s.toFixed(1)} · scanning ${d.rates.scan_files_per_s.toFixed(1)}/s`
    : `${num(ra.files)} files moved in last run`;
  $('mbps').textContent = bytes(r.alive ? d.rates.live.bytes_per_s : main.bytes_per_s) + '/s';
  const lg = (r.alive && r.large) || [];
  $('largecard').style.display = lg.length ? 'block' : 'none';
  $('large').replaceChildren(...lg.map(f => {
    const pct = f.size ? 100 * f.done / f.size : 0, rate = f.bytes_per_s || 0;
    const eta = rate > 0 ? (f.size - f.done) / rate : null;
    const box = document.createElement('div'); box.className = 'lf';
    const top = document.createElement('div'); top.className = 'lft';
    const name = document.createElement('span'); name.className = 'path'; name.textContent = f.name; name.title = f.source;
    const info = document.createElement('span'); info.className = 's';
    info.textContent = `${f.phase} · ${bytes(f.done)} / ${bytes(f.size)} · ${bytes(rate)}/s · ${eta == null ? '' : dur(eta) + ' left'}`;
    top.append(name, info);
    const bar = document.createElement('div'); bar.className = 'progress';
    const fill = document.createElement('div'); fill.style.width = pct.toFixed(1) + '%'; bar.append(fill);
    box.append(top, bar); return box; }));
  $('mbps2').textContent = r.alive ? `10-min ${bytes(b.bytes_per_s)}/s · run ${bytes(ra.bytes_per_s)}/s` : 'last run average';
  $('eta').textContent = r.alive ? dur(d.eta_seconds) : (r.finished ? 'done' : '–');
  $('pend').textContent = `${num(d.pending.files)} pending · ${bytes(d.pending.bytes)}`;
  $('batch').textContent = r.batch ?? '–';
  const bt = r.batch_total || 0, bd = r.batch_done || 0, moving = r.alive && (r.phase || '').startsWith('transferring');
  $('batchs').textContent = !r.alive ? (r.finished_at ? `ended ${localTime(r.finished_at)}` : '')
    : moving ? `${num(bd)} / ${num(bt)} files`
    : r.phase === 'reading dates' && r.dates_total ? `dates ${num(r.dates_read || 0)} / ${num(r.dates_total)}`
    : (r.scanned_in_batch ? `${num(r.scanned_in_batch)} scanned` : '');
  const reading = r.alive && r.phase === 'reading dates' && r.dates_total;
  $('bar').style.width = (moving && bt ? 100*bd/bt : reading ? 100*(r.dates_read||0)/r.dates_total : 0) + '%';

  const svg = $('chart'), pts = d.per_minute; svg.replaceChildren();
  const W = 600, H = 90, max = Math.max(1, ...pts.map(p => p.files));
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const now = new Date(); const bw = W/60;
  pts.forEach(p => {
    const t = new Date(p.minute + ':00Z'); const ago = Math.floor((now - t)/60000);
    if (ago < 0 || ago >= 60) return;
    const h = Math.max(2, (H-4) * p.files / max);
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', W - (ago+1)*bw + 1); rect.setAttribute('width', bw - 2);
    rect.setAttribute('y', H - h); rect.setAttribute('height', h); rect.setAttribute('fill', 'var(--bar)');
    const tip = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    tip.textContent = `${t.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})} · ${p.files} files · ${bytes(p.bytes)}`; rect.appendChild(tip);
    svg.appendChild(rect);
  });

  const w = d.worker || {};
  $('worker').textContent = w.alive ? `· worker ${w.pid}: ${w.state || ''}${w.current ? ' — ' + w.current : ''}`
    : (w.pid ? '· worker not running (make worker-bg)' : '· no worker yet (make worker-bg)');
  if (w.hash_pending) $('worker').textContent += ` · ${num(w.hash_pending)} moved files awaiting their hash (filled in when idle)`;
  const qt = $('queue'); head(qt, [['#'], ['Status'], ['Transfer'], ['Folder'], ['Result'], ['']]);
  if (!d.queue.length) { const tr=row(qt); cell(tr, ''); cell(tr, 'empty', 'muted'); cell(tr, ''); cell(tr, 'add a folder above', 's'); cell(tr, ''); cell(tr, ''); }
  d.queue.forEach(q => { const tr=row(qt); cell(tr, q.id, 's');
    cell(tr, q.status, 'st-' + ({done:'done', failed:'error', running:'done', queued:'pending', stopped:'review'}[q.status] || ''));
    const t = q.transfer || q.transfer_expected, nasT = (t || '').startsWith('NAS');
    const tc = cell(tr, t ? (nasT ? '⚡ NAS' : 'Mac') + (q.transfer ? '' : ' (expected)') : '', 'xfer ' + (t ? (nasT ? 'nas' : 'mac') : ''));
    if (t) tc.title = t;
    cell(tr, q.source + (q.keep_sources ? '  [copy-only]' : ''), 'path'); cell(tr, q.result || (q.status === 'queued' ? 'added ' + localTime(q.added_at) : ''), 's');
    const td = cell(tr, '');
    if (q.status === 'queued') { const b = document.createElement('button'); b.className = 'qbtn'; b.textContent = 'Remove';
      b.onclick = async () => { const r = await (await fetch('/api/queue/' + q.id, {method:'DELETE'})).json(); qmsg(r); tick(); };
      td.appendChild(b); } });

  const st = $('statuses'); head(st, [['Status'], ['Files','n'], ['Size','n']]);
  d.statuses.forEach(s => { const tr=row(st); cell(tr, LABEL[s.status]||s.status, 'st-'+s.status); cell(tr, num(s.files), 'n'); cell(tr, bytes(s.bytes), 'n'); });

  const so = $('sources'); head(so, [['Source'], ['Moved','n'], ['Pending','n'], ['Left','n']]);
  Object.entries(d.sources).forEach(([src, c]) => { const tr=row(so);
    cell(tr, src, 'path'); cell(tr, num((c.done||0)+(c.duplicate_removed||0)), 'n'); cell(tr, num(c.pending||0), 'n');
    cell(tr, num((c.review||0)+(c.ignored||0)+(c.error||0)), 'n'); });

  const re = $('recent'); head(re, [['When'], ['Status'], ['From → To']]);
  d.recent.forEach(x => { const tr=row(re); cell(tr, localTime(x.updated_at), 's');
    cell(tr, LABEL[x.status]||x.status, 'st-'+x.status);
    const td = cell(tr, '', 'path');
    const from = document.createElement('div'); from.textContent = x.source; from.title = (x.source_root||'') + '/' + x.source;
    const to = document.createElement('div'); to.className = 'to';
    if (x.dest) { to.textContent = '→ ' + x.dest; to.title = (d.run.dest||'') + '/' + x.dest; }
    else if (x.note) { to.textContent = '⏸ ' + x.note; to.classList.add('st-' + x.status); }
    td.replaceChildren(from, to); });

  const er = $('errors'); head(er, [['File'], ['Error']]);
  if (!d.errors.length) { const tr=row(er); cell(tr, 'None', 'muted'); cell(tr, ''); }
  d.errors.forEach(x => { const tr=row(er); cell(tr, x.original_path, 'path'); cell(tr, x.error_message, 'st-error'); });

  $('meta').textContent = r.started_at ? `Run started ${localDateTime(r.started_at)} · elapsed ${dur(r.elapsed)} · heartbeat ${dur(r.heartbeat_age)} ago · dest ${r.dest||''}` : '';
}

async function tick() {
  try { render(await (await fetch('/api/status')).json()); }
  catch (e) { $('pill').textContent = 'status server unreachable'; $('pill').className = 'pill stale'; }
}
function qmsg(r) { const m = $('qmsg'); m.textContent = r.message || ''; m.className = r.ok ? 'ok' : 'err'; }
let dirTimer = null, lastDirQuery = null;
$('qpath').addEventListener('input', () => {
  clearTimeout(dirTimer);
  dirTimer = setTimeout(async () => {
    const v = $('qpath').value.trim(); if (!v.startsWith('/')) return;
    const base = v.endsWith('/') ? v : v.slice(0, v.lastIndexOf('/') + 1) || '/';
    if (base === lastDirQuery) return; lastDirQuery = base;
    const r = await (await fetch('/api/dirs?path=' + encodeURIComponent(base))).json();
    $('qdirs').replaceChildren(...r.dirs.map(p => { const o = document.createElement('option'); o.value = p; return o; }));
  }, 200);
});
$('qform').addEventListener('submit', async ev => {
  ev.preventDefault();
  const path = $('qpath').value.trim(); if (!path) return;
  const r = await (await fetch('/api/queue', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({path, keep_sources: $('qkeep').checked, split: $('qsplit').checked})})).json();
  qmsg(r); if (r.ok) $('qpath').value = ''; tick();
});
$('qphotos').onclick = async () => {
  $('qmsg').textContent = 'reading the Photos library…';
  const r = await (await fetch('/api/queue', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({photos: true})})).json();
  qmsg(r); tick();
};
tick(); setInterval(tick, 2000);
</script></body></html>
"""


_mounts_cache = {'at': 0.0, 'mounts': {}}


def _expected_server(source, dest):
    """The NAS a queued folder would transfer on (same SMB server as the store), else None."""
    if time.time() - _mounts_cache['at'] > 30:
        _mounts_cache.update(at=time.time(), mounts=smb_mounts())
    return same_server([source], dest, _mounts_cache['mounts'])


def _queue_dest(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute("SELECT value FROM progress WHERE key = 'run'").fetchone()
        return json.loads(row[0]).get('dest') if row else None
    finally:
        conn.close()


def list_subdirs(path):
    """Immediate subfolders of path (for the queue box's suggestions), skipping system/bundle folders."""
    from photo_organizer.scanner import should_skip_dir
    path = os.path.abspath(os.path.expanduser(path or '/Volumes'))
    if not os.path.isdir(path):
        path = os.path.dirname(path)
    try:
        names = sorted(e.name for e in os.scandir(path)
                       if e.is_dir(follow_symlinks=False) and not e.name.startswith('.') and not should_skip_dir(e.name))
    except OSError:
        names = []
    return {'path': path, 'dirs': [os.path.join(path, n) for n in names[:200]]}


def make_handler(db_path, port):
    local_hosts = {f'localhost:{port}', f'127.0.0.1:{port}', f'[::1]:{port}'}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype='application/json'):
            if isinstance(body, (dict, list)):
                body = json.dumps(body)
            body = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _trusted(self, write=False):
            """Queue changes delete files, so only this page may make them: the Host must be this
            server on localhost (blocks DNS rebinding), any Origin must match it, and writes must be
            JSON (a cross-site page can't send that without a CORS preflight, which is never granted)."""
            host = self.headers.get('Host', '')
            origin = self.headers.get('Origin')
            if host not in local_hosts or (origin and origin != f'http://{host}'):
                return False
            if write and not self.headers.get('Content-Type', '').startswith('application/json'):
                return False
            return True

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == '/':
                return self._send(200, PAGE, 'text/html; charset=utf-8')
            if url.path == '/archive':
                return self._send(200, ARCHIVE_PAGE, 'text/html; charset=utf-8')
            if url.path == '/api/dirs':
                if not self._trusted():
                    return self.send_error(403)
                return self._send(200, list_subdirs(query.get('path', [''])[0]))
            try:
                if url.path == '/api/status':
                    return self._send(200, snapshot(db_path))
                if url.path == '/api/archive':
                    return self._send(200, archive_snapshot(db_path))
                if url.path == '/api/archive/month':
                    return self._send(200, month_detail(db_path, query.get('ym', [''])[0]))
            except sqlite3.Error as e:
                return self.send_error(503, f"catalog unavailable: {e}")
            self.send_error(404)

        def do_POST(self):
            if urlparse(self.path).path != '/api/queue':
                return self.send_error(404)
            if not self._trusted(write=True):
                return self.send_error(403)
            try:
                length = min(int(self.headers.get('Content-Length', 0)), 65536)
                payload = json.loads(self.rfile.read(length) or b'{}')
                path, keep = payload.get('path', ''), bool(payload.get('keep_sources'))
                split = bool(payload.get('split'))
                photos = bool(payload.get('photos'))
            except (ValueError, AttributeError):
                return self._send(400, {'ok': False, 'message': 'bad request'})
            if photos:
                conn = init_db(db_path)
                try:
                    queued, message = enqueue_photos(conn)
                except Exception as e:      # library unreadable (permissions, osxphotos version)
                    queued, message = [], f"can't read the Apple Photos library: {e}"
                finally:
                    conn.close()
                return self._send(200 if queued else 400, {'ok': bool(queued), 'message': message})
            if not isinstance(path, str) or not path.strip():
                return self._send(400, {'ok': False, 'message': 'enter a folder path'})
            conn = init_db(db_path)
            try:
                if split:
                    queued, message = enqueue_split(conn, path, _queue_dest(db_path), keep)
                else:
                    queued, message = enqueue(conn, path, _queue_dest(db_path), keep)
            finally:
                conn.close()
            return self._send(200 if queued else 400, {'ok': bool(queued), 'message': message})

        def do_DELETE(self):
            parts = urlparse(self.path).path.rstrip('/').split('/')
            if parts[:3] != ['', 'api', 'queue'] or len(parts) != 4 or not parts[3].isdigit():
                return self.send_error(404)
            if not self._trusted():
                return self.send_error(403)
            conn = init_db(db_path)
            try:
                n = conn.execute("DELETE FROM queue WHERE id = ? AND status = 'queued'", (int(parts[3]),)).rowcount
                conn.commit()
            finally:
                conn.close()
            return self._send(200 if n else 400, {'ok': bool(n), 'message': 'removed' if n else 'only queued items can be removed'})

        def log_message(self, *args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Live status page for photo-organizer runs.")
    parser.add_argument('--db', default=DEFAULT_DB_PATH, help=f'Catalog path (default: {DEFAULT_DB_PATH})')
    parser.add_argument('--port', type=int, default=8077)
    parser.add_argument('--host', default='127.0.0.1',
                        help='Bind address; 0.0.0.0 exposes file paths on the LAN (no auth)')
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args()
    if not os.path.exists(args.db):
        raise SystemExit(f"Catalog not found: {args.db} (start photo-organizer first)")

    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.db, args.port))
    url = f"http://{'localhost' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"
    print(f"Status page: {url}  (Ctrl-C to stop)")
    if not args.no_open:
        Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
