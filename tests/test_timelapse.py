import numpy as np
import pytest

cv2 = pytest.importorskip('cv2')
from photo_organizer.analysis import timelapse as t  # noqa: E402


def test_eye_transform_puts_the_eyes_on_the_frame_points_whatever_the_tilt():
    W, H = t.SIZE
    A = t.eye_transform([300, 420], [420, 380])                  # a tilted, off-centre face
    got = np.array([[300, 420, 1], [420, 380, 1]]) @ A.T
    want = [[W * (0.5 - t.EYE_GAP / 2), H * t.EYE_Y], [W * (0.5 + t.EYE_GAP / 2), H * t.EYE_Y]]
    assert np.allclose(got, want, atol=1e-6)


def test_decent_needs_a_frontal_big_sharp_face():
    m = {'yaw': 5, 'pitch': -3, 'eye_px': 120, 'sharp': 200}
    assert t.decent(m)
    assert not t.decent(dict(m, yaw=35)) and not t.decent(dict(m, eye_px=20))
    assert not t.decent(dict(m, sharp=10)) and not t.decent(None)
    soft_frontal = {'m': dict(m, sharp=10)}
    turned_sharp = {'m': dict(m, yaw=40, sharp=900)}
    assert t.choose([turned_sharp, soft_frontal, {'m': None}]) is soft_frontal, "frontal beats sharp"


def test_morph_starts_and_ends_on_the_two_photos():
    W, H = 200, 250
    rng = np.random.default_rng(5)
    img1 = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)
    img2 = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)
    base = np.array([[50, 60], [150, 60], [100, 120], [70, 180], [130, 180],
                     [0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]], np.float32)
    p2 = base + np.array([[5, 3], [-4, 2], [0, 6], [3, -2], [-3, 4], [0, 0], [0, 0], [0, 0], [0, 0]], np.float32)
    tris = t._triangles((base + p2) / 2, (W, H))
    assert len(tris) >= 8
    start, end = t.morph(img1, img2, base, p2, tris, 0.0), t.morph(img1, img2, base, p2, tris, 1.0)
    # Triangle edges are antialiased, so compare most pixels rather than all of them.
    assert np.mean(np.abs(start.astype(int) - img1) <= 2) > 0.95
    assert np.mean(np.abs(end.astype(int) - img2) <= 2) > 0.95
    mid = t.morph(img1, img2, base, p2, tris, 0.5)
    assert mid.shape == img1.shape and mid.dtype == np.uint8


def test_timelapse_page_lists_months_and_saves_a_pick(tmp_path):
    pytest.importorskip('fastapi')
    pytest.importorskip('httpx')
    import json
    from fastapi.testclient import TestClient
    from PIL import Image
    from photo_organizer.analysis import ark
    db = str(tmp_path / 'analysis.db')
    conn = ark.open_db(db)
    for fid in (1, 2):
        p = tmp_path / f'2019-01-0{fid}.jpg'
        Image.new('RGB', (64, 48), (fid * 60, 0, 0)).save(p)
        conn.execute("INSERT INTO images (sha256, path) VALUES (?, ?)", (f's{fid}', str(p)))
        conn.execute("INSERT INTO faces (id, sha256, face_idx, x1, y1, x2, y2, det_score) "
                     "VALUES (?, ?, 0, 10, 10, 40, 40, 0.9)", (fid, f's{fid}'))
    conn.commit()
    from photo_organizer.analysis import ark_faces
    ark_faces.name_faces(conn, [1, 2], 'Kid')
    out = tmp_path / 'tl' / 'kid'
    out.mkdir(parents=True)
    m = {'yaw': 0, 'pitch': 0, 'sharp': 100, 'eye_px': 90}
    (out / 'candidates.json').write_text(json.dumps({'1': m, '2': dict(m, yaw=50)}))
    (out / 'picks.json').write_text(json.dumps({'person': 'Kid', 'picks': {'2019-01': 1}, 'overrides': {}}))
    client = TestClient(t.make_app(db, tmp_path / 'tl', catalog_path=str(tmp_path / 'none.db')))
    assert client.get('/api/list').json()[0]['slug'] == 'kid'
    got = client.get('/api/t/kid').json()
    assert got['person'] == 'Kid' and got['months'][0]['month'] == '2019-01'
    assert [(f['id'], f['decent']) for f in got['months'][0]['faces']] == [(1, True), (2, False)]
    assert client.post('/api/t/kid/pick', json={'month': '2019-01', 'face_id': 2}).status_code == 200
    got = client.get('/api/t/kid').json()['months'][0]
    assert got['pick'] == 2 and got['chosen_by'] == 'you'
    assert client.get('/photo/1.jpg').headers['content-type'] == 'image/jpeg'
    assert client.post('/api/t/kid/skip', json={'month': '2019-01'}).status_code == 200
    got = client.get('/api/t/kid').json()['months'][0]
    assert got['skipped'] and got['pick'] is None
    client.post('/api/t/kid/skip', json={'month': '2019-01', 'skip': False})
    assert client.post('/api/t/kid/not_person', json={'month': '2019-01', 'face_id': 2}).status_code == 200
    assert conn.execute("SELECT person_id FROM faces WHERE id = 2").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM face_not_person WHERE face_id = 2").fetchone()[0] == 1
    assert client.get('/api/t/kid').json()['months'][0]['pick'] is None, "re-picked on the next rebuild"
    assert client.post('/api/t/kid/not_person', json={'month': '2019-01', 'face_id': 2}).status_code == 404
    assert client.post('/api/t/kid/born', json={'born': '2018-07'}).status_code == 200
    got = client.get('/api/t/kid').json()
    assert got['born'] == '2018-07' and got['months'][0]['age'] == '6m'
    assert client.post('/api/t/kid/born', json={'born': 'July'}).status_code == 400
    (out / 'kid.mp4').write_bytes(b'mp4')
    r = client.get('/video/kid.mp4', params={'download': 1})
    assert r.content == b'mp4' and 'Kid%20timelapse.mp4' in r.headers['content-disposition']
    assert 'content-disposition' not in client.get('/video/kid.mp4').headers
    assert client.get('/api/t/nobody').status_code == 404
    assert client.get('/api/t/..%2Fx').status_code == 404


def test_eye_opening_tells_open_eyes_from_shut_ones():
    def face(open_):
        P = np.zeros((68, 2))
        for start, x0 in ((36, 30), (42, 70)):              # each eye: corners 20 px apart
            P[start:start + 6] = [[x0, 50], [x0 + 6, 50 - open_], [x0 + 14, 50 - open_],
                                  [x0 + 20, 50], [x0 + 14, 50 + open_], [x0 + 6, 50 + open_]]
        return P.tolist()
    assert t.eye_opening(face(3)) == pytest.approx(0.3)
    assert t.eye_opening(face(1)) < t.MIN_EYES
    m = {'yaw': 0, 'pitch': 0, 'eye_px': 90, 'sharp': 200}
    assert t.decent(dict(m, lm=face(3))) and not t.decent(dict(m, lm=face(1)))
    assert t.decent(m), "measured before landmarks were kept: not held against it"


def test_age_label_counts_months_from_the_birth_month():
    assert t.age_label('2012-01', '2012-01') == 'newborn'
    assert t.age_label('2012-04', '2012-01') == '3m'
    assert t.age_label('2013-01', '2012-01') == '1y'
    assert t.age_label('2013-03', '2012-01') == '1y 2m'
    assert t.age_label('2026-11', '2012-01') == '14y 10m'
    assert t.age_label('2011-12', '2012-01') == '2011-12', "before the birth month: the date"
    assert t.age_label('2013-03', None) == '2013-03'


def test_render_joins_the_months_clips_into_one_video(tmp_path):
    import shutil
    import subprocess
    from PIL import Image
    if not shutil.which('ffmpeg'):
        pytest.skip('no ffmpeg')
    specs = []
    for i in range(3):
        p = tmp_path / f'{i}.jpg'
        Image.new('RGB', (400, 300), (60 * i, 90, 120)).save(p)
        specs.append({'label': f'{i}m', 'path': str(p),
                      'm': {'left': [170, 140], 'right': [230, 140], 'lm': None}})
    out = tmp_path / 'out.mp4'
    t.render(specs, out, hold=2, steps=3, log=lambda s: None, workers=2)
    frames = subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0',
                             '-show_entries', 'stream=nb_read_frames', '-of', 'csv=p=0', str(out)],
                            capture_output=True, text=True).stdout.strip()
    assert int(frames) == 3 * 2 + 2 * 3, "each month held, then morphed into the next"


def test_versions_keep_the_three_previous_videos(tmp_path):
    import os
    import time as _t
    video = tmp_path / 'kid.mp4'
    for i in range(5):
        video.write_bytes(f'v{i}'.encode())
        os.utime(video, (1_700_000_000 + i * 60,) * 2)
        t.keep_version(video)
        assert not video.exists()
    kept = t.versions(video)
    assert len(kept) == 3 and [ (tmp_path / 'versions' / v['file']).read_bytes() for v in kept] == [b'v4', b'v3', b'v2']
    assert kept[0]['rendered'] == _t.strftime('%Y-%m-%d %H:%M', _t.localtime(1_700_000_000 + 4 * 60))


def test_the_label_sits_in_the_upper_right():
    img = np.zeros((1350, 1080, 3), np.uint8)
    t._label(img, '1y 2m')
    ys, xs = np.nonzero(img.max(axis=2))
    assert xs.min() > 1080 / 2 and ys.max() < 1350 / 6
