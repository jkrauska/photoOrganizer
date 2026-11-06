"""The places page: photos on a map, filtered by place, people, years and how sure the place is."""

import pytest
from PIL import Image

from photo_organizer.db import init_db
from photo_organizer.locations import init_locations


def test_places_filters_by_place_people_years_and_view(tmp_path):
    pytest.importorskip('fastapi')
    pytest.importorskip('httpx')
    from fastapi.testclient import TestClient
    from photo_organizer.analysis import ark
    from photo_organizer.analysis.places import make_app
    catalog, db = str(tmp_path / 'catalog.db'), str(tmp_path / 'analysis.db')
    client = TestClient(make_app(catalog, db, tmp_path / 'thumbs'))
    init_db(catalog).close()
    ark.open_db(db).close()
    assert client.get('/api/facets').json()['missing']           # before the first locations run

    conn = init_db(catalog)
    init_locations(conn)
    rows = [  # name, day, lat, lon, city, region, state, country, source
        ('a.jpg', '2018-09-07', 37.77, -122.43, 'San Francisco', 'San Francisco', 'California', 'United States', 'gps'),
        ('b.jpg', '2018-09-07', 37.77, -122.43, 'San Francisco', 'San Francisco', 'California', 'United States',
         'day_gps'),
        ('c.jpg', '2020-02-01', 39.24, -120.03, 'Kings Beach', 'Tahoe', 'California', 'United States', 'gps'),
        ('d.jpg', '2015-05-05', 48.85, 2.35, 'Paris', None, 'Île-de-France', 'France', 'gps'),
        ('e.jpg', '2016-01-01', None, None, 'Chicago', None, 'Illinois', 'United States', 'image_setting'),
        ('f.mov', '2020-02-01', 39.24, -120.03, 'Kings Beach', 'Tahoe', 'California', 'United States', 'gps'),
        ('g.jpg', '2020-02-02', None, None, None, None, None, None, None),
    ]
    for i, (name, day, lat, lon, city, region, state, country, source) in enumerate(rows):
        path = tmp_path / 'ark' / day[:4] / day[5:7] / day / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (60, 40), (30 * i, 90, 200)).save(path, 'JPEG')
        conn.execute("INSERT INTO files (id, filename, original_path, dest_path, sha256, status, taken_at) "
                     "VALUES (?, ?, ?, ?, ?, 'done', ?)", (i + 1, name, f'/src/{name}', str(path), f'h{i}', f'{day}T12:00:00'))
        conn.execute("INSERT INTO locations (file_id, lat, lon, city, region, state, country, source, gps_read_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'x')", (i + 1, lat, lon, city, region, state, country, source))
    conn.commit()
    conn.close()
    a = ark.open_db(db)
    a.executemany("INSERT INTO persons (id, name) VALUES (?, ?)", [(1, 'Alice'), (2, 'Bob')])
    for sha, people in [('h0', [1, 2]), ('h2', [1]), ('h3', [2])]:
        a.execute("INSERT INTO images (sha256, path) VALUES (?, '')", (sha,))
        a.executemany("INSERT INTO faces (sha256, face_idx, person_id) VALUES (?, ?, ?)",
                      [(sha, j, p) for j, p in enumerate(people)])
    a.commit()
    a.close()
    shas = lambda **kw: [p['sha'] for p in client.get('/api/photos', params=kw).json()['items']]

    assert client.get('/').status_code == 200
    f = client.get('/api/facets').json()                        # GPS + same day by default; no videos
    assert f['total'] == 4 and f['mapped'] == 4 and f['years'] == ['2015', '2018', '2020']
    assert f['region'] == [['San Francisco', 2], ['Tahoe', 1]]
    assert f['bounds'] == [37.77, -122.43, 48.85, 2.35]
    f = client.get('/api/facets', params={'region': 'Tahoe'}).json()
    assert f['total'] == 1 and ['San Francisco', 2] in f['region']   # a menu lists what the others allow
    assert shas() == ['h2', 'h1', 'h0', 'h3']                    # newest first
    assert shas(sure='gps') == ['h2', 'h0', 'h3']
    assert shas(sure='any') == ['h2', 'h1', 'h0', 'h4', 'h3']    # Chicago from Gemma text, without a position
    assert shas(country='France') == ['h3'] and shas(state='California', start=2019) == ['h2']
    assert shas(p=[1]) == ['h2', 'h0'] and shas(p=[1, 2]) == ['h0']
    assert shas(bbox='37,-123,40,-119') == ['h2', 'h1', 'h0']    # the map's view
    assert shas(bbox='40,170,50,10') == ['h3']                   # a view across the date line
    cl = client.get('/api/clusters', params={'zoom': 8}).json()
    assert sorted(c['n'] for c in cl) == [1, 1, 2] and all(c['thumb'] for c in cl)
    assert sorted(c['n'] for c in client.get('/api/clusters', params={'zoom': 3}).json()) == [1, 3]   # bigger cells
    assert sum(c['n'] for c in client.get('/api/clusters', params={'zoom': 3, 'p': [2]}).json()) == 2
    item = client.get('/api/photos', params={'city': 'Paris'}).json()['items'][0]
    assert item['place'] == 'Paris, Île-de-France, France' and item['day'] == '2015-05-05'
    assert client.get('/thumb/h0.jpg').headers['content-type'] == 'image/jpeg'
    assert client.get('/photo/h3.jpg').headers['content-type'] == 'image/jpeg'
    assert client.get('/thumb/h5.jpg').status_code == 404        # the video
    assert client.get('/api/photos', params={'bbox': '1,2'}).status_code == 400
