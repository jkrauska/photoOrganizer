import pytest
from PIL import Image


def test_face_finder_lists_photos_with_all_the_chosen_people(tmp_path):
    pytest.importorskip('fastapi')
    pytest.importorskip('httpx')
    from fastapi.testclient import TestClient
    from photo_organizer.analysis import ark
    from photo_organizer.analysis.face_finder import make_app
    db = str(tmp_path / 'analysis.db')
    a = ark.open_db(db)
    a.executemany("INSERT INTO persons (id, name) VALUES (?, ?)", [(1, 'Alice'), (2, 'Bob'), (3, 'Carol')])
    photos = [('2010/06/2010-06-05/a.jpg', [1, 2, 3]),          # all three
              ('2014/01/2014-01-02/b.jpg', [1, 2, 3, None]),    # all three and a stranger
              ('2014/01/2014-01-02/c.jpg', [1, 3]),             # no Bob
              ('2015/03/2015-03-03/d.jpg', [3, 3]),             # Carol twice
              ('2015/03/2015-03-03/e.mov', [1, 2, 3])]          # a video frame isn't a photo here
    for i, (rel, people) in enumerate(photos):
        path = tmp_path / 'ark' / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (60, 40), (40 * i, 90, 200)).save(path, 'JPEG')
        a.execute("INSERT INTO images (sha256, path, caption) VALUES (?, ?, ?)", (f'h{i}', str(path), f'photo {i}'))
        a.executemany("INSERT INTO faces (sha256, face_idx, person_id) VALUES (?, ?, ?)",
                      [(f'h{i}', j, pid) for j, pid in enumerate(people)])
    a.commit()
    a.close()
    client = TestClient(make_app(db, tmp_path / 'thumbs'))
    find = lambda **kw: client.get('/api/find', params=kw).json()

    assert client.get('/').status_code == 200
    assert [p['name'] for p in client.get('/api/persons').json()] == ['Carol', 'Alice', 'Bob']
    r = find(p=[1, 2, 3])
    assert [p['sha'] for p in r['items']] == ['h1', 'h0'], "both with all three, newest first"
    assert r['years'] == [[2014, 1], [2010, 1]] and r['items'][0]['day'] == '2014-01-02'
    assert [p['sha'] for p in find(p=[1, 2, 3], alone=True)['items']] == ['h0'], "h1 has a stranger in it"
    assert [p['sha'] for p in find(p=[1, 3])['items']] == ['h2', 'h1', 'h0']
    assert [p['sha'] for p in find(p=[3])['items']] == ['h3', 'h2', 'h1', 'h0']
    assert [p['sha'] for p in find(p=[3], alone=True)['items']] == ['h3']
    r = find(p=[3], year=2014)
    assert r['total'] == 2 and r['all'] == 4
    assert find()['items'] == []
    assert client.get('/thumb/h0.jpg').headers['content-type'] == 'image/jpeg'
    assert (tmp_path / 'thumbs' / 'h0.jpg').exists()
    assert client.get('/photo/h0.jpg').headers['content-type'] == 'image/jpeg'
    assert client.get('/thumb/nope.jpg').status_code == 404
