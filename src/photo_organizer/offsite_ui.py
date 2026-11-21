"""The Offsite tab: is the S3 Deep Archive copy still there and up to date (photo_organizer.offsite)?

Green: the last verify (a bucket listing) found every tar at its recorded size within the last
offsite.STALE_DAYS days, and no backed-up year or month has gained files since its upload.
Yellow: never verified, verified too long ago, verify couldn't reach S3, or files were added to a
backed-up unit. Red: S3 is missing a tar or has it at the wrong size or storage class.
A unit whose upload is running shows as in progress with how far it got, and doesn't change the light.
"""

import threading
import time

from fastapi import FastAPI

from photo_organizer import offsite

CACHE_SECONDS = 60

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Offsite backup</title>
<style>
:root { --bg:#f6f7f9; --card:#fff; --fg:#1d2330; --muted:#6b7385; --line:#e3e6ec;
        --green:#2f7d62; --yellow:#b26b00; --red:#b3261e; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#12151b; --card:#1b1f27; --fg:#e7eaf0; --muted:#9aa3b5; --line:#2a303b;
  --green:#5cc49b; --yellow:#e0a040; --red:#ef6b62; } }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 -apple-system, system-ui, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:24px 24px 40px; }
.head { display:flex; align-items:center; gap:14px; margin-bottom:6px; }
.light { width:22px; height:22px; border-radius:50%; flex:none; }
.green { background:var(--green); } .yellow { background:var(--yellow); } .red { background:var(--red); }
h1 { font-size:20px; margin:0; } h2 { font-size:13px; margin:22px 0 8px; color:var(--muted);
  text-transform:uppercase; letter-spacing:.04em; }
.muted { color:var(--muted); } ul { margin:6px 0; padding-left:20px; }
button { font:inherit; padding:5px 14px; border-radius:6px; border:1px solid var(--line); background:var(--card);
  color:var(--fg); cursor:pointer; } button:disabled { opacity:.5; cursor:default; }
table { border-collapse:collapse; width:100%; background:var(--card); border:1px solid var(--line); border-radius:10px; }
th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); font-variant-numeric:tabular-nums; }
th { font-size:12px; color:var(--muted); font-weight:600; } td.n { text-align:right; }
.st-ok { color:var(--green); } .st-bad { color:var(--red); font-weight:600; } .st-unverified { color:var(--yellow); }
.st-progress { color:#2f6fde; font-weight:600; }
</style></head><body><main>
<div class="head"><span id="light" class="light"></span><h1>Offsite backup</h1>
  <button id="verify" onclick="verifyNow()">Verify now</button></div>
<div class="muted" id="when"></div>
<ul id="reasons"></ul>
<h2>Storage</h2>
<table><thead><tr><th>Bucket</th><th>Region</th><th class="n">Tars</th><th class="n">Size</th>
  <th>index.tsv class</th></tr></thead><tbody id="buckets"></tbody></table>
<div class="muted" style="margin-top:6px">Region and storage classes are what S3 reported at the last verify.
  Tars are written as DEEP_ARCHIVE (Bulk thaw up to 48 h); manifests and index.tsv as STANDARD.</div>
<h2>Tars</h2>
<table><thead><tr><th>Unit</th><th class="n">Files</th><th class="n">Size</th><th>Uploaded</th><th>Tar class</th>
  <th>Manifest class</th><th>S3</th><th class="n">Added since</th></tr></thead><tbody id="units"></tbody></table>
<h2>Not backed up yet</h2>
<div id="missing" class="muted"></div>
</main><script>
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const gib = n => (n / 2**30).toFixed(2) + ' GiB';
const unknown = v => v ? esc(v) : '<span class="muted">not verified</span>';
function render(s) {
  document.getElementById('buckets').innerHTML = s.buckets.map(b => `<tr><td>${esc(b.bucket)}</td>
    <td>${unknown(b.region)}</td><td class="n">${b.units.toLocaleString()}</td><td class="n">${gib(b.bytes)}</td>
    <td>${unknown(b.index_class)}</td></tr>`).join('')
    || '<tr><td colspan="5" class="muted">No bucket yet: scripts/offsite-setup.sh</td></tr>';
  document.getElementById('light').className = 'light ' + s.light;
  document.getElementById('when').textContent = s.verified_at ? 'Last verified ' + s.verified_at.replace('T', ' ') : 'Never verified';
  document.getElementById('reasons').innerHTML = s.reasons.length ? s.reasons.map(r => `<li>${esc(r)}</li>`).join('')
    : '<li>All tars present in S3 at their recorded size.</li>';
  document.getElementById('units').innerHTML = s.units.map(u => {
    const p = u.progress, fresh = p && !u.uploaded_at;
    const st = p ? `in progress ${Math.min(100, Math.floor(100 * p.bytes_done / (p.bytes || 1)))}%
      (${p.files_done.toLocaleString()}/${p.files.toLocaleString()} files)${fresh ? '' : ', re-uploading'}` : u.status;
    const cls = p ? 'progress' : u.status === 'ok' || u.status === 'unverified' ? u.status : 'bad';
    return `<tr><td>${esc(u.unit)}</td>
    <td class="n">${u.files.toLocaleString()}</td><td class="n">${gib(u.bytes)}</td>
    <td>${fresh ? 'started ' + esc(p.started_at.replace('T', ' ')) : esc(u.uploaded_at.replace('T', ' '))}</td>
    <td>${fresh ? '' : unknown(u.tar_class)}</td><td>${fresh ? '' : unknown(u.manifest_class)}</td>
    <td class="st-${cls}">${esc(st)}</td>
    <td class="n">${u.new_files ? u.new_files.toLocaleString() : ''}</td></tr>`;
  }).join('') || '<tr><td colspan="8" class="muted">Nothing yet: make offsite-upload</td></tr>';
  document.getElementById('missing').innerHTML = s.not_backed_up.map(y =>
    `${esc(y.year)} (${y.files.toLocaleString()} files${y.uploading ? ', upload in progress' : ''}${y.open ? ', year not over' : ''})`
  ).join(', ') || 'Every year is backed up.';
  clearTimeout(window.refresh);
  if (s.units.some(u => u.progress)) window.refresh = setTimeout(load, 60000);
}
async function load() { render(await (await fetch('api/summary')).json()); }
async function verifyNow() {
  const b = document.getElementById('verify');
  b.disabled = true; b.textContent = 'Verifying…';
  try {
    const r = await fetch('api/verify', {method: 'POST', headers: {'Content-Type': 'application/json'}});
    render(await r.json());
  } finally { b.disabled = false; b.textContent = 'Verify now'; }
}
load();
</script></body></html>
"""


def make_app(catalog, state=None):
    state = state or offsite.STATE_DIR
    app = FastAPI()
    lock = threading.Lock()
    cache = {'at': 0.0, 'summary': None}

    def summary(fresh=False):
        with lock:
            if fresh or time.monotonic() - cache['at'] > CACHE_SECONDS:
                cache['summary'], cache['at'] = offsite.summary(catalog, state), time.monotonic()
            return cache['summary']

    @app.get('/api/summary')
    def api_summary():
        return summary()

    @app.get('/api/light')
    def api_light():
        return {'light': summary()['light']}

    @app.post('/api/verify')
    def api_verify():
        offsite.verify(state)
        return summary(fresh=True)

    return app
