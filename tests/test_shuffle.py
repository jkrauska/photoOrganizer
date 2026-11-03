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
    assert client.post('/api/score', json={'sha': 'h1', 'score': 1}).status_code == 200
    assert client.get('/api/random').status_code == 503      # the only photo is one-star
    assert client.post('/api/score', json={'sha': 'h1', 'score': 3}).status_code == 200
    assert client.get('/api/years').json() == [2010, 2010]
    assert client.get('/api/random', params={'start': 2010, 'end': 2010}).json()['sha'] == 'h1'
    assert client.get('/api/random', params={'start': 2011}).status_code == 404


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
    skip = {'s1'} | {f'b{i}' for i in range(199)}          # one-star photos
    picks = {random_photo(conn, days, rng, skip) for _ in range(50)}
    assert picks == {('b199', f'{burst}/0199.jpg', None), None}


def test_shuffle_can_show_only_photos_of_one_person(tmp_path):
    pytest.importorskip('fastapi')
    pytest.importorskip('httpx')
    from fastapi.testclient import TestClient
    from photo_organizer.analysis import ark
    from photo_organizer.analysis.shuffle import make_app
    day = tmp_path / 'ark' / '2012' / '07' / '2012-07-04'
    day.mkdir(parents=True)
    catalog = str(tmp_path / 'catalog.db')
    conn = init_db(catalog)
    for name, color in [('ann.jpg', (200, 0, 0)), ('bob.jpg', (0, 200, 0)), ('both.jpg', (0, 0, 200)),
                        ('nobody.jpg', (90, 90, 90)), ('gone.jpg', None)]:
        if color:
            Image.new('RGB', (64, 48), color).save(day / name)
        conn.execute("INSERT INTO files (filename, original_path, dest_path, sha256, status) VALUES (?, ?, ?, ?, 'done')",
                     (name, f'/src/{name}', str(day / name), name.split('.')[0] + name[-3:]))
    conn.commit()
    conn.close()
    db = str(tmp_path / 'analysis.db')
    with ark.open_db(db) as a:
        a.executemany("INSERT INTO persons (id, name) VALUES (?, ?)", [(1, 'Ann'), (2, 'Bob'), (3, 'Nobody shown')])
        for sha, name, people in [('annjpg', 'ann.jpg', [1]), ('bobjpg', 'bob.jpg', [2]), ('bothjpg', 'both.jpg', [1, 2]),
                                  ('nobodyjpg', 'nobody.jpg', []), ('gonejpg', 'gone.jpg', [1])]:
            a.execute("INSERT INTO images (sha256, path) VALUES (?, ?)", (sha, str(day / name)))
            a.executemany("INSERT INTO faces (sha256, face_idx, person_id) VALUES (?, ?, ?)",
                          [(sha, i, pid) for i, pid in enumerate(people)])
    client = TestClient(make_app(catalog, db, tmp_path / 'cache', rng=random.Random(3)))

    assert client.get('/api/persons').json() == [{'id': 1, 'name': 'Ann', 'photos': 3},
                                                  {'id': 2, 'name': 'Bob', 'photos': 2}]
    seen = {client.get('/api/random?person=1').json()['sha'] for _ in range(30)}
    assert seen == {'annjpg', 'bothjpg'}, "Ann's photos only; one missing on disk is skipped"
    assert {client.get('/api/random?person=2').json()['sha'] for _ in range(30)} == {'bobjpg', 'bothjpg'}
    assert client.get('/api/random?person=3').status_code == 404
    assert 'nobodyjpg' in {client.get('/api/random').json()['sha'] for _ in range(40)}


def test_shuffle_year_range_narrows_the_days(tmp_path):
    from photo_organizer.analysis.shuffle import day_year, in_years
    days = ['/ark/1999/12/1999-12-31', '/ark/2005/06/2005-06-01', '/ark/2012/01/2012-01-01', '/ark/misc']
    assert day_year(days[1]) == 2005 and day_year(days[3]) is None
    assert in_years(days) == days
    assert in_years(days, 2000, 2012) == days[1:3]
    assert in_years(days, end=2005) == days[:2]
    assert in_years({d: [d] for d in days}, 2006) == {days[2]: [days[2]]}
