"""Growing-up timelapse: one good photo of a person per month, aligned on the eyes, morphed.

For each month, the person's hand-named faces are pre-ranked by detection score and size, then
looked at again one by one with InsightFace (detector + 3D landmarks) for the eye positions and
head pose, until one is decent: looking about at the camera, big enough and sharp enough
(sharpness is measured on the eye-aligned face, so photo resolution doesn't decide it). If none
of the first few is, the best of those is used. Every pick is warped so the eyes land on the
same two points of the frame, and consecutive picks are morphed into each other: the 68 face
landmarks, a few forehead points and the frame edges are triangulated, and each triangle is
warped toward the in-between shape while the colors blend, so the face grows rather than cuts.
ffmpeg writes the MP4.

Run:   uv run --extra analysis photo-ark-timelapse --person "Milo Krauska"
Page:  the Timelapse tab of photo-ui (make_app): each month's original photo, the other faces
       looked at, a click to use one of them instead, and Rebuild to render again.
Out:   ~/.photo-organizer/timelapse/<person>/  (video, picks.json, index.html review sheet,
       candidates.json: the measured candidates, reused on the next run)
"""

import argparse
import html
import json
import os
import re
import subprocess
import time
from contextlib import closing
from pathlib import Path

import cv2
import numpy as np

from photo_organizer.analysis import ark_faces
from photo_organizer.analysis.ark import DEFAULT_ANALYSIS_DB, load_image
from photo_organizer.db import DEFAULT_DB_PATH

OUT_ROOT = '~/.photo-organizer/timelapse'
PER_MONTH = 8                   # candidates tried per month at most
MORE = 8                        # faces looked at per "Look for more"
MAX_YAW, MAX_PITCH = 20, 20     # degrees; a pick must look about at the camera
MIN_EYE_PX = 40                 # eye distance in the photo; smaller faces are too soft to use
MIN_EYES = 0.2                  # eye opening (height / width, from the landmarks); below: shut or squinting
MIN_SHARP = 50                  # Laplacian variance of the aligned face that counts as in focus
SIZE = (1080, 1350)             # output frame (4:5)
EYE_Y, EYE_GAP = 0.42, 0.20     # eye line height and eye distance, as fractions of the frame
_DAY = re.compile(r'(\d{4})-(\d{2})-\d{2}')


def slug(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'person'


def _month(path, taken):
    if taken:
        return taken[:7]
    m = _DAY.search(path)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def candidates(conn, person, catalog_path=DEFAULT_DB_PATH, per_month=PER_MONTH):
    """{month 'YYYY-MM': [face rows]}: the person's hand-named faces, best detections first
    (the first per_month of them; per_month None for all)."""
    rows = conn.execute(
        "SELECT f.id, f.sha256, f.x1, f.y1, f.x2, f.y2, f.det_score, i.path FROM faces f "
        "JOIN images i ON i.sha256 = f.sha256 JOIN persons p ON p.id = f.person_id "
        "WHERE p.name = ? AND f.person_source = 'manual'", (person,)).fetchall()
    taken = ark_faces.taken_times(catalog_path, {r['sha256'] for r in rows})
    by_month = {}
    for r in rows:
        m = _month(r['path'], taken.get(r['sha256']))
        if m:
            by_month.setdefault(m, []).append(dict(r, taken=taken.get(r['sha256']) or ''))
    side = lambda r: min(r['x2'] - r['x1'], 400)
    return {m: sorted(rs, key=lambda r: -(r['det_score'] or 0) * side(r))[:per_month]
            for m, rs in sorted(by_month.items())}


class PoseModel:
    """InsightFace detector + 3D landmarks: eye points (kps) and pose (pitch, yaw, roll)."""

    def __init__(self):
        from insightface.app import FaceAnalysis
        self.app = FaceAnalysis(name="buffalo_l", allowed_modules=['detection', 'landmark_3d_68'],
                                providers=["CoreMLExecutionProvider", "CPUExecutionProvider"])
        self.app.prepare(ctx_id=0, det_size=(640, 640))

    def __call__(self, bgr):
        return self.app.get(bgr)


def _iou(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    return w * h / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - w * h)


def eye_transform(left, right, size=SIZE, eye_y=EYE_Y, eye_gap=EYE_GAP):
    """2x3 similarity transform putting the eyes (image left, image right) on fixed frame points."""
    W, H = size
    dst_l = np.array([W * (0.5 - eye_gap / 2), H * eye_y])
    dst_r = np.array([W * (0.5 + eye_gap / 2), H * eye_y])
    src = np.array([left, right], dtype=np.float64)
    d_src, d_dst = src[1] - src[0], dst_r - dst_l
    scale = np.linalg.norm(d_dst) / max(np.linalg.norm(d_src), 1e-6)
    ang = np.arctan2(d_dst[1], d_dst[0]) - np.arctan2(d_src[1], d_src[0])
    c, s = scale * np.cos(ang), scale * np.sin(ang)
    R = np.array([[c, -s], [s, c]])
    t = dst_l - R @ src[0]
    return np.hstack([R, t[:, None]])


def sharpness(bgr, left, right):
    """Variance of the Laplacian over the face, warped to a fixed 256 px eye-aligned crop."""
    A = eye_transform(left, right, (256, 256), 0.4, 0.36)
    face = cv2.warpAffine(bgr, A, (256, 256), flags=cv2.INTER_AREA)
    gray = cv2.cvtColor(face[40:230, 40:216], cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def measure(model, r):
    """Eye points (full-image coords), pose and sharpness of the stored face r, or None."""
    rgb, _ = load_image(r['path'])
    bgr = np.ascontiguousarray(np.asarray(rgb.convert('RGB'))[:, :, ::-1])
    x1, y1, x2, y2 = r['x1'], r['y1'], r['x2'], r['y2']
    w, h = x2 - x1, y2 - y1
    # Detect in a crop around the stored face: faster, and the face fills the detector's view.
    cx1, cy1 = int(max(0, x1 - w)), int(max(0, y1 - h))
    cx2, cy2 = int(min(bgr.shape[1], x2 + w)), int(min(bgr.shape[0], y2 + h))
    crop = bgr[cy1:cy2, cx1:cx2]
    found = model(crop)
    box = (x1 - cx1, y1 - cy1, x2 - cx1, y2 - cy1)
    best = max(found, key=lambda f: _iou(f.bbox, box), default=None)
    if best is None or _iou(best.bbox, box) < 0.3:
        return None
    kps = best.kps + [cx1, cy1]
    left, right = kps[0].tolist(), kps[1].tolist()
    pitch, yaw, roll = (float(v) for v in best.pose) if getattr(best, 'pose', None) is not None else (0, 0, 0)
    lm = getattr(best, 'landmark_3d_68', None)
    return {'left': left, 'right': right, 'pitch': pitch, 'yaw': yaw, 'roll': roll,
            'lm': (lm[:, :2] + [cx1, cy1]).round(1).tolist() if lm is not None else None,
            'eye_px': float(np.hypot(right[0] - left[0], right[1] - left[1])),
            'sharp': sharpness(bgr, left, right), 'size': [bgr.shape[1], bgr.shape[0]]}


def eye_opening(lm):
    """Mean eye aspect ratio over both eyes (68-point landmarks): about 0.3 open, under 0.15 shut."""
    P = np.asarray(lm)

    def ear(e):
        return (np.linalg.norm(e[1] - e[5]) + np.linalg.norm(e[2] - e[4])) / (2 * np.linalg.norm(e[0] - e[3]) + 1e-6)
    return float((ear(P[36:42]) + ear(P[42:48])) / 2)


def eyes(m):
    """The face's eye opening, or None when it was measured before landmarks were kept."""
    return eye_opening(m['lm']) if m and m.get('lm') else None


def decent(m):
    e = eyes(m)
    return bool(m) and abs(m['yaw']) <= MAX_YAW and abs(m['pitch']) <= MAX_PITCH \
        and m['eye_px'] >= MIN_EYE_PX and m['sharp'] >= MIN_SHARP and (e is None or e >= MIN_EYES)


def choose(cands):
    """Fallback pick among measured candidates: frontal and big enough first, then the sharpest."""
    ok = [c for c in cands if c.get('m')]
    if not ok:
        return None
    frontal = lambda c: abs(c['m']['yaw']) <= MAX_YAW and abs(c['m']['pitch']) <= MAX_PITCH \
        and (eyes(c['m']) or 1) >= MIN_EYES
    big = lambda c: c['m']['eye_px'] >= MIN_EYE_PX
    return max(ok, key=lambda c: (frontal(c) and big(c), big(c),
                                  c['m']['sharp'] - 3 * abs(c['m']['yaw']) - 2 * abs(c['m']['pitch'])))


def _backdrop(bgr, A, size):
    """The aligned photo mirrored past its edges, heavily blurred and a bit darker: fills what the
    photo doesn't reach (a close-up's edges) with its own colors instead of black."""
    W, H = size
    small = cv2.warpAffine(bgr, A / [[8], [8]], (W // 8, H // 8), flags=cv2.INTER_AREA,
                           borderMode=cv2.BORDER_REFLECT_101)
    small = cv2.GaussianBlur(small, (0, 0), 9)
    return (cv2.resize(small, size, interpolation=cv2.INTER_LINEAR) * 0.75).astype(np.uint8)


def aligned(c, size=SIZE):
    rgb, _ = load_image(c['path'])
    bgr = np.ascontiguousarray(np.asarray(rgb.convert('RGB'))[:, :, ::-1])
    A = eye_transform(c['m']['left'], c['m']['right'], size)
    img = cv2.warpAffine(bgr, A, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    inside = cv2.warpAffine(np.ones(bgr.shape[:2], np.float32), A, size, flags=cv2.INTER_LINEAR)
    if inside.min() >= 0.999:
        return img
    inside = cv2.GaussianBlur(inside, (0, 0), 6)[:, :, None]       # a soft seam, not a hard edge
    return (img * inside + _backdrop(bgr, A, size) * (1 - inside)).astype(np.uint8)


def morph_points(c, size=SIZE):
    """The pick's landmarks in frame coordinates, plus forehead and frame-edge points, or None."""
    lm = c['m'].get('lm')
    if not lm:
        return None
    A = eye_transform(c['m']['left'], c['m']['right'], size)
    P = np.asarray(lm) @ A[:, :2].T + A[:, 2]
    W, H = size
    brows, chin = P[17:27], P[8]
    up = brows - (chin - P[27]) * 0.55                 # above the brows: the forehead and hairline
    edge = np.array([[0, 0], [W / 2, 0], [W - 1, 0], [W - 1, H / 2], [W - 1, H - 1], [W / 2, H - 1],
                     [0, H - 1], [0, H / 2]])
    return np.clip(np.vstack([P, up, edge]), 0, [W - 1, H - 1]).astype(np.float32)


def _triangles(points, size):
    """Delaunay triangles over the points, as index triples."""
    W, H = size
    sub = cv2.Subdiv2D((0, 0, W, H))
    for x, y in points:
        sub.insert((float(x), float(y)))
    tris = []
    for t in sub.getTriangleList():
        idx = [int(np.argmin(((points - t[k:k + 2]) ** 2).sum(axis=1))) for k in (0, 2, 4)]
        if len(set(idx)) == 3:
            tris.append(idx)
    return tris


def _warp_into(src, out, s_tri, d_tri):
    """Warp triangle s_tri of src onto triangle d_tri of out (float images, in place)."""
    x, y, w, h = cv2.boundingRect(np.float32([d_tri]))
    sx, sy, sw, sh = cv2.boundingRect(np.float32([s_tri]))
    patch = src[sy:sy + sh, sx:sx + sw]
    if w <= 0 or h <= 0 or patch.size == 0:
        return
    M = cv2.getAffineTransform(np.float32([(p[0] - sx, p[1] - sy) for p in s_tri]),
                               np.float32([(p[0] - x, p[1] - y) for p in d_tri]))
    warped = cv2.warpAffine(patch, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)
    mask = np.zeros((h, w, 1), np.float32)
    cv2.fillConvexPoly(mask, np.int32([(p[0] - x, p[1] - y) for p in d_tri]), 1.0, cv2.LINE_AA)
    roi = out[y:y + h, x:x + w]
    hh, ww = roi.shape[:2]
    roi[:] = roi * (1 - mask[:hh, :ww]) + warped[:hh, :ww] * mask[:hh, :ww]


def morph(img1, img2, p1, p2, tris, t):
    """The frame t of the way from img1 (morph points p1) to img2 (p2)."""
    P = (1 - t) * p1 + t * p2
    a, b = img1.astype(np.float32), img2.astype(np.float32)
    w1, w2 = a.copy(), b.copy()
    for i, j, k in tris:
        tri = [P[i], P[j], P[k]]
        _warp_into(a, w1, [p1[i], p1[j], p1[k]], tri)
        _warp_into(b, w2, [p2[i], p2[j], p2[k]], tri)
    return np.clip((1 - t) * w1 + t * w2, 0, 255).astype(np.uint8)


def _label(img, text):
    H = img.shape[0]
    for color, thick in (((0, 0, 0), 6), ((255, 255, 255), 2)):
        cv2.putText(img, text, (40, H - 50), cv2.FONT_HERSHEY_SIMPLEX, 1.6, color, thick, cv2.LINE_AA)
    return img


def _ease(t):
    return t * t * (3 - 2 * t)                      # smoothstep: eases into and out of each morph


def render(frames, out_path, fps=30, hold=4, steps=16, log=print):
    """MP4 of the frames [(label, image, morph points or None)]: each held `hold` frames, then
    morphed into the next over `steps` frames (a plain crossfade when either has no landmarks)."""
    W, H = frames[0][1].shape[1], frames[0][1].shape[0]
    ff = subprocess.Popen(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
                           '-s', f'{W}x{H}', '-r', str(fps), '-i', '-', '-c:v', 'libx264',
                           '-pix_fmt', 'yuv420p', '-crf', '20', str(out_path)], stdin=subprocess.PIPE)
    start = time.time()
    for i, (label, img, pts) in enumerate(frames):
        for _ in range(hold):
            ff.stdin.write(_label(img.copy(), label).tobytes())
        if i + 1 < len(frames):
            nlabel, nxt, npts = frames[i + 1]
            tris = _triangles((pts + npts) / 2, (W, H)) if pts is not None and npts is not None else None
            for k in range(1, steps + 1):
                t = _ease(k / (steps + 1))
                f = morph(img, nxt, pts, npts, tris, t) if tris else cv2.addWeighted(img, 1 - t, nxt, t, 0)
                ff.stdin.write(_label(f, label if t < 0.5 else nlabel).tobytes())
        if (i + 1) % 20 == 0:
            log(f"  rendered {i + 1}/{len(frames)} months  ({(time.time() - start) / (i + 1):.1f}s each)")
    ff.stdin.close()
    if ff.wait():
        raise RuntimeError("ffmpeg failed")


def review_sheet(out_dir, person, months, picks):
    """index.html: each month's pick and the other candidates, with their measurements."""
    def card(c, chosen):
        m = c.get('m')
        info = (f"yaw {m['yaw']:.0f}° pitch {m['pitch']:.0f}° · sharp {m['sharp']:.0f} · eyes {m['eye_px']:.0f}px"
                if m else 'no usable detection')
        img = f'<img src="thumbs/{c["id"]}.jpg" loading="lazy">' if m else '<div class="none"></div>'
        return (f'<figure class="{"pick" if chosen else ""}" title="{html.escape(c["path"])}">{img}'
                f'<figcaption>{html.escape(os.path.basename(c["path"]))}<br>{info}</figcaption></figure>')
    rows = []
    for month, cands in months.items():
        pick = picks.get(month)
        rows.append(f'<section><h3>{month}</h3><div class="row">' +
                    ''.join(card(c, pick == c['id']) for c in cands) + '</div></section>')
    page = f"""<!doctype html><meta charset="utf-8"><title>{html.escape(person)} timelapse</title>
<style>body{{font:14px -apple-system,sans-serif;margin:20px;background:#111;color:#ddd}}
.row{{display:flex;gap:10px;flex-wrap:wrap}} figure{{margin:0;width:150px}} img,.none{{width:150px;height:150px;
object-fit:cover;border-radius:6px;background:#333}} figure.pick img{{outline:4px solid #2a7}}
figcaption{{font-size:11px;color:#999}} h3{{margin:18px 0 6px}} video{{max-height:70vh}}</style>
<h1>{html.escape(person)}: {len(picks)} months</h1><video src="{slug(person)}.mp4" controls loop></video>
{''.join(rows)}"""
    (out_dir / 'index.html').write_text(page)


def build(db_path, person, out_root=OUT_ROOT, catalog_path=DEFAULT_DB_PATH, per_month=PER_MONTH, log=print):
    out_dir = Path(out_root).expanduser() / slug(person)
    (out_dir / 'thumbs').mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / 'candidates.json'
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    with closing(ark_faces.connect(db_path)) as conn:
        months = candidates(conn, person, catalog_path, None)
    log(f"{person}: {len(months)} months; trying up to {per_month} faces a month until one is decent")
    model = None
    start, n = time.time(), 0
    for month, cands in months.items():
        for c in cands[:per_month]:         # in pre-rank order, stopping at the first decent one
            key = str(c['id'])
            if key not in cache:
                model = model or PoseModel()
                try:
                    cache[key] = measure(model, c)
                except Exception as e:
                    log(f"  {month} face {c['id']}: {type(e).__name__}: {e}")
                    cache[key] = None
                n += 1
                if n % 25 == 0:
                    cache_path.write_text(json.dumps(cache))
                    log(f"  measured {n} faces, at {month}  ({(time.time() - start) / n:.1f}s each)")
            c['m'] = cache[key]
            if decent(c['m']):
                break
    cache_path.write_text(json.dumps(cache))

    picks_path = out_dir / 'picks.json'
    saved = json.loads(picks_path.read_text()) if picks_path.exists() else {}
    overrides, skip = saved.get('overrides', {}), saved.get('skip', [])
    picks, frames = {}, []
    for month, cands in months.items():
        if month in skip:
            continue
        cands = [dict(x, m=cache[str(x['id'])]) for x in cands if str(x['id']) in cache]  # incl. Look for more
        c = (next((x for x in cands if str(x['id']) == str(overrides.get(month)) and x.get('m')), None)
             or next((x for x in cands if decent(x.get('m'))), None) or choose(cands))
        for x in cands:                                   # review thumbnails, eye-aligned
            t = out_dir / 'thumbs' / f"{x['id']}.jpg"
            if x.get('m') and not t.exists():
                cv2.imwrite(str(t), cv2.resize(aligned(x, (432, 540)), (150, 188))[19:169], [cv2.IMWRITE_JPEG_QUALITY, 85])
        if c is None:
            continue
        if not c['m'].get('lm'):                          # measured before landmarks were kept
            model = model or PoseModel()
            c['m'] = cache[str(c['id'])] = measure(model, c) or c['m']
        picks[month] = c['id']
        frames.append((month, aligned(c), morph_points(c)))
    cache_path.write_text(json.dumps(cache))
    picks_path.write_text(json.dumps({'person': person, 'picks': picks, 'overrides': overrides, 'skip': skip},
                                     indent=1))
    video = out_dir / f"{slug(person)}.mp4"
    render(frames, video, log=log)
    review_sheet(out_dir, person, months, picks)
    log(f"{len(frames)} frames → {video}\nreview: {out_dir / 'index.html'}")
    return video


def months_summary(db_path, out_dir, catalog_path=DEFAULT_DB_PATH):
    """(person, [{month, pick, chosen_by, faces: [checked candidates]}]) for a built timelapse."""
    data = json.loads((out_dir / 'picks.json').read_text())
    cache = json.loads((out_dir / 'candidates.json').read_text())
    person, picks, overrides = data.get('person'), data.get('picks', {}), data.get('overrides', {})
    skip = set(data.get('skip', []))
    with closing(ark_faces.connect(db_path)) as conn:
        if not person:                                  # picks.json from before 'person' was kept
            person = next((n for n in ark_faces.persons(conn) if slug(n) == out_dir.name), out_dir.name)
        months = candidates(conn, person, catalog_path, None)
    out = []
    for month, cands in months.items():
        faces = []
        for c in cands:
            m = cache.get(str(c['id']))
            if str(c['id']) not in cache:
                continue                                # never looked at: the month stopped earlier
            faces.append({'id': c['id'], 'file': os.path.basename(c['path']), 'taken': c['taken'],
                          'measured': bool(m), 'decent': decent(m),
                          **({k: round(m[k]) for k in ('yaw', 'pitch', 'sharp', 'eye_px')} if m else {}),
                          'eyes': round(eyes(m), 2) if eyes(m) is not None else None})
        left = sum(1 for c in cands if str(c['id']) not in cache)
        out.append({'month': month, 'more': left, 'pick': None if month in skip else picks.get(month), 'faces': faces,
                    'chosen_by': 'you' if month in overrides else 'auto', 'skipped': month in skip})
    return person, out


def make_app(db_path, out_root=OUT_ROOT, catalog_path=DEFAULT_DB_PATH):
    """The Timelapse page's API: built timelapses, their months, photos, the video, and picks."""
    import threading
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, Response
    from pydantic import BaseModel

    root = Path(out_root).expanduser()
    app = FastAPI()
    jobs = {}                                           # slug -> {'state', 'log'}

    class PickIn(BaseModel):
        month: str
        face_id: int | None = None                      # None: back to the automatic pick

    class SkipIn(BaseModel):
        month: str
        skip: bool = True

    class NotIn(BaseModel):
        month: str
        face_id: int

    def edit(d, fn):
        path = d / 'picks.json'
        data = json.loads(path.read_text())
        fn(data)
        path.write_text(json.dumps(data, indent=1))

    def folder(name):
        d = root / name
        if not re.fullmatch(r'[a-z0-9-]+', name) or not (d / 'picks.json').exists():
            raise HTTPException(404)
        return d

    def person_of(d):
        p = json.loads((d / 'picks.json').read_text()).get('person')
        if p:
            return p
        with closing(ark_faces.connect(db_path)) as conn:
            return next((n for n in ark_faces.persons(conn) if slug(n) == d.name), d.name)

    @app.get("/api/list")
    def listing():
        return [{'slug': d.name, 'person': person_of(d),
                 'video': (d / f'{d.name}.mp4').exists(), 'job': jobs.get(d.name, {}).get('state')}
                for d in sorted(root.glob('*')) if (d / 'picks.json').exists()]

    @app.get("/api/t/{name}")
    def detail(name: str):
        d = folder(name)
        person, months = months_summary(db_path, d, catalog_path)
        v = d / f'{name}.mp4'
        return {'person': person, 'months': months, 'job': jobs.get(name),
                'video_mtime': int(v.stat().st_mtime) if v.exists() else None}

    @app.post("/api/t/{name}/pick")
    def pick(name: str, body: PickIn):
        d = folder(name)
        path = d / 'picks.json'
        data = json.loads(path.read_text())
        ov = data.setdefault('overrides', {})
        if body.face_id is None:
            ov.pop(body.month, None)
        else:
            ov[body.month] = body.face_id
            data.setdefault('picks', {})[body.month] = body.face_id
        path.write_text(json.dumps(data, indent=1))
        return {'ok': True}

    @app.post("/api/t/{name}/skip")
    def skip_month(name: str, body: SkipIn):
        def fn(data):
            months = set(data.get('skip', []))
            (months.add if body.skip else months.discard)(body.month)
            data['skip'] = sorted(months)
        edit(folder(name), fn)
        return {'ok': True}

    @app.post("/api/t/{name}/not_person")
    def not_person(name: str, body: NotIn):
        """The face isn't this person: its name comes off (and is remembered as a No, like the
        faces page's), and the month goes back to an automatic pick on the next rebuild."""
        d = folder(name)
        person, _ = months_summary(db_path, d, catalog_path)
        with closing(ark_faces.connect(db_path)) as conn:
            pid = conn.execute("SELECT id FROM persons WHERE name = ?", (person,)).fetchone()
            r = conn.execute("SELECT person_id FROM faces WHERE id = ?", (body.face_id,)).fetchone()
            if not pid or not r or r['person_id'] != pid['id']:
                raise HTTPException(404, "that face isn't named as this person")
            ark_faces.name_faces(conn, [body.face_id], '')
            conn.execute("INSERT OR IGNORE INTO face_not_person (face_id, person_id) VALUES (?, ?)",
                         (body.face_id, pid['id']))
            conn.commit()

        def fn(data):
            if data.get('picks', {}).get(body.month) == body.face_id:
                data['picks'].pop(body.month)
            if data.get('overrides', {}).get(body.month) == body.face_id:
                data['overrides'].pop(body.month)
        edit(d, fn)
        return {'ok': True}

    model_lock = threading.Lock()
    model_box = {}

    @app.post("/api/t/{name}/more")
    def more(name: str, body: SkipIn):
        """Look at the month's next few named faces (best detections first); returns how many of
        them are decent. The month's pick stays until you click one, or Rebuild re-picks."""
        d = folder(name)
        person, _ = months_summary(db_path, d, catalog_path)
        with closing(ark_faces.connect(db_path)) as conn:
            cands = candidates(conn, person, catalog_path, None).get(body.month, [])
        cache_path = d / 'candidates.json'
        with model_lock:
            cache = json.loads(cache_path.read_text())
            todo = [c for c in cands if str(c['id']) not in cache][:MORE]
            if todo and 'm' not in model_box:
                model_box['m'] = PoseModel()
            found = 0
            for c in todo:
                try:
                    m = measure(model_box['m'], c)
                except Exception:
                    m = None
                cache[str(c['id'])] = m
                found += decent(m)
                if m:
                    t = d / 'thumbs' / f"{c['id']}.jpg"
                    cv2.imwrite(str(t), cv2.resize(aligned(dict(c, m=m), (432, 540)), (150, 188))[19:169],
                                [cv2.IMWRITE_JPEG_QUALITY, 85])
            cache_path.write_text(json.dumps(cache))
        return {'ok': True, 'looked': len(todo), 'decent': found}

    @app.post("/api/t/{name}/rebuild")
    def rebuild(name: str):
        d = folder(name)
        if jobs.get(name, {}).get('state') == 'running':
            return {'ok': True, 'already': True}
        person, _ = months_summary(db_path, d, catalog_path)
        job = jobs[name] = {'state': 'running', 'log': []}

        def run():
            try:
                build(db_path, person, root, catalog_path, log=lambda m: job['log'].append(m))
                job['state'] = 'done'
            except Exception as e:
                job['state'] = f'failed: {type(e).__name__}: {e}'
        threading.Thread(target=run, daemon=True).start()
        return {'ok': True}

    @app.get("/video/{name}.mp4")
    def video(name: str):
        v = folder(name) / f'{name}.mp4'
        if not v.exists():
            raise HTTPException(404)
        return FileResponse(v, media_type='video/mp4')

    @app.get("/aligned/{name}/{face_id}.jpg")
    def aligned_thumb(name: str, face_id: int):
        t = folder(name) / 'thumbs' / f'{face_id}.jpg'
        if not t.exists():
            raise HTTPException(404)
        return FileResponse(t, media_type='image/jpeg')

    @app.get("/photo/{face_id}.jpg")
    def photo(face_id: int, side: int = 640):
        """The original photo the face is in (oriented, resized), cached on disk."""
        side = max(160, min(side, 2400))
        cache = root / '_photos' / f'{face_id}_{side}.jpg'
        if not cache.exists():
            with closing(ark_faces.connect(db_path)) as conn:
                r = conn.execute("SELECT i.path FROM faces f JOIN images i ON i.sha256 = f.sha256 "
                                 "WHERE f.id = ?", (face_id,)).fetchone()
            if not r or not os.path.exists(r['path']):
                raise HTTPException(404)
            im, _ = load_image(r['path'])
            im = im.convert('RGB')
            im.thumbnail((side, side))
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix('.tmp')
            im.save(tmp, 'JPEG', quality=86)
            tmp.replace(cache)
        return Response(cache.read_bytes(), media_type='image/jpeg')

    return app


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>photoArk timelapse</title>
<style>
:root { --bg:#f6f5f2; --card:#fff; --ink:#1d1d1f; --muted:#6b6b70; --line:#e3e1dc; --accent:#2f6fde; --ok:#2a7; }
@media (prefers-color-scheme: dark) { :root { --bg:#161618; --card:#1f1f22; --ink:#ececee; --muted:#9a9aa1;
  --line:#2e2e33; --accent:#6ea0ff; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.4 -apple-system, system-ui, sans-serif; }
header { position:sticky; top:0; z-index:2; background:var(--bg); border-bottom:1px solid var(--line);
  padding:10px 16px; display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
header h1 { font-size:17px; margin:0; }
select, button { font:inherit; padding:5px 10px; border-radius:8px; border:1px solid var(--line);
  background:var(--card); color:var(--ink); cursor:pointer; }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
#status { color:var(--muted); font-size:13px; }
#top { display:flex; gap:16px; padding:16px; flex-wrap:wrap; align-items:flex-start; }
video { max-height:60vh; max-width:100%; border-radius:10px; background:#000; }
#help { color:var(--muted); max-width:420px; }
main { padding:0 16px 40px; display:grid; gap:12px; grid-template-columns:repeat(auto-fill, minmax(min(100%, 340px), 1fr)); }
.month { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:10px; }
.month h3 { margin:0 0 6px; font-size:15px; display:flex; gap:8px; align-items:baseline; }
.month h3 small { color:var(--muted); font-weight:400; font-size:12px; }
.month .acts { display:flex; gap:6px; margin:0 0 6px; }
.month .acts button { font-size:12px; padding:2px 8px; }
.month.skipped { opacity:.55; }
.orig { width:100%; aspect-ratio:4/3; object-fit:contain; background:#0002; border-radius:8px; cursor:zoom-in; }
.none { color:var(--muted); padding:30px 0; text-align:center; }
.alts { display:flex; gap:6px; margin-top:6px; flex-wrap:wrap; }
.alts figure { margin:0; width:64px; text-align:center; font-size:10px; color:var(--muted); }
.alts img { width:64px; height:64px; object-fit:cover; border-radius:6px; cursor:pointer; outline:2px solid transparent; }
.alts img.on { outline-color:var(--ok); }
.alts img.bad { opacity:.45; }
#viewer { position:fixed; inset:0; background:rgba(0,0,0,.9); display:none; align-items:center; justify-content:center;
  flex-direction:column; gap:8px; z-index:5; }
#viewer img { max-width:96vw; max-height:88vh; }
#viewer p { color:#ddd; margin:0; }
</style></head><body>
<header><h1>Timelapse</h1><select id="who"></select>
  <button class="primary" id="rebuild">Rebuild video</button><span id="status"></span></header>
<div id="top"><video id="video" controls loop muted playsinline></video>
  <div id="help">Each month shows the original photo chosen for it, and below it the faces that were
  looked at (green outline: the pick; faded: turned, too small, eyes shut or soft). Click one to
  use it instead, Look for more to check more photos from that month, or Remove this month to
  leave it out; then Rebuild video. Click a photo to see it large; Esc closes.</div></div>
<main id="list"></main>
<div id="viewer" onclick="this.style.display='none'"><img id="vimg"><p id="vcap"></p></div>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let current = null, data = null, poll = null;

async function init() {
  const list = await (await fetch('api/list')).json();
  if (!list.length) { $('#list').innerHTML = '<p class="none">No timelapse built yet. Run: photo-ark-timelapse --person "Name"</p>'; return; }
  $('#who').innerHTML = list.map(x => `<option value="${x.slug}">${esc(x.person)}</option>`).join('');
  let saved = null; try { saved = localStorage.getItem('timelapse'); } catch (e) {}
  if (saved && list.some(x => x.slug === saved)) $('#who').value = saved;
  await load($('#who').value);
}

async function load(slug) {
  current = slug;
  try { localStorage.setItem('timelapse', slug); } catch (e) {}
  data = await (await fetch(`api/t/${slug}`)).json();
  if (data.video_mtime) $('#video').src = `video/${slug}.mp4?v=${data.video_mtime}`;
  else $('#video').removeAttribute('src');
  showJob(data.job);
  const picked = data.months.filter(m => m.pick).length;
  $('#list').innerHTML = data.months.map(card).join('');
  $('#status').textContent = $('#status').textContent || `${picked} of ${data.months.length} months have a photo`;
}

function card(m) {
  const pick = m.faces.find(f => f.id === m.pick);
  const when = pick && pick.taken ? pick.taken.slice(0, 10) : '';
  const head = `<h3>${esc(m.month)} <small>${esc(when)}${pick ? ' · ' + esc(pick.file) : ''}${m.chosen_by === 'you' ? ' · your pick' : ''}</small></h3>
    <div class="acts">${pick ? `<button onclick="notPerson('${m.month}', ${pick.id})">Not ${esc(data.person.split(' ')[0])}</button>` : ''}
      <button onclick="skipMonth('${m.month}', ${!m.skipped})">${m.skipped ? 'Use this month' : 'Remove this month'}</button>
      ${m.more && !m.skipped ? `<button onclick="lookMore(this, '${m.month}')">Look for more (${m.more} left)</button>` : ''}</div>`;
  if (m.skipped) return `<div class="month skipped"><h3>${esc(m.month)} <small>removed: left out of the video</small></h3>
    <div class="acts"><button onclick="skipMonth('${m.month}', false)">Use this month</button></div></div>`;
  const orig = pick ? `<img class="orig" loading="lazy" src="photo/${pick.id}.jpg" data-id="${pick.id}"
      data-cap="${esc(m.month)} · ${esc(pick.file)}" onclick="view(this)">` : '<div class="none">no usable photo</div>';
  const alts = m.faces.filter(f => f.measured).map(f => `<figure><img loading="lazy" src="aligned/${current}/${f.id}.jpg"
      class="${f.id === m.pick ? 'on' : ''} ${f.decent ? '' : 'bad'}" title="${esc(f.file)} · yaw ${f.yaw}° pitch ${f.pitch}° · sharp ${f.sharp} · eyes ${f.eye_px}px${f.eyes != null ? ' · eye opening ' + f.eyes : ''}"
      onclick="choose('${m.month}', ${f.id})"><br>${why(f)}</figure>`).join('');
  return `<div class="month">${head}${orig}<div class="alts">${alts}</div></div>`;
}

function why(f) {
  if (f.decent) return 'ok';
  if (Math.abs(f.yaw) > 20 || Math.abs(f.pitch) > 20) return 'turned';
  if (f.eye_px < 40) return 'small';
  if (f.eyes != null && f.eyes < 0.2) return 'eyes shut';
  return 'soft';
}

async function lookMore(btn, month) {
  btn.disabled = true; btn.textContent = 'Looking…';
  const r = await fetch(`api/t/${current}/more`, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                  body: JSON.stringify({month})});
  const d = r.ok ? await r.json() : null;
  $('#status').textContent = d ? `${month}: looked at ${d.looked} more, ${d.decent} decent; click one to use it` : 'Look for more failed';
  const y = window.scrollY; await load(current); window.scrollTo(0, y);
}

async function act(url, body, msg) {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (!r.ok) { alert((await r.json()).detail || "That did not work"); return; }
  $('#status').textContent = msg;
  const y = window.scrollY; await load(current); window.scrollTo(0, y);
}
const skipMonth = (month, skip) => act(`api/t/${current}/skip`, {month, skip},
  (skip ? `${month} removed` : `${month} back in`) + '; Rebuild video to see it');
const notPerson = (month, face_id) => confirm(`Take the name ${data.person} off this face?`) &&
  act(`api/t/${current}/not_person`, {month, face_id}, 'Name removed; Rebuild video picks another photo for that month');

async function choose(month, id) {
  await fetch(`api/t/${current}/pick`, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                        body: JSON.stringify({month, face_id: id})});
  $('#status').textContent = 'Pick changed; Rebuild video to see it';
  const y = window.scrollY; await load(current); window.scrollTo(0, y);
}

function view(img) {
  $('#vimg').src = `photo/${img.dataset.id}.jpg?side=2000`;
  $('#vcap').textContent = img.dataset.cap;
  $('#viewer').style.display = 'flex';
}

function showJob(job) {
  clearTimeout(poll);
  if (!job) return;
  if (job.state === 'running') {
    $('#status').textContent = 'Rebuilding… ' + (job.log.slice(-1)[0] || '');
    poll = setTimeout(async () => { const d = await (await fetch(`api/t/${current}`)).json();
      if (d.job && d.job.state !== 'running') { await load(current); } else showJob(d.job); }, 3000);
  } else $('#status').textContent = job.state === 'done' ? 'Video rebuilt' : job.state;
}

$('#rebuild').onclick = async () => {
  await fetch(`api/t/${current}/rebuild`, {method: 'POST'});
  showJob({state: 'running', log: []}); load(current);
};
$('#who').onchange = () => { $('#status').textContent = ''; load($('#who').value); };
document.addEventListener('keydown', e => { if (e.key === 'Escape') $('#viewer').style.display = 'none'; });
init();
</script></body></html>"""


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--person', required=True, help='name as on the faces page')
    p.add_argument('--db', default=DEFAULT_ANALYSIS_DB)
    p.add_argument('--catalog', default=DEFAULT_DB_PATH)
    p.add_argument('--out', default=OUT_ROOT)
    p.add_argument('--per-month', type=int, default=PER_MONTH, help='candidates measured per month')
    a = p.parse_args()
    build(os.path.expanduser(a.db), a.person, a.out, os.path.expanduser(a.catalog), a.per_month)


if __name__ == '__main__':
    main()
