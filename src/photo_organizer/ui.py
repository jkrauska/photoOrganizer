"""Every photoArk page on one server, with a nav bar across the top:

  /          status of the organizer run (queue changes only from this Mac)
  /archive   what's in the store by year, month, day and camera
  /days/     day summaries
  /faces/    name face clusters
  /finder/   photos with all of up to four named people in them
  /shuffle/  random walk through the store, with 1-5 scores

  photo-ui [--host 0.0.0.0] [--port 8077]
"""

import argparse
import os
import re
import socket
import webbrowser
from pathlib import Path
from threading import Timer

import uvicorn
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from photo_organizer import status
from photo_organizer.analysis import day_browser, face_browser, face_finder, shuffle
from photo_organizer.analysis.ark import DEFAULT_ANALYSIS_DB
from photo_organizer.db import DEFAULT_DB_PATH

NAV = [('/', 'status', 'Status'), ('/archive', 'archive', 'Archive'), ('/days/', 'days', 'Days'),
       ('/faces/', 'faces', 'Faces'), ('/finder/', 'finder', 'Face Finder'), ('/shuffle/', 'shuffle', 'Shuffle')]
NAV_STYLE = """<style>
#ark-nav { position:fixed; top:0; left:0; right:0; height:36px; z-index:1000; display:flex; align-items:center;
  gap:2px; padding:0 12px; background:#1d1d1f; font:500 13px -apple-system, system-ui, sans-serif;
  box-shadow:0 1px 0 rgba(255,255,255,.08); overflow-x:auto; }
#ark-nav b { color:#fff; margin-right:10px; font-weight:650; white-space:nowrap; }
#ark-nav a { color:#b8b8be; text-decoration:none; padding:5px 10px; border-radius:6px; white-space:nowrap; }
#ark-nav a:hover { background:#333; color:#fff; }
#ark-nav a.on { background:#2f6fde; color:#fff; }
body { padding-top:36px; box-sizing:border-box; }
header { top:36px; }
</style>"""
# The shuffle page is full screen: the bar floats over it and shows with the cursor, like its help.
OVERLAY_STYLE = """<style>
body { padding-top:0; }
#ark-nav { background:rgba(0,0,0,.45); box-shadow:none; opacity:0; transition:opacity .6s; }
body.cursor #ark-nav { opacity:1; }
</style>"""


def with_nav(html, active, overlay=False):
    """The page with the shared nav bar (its own Status/Archive links give way to it)."""
    links = ''.join(f'<a href="{href}"{" class=on" if key == active else ""}>{label}</a>' for href, key, label in NAV)
    # stopPropagation: the shuffle page steps to another photo on any click.
    bar = f'<nav id="ark-nav" onclick="event.stopPropagation()"><b>photoArk</b>{links}</nav>'
    if active in ('status', 'archive'):
        html = re.sub(r'<nav>.*?</nav>', '', html, count=1, flags=re.S)
    html = html.replace('</head>', NAV_STYLE + (OVERLAY_STYLE if overlay else '') + '</head>', 1)
    return re.sub(r'(<body[^>]*>)', lambda m: m.group(1) + bar, html, count=1)


def make_app(catalog, analysis_db, port, crops, thumbs, cache):
    app = FastAPI()
    pages = {
        'days': (day_browser.make_app(analysis_db, thumbs), day_browser.PAGE, False),
        'faces': (face_browser.make_ark_app(analysis_db, crops, catalog), face_browser.ARK_HTML, False),
        'finder': (face_finder.make_app(analysis_db, thumbs), face_finder.PAGE, False),
        'shuffle': (shuffle.make_app(catalog, analysis_db, cache), shuffle.PAGE, True),
    }
    for key, (sub, page, overlay) in pages.items():
        # Each page's URLs are relative, so it needs the trailing slash to find its API.
        app.add_api_route(f'/{key}', lambda key=key: RedirectResponse(f'/{key}/'), include_in_schema=False)
        html = with_nav(page, key, overlay)
        app.add_api_route(f'/{key}/', lambda html=html: HTMLResponse(html), include_in_schema=False)
        app.mount(f'/{key}', sub)

    hosts = status.local_hosts(port)
    status_pages = {'/': with_nav(status.PAGE, 'status'), '/archive': with_nav(status.ARCHIVE_PAGE, 'archive')}

    @app.api_route('/{rest:path}', methods=['GET', 'POST', 'DELETE'], include_in_schema=False)
    async def status_routes(request: Request, rest: str):
        if request.method == 'GET' and request.url.path in status_pages:
            return HTMLResponse(status_pages[request.url.path], headers={'Cache-Control': 'no-store'})
        raw = request.url.path + (f'?{request.url.query}' if request.url.query else '')
        body = (await request.body())[:65536]
        code, out, ctype = await run_in_threadpool(status.respond, catalog, hosts, request.method, raw,
                                                   request.headers, body)
        out, ctype = status.encode(out, ctype)
        return Response(out, status_code=code, media_type=ctype, headers={'Cache-Control': 'no-store'})

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--catalog', default=DEFAULT_DB_PATH, help='organizer catalog')
    parser.add_argument('--db', default=DEFAULT_ANALYSIS_DB, help='analysis DB (faces, days, scores)')
    parser.add_argument('--port', type=int, default=8077)
    parser.add_argument('--host', default='127.0.0.1',
                        help='0.0.0.0 to serve the LAN (no login; queue changes still only from this Mac)')
    parser.add_argument('--crops', default='~/.photo-organizer/face_crops', help='face crop cache')
    parser.add_argument('--thumbs', default=day_browser.DEFAULT_THUMBS, help='day thumbnail cache')
    parser.add_argument('--cache', default=shuffle.DEFAULT_CACHE, help='shuffle photo cache')
    parser.add_argument('--no-open', action='store_true', help="don't open a browser")
    args = parser.parse_args()
    catalog, db = os.path.expanduser(args.catalog), os.path.expanduser(args.db)
    for path in (catalog, db):
        if not os.path.exists(path):
            raise SystemExit(f"{path} not found.")
    app = make_app(catalog, db, args.port, Path(args.crops).expanduser(), Path(args.thumbs).expanduser(),
                   Path(args.cache).expanduser())
    url = f"http://127.0.0.1:{args.port}/"
    print(f"photoArk pages → {url}")
    if args.host != '127.0.0.1':
        print(f"(also on the LAN at http://{socket.gethostname()}:{args.port}/, no login)")
    if not args.no_open:
        Timer(0.6, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == '__main__':
    main()
