import random
import sqlite3

import pytest
from PIL import Image

from photo_organizer.db import init_db


def test_shuffle_serves_random_stored_photos_with_captions_and_keeps_scores(tmp_path):
    pytest.importorskip('fastapi')
    pytest.importorskip('httpx')
    from fastapi.testclient import TestClient
    from photo_organizer.analysis import ark
    from photo_organizer.analysis.shuffle import make_app
    day = tmp_path / 'ark' / '2010' / '06' / '2010-06-05'
    day.mkdir(parents=True)
    Image.new('RGB', (3000, 1500), (0, 90, 200)).save(day / 'a.jpg')
    (day / 'b.mov').write_bytes(b'not a photo')
    catalog = str(tmp_path / 'catalog.db')
    init_db(catalog).close()
    with sqlite3.connect(catalog) as conn:
        for name, sha, status, path in [('a.jpg', 'h1', 'done', day / 'a.jpg'),
                                        ('b.mov', 'h2', 'done', day / 'b.mov'),          # video
                                        ('c.jpg', 'h3', 'pending', day / 'c.jpg'),       # not stored yet
                                        ('d.jpg', 'h4', 'done', tmp_path / 'gone.jpg')]: # missing on disk
            conn.execute("INSERT INTO files (filename, original_path, dest_path, sha256, status, taken_at) "
                         "VALUES (?, ?, ?, ?, ?, '2010-06-05T10:11:12')", (name, f'/src/{name}', str(path), sha, status))
    db = str(tmp_path / 'analysis.db')
    with ark.open_db(db) as conn:
        conn.execute("INSERT INTO images (sha256, path, caption, setting, keywords) "
                     "VALUES ('h1', ?, 'a blue wall', 'outdoor', '[\"blue\", \"wall\"]')", (str(day / 'a.jpg'),))
    client = TestClient(make_app(catalog, db, tmp_path / 'cache', rng=random.Random(1)))
    assert client.get('/').status_code == 200
    for _ in range(10):
        p = client.get('/api/random').json()
        assert p['sha'] == 'h1' and p['caption'] == 'a blue wall' and p['keywords'] == ['blue', 'wall']
        assert p['taken_at'] == '2010-06-05T10:11:12' and p['score'] is None
    r = client.get('/photo/h1.jpg')
    assert r.headers['content-type'] == 'image/jpeg'
    from io import BytesIO
    assert Image.open(BytesIO(r.content)).size == (2048, 1024)
    assert (tmp_path / 'cache' / 'h1' / 'h1.jpg').exists()
    assert client.get('/photo/h2.jpg').status_code == 404       # videos aren't served
    assert client.get('/photo/h4.jpg').status_code == 404
    assert client.get('/photo/..%2Fx.jpg').status_code == 404

    assert client.post('/api/score', json={'sha': 'h1', 'score': 2}).status_code == 200
    assert client.post('/api/score', json={'sha': 'h1', 'score': 4}).status_code == 200   # re-scoring replaces
    assert client.post('/api/score', json={'sha': 'h1', 'score': 9}).status_code == 422
    assert client.post('/api/score', json={'sha': 'nope', 'score': 3}).status_code == 404
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT sha256, score, path FROM photo_scores").fetchall() == [('h1', 4, str(day / 'a.jpg'))]
    assert client.get('/api/random').json()['score'] == 4
    assert client.get('/api/photo/h1').json()['score'] == 4
    assert client.post('/api/score', json={'sha': 'h1', 'score': None}).status_code == 200    # 0 clears it
    assert client.get('/api/photo/h1').json()['score'] is None


def test_shuffle_picks_the_day_first_so_bursts_dont_crowd_out_other_days(tmp_path):
    from photo_organizer.analysis.shuffle import photo_days, random_photo
    catalog = str(tmp_path / 'catalog.db')
    conn = init_db(catalog)
    burst, single = '/ark/2017/05/2017-05-20', '/ark/2017/05/2017-05-21'
    rows = [(f'{burst}/{i:04}.jpg', f'b{i}', 'done') for i in range(200)]
    rows += [(f'{single}/one.jpg', 's1', 'done'), (f'{single}/clip.mov', 's2', 'done'),
             ('/ark/2017/05/2017-05-22/only.mov', 'v1', 'done'),        # a day of videos only
             ('/ark/2017/05/2017-05-23/x.jpg', 'p1', 'pending')]       # not stored yet
    conn.executemany("INSERT INTO files (filename, original_path, dest_path, sha256, status) VALUES ('f', ?, ?, ?, ?)",
                     [(p, p, sha, status) for p, sha, status in rows])
    conn.commit()
    days = photo_days(conn)
    assert days == [burst, single]
    rng = random.Random(7)
    picks = [random_photo(conn, days, rng)[0] for _ in range(400)]
    assert 150 < picks.count('s1') < 250              # half the picks, not 1 in 201
    assert 's2' not in picks
