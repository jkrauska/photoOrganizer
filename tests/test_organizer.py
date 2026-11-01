import datetime
import os
import shutil
import sqlite3
import subprocess

import pytest
from PIL import Image

from photo_organizer.cli import run
from photo_organizer.dates import date_from_dirpath, parse_exif_datetime
from photo_organizer.db import BACKUP_NAME, init_db
from photo_organizer.hashing import sha256_file
from photo_organizer.transfer import recover_interrupted

pytestmark = pytest.mark.skipif(not shutil.which('exiftool'), reason="exiftool not installed")


def make_jpeg(path, date=None, color=(10, 20, 30)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new('RGB', (8, 8), color).save(path, 'JPEG')
    if date:
        subprocess.run(['exiftool', '-q', '-overwrite_original', f'-DateTimeOriginal={date}', path],
                       check=True)
    return path


@pytest.fixture
def env(tmp_path):
    src, dest = tmp_path / 'src', tmp_path / 'dest'
    src.mkdir()
    dest.mkdir()
    db = tmp_path / 'local' / 'catalog.db'

    def go(sources=None, **kw):
        kw.setdefault('execute', True)
        return run([str(s) for s in (sources or [src])], str(dest), str(db), **kw)

    def rows():
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        out = {r['original_path']: dict(r) for r in conn.execute("SELECT * FROM files")}
        conn.close()
        return out

    return src, dest, go, rows


def files_under(root):
    return sorted(os.path.relpath(os.path.join(d, f), root)
                  for d, _, fs in os.walk(root) for f in fs if f != BACKUP_NAME)


def test_same_filename_in_different_folders_keeps_each_date(env):
    src, dest, go, _ = env
    make_jpeg(f"{src}/2021-soccer/JMK06399.jpg", "2021:04:17 16:13:40", (1, 1, 1))
    make_jpeg(f"{src}/2022-trip/JMK06399.jpg", "2022:05:21 10:00:00", (2, 2, 2))
    go()
    assert files_under(dest) == ['2021/04/2021-04-17/JMK06399.jpg', '2022/05/2022-05-21/JMK06399.jpg']
    assert files_under(src) == []
    assert not any(os.scandir(src)), "emptied folders are pruned"


def test_identical_files_in_one_batch_are_copied_once(env):
    src, dest, go, rows = env
    first = make_jpeg(f"{src}/a/IMG_1.jpg", "2020:01:02 03:04:05")
    for d in 'bcdefg':
        os.makedirs(f"{src}/{d}")
        shutil.copy2(first, f"{src}/{d}/IMG_1.jpg")
    go(workers=4)
    assert files_under(dest) == ['2020/01/2020-01-02/IMG_1.jpg']
    assert files_under(src) == []
    statuses = sorted(r['status'] for r in rows().values())
    assert statuses == ['done'] + ['duplicate_removed'] * 6


def test_duplicate_source_kept_when_canonical_copy_fails_verification(env):
    src, dest, go, rows = env
    original = make_jpeg(f"{src}/a/IMG_1.jpg", "2020:01:02 03:04:05")
    keep = shutil.copy2(original, src / 'spare.jpg')
    shutil.move(keep, env[0].parent / 'spare.jpg')
    go()
    canonical = dest / '2020/01/2020-01-02/IMG_1.jpg'
    with open(canonical, 'ab') as f:
        f.write(b'corruption')
    os.makedirs(src / 'b')
    shutil.copy2(env[0].parent / 'spare.jpg', src / 'b' / 'IMG_1.jpg')
    go()
    assert (src / 'b' / 'IMG_1.jpg').exists(), "source must survive a failed canonical check"
    assert rows()[str(src / 'b' / 'IMG_1.jpg')]['status'] == 'error'


def test_uncertain_files_are_left_in_place(env):
    src, dest, go, rows = env
    make_jpeg(f"{src}/misc/scan_0001.jpg")                       # no EXIF, no date in name
    make_jpeg(f"{src}/misc/2015-06-12 22.45.39.jpg", color=(5, 5, 5))  # date only in filename
    make_jpeg(f"{src}/2019/stuff/nodate.jpg", color=(6, 6, 6))   # year-only folder is too vague
    (src / 'misc' / 'notes.txt').write_text('hi')
    go()
    assert files_under(dest) == ['2015/06/2015-06-12/2015-06-12 22.45.39.jpg']
    assert sorted(files_under(src)) == ['2019/stuff/nodate.jpg', 'misc/notes.txt', 'misc/scan_0001.jpg']
    r = rows()
    assert r[str(src / 'misc' / 'scan_0001.jpg')]['status'] == 'review'
    assert r[str(src / 'misc' / 'notes.txt')]['status'] == 'ignored'


def test_bundles_and_nas_system_folders_are_untouched(env):
    src, dest, go, rows = env
    make_jpeg(f"{src}/Photos Library.photoslibrary/originals/x.jpg", "2020:01:01 00:00:00")
    make_jpeg(f"{src}/@Recycle/y.jpg", "2020:01:01 00:00:00", (3, 3, 3))
    make_jpeg(f"{src}/Tahoe.cocatalog/Cache/z.jpg", "2020:01:01 00:00:00", (4, 4, 4))
    go()
    assert files_under(dest) == []
    assert len(files_under(src)) == 3
    assert rows() == {}


def test_sidecars_follow_their_photo(env):
    src, dest, go, _ = env
    make_jpeg(f"{src}/d/IMG_1.jpg", "2020:07:04 12:00:00")
    (src / 'd' / 'IMG_1.jpg.xmp').write_text('<x/>')
    (src / 'd' / 'IMG_1.xmp').write_text('<y/>')
    go(batch_size=1)
    assert files_under(dest) == ['2020/07/2020-07-04/IMG_1.jpg', '2020/07/2020-07-04/IMG_1.jpg.xmp',
                                 '2020/07/2020-07-04/IMG_1.xmp']


def test_prior_run_mtime_guess_is_not_trusted(env, tmp_path):
    src, dest, go, rows = env
    old = tmp_path / 'photosCleanup2026'
    guessed = make_jpeg(f"{old}/2019/03/2019-03-05/guess.jpg")
    solid = make_jpeg(f"{old}/2018/01/2018-01-09/solid.jpg", color=(9, 9, 9))
    prior = sqlite3.connect(old / 'photo_organizer.db')
    prior.execute("CREATE TABLE files (original_path TEXT, dest_path TEXT, date_source TEXT, status TEXT)")
    prior.execute("INSERT INTO files VALUES ('/ssd/a/guess.jpg', ?, 'mtime', 'done')", (guessed,))
    prior.execute("INSERT INTO files VALUES ('/ssd/b/solid.jpg', ?, 'filename', 'done')", (solid,))
    prior.commit()
    prior.close()
    go(sources=[old])
    assert files_under(dest) == ['2018/01/2018-01-09/solid.jpg']
    r = rows()
    assert r[guessed]['status'] == 'review'
    assert r[solid]['origin_path'] == '/ssd/b/solid.jpg'


def test_dry_run_changes_nothing_and_terminates(env):
    src, dest, go, rows = env
    for i in range(5):
        make_jpeg(f"{src}/d{i}/IMG_{i}.jpg", f"2020:01:0{i + 1} 00:00:00", (i, i, i))
    go(execute=False, batch_size=1)
    assert files_under(dest) == []
    assert len(files_under(src)) == 5
    assert all(r['dest_path'] for r in rows().values())
    go(batch_size=2)
    assert len(files_under(dest)) == 5
    assert files_under(src) == []


def test_interrupted_partial_copy_is_recovered(env, tmp_path):
    src, dest, go, rows = env
    photo = make_jpeg(f"{src}/a/IMG_1.jpg", "2020:01:02 03:04:05")
    go(execute=False)
    planned = rows()[photo]['dest_path']
    os.makedirs(os.path.dirname(planned))
    with open(planned, 'wb') as f:
        f.write(b'partial')
    conn = init_db(str(tmp_path / 'local' / 'catalog.db'))
    conn.execute("UPDATE files SET status = 'copying', sha256 = ?", (sha256_file(photo),))
    conn.commit()
    assert recover_interrupted(conn) == 1
    conn.close()
    go()
    assert sha256_file(planned) == rows()[photo]['sha256']
    assert not os.path.exists(photo)


def test_status_snapshot_reflects_finished_run(env, tmp_path):
    from photo_organizer.status import snapshot
    src, dest, go, _ = env
    make_jpeg(f"{src}/a/IMG_1.jpg", "2020:01:02 03:04:05")
    make_jpeg(f"{src}/a/undated.jpg", color=(7, 7, 7))
    go()
    snap = snapshot(str(tmp_path / 'local' / 'catalog.db'))
    assert snap['run']['phase'] == 'finished' and not snap['run']['alive']
    assert {s['status']: s['files'] for s in snap['statuses']} == {'done': 1, 'review': 1}
    assert snap['rates']['1m']['files'] == 1
    assert snap['recent'][0]['status'] in ('done', 'review')


def test_video_container_dates_utc_unless_camera_writes_local():
    from photo_organizer.dates import pick_capture_date
    sony = {'CreateDate': '2012:02:19 15:28:53', 'MajorBrand': 'MPEG-4 (.MP4) for SonyPSP'}
    assert pick_capture_date(sony, True)[0].isoformat() == '2012-02-19T15:28:53'
    iphone = {'CreateDate': '2012:02:21 03:35:31', 'MajorBrand': 'Apple QuickTime (.MOV/QT)'}
    local = pick_capture_date(iphone, True)[0]
    assert local.astimezone(datetime.timezone.utc).isoformat() == '2012-02-21T03:35:31+00:00'
    zoned = {'CreationDate': '2019:07:04 21:10:00-07:00', 'CreateDate': '2019:07:05 04:10:00'}
    assert pick_capture_date(zoned, True)[0].isoformat() == '2019-07-04T21:10:00-07:00'
    photo = {'CreateDate': '2010:05:01 08:00:00'}
    assert pick_capture_date(photo, False)[0].isoformat() == '2010-05-01T08:00:00'


def test_parse_exif_datetime_keeps_offset():
    dt = parse_exif_datetime("2019:01:01 11:59:32-07:00")
    assert dt.isoformat() == "2019-01-01T11:59:32-07:00"
    assert parse_exif_datetime("0000:00:00 00:00:00") is None


def test_dirpath_needs_full_date():
    assert date_from_dirpath("/s/2019/03/x.jpg", "/s") is None
    assert str(date_from_dirpath("/s/2019/2019-03-05/x.jpg", "/s")) == "2019-03-05"
