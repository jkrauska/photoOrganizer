"""Large jobs start moving quickly and can be split into one queue item per subfolder."""

import os
import shutil

import pytest

from photo_organizer.db import init_db
from photo_organizer.scanner import PriorCatalogs, iter_source_files, scan_batch
from photo_organizer.work_queue import enqueue_split, worker
from test_organizer import env, files_under, make_jpeg  # noqa: F401  (env is a fixture)

pytestmark = pytest.mark.skipif(not shutil.which('exiftool'), reason="exiftool not installed")


def _scan(tmp_path, root, limit, max_bytes=1 << 40):
    conn = init_db(str(tmp_path / 'scan.db'))
    it = iter_source_files([str(root)], str(tmp_path / 'dest'), set())
    sizes = []
    while True:
        ids, exhausted = scan_batch(it, conn, set(), PriorCatalogs(), limit, max_bytes=max_bytes)
        if ids:
            sizes.append(len(ids))
        if exhausted or not ids:
            return sizes, conn


def test_big_folder_splits_between_photos_never_between_photo_and_sidecar(tmp_path):
    root = tmp_path / 'big'
    root.mkdir()
    for i in range(10):
        (root / f"IMG_{i}.jpg").write_bytes(b'x')
        (root / f"IMG_{i}.xmp").write_bytes(b'x')
    sizes, conn = _scan(tmp_path, root, limit=3)
    assert sum(sizes) == 20 and len(sizes) > 1
    assert all(n % 2 == 0 for n in sizes), "each photo stays with its sidecar"


def test_batch_ends_after_its_byte_budget_at_a_folder_boundary(tmp_path):
    root = tmp_path / 'videos'
    for d in 'abc':
        (root / d).mkdir(parents=True)
        (root / d / 'clip.mov').write_bytes(b'x' * 1000)
    sizes, _ = _scan(tmp_path, root, limit=100, max_bytes=1500)
    assert sizes == [2, 1]


def test_split_queues_each_subfolder_then_the_top_folder(env, tmp_path):
    src, dest, go, rows = env
    make_jpeg(f"{src}/2019/a.jpg", "2019:01:01 10:00:00", (1, 1, 1))
    make_jpeg(f"{src}/2020/b.jpg", "2020:01:01 10:00:00", (2, 2, 2))
    make_jpeg(f"{src}/top.jpg", "2021:01:01 10:00:00", (3, 3, 3))
    os.makedirs(f"{src}/@eaDir")
    db = str(tmp_path / 'local' / 'catalog.db')
    conn = init_db(db)
    queued, message = enqueue_split(conn, str(src), str(dest))
    assert queued == [f"{src}/2019", f"{src}/2020", str(src)]
    assert message == "queued 2 subfolders + the top folder"
    worker(conn, str(dest), db, str(tmp_path / 'STOP'), exit_when_empty=True)
    assert len(files_under(dest)) == 3
    assert [r[0] for r in conn.execute("SELECT status FROM queue ORDER BY id")] == ['done'] * 3
