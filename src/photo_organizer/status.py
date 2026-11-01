"""Live status page for an organizer run: throughput, ETA, per-source progress, errors.

Reads the catalog read-only, so it can run alongside (or after) `photo-organizer`.
"""

import argparse
import datetime
import json
import os
import sqlite3
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Timer

from photo_organizer.db import DEFAULT_DB_PATH

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
        return {
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
main { max-width:1100px; margin:0 auto; padding:20px 16px 40px; }
header { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; margin-bottom:16px; }
h1 { font-size:20px; margin:0; }
.pill { padding:2px 10px; border-radius:999px; font-size:12px; font-weight:600; border:1px solid var(--line); }
.pill.live { color:var(--accent); border-color:var(--accent); }
.pill.stale { color:var(--warn); border-color:var(--warn); }
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
.path .to { color:var(--accent); margin-top:2px; }
.path .to.st-review, .path .to.st-ignored { color:var(--warn); } .path .to.st-error { color:var(--bad); }
.st-error { color:var(--bad); } .st-review, .st-ignored { color:var(--warn); } .st-done, .st-duplicate_removed { color:var(--accent); }
.progress { height:8px; background:var(--line); border-radius:4px; overflow:hidden; margin-top:8px; }
.progress > div { height:100%; background:var(--bar); }
svg { width:100%; height:90px; display:block; }
.two { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:12px; }
@media (max-width:760px) { .two { grid-template-columns:1fr; } }
</style></head><body><main>
<header><h1>Photo Organizer</h1><span id="pill" class="pill">loading</span><span id="phase" class="muted"></span></header>
<div class="grid">
  <div class="card"><div class="k">Images / s</div><div class="v" id="fps">–</div><div class="s" id="fps2"></div></div>
  <div class="card"><div class="k">Throughput</div><div class="v" id="mbps">–</div><div class="s" id="mbps2"></div></div>
  <div class="card"><div class="k">ETA (scanned so far)</div><div class="v" id="eta">–</div><div class="s" id="pend"></div></div>
  <div class="card"><div class="k">Batch</div><div class="v" id="batch">–</div><div class="s" id="batchs"></div>
    <div class="progress"><div id="bar" style="width:0"></div></div></div>
</div>
<div class="card" style="margin-bottom:12px"><h2>Moved per minute (last hour)</h2><svg id="chart" preserveAspectRatio="none"></svg></div>
<div class="two">
  <div class="card"><h2>Catalog</h2><table id="statuses"></table></div>
  <div class="card"><h2>Sources</h2><table id="sources"></table></div>
</div>
<div class="card" style="margin-bottom:12px"><h2>Recent activity</h2><table id="recent"></table></div>
<div class="card"><h2>Errors</h2><table id="errors"></table></div>
<p class="s" id="meta"></p>
</main>
<script>
const LABEL = {done:'moved', duplicate_removed:'duplicate removed', pending:'pending', review:'left: no date',
               ignored:'left: unhandled type', error:'left: error', copying:'copying'};
const $ = id => document.getElementById(id);
const num = n => (n ?? 0).toLocaleString();
function bytes(b) { const u=['B','KB','MB','GB','TB']; let i=0; b=b||0; while (b>=1000 && i<u.length-1) { b/=1000; i++; }
  return b.toFixed(b<10&&i?1:0)+' '+u[i]; }
function dur(s) { if (s==null) return '–'; s=Math.round(s); const d=Math.floor(s/86400), h=Math.floor(s%86400/3600), m=Math.floor(s%3600/60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : m ? `${m}m ${s%60}s` : `${s}s`; }
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
  $('phase').textContent = r.alive ? `${r.phase || ''}${r.current_dir ? ' · ' + r.current_dir : ''}` : '';

  const a = d.rates['1m'], b = d.rates['10m'], ra = d.rates.run;
  const main = r.alive ? a : ra;
  document.querySelector('#fps').previousElementSibling.textContent = r.alive ? 'Images / s' : 'Images / s (run avg)';
  $('fps').textContent = main.files_per_s.toFixed(1);
  $('fps2').textContent = r.alive
    ? `10-min ${b.files_per_s.toFixed(1)} · run ${ra.files_per_s.toFixed(1)} · scanning ${d.rates.scan_files_per_s.toFixed(1)}/s`
    : `${num(ra.files)} files moved in last run`;
  $('mbps').textContent = bytes(main.bytes_per_s) + '/s';
  $('mbps2').textContent = r.alive ? `10-min ${bytes(b.bytes_per_s)}/s · run ${bytes(ra.bytes_per_s)}/s` : 'last run average';
  $('eta').textContent = r.alive ? dur(d.eta_seconds) : (r.finished ? 'done' : '–');
  $('pend').textContent = `${num(d.pending.files)} pending · ${bytes(d.pending.bytes)}`;
  $('batch').textContent = r.batch ?? '–';
  const bt = r.batch_total || 0, bd = r.batch_done || 0, moving = r.alive && r.phase === 'transferring';
  $('batchs').textContent = !r.alive ? (r.finished_at ? `ended ${r.finished_at.slice(11, 19)}Z` : '')
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
    tip.textContent = `${p.minute}Z · ${p.files} files · ${bytes(p.bytes)}`; rect.appendChild(tip);
    svg.appendChild(rect);
  });

  const st = $('statuses'); head(st, [['Status'], ['Files','n'], ['Size','n']]);
  d.statuses.forEach(s => { const tr=row(st); cell(tr, LABEL[s.status]||s.status, 'st-'+s.status); cell(tr, num(s.files), 'n'); cell(tr, bytes(s.bytes), 'n'); });

  const so = $('sources'); head(so, [['Source'], ['Moved','n'], ['Pending','n'], ['Left','n']]);
  Object.entries(d.sources).forEach(([src, c]) => { const tr=row(so);
    cell(tr, src, 'path'); cell(tr, num((c.done||0)+(c.duplicate_removed||0)), 'n'); cell(tr, num(c.pending||0), 'n');
    cell(tr, num((c.review||0)+(c.ignored||0)+(c.error||0)), 'n'); });

  const re = $('recent'); head(re, [['When'], ['Status'], ['From → To']]);
  d.recent.forEach(x => { const tr=row(re); cell(tr, (x.updated_at||'').slice(11,19)+'Z', 's');
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

  $('meta').textContent = r.started_at ? `Run started ${r.started_at} · elapsed ${dur(r.elapsed)} · heartbeat ${dur(r.heartbeat_age)} ago · dest ${r.dest||''}` : '';
}

async function tick() {
  try { render(await (await fetch('/api/status')).json()); }
  catch (e) { $('pill').textContent = 'status server unreachable'; $('pill').className = 'pill stale'; }
}
tick(); setInterval(tick, 2000);
</script></body></html>
"""


def make_handler(db_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/':
                body, ctype = PAGE.encode(), 'text/html; charset=utf-8'
            elif self.path == '/api/status':
                try:
                    body = json.dumps(snapshot(db_path)).encode()
                except sqlite3.Error as e:
                    self.send_error(503, f"catalog unavailable: {e}")
                    return
                ctype = 'application/json'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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

    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.db))
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
