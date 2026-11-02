"""Apple Photos library years ('photos:2016') are copied into the store; the library is never changed."""

import datetime
import os
from types import SimpleNamespace

import pytest

import photo_organizer.cli as cli
import photo_organizer.photos_library as pl
from photo_organizer.db import init_db
from photo_organizer.work_queue import enqueue, enqueue_photos, worker
from test_organizer import env, files_under, make_jpeg  # noqa: F401  (env is a fixture)

PT = datetime.timezone(datetime.timedelta(hours=-8))


class FakeLibrary:
    def __init__(self, photos):
        self._photos = photos

    def photos(self, from_date=None, to_date=None):
        return [p for p in self._photos if not from_date or
                (p.date and from_date <= p.date.replace(tzinfo=None) < to_date)]


def asset(lib, uuid, name, when, live=False, edited=False, local=True, raw=False):
    """An asset whose files sit in a fake library folder, named by UUID like the real one."""
    orig = lib / 'originals' / f"{uuid}{os.path.splitext(name)[1].lower()}"
    orig.parent.mkdir(parents=True, exist_ok=True)
    make_jpeg(str(orig), "2001:01:01 00:00:00", (sum(map(ord, uuid)) % 255, 1, 2))   # EXIF loses to Photos' date
    p = SimpleNamespace(uuid=uuid, original_filename=name, date=when, path=str(orig) if local else None,
                        has_raw=raw, path_raw=None, live_photo=live, path_live_photo=None,
                        hasadjustments=edited, path_edited=None,
                        exif_info=SimpleNamespace(camera_make='SONY', camera_model='ILCE-1', lens_model='FE 50mm F1.2 GM'))
    if not local:
        orig.unlink()
    if live:
        p.path_live_photo = lib / 'originals' / f"{uuid}_3.mov"      # a Path, as osxphotos returns for some
        open(p.path_live_photo, 'wb').write(b'mov' + uuid.encode())
    if edited:
        p.path_edited = str(lib / 'renders' / f"{uuid}_1_201_a.heic")
        os.makedirs(os.path.dirname(p.path_edited), exist_ok=True)
        open(p.path_edited, 'wb').write(b'edit' + uuid.encode())
    if raw:
        p.path_raw = str(lib / 'originals' / f"{uuid}_raw.arw")
        open(p.path_raw, 'wb').write(b'raw' + uuid.encode())
    return p


@pytest.fixture
def library(tmp_path, monkeypatch):
    lib = tmp_path / 'Photos Library.photoslibrary'
    photos = []
    fake = FakeLibrary(photos)
    monkeypatch.setattr(pl, 'render_xmp', lambda p: f"<x:xmpmeta>{p.uuid}</x:xmpmeta>")
    monkeypatch.setattr(pl, 'SIDECAR_DIR', str(tmp_path / 'sidecars'))
    monkeypatch.setattr(cli, 'open_library', lambda: fake)
    monkeypatch.setattr('photo_organizer.work_queue.open_library', lambda: fake)
    return lib, photos


def snapshot(root):
    return {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_year_is_copied_with_live_edit_raw_and_sidecar_and_library_untouched(env, library):
    _, dest, go, rows = env
    lib, photos = library
    photos += [asset(lib, 'AAAA', 'IMG_0001.JPG', datetime.datetime(2016, 3, 4, 23, 30, tzinfo=PT),
                     live=True, edited=True),
               asset(lib, 'BBBB', 'IMG_0002.JPG', datetime.datetime(2016, 7, 1, 9, 0, tzinfo=PT), raw=True),
               asset(lib, 'CCCC', 'IMG_0003.JPG', datetime.datetime(2017, 1, 1, 0, 5, tzinfo=PT))]
    before = snapshot(lib)
    totals = go(sources=['photos:2016'])
    assert snapshot(lib) == before
    assert files_under(dest) == [
        '2016/03/2016-03-04/IMG_0001.JPG', '2016/03/2016-03-04/IMG_0001.JPG.xmp',
        '2016/03/2016-03-04/IMG_0001.MOV', '2016/03/2016-03-04/IMG_0001_edited.HEIC',
        '2016/07/2016-07-01/IMG_0002.ARW', '2016/07/2016-07-01/IMG_0002.JPG',
        '2016/07/2016-07-01/IMG_0002.JPG.xmp',
    ]
    assert totals['keep_sources'] and totals['done'] == 7
    assert {r['photos_uuid'] for r in rows().values()} == {'AAAA', 'BBBB'}
    cams = {r['filename']: r['camera'] for r in rows().values()}
    assert cams['IMG_0001.MOV'] == 'Sony A1' and cams['IMG_0001.JPG.xmp'] is None
    assert go(sources=['photos:2016'])['done'] == 0       # a rerun finds nothing new


def test_icloud_only_assets_wait_for_review_and_are_retried(env, library):
    _, dest, go, rows = env
    lib, photos = library
    photos.append(asset(lib, 'DDDD', 'IMG_0004.JPG', datetime.datetime(2020, 5, 5, 12, tzinfo=PT), local=False))
    assert go(sources=['photos:2020'])['review'] == 1
    (row,) = rows().values()
    assert row['status'] == 'review' and row['error_message'] == pl.ICLOUD_ONLY
    make_jpeg(str(lib / 'originals' / 'DDDD.jpg'), "2001:01:01 00:00:00")      # downloaded since
    photos[0].path = str(lib / 'originals' / 'DDDD.jpg')
    go(sources=['photos:2020'])
    assert files_under(dest) == ['2020/05/2020-05-05/IMG_0004.JPG', '2020/05/2020-05-05/IMG_0004.JPG.xmp']


def test_content_already_in_the_store_is_not_copied_again(env, library):
    src, dest, go, rows = env
    lib, photos = library
    p = asset(lib, 'EEEE', 'IMG_0005.JPG', datetime.datetime(2014, 2, 2, 8, tzinfo=PT))
    photos.append(p)
    import shutil
    shutil.copy2(p.path, src / 'IMG_0005.JPG')
    go()                                                  # the same photo arrives from a folder first
    go(sources=['photos:2014'])
    by_kind = {os.path.basename(k): v['status'] for k, v in rows().items()}
    assert by_kind['EEEE.jpg'] == 'duplicate_kept'
    assert os.path.exists(p.path)
    # Its sidecar went next to the copy already in the store, not beside a photo that isn't there.
    assert files_under(dest) == ['2001/01/2001-01-01/IMG_0005.JPG', '2001/01/2001-01-01/IMG_0005.JPG.xmp']


def test_sidecar_of_a_stored_photo_goes_beside_it_even_under_another_name(env, library):
    src, dest, go, rows = env
    lib, photos = library
    p = asset(lib, 'GGGG', 'IMG_0006.JPG', datetime.datetime(2014, 2, 3, 8, tzinfo=PT))
    photos.append(p)
    import shutil
    (src / '2014-02-01').mkdir()
    shutil.copy2(p.path, src / '2014-02-01' / 'renamed.jpg')     # stored under another day and name
    go()
    go(sources=['photos:2014'])
    assert files_under(dest) == ['2001/01/2001-01-01/renamed.jpg', '2001/01/2001-01-01/renamed.jpg.xmp']


def test_queue_takes_photos_years_oldest_first(env, library, tmp_path):
    _, dest, go, rows = env
    lib, photos = library
    photos += [asset(lib, 'F1', 'A.JPG', datetime.datetime(2019, 1, 2, tzinfo=PT)),
               asset(lib, 'F2', 'B.JPG', datetime.datetime(2011, 1, 2, tzinfo=PT))]
    conn = init_db(str(tmp_path / 'local' / 'catalog.db'))
    queued, msg = enqueue_photos(conn)
    assert queued == ['photos:2011', 'photos:2019'] and '2011–2019' in msg
    assert enqueue(conn, 'photos:2011')[0] is None                       # already queued
    assert enqueue(conn, 'photos:twenty')[0] is None
    assert conn.execute("SELECT MIN(keep_sources) FROM queue").fetchone()[0] == 1
    worker(conn, str(dest), str(tmp_path / 'local' / 'catalog.db'), str(tmp_path / 'STOP'), exit_when_empty=True)
    assert [r['status'] for r in conn.execute("SELECT status FROM queue ORDER BY id")] == ['done', 'done']
    assert files_under(dest) == ['2011/01/2011-01-02/B.JPG', '2011/01/2011-01-02/B.JPG.xmp',
                                 '2019/01/2019-01-02/A.JPG', '2019/01/2019-01-02/A.JPG.xmp']
