import sqlite3

import numpy as np
from PIL import Image

from photo_organizer.analysis import ark, screenshots


def test_a_screenshot_is_a_png_at_a_screen_size_without_a_camera():
    assert screenshots.is_screenshot('/s/IMG_5920.PNG', 1242, 2208)
    assert screenshots.is_screenshot('/s/IMG_1.png', 2208, 1242), "either way up"
    assert not screenshots.is_screenshot('/s/IMG_1.JPG', 1242, 2208), "JPGs are real photos"
    assert not screenshots.is_screenshot('/s/IMG_1.PNG', 1242, 2208, camera='Apple')
    assert not screenshots.is_screenshot('/s/IMG_1.PNG', 1200, 2208)


def test_find_and_forget_the_faces_in_screenshots(tmp_path):
    conn = ark.open_db(str(tmp_path / 'analysis.db'))
    shots = [('a', '/s/IMG_1.PNG', 1242, 2208), ('b', '/s/IMG_2.PNG', 1242, 2208),  # b has a camera
             ('c', '/s/IMG_3.JPG', 1242, 2208), ('d', '/s/IMG_4.PNG', 800, 600)]
    for i, (sha, path, w, h) in enumerate(shots):
        conn.execute("INSERT INTO images (sha256, path, width, height) VALUES (?, ?, ?, ?)", (sha, path, w, h))
        conn.execute("INSERT INTO faces (id, sha256, face_idx, x1, y1, x2, y2, det_score) "
                     "VALUES (?, ?, 0, 1, 1, 9, 9, 0.9)", (i + 1, sha))
    conn.execute("INSERT INTO faces (id, sha256, face_idx, x1, y1, x2, y2, det_score) VALUES (9, 'a', 1, 1, 1, 9, 9, 0.9)")
    conn.commit()
    from photo_organizer.analysis import ark_faces
    ark_faces.name_faces(conn, [9], 'Milo')
    cat_path = tmp_path / 'catalog.db'
    cat = sqlite3.connect(cat_path)
    cat.execute("CREATE TABLE files (sha256 TEXT, camera_make TEXT)")
    cat.executemany("INSERT INTO files VALUES (?, ?)", [('a', None), ('b', 'Apple')])
    cat.commit()
    cat.close()
    found = screenshots.find(conn, str(cat_path))
    assert [(s['sha256'], s['faces'], s['named']) for s in found] == [('a', 2, 1)]
    assert screenshots.forget(conn, found, tmp_path / 'crops') == 2
    assert sorted(r[0] for r in conn.execute("SELECT id FROM faces")) == [2, 3, 4]


def test_the_face_pass_records_no_faces_in_a_screenshot(tmp_path):
    conn = ark.open_db(str(tmp_path / 'analysis.db'))
    p = tmp_path / 'IMG_5920.PNG'
    Image.new('RGB', (1242, 2208), (90, 90, 90)).save(p)
    calls = []

    def face_fn(im):
        calls.append(im.size)
        return [{'bbox': [1, 1, 9, 9], 'det_score': 0.9, 'age': 9, 'gender': 'M',
                 'embedding': np.zeros(512, np.float32)}]
    ark.analyze_one(conn, 'sha', str(p), face_fn)
    assert calls == [], "the face model isn't even run"
    assert conn.execute("SELECT faces_at FROM images WHERE sha256 = 'sha'").fetchone()[0], "checked"
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 0
