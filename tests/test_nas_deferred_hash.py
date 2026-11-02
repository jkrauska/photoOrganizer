"""NAS mode moves content that can't be a duplicate without a full hash; the hash comes later."""

import os
import shutil

import pytest

from photo_organizer.db import init_db
from photo_organizer.hashing import sha256_file
from photo_organizer.transfer import HASH_PENDING, backfill_hashes, hashes_pending
from test_organizer import env, files_under, local_nas, make_jpeg  # noqa: F401  (env is a fixture)

pytestmark = pytest.mark.skipif(not shutil.which('exiftool'), reason="exiftool not installed")


@pytest.fixture
def nas_env(env, tmp_path, monkeypatch):
    import photo_organizer.cli as cli
    monkeypatch.setattr(cli, 'detect_nas', lambda sources, d: (local_nas(tmp_path), None))
    src, dest, go, rows = env
    db = str(tmp_path / 'local' / 'catalog.db')

    def connect():
        return init_db(db)
    return src, dest, go, rows, connect


def test_unique_size_is_moved_without_hash_then_backfilled(nas_env, tmp_path):
    src, dest, go, rows, connect = nas_env
    a = make_jpeg(f"{src}/IMG_1.jpg", "2020:01:02 03:04:05")
    want = sha256_file(a)
    go()
    (row,) = rows().values()
    assert row['status'] == 'done' and row['sha256'] is None and row['error_message'] == HASH_PENDING
    assert not src.exists()

    conn = connect()
    assert hashes_pending(conn) == 1
    fs = local_nas(tmp_path)
    try:
        assert backfill_hashes(conn, fs) == (1, 0)
    finally:
        fs.close()
    (row,) = rows().values()
    assert row['sha256'] == want and row['error_message'] is None
    assert hashes_pending(conn) == 0


def test_same_size_arrival_hashes_the_unhashed_file_first(nas_env, tmp_path):
    src, dest, go, rows, _ = nas_env
    a = make_jpeg(f"{src}/IMG_1.jpg", "2020:01:02 03:04:05")
    later = tmp_path / 'later'
    later.mkdir()
    shutil.copy2(a, later / 'copy-of-IMG_1.jpg')
    go()                                        # moved as a clone, no hash
    go(sources=[later])                         # identical content arrives
    assert files_under(dest) == ['2020/01/2020-01-02/IMG_1.jpg']
    by_name = {os.path.basename(k): v for k, v in rows().items()}
    assert by_name['IMG_1.jpg']['sha256'] is not None
    assert by_name['copy-of-IMG_1.jpg']['status'] == 'duplicate_removed'
    assert not later.exists()


def test_backfill_removes_a_store_copy_whose_content_is_already_stored(nas_env, tmp_path):
    src, dest, go, rows, connect = nas_env
    make_jpeg(f"{src}/IMG_1.jpg", "2020:01:02 03:04:05")
    go()
    (first,) = rows().values()
    twin = dest / '2020/01/2020-01-02/IMG_1_twin.jpg'
    shutil.copy2(first['dest_path'], twin)
    conn = connect()
    conn.execute("INSERT INTO files (original_path, filename, file_size, status, dest_path) "
                 "VALUES ('/gone/IMG_1_twin.jpg', 'IMG_1_twin.jpg', ?, 'done', ?)",
                 (twin.stat().st_size, str(twin)))
    conn.commit()
    fs = local_nas(tmp_path)
    try:
        assert backfill_hashes(conn, fs) == (2, 1)
    finally:
        fs.close()
    assert files_under(dest) == ['2020/01/2020-01-02/IMG_1.jpg']
    statuses = sorted(r['status'] for r in rows().values())
    assert statuses == ['done', 'duplicate_removed']


def test_quick_hash_is_the_same_on_the_mac_and_the_nas(tmp_path):
    from photo_organizer.hashing import QUICK_SAMPLE, quick_hash
    fs = local_nas(tmp_path)
    try:
        for size in (0, 10, QUICK_SAMPLE + 5, 3 * QUICK_SAMPLE + 7):
            f = tmp_path / f"f{size}"
            f.write_bytes(os.urandom(size))
            assert quick_hash(str(f)) == fs.quick_hash(str(f))
        a, b = tmp_path / 'a', tmp_path / 'b'
        a.write_bytes(b'x' * (3 * QUICK_SAMPLE))
        b.write_bytes(b'x' * (3 * QUICK_SAMPLE - 1) + b'y')
        assert quick_hash(str(a)) != quick_hash(str(b)), "the last byte is sampled"
    finally:
        fs.close()


def test_same_size_but_different_content_is_moved_without_full_hash(nas_env):
    src, dest, go, rows, _ = nas_env
    for i in range(3):      # same pixel size and date, different colors: often the same file size
        make_jpeg(f"{src}/IMG_{i}.jpg", "2020:01:02 03:04:05", (i * 80, 0, 0))
    os.truncate(f"{src}/IMG_1.jpg", os.path.getsize(f"{src}/IMG_0.jpg"))
    os.truncate(f"{src}/IMG_2.jpg", os.path.getsize(f"{src}/IMG_0.jpg"))
    go()
    stored = rows().values()
    assert all(r['status'] == 'done' and r['sha256'] is None and r['quick_hash'] for r in stored)
    assert len(files_under(dest)) == 3
