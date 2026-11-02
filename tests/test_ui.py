import json
import sqlite3

import pytest
from PIL import Image

from photo_organizer.db import init_db


def test_one_server_serves_every_page_with_the_nav_and_keeps_queue_changes_local(tmp_path):
    pytest.importorskip('fastapi')
    pytest.importorskip('httpx')
    from fastapi.testclient import TestClient
    from photo_organizer.analysis import ark
    from photo_organizer.ui import make_app
    day = tmp_path / 'ark' / '2010' / '06' / '2010-06-05'
    day.mkdir(parents=True)
    Image.new('RGB', (800, 600), (0, 90, 200)).save(day / 'a.jpg')
    catalog = str(tmp_path / 'catalog.db')
    init_db(catalog).close()
    with sqlite3.connect(catalog) as conn:
        conn.execute("INSERT INTO files (filename, original_path, dest_path, sha256, status, taken_at) "
                     "VALUES ('a.jpg', '/src/a.jpg', ?, 'h1', 'done', '2010-06-05T10:11:12')", (str(day / 'a.jpg'),))
    db = str(tmp_path / 'analysis.db')
    ark.open_db(db).close()
    folder = tmp_path / 'dump'
    folder.mkdir()
    client = TestClient(make_app(catalog, db, 8077, tmp_path / 'crops', tmp_path / 'thumbs', tmp_path / 'cache'),
                        base_url='http://localhost:8077')

    for path, active in [('/', 'Status'), ('/archive', 'Archive'), ('/days/', 'Days'),
                         ('/faces/', 'Faces'), ('/shuffle/', 'Shuffle')]:
        r = client.get(path)
        assert r.status_code == 200 and 'id="ark-nav"' in r.text, path
        assert f'class=on>{active}</a>' in r.text, path
        assert r.text.count('<nav') == 1, f"{path}: the page's own nav gives way to the shared one"
    assert client.get('/faces', follow_redirects=False).headers['location'] == '/faces/'

    # Each page's API answers under its prefix; the status API stays at the root.
    assert client.get('/shuffle/api/random').json()['sha'] == 'h1'
    assert client.get('/shuffle/photo/h1.jpg').headers['content-type'] == 'image/jpeg'
    assert client.get('/faces/api/clusters').status_code == 200
    assert client.get('/days/api/years').status_code == 200
    assert client.get('/api/status').status_code == 200
    assert client.get('/api/archive').status_code == 200
    assert client.get('/api/analysis').json()['available'], "the analysis DB is there"

    body = json.dumps({'path': str(folder)})
    ok = {'Content-Type': 'application/json', 'Origin': 'http://localhost:8077'}
    assert client.post('/api/queue', content=body, headers={**ok, 'Host': 'studio1.local:8077',
                                                             'Origin': 'http://studio1.local:8077'}).status_code == 403
    assert client.post('/api/queue', content=body, headers={**ok, 'Origin': 'http://evil.example'}).status_code == 403
    r = client.post('/api/queue', content=body, headers=ok)
    assert r.status_code == 200 and r.json()['ok']
