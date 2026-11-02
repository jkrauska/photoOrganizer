"""Full-screen random walk through the photoArk store, with a 1–5 score per photo.

Run:   make ark-shuffle-ui   (or: uv run photo-shuffle)
Then:  http://127.0.0.1:8080/

A random day folder, then a random stored photo in it (catalog files with status 'done', no
videos), fades in with a slow pan/zoom. Space plays/pauses, ← → step back through the history and
forward, 1–5 score the photo on screen and move on to the next one, 0 clears its score, + / -
change the pace, F goes full screen. One-star photos are never shown again. The bottom overlay
shows when the photo was taken and Gemma's caption from analysis.db when there is one.

Scores are kept in analysis.db, table photo_scores (one row per sha256; a new score replaces the
old one), e.g. the photos to clean out:
    SELECT path FROM photo_scores WHERE score <= 1;
It binds 127.0.0.1 unless --host says otherwise (--host 0.0.0.0 serves the LAN, with no login).
"""

import argparse
import io
import json
import os
import random
import socket
import sqlite3
import time
import webbrowser
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Timer

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from photo_organizer.analysis.ark import DEFAULT_ANALYSIS_DB, load_image, open_db
from photo_organizer.constants import PHOTO_EXTENSIONS

PHOTO_SIDE = 2048
DEFAULT_CATALOG = '~/.photo-organizer/catalog.db'
DEFAULT_CACHE = '~/.photo-organizer/shuffle-cache'
DAYS_REFRESH = 600              # seconds between rescans of the catalog's day folders
HIDE_SCORE = 1                  # photos scored this or lower aren't shown
RECENT_DAYS = 300               # don't revisit one of the last this many days shown

SCORES_SCHEMA = """
CREATE TABLE IF NOT EXISTS photo_scores (
    sha256 TEXT PRIMARY KEY,
    score INTEGER NOT NULL,         -- 1 (clean out) .. 5 (best)
    path TEXT,                      -- dest_path in the store when scored
    scored_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photo_scores_score ON photo_scores(score);
"""


def connect_ro(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_scores(conn):
    conn.executescript(SCORES_SCHEMA)


def set_score(conn, sha, score, path=None):
    """score 1..5 replaces the photo's score; None clears it."""
    with conn:
        if score is None:
            conn.execute("DELETE FROM photo_scores WHERE sha256 = ?", (sha,))
        else:
            conn.execute("INSERT INTO photo_scores (sha256, score, path, scored_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(sha256) DO UPDATE SET score = excluded.score, path = excluded.path, "
                         "scored_at = excluded.scored_at",
                         (sha, score, path, datetime.now(timezone.utc).isoformat(timespec='seconds')))


def _is_photo(path):
    return bool(path) and os.path.splitext(path)[1].lower() in PHOTO_EXTENSIONS


def photo_days(catalog):
    """Sorted day folders that hold at least one stored photo."""
    return sorted({os.path.dirname(path) for (path,) in catalog.execute(
        "SELECT dest_path FROM files WHERE status = 'done' AND sha256 IS NOT NULL") if _is_photo(path)})


def random_photo(catalog, days, rng=random, skip=frozenset()):
    """(sha256, dest_path, taken_at) of a random stored photo in a random one of the day folders,
    or None. Picking the day first means a day of a thousand burst shots comes up no more often
    than a day with a single photo. Photos whose sha256 is in skip are never picked."""
    day = rng.choice(days)
    rows = [r for r in catalog.execute(
        "SELECT sha256, dest_path, taken_at FROM files WHERE dest_path >= ? AND dest_path < ? "
        "AND status = 'done' AND sha256 IS NOT NULL", (day + '/', day + '0'))   # '0' sorts right after '/'
        if os.path.dirname(r['dest_path']) == day and _is_photo(r['dest_path']) and r['sha256'] not in skip]
    if not rows:
        return None
    r = rng.choice(rows)
    return r['sha256'], r['dest_path'], r['taken_at']


def describe(analysis, sha):
    """Gemma's caption/setting/keywords for the photo and its score, from analysis.db."""
    r = analysis.execute("SELECT caption, setting, keywords FROM images WHERE sha256 = ?", (sha,)).fetchone()
    s = analysis.execute("SELECT score FROM photo_scores WHERE sha256 = ?", (sha,)).fetchone()
    try:
        keywords = json.loads(r['keywords'] or '[]') if r else []
    except ValueError:
        keywords = []
    return {'caption': r['caption'] if r else None, 'setting': r['setting'] if r else None,
            'keywords': keywords if isinstance(keywords, list) else [], 'score': s['score'] if s else None}


def _jpeg(path, side):
    im, _ = load_image(path)
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=86)
    return buf.getvalue()


class ScoreIn(BaseModel):
    sha: str
    score: int | None = Field(None, ge=1, le=5)


def make_app(catalog_path, analysis_path, cache_dir, rng=random):
    with closing(open_db(analysis_path)) as rw:
        init_scores(rw)
    cache_dir = Path(cache_dir)
    app = FastAPI()
    # The day list is a scan of the whole catalog, so it's kept and refreshed now and then (the
    # transfer job keeps adding days). Recently shown days are skipped so days don't repeat soon.
    days = {'list': [], 'at': 0.0}
    recent = deque(maxlen=RECENT_DAYS)
    lock = Lock()

    def day_list(catalog):
        with lock:
            if not days['list'] or time.monotonic() - days['at'] > DAYS_REFRESH:
                days['list'], days['at'] = photo_days(catalog), time.monotonic()
                recent.clear()
            return days['list']

    def stored_path(catalog, sha):
        for r in catalog.execute("SELECT dest_path FROM files WHERE sha256 = ? AND status = 'done'", (sha,)):
            if _is_photo(r['dest_path']) and os.path.exists(r['dest_path']):
                return r['dest_path']
        raise HTTPException(404)

    @app.get("/api/random")
    def api_random():
        with closing(connect_ro(catalog_path)) as catalog, closing(connect_ro(analysis_path)) as analysis:
            folders = day_list(catalog)
            if not folders:
                raise HTTPException(404, "no stored photos in the catalog")
            fresh = len(folders) > 2 * len(recent)
            hidden = {r[0] for r in analysis.execute(       # one star: never shown again
                "SELECT sha256 FROM photo_scores WHERE score <= ?", (HIDE_SCORE,))}
            for _ in range(20):         # skip catalog rows whose file isn't there (store offline, moved)
                hit = random_photo(catalog, folders, rng, hidden)
                if hit is None:
                    continue
                sha, path, taken_at = hit
                day = os.path.dirname(path)
                with lock:
                    if fresh and day in recent:
                        continue
                    recent.append(day)
                if os.path.exists(path):
                    return {'sha': sha, 'path': path, 'taken_at': taken_at, **describe(analysis, sha)}
        raise HTTPException(503, "photos in the catalog are missing on disk (is the store mounted?)")

    @app.get("/api/photo/{sha}")
    def api_photo(sha: str):
        with closing(connect_ro(catalog_path)) as catalog, closing(connect_ro(analysis_path)) as analysis:
            r = catalog.execute("SELECT dest_path, taken_at FROM files WHERE sha256 = ? AND status = 'done'",
                                (sha,)).fetchone()
            if not r:
                raise HTTPException(404)
            return {'sha': sha, 'path': r['dest_path'], 'taken_at': r['taken_at'], **describe(analysis, sha)}

    @app.get("/photo/{sha}.jpg")
    def photo(sha: str):
        if not sha.isalnum():
            raise HTTPException(404)
        cached = cache_dir / sha[:2] / f"{sha}.jpg"
        if not cached.exists():
            with closing(connect_ro(catalog_path)) as catalog:
                path = stored_path(catalog, sha)
            try:
                data = _jpeg(path, PHOTO_SIDE)
            except Exception as e:
                raise HTTPException(415, f"can't decode {path}: {e}")
            cached.parent.mkdir(parents=True, exist_ok=True)
            tmp = cached.with_suffix('.tmp')
            tmp.write_bytes(data)
            tmp.replace(cached)
        return Response(cached.read_bytes(), media_type='image/jpeg',
                        headers={'Cache-Control': 'max-age=86400'})

    @app.post("/api/score")
    def score(body: ScoreIn):
        with closing(connect_ro(catalog_path)) as catalog:
            r = catalog.execute("SELECT dest_path FROM files WHERE sha256 = ? AND status = 'done'",
                                (body.sha,)).fetchone()
        if not r:
            raise HTTPException(404)
        with closing(open_db(analysis_path)) as rw:
            set_score(rw, body.sha, body.score, r['dest_path'])
        return {'ok': True, 'score': body.score}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE

    return app


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>photoArk shuffle</title>
<style>
  html, body { margin: 0; height: 100%; background: #000; overflow: hidden; color: #eee;
               font: 15px -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif; cursor: none; }
  body.cursor { cursor: default; }
  .slide { position: absolute; inset: 0; opacity: 0; transition: opacity 1.6s ease; overflow: hidden; }
  .slide.on { opacity: 1; }
  .slide .bg { position: absolute; inset: -40px; background-size: cover; background-position: center;
               filter: blur(40px) brightness(.45); }
  .slide img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain;
               will-change: transform; }
  #info { position: fixed; left: 0; right: 0; bottom: 0; padding: 60px 28px 22px;
          background: linear-gradient(transparent, rgba(0,0,0,.75)); transition: opacity .6s; }
  #date { font-size: 22px; font-weight: 600; letter-spacing: .01em; text-shadow: 0 1px 4px #000; }
  #cap { margin-top: 6px; max-width: 70em; line-height: 1.4; color: #ddd; text-shadow: 0 1px 3px #000; }
  #meta { margin-top: 6px; font-size: 12px; color: #999; }
  #badge { position: fixed; top: 18px; right: 22px; padding: 4px 11px; border-radius: 14px;
           background: rgba(0,0,0,.55); font-size: 18px; letter-spacing: 2px; display: none; }
  #badge.low { color: #f88; } #badge.high { color: #fd6; }
  #flash { position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%) scale(.9);
           font-size: 90px; font-weight: 700; color: #fff; text-shadow: 0 2px 20px #000; opacity: 0;
           transition: opacity .25s, transform .25s; pointer-events: none; }
  #flash.on { opacity: 1; transform: translate(-50%, -50%) scale(1); }
  #state { position: fixed; top: 18px; left: 22px; padding: 4px 11px; border-radius: 14px;
           background: rgba(0,0,0,.55); font-size: 13px; color: #ccc; opacity: 0; transition: opacity .4s; }
  #state.on { opacity: 1; }
  #help { position: fixed; top: 52px; left: 22px; font-size: 12px; color: #888; line-height: 1.6;
          background: rgba(0,0,0,.55); padding: 8px 12px; border-radius: 8px; display: none; }
  body.cursor #help { display: block; }
  kbd { border: 1px solid #555; border-radius: 3px; padding: 0 4px; color: #ccc; }
</style></head><body>
<div class="slide" id="s0"><div class="bg"></div><img alt=""></div>
<div class="slide" id="s1"><div class="bg"></div><img alt=""></div>
<div id="info"><div id="date"></div><div id="cap"></div><div id="meta"></div></div>
<div id="badge"></div><div id="flash"></div><div id="state"></div>
<div id="help"><kbd>space</kbd> play / pause &nbsp; <kbd>←</kbd> <kbd>→</kbd> back / next<br>
<kbd>1</kbd>–<kbd>5</kbd> score &nbsp; <kbd>0</kbd> clear score &nbsp; <kbd>+</kbd> <kbd>-</kbd> pace &nbsp;
<kbd>F</kbd> full screen &nbsp; <kbd>I</kbd> hide text</div>
<script>
const $ = s => document.querySelector(s);
const FADE = 1600;
let pace = +(localStorage.getItem('shuffle-pace') || 8000);   // ms each photo stays up
let playing = true, timer = null, due = 0, left = 0;
let history = [], at = -1;             // photos shown; at = the one on screen
const ahead = [];                      // prefetched random photos, decoded and ready
let front = 0, anim = null, busy = false, queued = 0;   // queued: arrow presses made while loading
let autoAt = 0, outgoing = null;   // when the timer last changed photo, and the photo it faded out

async function fetchRandom() {
  for (let i = 0; i < 5; i++) {
    const r = await fetch('/api/random');
    if (!r.ok) { await new Promise(ok => setTimeout(ok, 1500)); continue; }
    const p = await r.json();
    if (await preload(p)) return p;
  }
  return null;
}
function preload(p) {
  return new Promise(ok => {
    const im = new Image();
    im.onload = () => { p.w = im.naturalWidth; p.h = im.naturalHeight; ok(true); };
    im.onerror = () => ok(false);
    im.src = `/photo/${p.sha}.jpg`;
  });
}
let filling = false;
async function fill() {
  if (filling) return;
  filling = true;
  try { while (ahead.length < 4) { const p = await fetchRandom(); if (!p) break; ahead.push(p); } }
  finally { filling = false; }
}

function fmtDate(p) {
  const t = p.taken_at || (p.path.match(/(\d{4}-\d{2}-\d{2})/) || [])[1];
  if (!t) return 'Date unknown';
  const d = new Date(t.length == 10 ? t + 'T12:00:00' : t.replace(' ', 'T'));
  if (isNaN(d)) return t;
  let s = d.toLocaleDateString(undefined, {weekday: 'long', year: 'numeric', month: 'long', day: 'numeric'});
  if (t.length > 10) s += ' · ' + d.toLocaleTimeString(undefined, {hour: 'numeric', minute: '2-digit'});
  return s;
}
function kenBurns(img) {
  // Zoom in or out between 1.0 and ~1.18, drifting toward a random corner.
  const z0 = 1 + Math.random() * .05, z1 = 1.1 + Math.random() * .08;
  const [a, b] = Math.random() < .5 ? [z0, z1] : [z1, z0];
  const dx = (Math.random() * 2 - 1) * 3.5, dy = (Math.random() * 2 - 1) * 2.5;
  return img.animate([{transform: `scale(${a}) translate(${-dx}%, ${-dy}%)`},
                      {transform: `scale(${b}) translate(${dx}%, ${dy}%)`}],
                     {duration: pace + FADE * 2, easing: 'linear', fill: 'forwards'});
}
function show(p) {
  const back = $('#s' + (1 - front)), cur = $('#s' + front);
  const img = back.querySelector('img');
  img.getAnimations().forEach(a => a.cancel());
  img.src = `/photo/${p.sha}.jpg`;
  back.querySelector('.bg').style.backgroundImage = `url(/photo/${p.sha}.jpg)`;
  anim = kenBurns(img);
  if (!playing) anim.pause();
  back.classList.add('on'); cur.classList.remove('on');
  front = 1 - front;
  $('#date').textContent = fmtDate(p);
  $('#cap').textContent = p.caption || '';
  const bits = [];
  if (p.setting) bits.push(p.setting);
  if (p.keywords && p.keywords.length) bits.push(p.keywords.slice(0, 8).join(', '));
  bits.push(p.path.split('/').pop());
  $('#meta').textContent = bits.join(' · ');
  badge(p.score);
}
function badge(score) {
  const b = $('#badge');
  if (!score) { b.style.display = 'none'; return; }
  b.textContent = '★'.repeat(score) + '☆'.repeat(5 - score);
  b.className = score <= 2 ? 'low' : score >= 4 ? 'high' : '';
  b.style.display = 'block';
}
let flashT, stateT;
function flash(text) {
  const f = $('#flash'); f.textContent = text; f.classList.add('on');
  clearTimeout(flashT); flashT = setTimeout(() => f.classList.remove('on'), 900);
}
function status(text, stay) {
  const s = $('#state'); s.textContent = text; s.classList.add('on');
  clearTimeout(stateT); if (!stay) stateT = setTimeout(() => s.classList.remove('on'), 1500);
}

function schedule(ms) {
  clearTimeout(timer);
  if (!playing) { left = ms; return; }
  due = Date.now() + ms;
  timer = setTimeout(() => step(1, true), ms);
}
async function step(by, auto) {
  if (busy) { if (!auto) queued += by; return; }
  busy = true;
  const was = history[at];
  try {
    // One-star photos are never shown again, so stepping through the history skips them.
    const ok = i => history[i] && history[i].score !== 1;
    let i = at + by;
    while (history[i] && !ok(i)) i += by;
    if (by < 0) {
      if (i < 0) { status('Start of history'); return; }
      at = i;
    } else if (i < history.length) {
      at = i;
    } else {
      let p = ahead.shift();
      if (!p) { status('Loading…', true); p = await fetchRandom(); }
      if (!p) { status('No photos — is the store mounted?', true); schedule(5000); return; }
      history.push(p);
      if (history.length > 300) history.shift();
      at = history.length - 1;
      fill();
    }
    $('#state').classList.remove('on');
    autoAt = auto ? Date.now() : 0; outgoing = was;
    show(history[at]);
    schedule(pace + FADE);
  } finally {
    busy = false;
    if (queued) { const q = Math.sign(queued); queued -= q; step(q); }
  }
}
function toggle() {
  playing = !playing;
  if (playing) { anim && anim.play(); schedule(left || pace); status('▶ Playing'); }
  else { left = Math.max(500, due - Date.now()); clearTimeout(timer); anim && anim.pause(); status('❚❚ Paused', true); }
}
async function score(n) {
  // A key pressed while the timer is still fading a photo out is meant for that photo.
  const p = autoAt && Date.now() - autoAt < FADE * .7 && outgoing ? outgoing : history[at];
  if (!p) return;
  const r = await fetch('/api/score', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                       body: JSON.stringify({sha: p.sha, score: n || null})});
  if (!r.ok) { status('Could not save the score'); return; }
  p.score = n || null;
  flash(n ? '★'.repeat(n) : 'Score cleared');
  if (history[at] !== p) return;      // scored the photo the timer was already fading out
  badge(p.score);
  if (n) step(1);                     // scored: on to the next photo
}

document.addEventListener('keydown', e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key == ' ') { e.preventDefault(); toggle(); }
  else if (e.key == 'ArrowRight') { e.preventDefault(); step(1); }
  else if (e.key == 'ArrowLeft') { e.preventDefault(); step(-1); }
  else if (/^[0-5]$/.test(e.key)) score(+e.key);
  else if (e.key == '+' || e.key == '=' || e.key == '-') {
    pace = Math.min(60000, Math.max(3000, pace + (e.key == '-' ? -2000 : 2000)));
    try { localStorage.setItem('shuffle-pace', pace); } catch (_) {}
    status(`${pace / 1000} s per photo`);
  }
  else if (e.key == 'f' || e.key == 'F') {
    if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen();
  }
  else if (e.key == 'i' || e.key == 'I') $('#info').style.opacity = $('#info').style.opacity == '0' ? '' : '0';
});
let idle;
document.addEventListener('mousemove', () => {
  document.body.classList.add('cursor');
  clearTimeout(idle); idle = setTimeout(() => document.body.classList.remove('cursor'), 2500);
});
document.addEventListener('click', e => { if (e.target.closest('#help')) return; e.clientX < innerWidth / 3 ? step(-1) : step(1); });

(async () => { status('Loading…', true); await step(1); fill(); })();
</script></body></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--catalog', default=DEFAULT_CATALOG, help='organizer catalog (read only)')
    parser.add_argument('--db', default=DEFAULT_ANALYSIS_DB, help='analysis DB (captions; scores go here)')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--host', default='127.0.0.1', help='0.0.0.0 to serve the LAN (no login)')
    parser.add_argument('--cache', default=DEFAULT_CACHE, help='resized photo cache')
    parser.add_argument('--no-open', action='store_true', help="don't open a browser")
    args = parser.parse_args()
    catalog = os.path.expanduser(args.catalog)
    db_path = os.path.expanduser(args.db)
    if not os.path.exists(catalog):
        raise SystemExit(f"{catalog} not found.")
    with closing(connect_ro(catalog)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM files WHERE status = 'done'").fetchone()[0]
    url = f"http://127.0.0.1:{args.port}/"
    print(f"{n:,} stored files in {catalog} → {url}")
    if args.host != '127.0.0.1':
        print(f"(also on the LAN at http://{socket.gethostname()}:{args.port}/, no login)")
    if not args.no_open:
        Timer(0.6, lambda: webbrowser.open(url)).start()
    uvicorn.run(make_app(catalog, db_path, Path(args.cache).expanduser()), host=args.host, port=args.port,
                log_level="warning")


if __name__ == '__main__':
    main()
