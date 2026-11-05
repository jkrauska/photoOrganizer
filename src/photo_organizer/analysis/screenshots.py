"""Phone and tablet screenshots in the store, and the faces found in them.

A screenshot is a PNG at exactly an iPhone or iPad screen size (either way up) whose file names
no camera. Its faces are lock-screen wallpapers, app avatars, ads and video frames: not people at
that moment, and dated when the screen was captured, not when any photo in it was taken. JPGs at
those sizes are left alone: those turned out to be real photos that lost their camera details.

  photo-ark-screenshots            list them, with how many faces and names each holds
  photo-ark-screenshots --forget   delete those faces (like Forget on the faces page); the
                                   images stay checked for faces, so they don't come back
"""

import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from photo_organizer.analysis import ark_faces
from photo_organizer.analysis.ark import DEFAULT_ANALYSIS_DB
from photo_organizer.db import DEFAULT_DB_PATH

SCREEN_SIZES = {  # portrait pixels: iPhone 5 .. 16 Pro Max, iPads
    (640, 1136), (750, 1334), (1242, 2208), (1080, 1920), (1125, 2436), (828, 1792), (1242, 2688),
    (1170, 2532), (1284, 2778), (1179, 2556), (1290, 2796), (1206, 2622), (1320, 2868),
    (1536, 2048), (1668, 2224), (1668, 2388), (2048, 2732), (1488, 2266), (1640, 2360), (1620, 2160)}
DEFAULT_CROPS = '~/.photo-organizer/face_crops'


def is_screenshot(path, width, height, camera=None):
    return (os.path.splitext(path)[1].lower() == '.png' and not camera and bool(width)
            and ((width, height) in SCREEN_SIZES or (height, width) in SCREEN_SIZES))


def _cameras(catalog_path, shas):
    """{sha256: camera make} for the shas the catalog names a camera for."""
    if not catalog_path or not os.path.exists(catalog_path) or not shas:
        return {}
    cat = sqlite3.connect(f"file:{catalog_path}?mode=ro", uri=True, timeout=30)
    try:
        out, shas = {}, list(shas)
        for i in range(0, len(shas), 900):
            chunk = shas[i:i + 900]
            out.update(cat.execute(f"SELECT sha256, MAX(camera_make) FROM files WHERE sha256 IN "
                                   f"({','.join('?' * len(chunk))}) AND camera_make IS NOT NULL "
                                   f"GROUP BY sha256", chunk).fetchall())
        return out
    finally:
        cat.close()


def find(conn, catalog_path=DEFAULT_DB_PATH):
    """[{sha256, path, faces, named}] for screenshots that have faces."""
    rows = conn.execute(
        "SELECT i.sha256, i.path, i.width, i.height, COUNT(f.id) n, "
        "SUM(f.person_source = 'manual') named FROM images i JOIN faces f ON f.sha256 = i.sha256 "
        "WHERE lower(i.path) LIKE '%.png' GROUP BY i.sha256").fetchall()
    rows = [r for r in rows if is_screenshot(r['path'], r['width'], r['height'])]
    cams = _cameras(catalog_path, [r['sha256'] for r in rows])
    return [{'sha256': r['sha256'], 'path': r['path'], 'faces': r['n'], 'named': r['named'] or 0}
            for r in rows if not cams.get(r['sha256'])]


def forget(conn, shots, crop_dir):
    """Delete every face in these screenshots. Returns faces deleted."""
    shas = [s['sha256'] for s in shots]
    ids = []
    for i in range(0, len(shas), 900):
        chunk = shas[i:i + 900]
        ids += [r[0] for r in conn.execute(f"SELECT id FROM faces WHERE sha256 IN "
                                           f"({','.join('?' * len(chunk))})", chunk)]
    return ark_faces.forget_faces(conn, ids, crop_dir) if ids else 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--db', default=DEFAULT_ANALYSIS_DB)
    p.add_argument('--catalog', default=DEFAULT_DB_PATH)
    p.add_argument('--crops', default=DEFAULT_CROPS)
    p.add_argument('--forget', action='store_true', help='delete the faces found in screenshots')
    a = p.parse_args()
    with closing(ark_faces.connect(os.path.expanduser(a.db))) as conn:
        shots = find(conn, os.path.expanduser(a.catalog))
        print(f"{len(shots)} screenshots with {sum(s['faces'] for s in shots)} faces, "
              f"{sum(s['named'] for s in shots)} of them named by hand")
        for s in shots:
            if s['named']:
                print(f"  {s['path']}  {s['faces']} faces, {s['named']} named")
        if a.forget and shots:
            print(f"forgot {forget(conn, shots, Path(a.crops).expanduser())} faces")


if __name__ == '__main__':
    main()
