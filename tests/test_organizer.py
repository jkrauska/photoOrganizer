import datetime
import json
import os
import shutil
import sqlite3
import subprocess

import numpy as np
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
        kw.setdefault('stop_file', str(tmp_path / 'STOP'))
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
    assert not src.exists(), "an emptied source folder is removed along with its subfolders"


def test_source_folder_kept_while_anything_remains(env):
    src, dest, go, _ = env
    make_jpeg(f"{src}/a/IMG_1.jpg", "2020:01:02 03:04:05")
    (src / '.DS_Store').write_text('junk')
    (src / 'keep.txt').write_text('unhandled type stays')
    go()
    assert sorted(os.listdir(src)) == ['.DS_Store', 'keep.txt'], "emptied subfolder removed, root kept"


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


def test_run_only_moves_its_own_sources(env, tmp_path):
    src, dest, go, rows = env
    other = tmp_path / 'other'
    make_jpeg(f"{other}/d/IMG_9.jpg", "2019:09:09 09:09:09", (9, 1, 1))
    make_jpeg(f"{src}/d/IMG_1.jpg", "2020:01:02 03:04:05")
    go(sources=[other], execute=False)       # dry run plans the other folder
    go()                                      # execute on src only
    assert files_under(dest) == ['2020/01/2020-01-02/IMG_1.jpg']
    assert os.path.exists(f"{other}/d/IMG_9.jpg")
    assert rows()[f"{other}/d/IMG_9.jpg"]['status'] == 'pending'


def test_stop_file_stops_after_current_batch_and_rerun_resumes(env, tmp_path):
    src, dest, go, rows = env
    for i in range(4):
        make_jpeg(f"{src}/d{i}/IMG_{i}.jpg", f"2020:02:0{i + 1} 00:00:00", (i, 2 * i, 3))
    stop = tmp_path / 'STOP'
    batches = []

    def confirm(_prompt):
        batches.append(1)
        if len(batches) == 2:
            stop.touch()        # stop requested once batch 1 is done, before batch 2 transfers
        return True

    go(batch_size=1, confirm=confirm)
    assert len(batches) == 2, "no further batch starts after a stop request"
    assert len(files_under(dest)) == 1, "batch 1 completed; batch 2 transferred nothing"
    assert len(files_under(src)) == 3, "untouched files stay in place"
    go(batch_size=1)                  # rerun clears the stale stop file and finishes
    assert len(files_under(dest)) == 4 and files_under(src) == []


def test_unique_size_skips_prehash_but_same_size_duplicates_are_caught(env):
    src, dest, go, rows = env
    a = make_jpeg(f"{src}/a/IMG_1.jpg", "2020:03:03 03:03:03", (1, 2, 3))
    make_jpeg(f"{src}/b/OTHER.jpg", "2020:03:04 03:03:03", (200, 100, 50))
    os.makedirs(src / 'c')
    shutil.copy2(a, src / 'c' / 'IMG_1.jpg')
    go(workers=4)
    assert files_under(dest) == ['2020/03/2020-03-03/IMG_1.jpg', '2020/03/2020-03-04/OTHER.jpg']
    assert sorted(r['status'] for r in rows().values()) == ['done', 'done', 'duplicate_removed']
    assert all(r['sha256'] for r in rows().values())


def test_time_machine_deny_acl_is_stripped_only_after_verified_copy(env):
    src, dest, go, rows = env
    photo = make_jpeg(f"{src}/a/IMG_1.jpg", "2020:05:05 05:05:05")
    subprocess.run(['chmod', '+a', 'group:everyone deny write,delete,append,writeattr,writeextattr,chown',
                    photo], check=True)
    go()
    assert files_under(dest) == ['2020/05/2020-05-05/IMG_1.jpg']
    assert not os.path.exists(photo)
    assert rows()[photo]['status'] == 'done' and rows()[photo]['error_message'] is None


def test_undeletable_source_stays_done_and_is_retried(env):
    src, dest, go, rows = env
    photo = make_jpeg(f"{src}/a/IMG_1.jpg", "2020:05:05 05:05:05")
    os.chmod(src / 'a', 0o555)              # folder not writable: the delete fails, not via an ACL
    try:
        go()
        r = rows()[photo]
        assert r['status'] == 'done' and r['error_message'].startswith('source not removed')
        assert os.path.exists(photo) and files_under(dest) == ['2020/05/2020-05-05/IMG_1.jpg']
    finally:
        os.chmod(src / 'a', 0o755)
    go()                                    # next run re-verifies the copy and removes the source
    assert not os.path.exists(photo) and rows()[photo]['error_message'] is None


def test_queue_worker_processes_folders_in_order(env, tmp_path):
    from photo_organizer.db import init_db
    from photo_organizer.work_queue import add, worker
    src, dest, go, rows = env
    other = tmp_path / 'other'
    make_jpeg(f"{src}/a/IMG_1.jpg", "2020:06:06 06:06:06", (1, 1, 1))
    make_jpeg(f"{other}/b/IMG_2.jpg", "2021:07:07 07:07:07", (2, 2, 2))
    db = str(tmp_path / 'local' / 'catalog.db')
    conn = init_db(db)
    assert add(conn, [str(src), str(other), str(src), str(tmp_path / 'missing')]) == [str(src), str(other)]
    worker(conn, str(dest), db, str(tmp_path / 'STOP'), exit_when_empty=True)
    assert files_under(dest) == ['2020/06/2020-06-06/IMG_1.jpg', '2021/07/2021-07-07/IMG_2.jpg']
    assert [tuple(r) for r in conn.execute("SELECT status, transfer FROM queue ORDER BY id")] == \
        [('done', 'Mac'), ('done', 'Mac')], "each queued folder records how its files moved"
    assert not src.exists() and not other.exists()


def test_second_run_is_refused_while_one_holds_the_catalog(env, tmp_path):
    from photo_organizer.db import RunLocked, acquire_run_lock
    src, dest, go, _ = env
    held = acquire_run_lock(str(tmp_path / 'local' / 'catalog.db'))
    try:
        with pytest.raises(RunLocked):
            go()
    finally:
        os.close(held)


def test_archive_summary_counts_by_year_month_and_day(env, tmp_path):
    from photo_organizer.archive import archive_snapshot, month_detail
    src, dest, go, _ = env
    make_jpeg(f"{src}/a/IMG_1.jpg", "2019:03:02 10:00:00", (1, 1, 1))
    make_jpeg(f"{src}/a/IMG_2.jpg", "2019:03:02 11:00:00", (2, 2, 2))
    make_jpeg(f"{src}/b/IMG_3.jpg", "2020:12:25 09:00:00", (3, 3, 3))
    go()
    db = str(tmp_path / 'local' / 'catalog.db')
    snap = archive_snapshot(db)
    assert snap['total']['files'] == 3 and snap['total']['photo'] == 3
    assert [(y['year'], y['files']) for y in snap['years']] == [('2019', 2), ('2020', 1)]
    assert set(snap['months']) == {'2019-03', '2020-12'}
    days = month_detail(db, '2019-03')['days']
    assert [(d['day'], d['files']) for d in days] == [('2019-03-02', 2)]
    assert month_detail(db, "x' OR 1=1 --")['days'] == []


def test_status_page_queue_endpoints_only_accept_same_origin_json(tmp_path):
    import http.client
    import json as _json
    import threading
    from http.server import ThreadingHTTPServer
    from photo_organizer.db import init_db
    from photo_organizer.status import make_handler
    db = str(tmp_path / 'catalog.db')
    init_db(db).close()
    folder = tmp_path / 'dump'
    folder.mkdir()
    server = ThreadingHTTPServer(('127.0.0.1', 0), None)
    port = server.server_address[1]
    server.RequestHandlerClass = make_handler(db, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def req(method, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', port)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read()

    ok_headers = {'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{port}'}
    body = _json.dumps({'path': str(folder)})
    try:
        assert req('POST', '/api/queue', body, {'Content-Type': 'application/x-www-form-urlencoded'})[0] == 403
        assert req('POST', '/api/queue', body, {**ok_headers, 'Origin': 'http://evil.example'})[0] == 403
        assert req('POST', '/api/queue', body, {**ok_headers, 'Host': 'evil.example:80'})[0] == 403
        status, resp = req('POST', '/api/queue', body, ok_headers)
        assert status == 200 and _json.loads(resp)['ok']
        assert req('POST', '/api/queue', body, ok_headers)[0] == 400          # already queued
        assert req('POST', '/api/queue', _json.dumps({'path': str(tmp_path / 'nope')}), ok_headers)[0] == 400
        dirs = _json.loads(req('GET', f'/api/dirs?path={tmp_path}')[1])['dirs']
        assert str(folder) in dirs
        qid = init_db(db).execute("SELECT id FROM queue").fetchone()[0]
        assert req('DELETE', f'/api/queue/{qid}', headers={'Origin': 'http://evil.example'})[0] == 403
        assert req('DELETE', f'/api/queue/{qid}')[0] == 200
        assert init_db(db).execute("SELECT COUNT(*) FROM queue").fetchone()[0] == 0
    finally:
        server.shutdown()


def test_enqueue_rejects_folders_overlapping_the_store(tmp_path):
    from photo_organizer.db import init_db
    from photo_organizer.work_queue import enqueue
    store = tmp_path / 'store'
    (store / '2020').mkdir(parents=True)
    conn = init_db(str(tmp_path / 'c.db'))
    assert enqueue(conn, str(store / '2020'), str(store))[0] is None
    assert enqueue(conn, str(tmp_path), str(store))[0] is None
    assert enqueue(conn, str(store / '..' / 'store' / '2020'), str(store))[0] is None


def test_copy_keeps_mtime_but_not_local_flags(tmp_path):
    from photo_organizer.hashing import copy_and_hash
    UF_TRACKED = 0x40                         # set by macOS document tracking; SMB rejects it
    src = tmp_path / 'tracked.arw'
    src.write_bytes(b'raw bytes')
    os.utime(src, (1_600_000_000, 1_600_000_000))
    os.chflags(src, UF_TRACKED)
    dst = tmp_path / 'copy.arw'
    copy_and_hash(str(src), str(dst))
    assert dst.stat().st_mtime == 1_600_000_000
    assert dst.stat().st_flags & UF_TRACKED == 0


def test_copy_and_hash_never_overwrites(tmp_path):
    from photo_organizer.hashing import copy_and_hash
    (tmp_path / 'a').write_bytes(b'new')
    (tmp_path / 'b').write_bytes(b'existing')
    with pytest.raises(FileExistsError):
        copy_and_hash(str(tmp_path / 'a'), str(tmp_path / 'b'))
    assert (tmp_path / 'b').read_bytes() == b'existing'


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


def test_copy_only_keeps_sources_and_records_duplicates(env):
    src, dest, go, rows = env
    a = make_jpeg(f"{src}/a/IMG_1.jpg", "2020:08:08 08:08:08", (8, 8, 8))
    os.makedirs(src / 'b')
    b = shutil.copy2(a, src / 'b' / 'IMG_1.jpg')
    go(keep_sources=True)
    assert files_under(dest) == ['2020/08/2020-08-08/IMG_1.jpg']
    assert os.path.exists(a) and os.path.exists(b), "copy-only never deletes"
    assert sorted(r['status'] for r in rows().values()) == ['done', 'duplicate_kept']


def test_time_machine_paths_are_always_copy_only(env, tmp_path):
    from photo_organizer.transfer import remove_source
    src, dest, go, rows = env
    tm = tmp_path / 'TM' / 'Backups.backupdb' / 'mbp' / '2015-08-06' / 'Desktop'
    photo = make_jpeg(f"{tm}/2015-07-04/IMG_9.jpg", "2015:07:04 12:00:00", (4, 7, 4))
    go(sources=[tm])                          # keep_sources not requested
    assert files_under(dest) == ['2015/07/2015-07-04/IMG_9.jpg']
    assert os.path.exists(photo), "nothing inside a Time Machine backup is deleted"
    with pytest.raises(PermissionError):
        remove_source(photo)


def test_queue_marks_time_machine_folders_copy_only(tmp_path):
    from photo_organizer.db import init_db
    from photo_organizer.work_queue import enqueue
    tm = tmp_path / 'Backups.backupdb' / 'x'
    tm.mkdir(parents=True)
    plain = tmp_path / 'plain'
    plain.mkdir()
    conn = init_db(str(tmp_path / 'c.db'))
    enqueue(conn, str(tm))
    enqueue(conn, str(plain))
    assert [r[0] for r in conn.execute("SELECT keep_sources FROM queue ORDER BY id")] == [1, 0]


def test_large_file_copy_reports_progress(tmp_path, monkeypatch):
    from photo_organizer import hashing
    monkeypatch.setattr(hashing, 'LARGE_FILE', 1024)
    src = tmp_path / 'big.mp4'
    src.write_bytes(os.urandom(20 * 1024 * 1024 + 17))
    seen = []
    digest = hashing.copy_and_hash(str(src), str(tmp_path / 'copy.mp4'), progress=seen.append)
    assert digest == hashing.sha256_file(str(src)) == hashing.sha256_file(str(tmp_path / 'copy.mp4'))
    assert seen[-1] == src.stat().st_size and seen == sorted(seen) and len(seen) > 1


def test_run_tracks_large_files_end_to_end(env, monkeypatch):
    from photo_organizer import hashing, transfer
    monkeypatch.setattr(hashing, 'LARGE_FILE', 1024)
    monkeypatch.setattr(transfer, 'LARGE_FILE', 1024)
    src, dest, go, rows = env
    photo = make_jpeg(f"{src}/a/IMG_1.jpg", "2020:09:09 09:09:09", (9, 0, 9))
    with open(photo, 'ab') as f:
        f.write(os.urandom(256 * 1024))      # well over the patched 1 KB threshold
    go()
    assert files_under(dest) == ['2020/09/2020-09-09/IMG_1.jpg']
    assert rows()[photo]['status'] == 'done' and not os.path.exists(photo)


def test_worker_ignores_a_stale_stop_file(env, tmp_path):
    from photo_organizer.db import init_db
    from photo_organizer.work_queue import add, worker
    src, dest, go, rows = env
    make_jpeg(f"{src}/a/IMG_1.jpg", "2020:10:10 10:10:10", (10, 10, 10))
    stop = tmp_path / 'STOP'
    stop.touch()                              # left behind by an earlier `make stop`
    db = str(tmp_path / 'local' / 'catalog.db')
    conn = init_db(db)
    add(conn, [str(src)])
    worker(conn, str(dest), db, str(stop), exit_when_empty=True)
    assert files_under(dest) == ['2020/10/2020-10-10/IMG_1.jpg']


def test_prune_removes_folders_left_with_only_thumbnail_caches(tmp_path):
    from photo_organizer.cleanup import prune_empty_dirs
    root = tmp_path / 'Before 2010'
    for m in ('05', '11'):
        thumb = root / '2006' / m / '.@__thumb'
        thumb.mkdir(parents=True)
        (thumb / f's800IMG_{m}.JPG').write_bytes(b'thumb')
    (root / '2006' / '.DS_Store').write_bytes(b'x')
    keep = root / '2007' / '01'
    (keep / '@eaDir').mkdir(parents=True)
    (keep / 'IMG_1.JPG').write_bytes(b'photo')

    prune_empty_dirs(str(root))

    assert not (root / '2006').exists()
    assert (keep / 'IMG_1.JPG').exists() and (keep / '@eaDir').exists()


def local_nas(root):
    """A NasFS whose helper runs here instead of over ssh, with the identity path mapping."""
    import sys
    from photo_organizer.nas import HELPER, NasFS
    return NasFS('test-nas', [(str(root), str(root))], command=[sys.executable, '-u', '-c', HELPER])


def test_nas_mode_moves_verifies_and_dedups_on_the_server(env, tmp_path, monkeypatch):
    import photo_organizer.cli as cli
    src, dest, go, rows = env
    monkeypatch.setattr(cli, 'detect_nas', lambda sources, d: (local_nas(tmp_path), None))
    a = make_jpeg(f"{src}/a/IMG_1.jpg", "2020:01:02 03:04:05")
    os.utime(a, (1_600_000_000, 1_600_000_000))
    shutil.copy2(a, f"{src}/b-IMG_1.jpg")
    (src / 'a' / '.@__thumb').mkdir()
    (src / 'a' / '.@__thumb' / 'IMG_1.jpg').write_bytes(b'thumb')
    want = sha256_file(a)
    go()
    stored = files_under(dest)
    assert len(stored) == 1 and stored[0].startswith('2020/01/2020-01-02/')
    copy = dest / stored[0]
    assert sha256_file(str(copy)) == want and copy.stat().st_mtime == 1_600_000_000
    assert sorted(r['status'] for r in rows().values()) == ['done', 'duplicate_removed']
    assert not src.exists()


def test_nas_helper_refuses_unmapped_paths_and_mismatched_sizes(tmp_path):
    fs = local_nas(tmp_path / 'share')
    (tmp_path / 'share').mkdir()
    f = tmp_path / 'share' / 'x.bin'
    f.write_bytes(b'12345')
    try:
        with pytest.raises(ValueError):
            fs.exists(str(tmp_path / 'elsewhere' / 'x.bin'))
        with pytest.raises(OSError, match='catalog size'):
            fs.copy_and_hash(str(f), str(tmp_path / 'share' / 'y.bin'), 4)
        assert not (tmp_path / 'share' / 'y.bin').exists()
        with pytest.raises(FileExistsError):
            fs.copy_and_hash(str(f), str(f), 5)
        assert f.read_bytes() == b'12345'
    finally:
        fs.close()


def test_nas_detection_maps_shares_by_listing():
    from photo_organizer import nas

    class FakeFS:
        def __init__(self, target, mapping):
            self.target, self.mapping = target, mapping

        def call(self, op, **kw):
            return {'ping': {}, 'candidates': {
                'photoArk': [['/mnt/Pool/photoArk', ['2018']], ['/mnt/Pool/old/photoArk', ['x']]],
                'fileshares': [['/mnt/Pool/fileshares', ['photos']]]}.get(kw.get('share'))}[op] \
                if op == 'candidates' else {}

        def close(self):
            pass

    mounts = {'/Volumes/photoArk': ('me', 'nas.local', 'photoArk'),
              '/Volumes/fileshares': ('me', 'nas.local', 'fileshares'),
              '/Volumes/other': ('me', 'other.local', 'x')}
    listings = {'/Volumes/photoArk': ['2018'], '/Volumes/fileshares': ['photos']}
    orig = nas._visible_names
    nas._visible_names = listings.__getitem__
    try:
        fs, _ = nas.detect(['/Volumes/fileshares/photos/Raw'], '/Volumes/photoArk', mounts, FakeFS)
        assert fs.target == 'me@nas.local'
        assert fs.mapping == [('/Volumes/fileshares', '/mnt/Pool/fileshares'),
                              ('/Volumes/photoArk', '/mnt/Pool/photoArk')]
        assert nas.detect(['/Volumes/other/a'], '/Volumes/photoArk', mounts, FakeFS) == (None, None)
        assert nas.detect(['/Users/me/Pictures'], '/Volumes/photoArk', mounts, FakeFS) == (None, None)
        assert nas.same_server(['/Volumes/fileshares/x'], '/Volumes/photoArk', mounts) == 'nas.local'
    finally:
        nas._visible_names = orig


def _ark_catalog(tmp_path, rows):
    from photo_organizer.db import init_db
    db = str(tmp_path / 'catalog.db')
    conn = init_db(db)
    for i, (path, sha, status) in enumerate(rows):
        conn.execute("INSERT INTO files (original_path, filename, file_size, status, sha256, dest_path) "
                     "VALUES (?, ?, 1, ?, ?, ?)", (f"/src/{i}", os.path.basename(path), status, sha, path))
    conn.commit()
    conn.close()
    return db


def test_ark_selects_stored_images_once_per_hash(tmp_path):
    from photo_organizer.analysis.ark import select_images
    db = _ark_catalog(tmp_path, [
        ('/store/2010/01/2010-01-01/a.jpg', 'h1', 'done'),
        ('/store/2010/01/2010-01-01/a_1.jpg', 'h1', 'done'),      # same content, second name
        ('/store/2010/02/2010-02-02/b.ARW', 'h2', 'done'),
        ('/store/2010/02/2010-02-02/c.mov', 'h3', 'done'),        # video: not analyzed
        ('/store/2010/03/2010-03-03/d.jpg', 'h4', 'error'),
        ('/store/2011/01/2011-01-01/e.jpg', 'h5', 'done'),
        ('/store/20100/x.jpg', 'h6', 'done'),                     # prefix must match a folder
    ])
    assert select_images(db, year=2010, dest='/store') == [
        ('h1', '/store/2010/01/2010-01-01/a.jpg'), ('h2', '/store/2010/02/2010-02-02/b.ARW')]


def test_ark_analysis_is_oriented_resumable_per_stage_and_keyed_by_hash(tmp_path):
    from photo_organizer.analysis import ark
    img = tmp_path / 'p.jpg'
    exif = Image.Exif()
    exif[0x0112] = 6                                             # stored landscape, shown portrait
    Image.new('RGB', (40, 20), (200, 10, 10)).save(img, 'JPEG', exif=exif)
    conn = ark.open_db(str(tmp_path / 'analysis.db'))
    seen = []

    def faces(im):
        seen.append(im.size)
        return [{'bbox': [1, 2, 11, 22], 'det_score': 0.9, 'age': 30, 'gender': 'F',
                 'embedding': np.ones(512, dtype=np.float32) / np.sqrt(512)}]

    def vlm(path):
        assert Image.open(path).size == (20, 40)
        return {'caption': 'a red square', 'scene': 'indoor', 'keywords': ['red', 7], 'subjects': ['square']}

    images = [('h1', str(img))]
    assert ark.run(conn, images, face_fn=faces, log=lambda s: None) == 1
    assert seen == [(20, 40)], "faces are found in the EXIF-oriented image"
    r = conn.execute("SELECT * FROM images").fetchone()
    assert (r['width'], r['height'], r['source'], r['vlm_at']) == (20, 40, 'file', None)
    assert ark.run(conn, images, face_fn=faces, log=lambda s: None) == 0, "faces stage resumes"
    assert ark.run(conn, images, face_fn=faces, vlm_fn=vlm, vlm_name='m', log=lambda s: None) == 1
    assert seen == [(20, 40)], "a later describe pass does not redo faces"
    r = conn.execute("SELECT * FROM images").fetchone()
    assert r['caption'] == 'a red square' and json.loads(r['keywords']) == ['red'] and r['vlm_model'] == 'm'
    f = conn.execute("SELECT * FROM faces").fetchone()
    assert (f['sha256'], f['x2'], f['y2'], len(f['embedding'])) == ('h1', 11, 22, 512 * 4)


def test_ark_seed_names_suggests_without_touching_manual_labels(tmp_path):
    from photo_organizer.analysis import ark
    conn = ark.open_db(str(tmp_path / 'analysis.db'))
    rng = np.random.default_rng(0)
    unit = lambda v: (v / np.linalg.norm(v)).astype(np.float32)
    alice, bob = unit(rng.normal(size=512)), unit(rng.normal(size=512))
    near = lambda v: unit(v + 0.3 * rng.normal(size=512) / 22)
    refs = {'Alice': {'cluster': [near(alice) for _ in range(5)], 'manual': []},
            'Bob': {'cluster': [], 'manual': [near(bob) for _ in range(3)]},
            # a chained cluster of strangers plus one hand-named face: only the latter is used
            'Dan': {'cluster': [unit(rng.normal(size=512)) for _ in range(20)], 'manual': [near(alice)]}}
    centroids = ark.reference_centroids(refs)
    assert 'too mixed' in centroids['Dan'][2] and centroids['Dan'][1] == 1
    del refs['Dan']
    centroids = ark.reference_centroids(refs)
    conn.execute("INSERT INTO images (sha256, path) VALUES ('h', '/p')")
    for i, e in enumerate([alice, bob, unit(rng.normal(size=512)), bob]):
        conn.execute("INSERT INTO faces (sha256, face_idx, det_score, embedding) VALUES ('h', ?, 0.9, ?)",
                     (i, e.tobytes()))
    pid = ark._person_id(conn, 'Carol')
    conn.execute("UPDATE faces SET person_id = ?, person_source = 'manual' WHERE face_idx = 3", (pid,))
    assert ark.seed_names(conn, centroids) == {'Alice': 1, 'Bob': 1}
    got = {r['face_idx']: (r['name'], r['person_source']) for r in conn.execute(
        "SELECT f.face_idx, p.name, f.person_source FROM faces f LEFT JOIN persons p ON p.id = f.person_id")}
    assert got == {0: ('Alice', 'suggested'), 1: ('Bob', 'suggested'), 2: (None, None), 3: ('Carol', 'manual')}
    assert ark.seed_names(conn, centroids) == {'Alice': 1, 'Bob': 1}, "re-seeding replaces old suggestions"


def _ark_face_db(tmp_path):
    """Analysis DB with one oriented image: faces 1-3 in cluster 0, face 4 unclustered."""
    from photo_organizer.analysis import ark
    img = tmp_path / 'p.jpg'
    exif = Image.Exif()
    exif[0x0112] = 6                                             # stored 80x40, shown 40x80
    im = Image.new('RGB', (80, 40), (0, 0, 255))
    im.paste((255, 0, 0), (0, 0, 40, 40))                        # left half red -> top half once turned
    im.save(img, 'JPEG', exif=exif)
    db = str(tmp_path / 'analysis.db')
    conn = ark.open_db(db)
    conn.execute("INSERT INTO images (sha256, path, width, height) VALUES ('h', ?, 40, 80)", (str(img),))
    for idx, (cluster, box) in enumerate([(0, (5, 5, 35, 35)), (0, (5, 45, 35, 75)),
                                          (0, (0, 0, 40, 40)), (None, (0, 40, 40, 80))]):
        conn.execute("INSERT INTO faces (id, sha256, face_idx, x1, y1, x2, y2, det_score, cluster_id) "
                     "VALUES (?, 'h', ?, ?, ?, ?, ?, ?, ?)", (idx + 1, idx, *box, 0.9 - idx / 10, cluster))
    conn.commit()
    return db, conn


def _ark_names(conn):
    return {r['id']: (r['name'], r['person_source']) for r in conn.execute(
        "SELECT f.id, p.name, f.person_source FROM faces f LEFT JOIN persons p ON p.id = f.person_id")}


def test_ark_faces_lists_clusters_and_names_clusters_and_faces(tmp_path):
    from photo_organizer.analysis import ark_faces
    db, conn = _ark_face_db(tmp_path)
    got = ark_faces.list_clusters(conn)
    assert [(c['id'], c['count'], c['label']) for c in got] == [('c0', 3, ''), ('unclustered', 1, '')]
    assert got[0]['sample'] == '/face/1.jpg', "sample is the best detection"
    assert [f['id'] for f in ark_faces.cluster_detail(conn, 'unclustered')] == [4]
    with pytest.raises(KeyError):
        ark_faces.cluster_detail(conn, 'x1')

    ark_faces.name_faces(conn, [2], 'Bob')                       # an individual override
    assert ark_faces.name_cluster(conn, 'c0', 'Alice', only=[1, 2, 4]) == 1, "only the faces shown"
    assert _ark_names(conn)[3] == (None, None)
    assert ark_faces.name_cluster(conn, 'c0', 'Alice') == 1, "the rest on the next save"
    assert _ark_names(conn) == {1: ('Alice', 'manual'), 2: ('Bob', 'manual'),
                                3: ('Alice', 'manual'), 4: (None, None)}
    assert ark_faces.list_clusters(conn)[0]['label'] == 'Alice'
    assert ark_faces.name_cluster(conn, 'c0', 'Alicia') == 2, "a rename follows the cluster's name"
    assert _ark_names(conn)[2] == ('Bob', 'manual') and _ark_names(conn)[1] == ('Alicia', 'manual')
    with pytest.raises(ValueError):
        ark_faces.name_cluster(conn, 'unclustered', 'X')
    ark_faces.name_faces(conn, [4], ' Carol ')
    ark_faces.name_faces(conn, [2], '')
    assert _ark_names(conn)[4] == ('Carol', 'manual') and _ark_names(conn)[2] == (None, None)
    assert ark_faces.persons(conn) == ['Alice', 'Alicia', 'Bob', 'Carol']


def test_ark_faces_accepts_and_rejects_suggestions_and_reseed_skips_rejected(tmp_path):
    from photo_organizer.analysis import ark, ark_faces
    db, conn = _ark_face_db(tmp_path)
    emb = np.full(512, 1 / np.sqrt(512), dtype=np.float32)
    conn.execute("UPDATE faces SET embedding = ?", (emb.tobytes(),))
    conn.commit()
    assert ark.seed_names(conn, {'Dee': (emb, 1, '')}) == {'Dee': 4}
    assert ark_faces.accept_suggestions(conn, [1, 2]) == 2
    assert ark_faces.reject_suggestions(conn, [3, 1]) == 1, "only suggested faces can be rejected"
    assert _ark_names(conn) == {1: ('Dee', 'manual'), 2: ('Dee', 'manual'),
                                3: (None, 'rejected'), 4: ('Dee', 'suggested')}
    assert ark.seed_names(conn, {'Dee': (emb, 1, '')}) == {'Dee': 1}
    assert _ark_names(conn)[3] == (None, 'rejected'), "a rejected suggestion is not made again"


def test_ark_faces_crops_from_the_oriented_image(tmp_path):
    from photo_organizer.analysis import ark_faces
    db, conn = _ark_face_db(tmp_path)
    crops = tmp_path / 'crops'
    top = Image.open(ark_faces.ensure_crop(conn, 3, crops)).convert('RGB')
    bottom = Image.open(ark_faces.ensure_crop(conn, 4, crops)).convert('RGB')
    assert top.size == (40, 50), "padded, clipped to the image"
    r, g, b = top.getpixel((20, 15))
    assert r > 200 and b < 60, "bbox is in pixels of the EXIF-oriented image"
    r, g, b = bottom.getpixel((20, 35))
    assert b > 200 and r < 60
    assert len(list(crops.iterdir())) == 2
    import io
    full = Image.open(io.BytesIO(ark_faces.full_image(conn, 3))).convert('RGB')
    assert full.size == (40, 80), "the whole photo, oriented"
    with pytest.raises(KeyError):
        ark_faces.ensure_crop(conn, 99, crops)


def test_ark_faces_forget_deletes_faces_and_crops_but_keeps_named_ones(tmp_path):
    from photo_organizer.analysis import ark_faces
    db, conn = _ark_face_db(tmp_path)
    crops = tmp_path / 'crops'
    ark_faces.ensure_crop(conn, 4, crops)
    assert ark_faces.forget_faces(conn, [4, 99], crops) == 1
    assert not list(crops.iterdir()), "a forgotten face's crop goes too"
    ark_faces.name_faces(conn, [1], 'Alice')
    conn.execute("UPDATE faces SET person_id = 1, person_source = 'suggested' WHERE id = 2")
    conn.commit()
    assert ark_faces.forget_cluster(conn, 'c0', crops) == 1
    assert sorted(_ark_names(conn)) == [1, 2]
    assert [c['id'] for c in ark_faces.list_clusters(conn)] == ['c0']


def test_ark_face_browser_app_names_over_http(tmp_path):
    pytest.importorskip('fastapi')
    pytest.importorskip('httpx')
    from fastapi.testclient import TestClient
    from photo_organizer.analysis.face_browser import make_ark_app
    db, conn = _ark_face_db(tmp_path)
    client = TestClient(make_ark_app(db, tmp_path / 'crops'))
    assert [c['id'] for c in client.get('/api/clusters').json()] == ['c0', 'unclustered']
    assert client.post('/api/label', json={'cluster_id': 'c0', 'label': 'Alice'}).json()['changed'] == 3
    assert client.post('/api/label_face', json={'face_id': 4, 'label': 'Bob'}).status_code == 200
    assert client.post('/api/label_face', json={'face_id': 99, 'label': 'Bob'}).status_code == 404
    assert client.post('/api/label_faces', json={'face_ids': [1, 2], 'label': 'Cy'}).json()['changed'] == 2
    assert _ark_names(conn)[1] == ('Cy', 'manual') and _ark_names(conn)[2] == ('Cy', 'manual')
    assert client.post('/api/label_faces', json={'face_ids': [2], 'label': ' '}).json()['changed'] == 1
    assert _ark_names(conn)[2] == (None, None)
    client.post('/api/label_faces', json={'face_ids': [2], 'label': 'Cy'})
    assert client.get('/api/cluster/c9x').status_code == 404
    assert client.get('/face/1.jpg').headers['content-type'] == 'image/jpeg'
    assert client.get('/photo/1.jpg').headers['content-type'] == 'image/jpeg'
    assert client.get('/photo/99.jpg').status_code == 404
    assert client.get('/api/persons').json() == ['Alice', 'Bob', 'Cy']
    assert client.post('/api/forget', json={'face_ids': [4]}).json()['deleted'] == 1
    client.post('/api/label_face', json={'face_id': 3, 'label': ''})
    assert client.post('/api/forget', json={'cluster_id': 'c0'}).json()['deleted'] == 1
    assert client.post('/api/forget', json={'cluster_id': 'bad'}).status_code == 404
    assert client.get('/api/cluster/c0/guess').json() is None
    assert client.post('/api/guess_no', json={'cluster_id': 'c0', 'name': 'Nobody'}).status_code == 404


def test_ark_cluster_splits_chains_of_look_alikes(tmp_path):
    from sklearn.cluster import DBSCAN
    from photo_organizer.analysis import ark
    rng = np.random.default_rng(1)
    u, v = np.linalg.qr(rng.normal(size=(512, 2)))[0].T
    # four people 55 degrees apart on a circle: neighbours are within eps 0.5 (cosine distance
    # 0.43), so plain DBSCAN chains all four into one cluster
    faces = []
    for k in range(4):
        t = np.radians(55 * k)
        centre = np.cos(t) * u + np.sin(t) * v
        for _ in range(10):
            e = centre + 0.02 * rng.normal(size=512) / np.sqrt(512)
            faces.append((k, (e / np.linalg.norm(e)).astype(np.float32)))
    X = np.stack([e for _, e in faces])
    assert len(set(DBSCAN(eps=0.5, min_samples=3, metric='cosine').fit_predict(X))) == 1
    conn = ark.open_db(str(tmp_path / 'analysis.db'))
    conn.execute("INSERT INTO images (sha256, path) VALUES ('h', '/p')")
    for i, (_, e) in enumerate(faces):
        conn.execute("INSERT INTO faces (sha256, face_idx, det_score, embedding) VALUES ('h', ?, 0.9, ?)",
                     (i, e.tobytes()))
    assert ark.cluster_faces(conn) == 4
    got = [r[0] for r in conn.execute("SELECT cluster_id FROM faces ORDER BY face_idx")]
    assert [len(set(got[k * 10:(k + 1) * 10])) for k in range(4)] == [1, 1, 1, 1]
    assert len(set(got)) == 4


def test_ark_faces_guesses_a_named_person_for_a_cluster_until_told_no(tmp_path):
    from photo_organizer.analysis import ark, ark_faces
    conn = ark.open_db(str(tmp_path / 'analysis.db'))
    rng = np.random.default_rng(2)
    unit = lambda v: (v / np.linalg.norm(v)).astype(np.float32)
    alice, bob = unit(rng.normal(size=512)), unit(rng.normal(size=512))
    near = lambda v: unit(v + 0.5 * rng.normal(size=512) / np.sqrt(512))
    conn.execute("INSERT INTO images (sha256, path) VALUES ('h', '/p')")
    faces = [(0, alice, 'Alice')] * 4 + [(1, bob, 'Bob')] * 4 + [(2, alice, None)] * 5 + [(3, None, None)] * 5
    for i, (cluster, centre, name) in enumerate(faces):
        e = near(centre) if centre is not None else unit(rng.normal(size=512))
        conn.execute("INSERT INTO faces (id, sha256, face_idx, det_score, cluster_id, embedding) "
                     "VALUES (?, 'h', ?, 0.9, ?, ?)", (i + 1, i, cluster, e.tobytes()))
        if name:
            ark_faces.name_faces(conn, [i + 1], name)
    assert ark_faces.guess_cluster(conn, 'c0') is None, "already named"
    assert ark_faces.guess_cluster(conn, 'c3') is None, "looks like nobody"
    assert ark_faces.guess_cluster(conn, 'unclustered') is None
    g = ark_faces.guess_cluster(conn, 'c2')
    assert g['name'] == 'Alice' and g['score'] > 0.55
    assert ark_faces.not_this_person(conn, 'c2', 'Alice') == 5
    assert ark_faces.guess_cluster(conn, 'c2') is None, "a No is remembered"
    ark_faces.forget_faces(conn, [9])
    assert conn.execute("SELECT COUNT(*) FROM face_not_person").fetchone()[0] == 4


def test_ark_days_split_into_bursts_and_sample_each_burst():
    from datetime import datetime, timedelta
    from photo_organizer.analysis.days import plan_samples, split_bursts
    t0 = datetime(2010, 6, 5, 9, 0)
    items = ([(f'a{i}', f'a{i}.jpg', t0 + timedelta(minutes=i)) for i in range(30)]               # 09:00-09:29
             + [(f'b{i}', f'b{i}.jpg', t0 + timedelta(hours=3, minutes=i)) for i in range(2)]     # 12:00-12:01
             + [(f'c{i}', f'c{i}.jpg', t0 + timedelta(hours=8, minutes=10 * i)) for i in range(12)]  # 17:00-18:50
             + [('u', 'u.jpg', None)])
    bursts = split_bursts(items, gap_minutes=45)
    assert [len(b) for b in bursts] == [30, 2, 12, 1]
    samples = plan_samples(bursts, per_burst=3, max_samples=7)
    assert [len(s) for s in samples] == [3, 1, 2, 1]                     # every burst is seen
    assert samples[0] == [5, 15, 25]                                      # spread across the burst
    assert [len(s) for s in plan_samples(bursts, per_burst=3, max_samples=2)] == [1, 0, 1, 0]
    game = [(f'g{i}', f'g{i}.jpg', t0 + timedelta(minutes=2 * i)) for i in range(150)]   # one 5-hour burst
    assert [len(s) for s in plan_samples(split_bursts(game), per_burst=3, max_samples=9)] == [9]


def test_ark_days_summarize_random_days_once_from_sampled_captions(tmp_path):
    from photo_organizer.analysis import ark, days
    from photo_organizer.db import init_db
    db = str(tmp_path / 'catalog.db')
    cat = init_db(db)
    n = 0
    for day, count in [('2010-06-05', 6), ('2010-07-01', 1), ('2011-01-02', 2)]:
        d = tmp_path / 'store' / day[:4] / day[5:7] / day
        d.mkdir(parents=True)
        for i in range(count):
            p = d / f'{i}.jpg'
            Image.new('RGB', (8, 8), (i * 30, 0, 0)).save(p)
            n += 1
            cat.execute("INSERT INTO files (original_path, filename, file_size, status, sha256, dest_path, taken_at) "
                        "VALUES (?, ?, 1, 'done', ?, ?, ?)",
                        (f'/src/{n}', p.name, f'h{n}', str(p), f'{day}T1{i}:00:00'))
    cat.commit()
    cat.close()
    found = days.select_days(db, prefix=str(tmp_path / 'store' / '2010'))
    assert sorted(len(v) for v in found.values()) == [1, 6]
    conn = ark.open_db(str(tmp_path / 'analysis.db'))
    captioned, prompts = [], []

    def vlm(path):
        captioned.append(path)
        return {'caption': f'frame {len(captioned)}', 'keywords': ['beach']}

    def summary(prompt):
        prompts.append(prompt)
        return {'title': 'Beach day', 'summary': 'A day at the beach.', 'keywords': ['beach', 3]}

    assert days.run_days(conn, found, vlm, summary, 'gemma', seed=1, max_samples=4, log=lambda s: None) == 1
    assert len(captioned) == 4                       # 4 of the 6 hourly bursts; the single-photo day is skipped
    big = next(p for p in prompts if '6 photos' in p)
    assert 'Saturday' in big and '6 burst(s)' in big and 'frame' in big and '[beach]' in big
    row = conn.execute("SELECT * FROM days WHERE n_images = 6").fetchone()
    assert row['title'] == 'Beach day' and json.loads(row['keywords']) == ['beach']
    assert sum(len(b['samples']) for b in json.loads(row['bursts'])) == 4
    assert days.run_days(conn, found, vlm, summary, 'gemma', log=lambda s: None) == 0   # done days skipped
    assert len(captioned) == 4


def test_ark_spread_order_takes_a_few_images_from_every_year_per_round(tmp_path):
    from photo_organizer.analysis.ark import select_images, spread_order
    rows = [(f'/store/{y}/01/{y}-01-01/{i}.jpg', f'{y}-{i}', 'done')
            for y, n in ((2003, 25), (2010, 3), (2021, 12)) for i in range(n)]
    db = _ark_catalog(tmp_path, rows + [('/store/misc/x.jpg', 'm', 'done')])
    images = select_images(db, dest='/store')
    assert len(images) == 41, "no year or prefix: the whole store"
    out = spread_order(images, per_year=10, seed=1)
    assert sorted(out) == sorted(images)
    years = [p.split('/')[2] for _, p in out]
    first = years[:24]                       # round 1: up to 10 from each year, plus the yearless file
    assert (first.count('2003'), first.count('2010'), first.count('2021'), first.count('misc')) == (10, 3, 10, 1)
    assert years[24:36].count('2003') == 10 and years[24:36].count('2021') == 2
    assert years[36:] == ['2003'] * 5
    assert out != spread_order(images, per_year=10, seed=2), "random within a year"


def test_ark_day_browser_searches_summaries_and_captions_and_opens_only_known_days(tmp_path):
    pytest.importorskip('fastapi')
    pytest.importorskip('httpx')
    from fastapi.testclient import TestClient
    from photo_organizer.analysis import ark, days
    from photo_organizer.analysis.day_browser import make_app
    day = tmp_path / '2010' / '06' / '2010-06-05'
    day.mkdir(parents=True)
    Image.new('RGB', (40, 20), (0, 90, 200)).save(day / 'a.jpg')
    db = str(tmp_path / 'analysis.db')
    conn = ark.open_db(db)
    days.init(conn)
    conn.execute("INSERT INTO images (sha256, path, caption) VALUES ('h1', ?, 'an orange tabby cat on a rug')",
                 (str(day / 'a.jpg'),))
    for d, date, title, kw in [(str(day), '2010-06-05', 'Beach day', ['beach', 'surf']),
                               ('/x/2011/01/2011-01-02', '2011-01-02', 'Ski trip', ['snow'])]:
        conn.execute("INSERT INTO days (day, date, n_images, n_bursts, bursts, title, summary, keywords, people, "
                     "summarized_at) VALUES (?, ?, 1, 1, ?, ?, 'x', ?, '[]', 'now')",
                     (d, date, json.dumps([{'start': '10:00', 'end': '10:00', 'n': 1,
                                            'samples': ['h1'] if d == str(day) else []}]), title, json.dumps(kw)))
    conn.commit()
    opened = []
    client = TestClient(make_app(db, tmp_path / 'thumbs', opener=opened.append), client=('127.0.0.1', 5000))
    remote = TestClient(make_app(db, tmp_path / 'thumbs', opener=opened.append), client=('192.168.1.9', 5000))
    assert remote.post('/api/open', json={'day': str(day)}).status_code == 403     # Finder is on this Mac
    assert remote.get('/api/local').json() == {'local': False}
    assert client.get('/api/local').json() == {'local': True}
    assert client.get('/api/days').json()['total'] == 2
    assert [d['title'] for d in client.get('/api/days', params={'q': 'surf'}).json()['items']] == ['Beach day']
    assert [d['title'] for d in client.get('/api/days', params={'q': 'tabby cat'}).json()['items']] == ['Beach day']
    assert client.get('/api/days', params={'q': 'tabby snow'}).json()['total'] == 0
    assert [d['title'] for d in client.get('/api/days', params={'year': '2011'}).json()['items']] == ['Ski trip']
    assert client.get('/api/years').json() == ['2010', '2011']
    assert client.get('/thumb/h1.jpg').headers['content-type'] == 'image/jpeg'
    assert (tmp_path / 'thumbs' / 'h1.jpg').exists()
    assert client.get('/thumb/nope.jpg').status_code == 404
    assert client.post('/api/open', json={'day': str(day)}).status_code == 200
    assert client.post('/api/open', json={'day': str(day), 'sha': 'h1'}).status_code == 200
    assert opened == [[str(day)], ['-R', str(day / 'a.jpg')]]
    assert client.post('/api/open', json={'day': '/etc'}).status_code == 404
    assert client.post('/api/open', json={'day': '/x/2011/01/2011-01-02'}).status_code == 404   # not mounted
    assert client.get('/').status_code == 200
    assert client.post('/api/wrong', json={'day': str(day), 'note': 'St. Louis'}).status_code == 200
    assert client.post('/api/wrong', json={'day': '/nope'}).status_code == 404
    beach = client.get('/api/days', params={'q': 'surf'}).json()['items'][0]
    assert beach['wrong_at'] and beach['wrong_note'] == 'St. Louis'
    assert beach['context'] == '' and not beach['context_pending']
    assert client.post('/api/context', json={'day': str(day), 'context': 'Aunt Jo visiting'}).status_code == 200
    assert client.post('/api/context', json={'day': '/nope', 'context': 'x'}).status_code == 404
    beach = client.get('/api/days', params={'q': 'aunt jo'}).json()['items'][0]    # context is searchable
    assert beach['context'] == 'Aunt Jo visiting' and beach['context_pending']


def test_ark_faces_cluster_detail_lists_unnamed_faces_before_named_ones(tmp_path):
    from photo_organizer.analysis import ark_faces
    db, conn = _ark_face_db(tmp_path)
    ark_faces.name_faces(conn, [1], 'Zed')
    ark_faces.name_faces(conn, [3], 'Amy')
    assert [f['id'] for f in ark_faces.cluster_detail(conn, 'c0')] == [2, 3, 1]


def test_ark_faces_cluster_detail_bands_unnamed_faces_by_similarity_in_shooting_order(tmp_path):
    from photo_organizer.analysis import ark, ark_faces
    conn = ark.open_db(str(tmp_path / 'analysis.db'))
    rng = np.random.default_rng(3)
    unit = lambda v: (v / np.linalg.norm(v)).astype(np.float32)
    alice = unit(rng.normal(size=512))
    near = lambda: unit(alice + 0.3 * rng.normal(size=512) / np.sqrt(512))
    r = rng.normal(size=512)
    other = unit(r - (r @ alice) * alice)                         # orthogonal to Alice
    stray = lambda: unit(0.45 * alice + 0.89 * other + 0.01 * rng.normal(size=512) / np.sqrt(512))  # ~0.45
    # (face id, file, embedding, named): files named against their shooting order (see taken)
    faces = [(1, 'a.jpg', near(), True), (2, 'b.jpg', near(), True), (3, 'c.jpg', near(), True),
             (4, 'z.jpg', near(), False), (5, 'y.jpg', near(), False),
             (6, 'x.jpg', stray(), False), (7, 'w.jpg', stray(), False)]
    for fid, name, e, named in faces:
        conn.execute("INSERT INTO images (sha256, path) VALUES (?, ?)", (f's{fid}', f'/s/2019/01/2019-01-05/{name}'))
        conn.execute("INSERT INTO faces (id, sha256, face_idx, det_score, cluster_id, embedding) "
                     "VALUES (?, ?, 0, 0.9, 0, ?)", (fid, f's{fid}', e.tobytes()))
        if named:
            ark_faces.name_faces(conn, [fid], 'Alice')
    cat_path = tmp_path / 'catalog.db'
    cat = sqlite3.connect(cat_path)
    cat.execute("CREATE TABLE files (sha256 TEXT, taken_at TEXT)")
    cat.executemany("INSERT INTO files VALUES (?, ?)", [('s4', '2019-01-05T09:00:00'), ('s5', '2019-01-05T10:00:00'),
                                                       ('s6', '2019-01-05T11:00:00'), ('s7', '2019-01-05T12:00:00')])
    cat.commit()
    cat.close()

    got = ark_faces.cluster_detail(conn, 'c0', catalog_path=str(cat_path))
    assert [f['id'] for f in got] == [4, 5, 6, 7, 1, 2, 3], "similar band first, each band in shooting order"
    assert got[0]['sim'] > 0.8 and got[0]['sim_to'] == 'Alice' and got[0]['taken'] == '2019-01-05T09:00:00'
    assert 0.4 < got[2]['sim'] < 0.5 and 'sim' not in got[4]
    # Without a catalog the store's date folders and filenames decide within a band.
    assert [f['id'] for f in ark_faces.cluster_detail(conn, 'c0')][:4] == [5, 4, 7, 6]
    assert [f['id'] for f in ark_faces.cluster_detail(conn, 'c0', limit=2)] == [5, 4]


def test_ark_faces_cluster_detail_shows_a_burst_of_the_same_second_once(tmp_path):
    from photo_organizer.analysis import ark, ark_faces
    conn = ark.open_db(str(tmp_path / 'analysis.db'))
    e = np.full(512, 1 / np.sqrt(512), dtype=np.float32).tobytes()
    # (face id, image, box, det): 1-3 one burst; 4 same second but elsewhere in the frame;
    # 5 same place a second later; 6 a second face in image a
    faces = [(1, 'a', (10, 10, 50, 50), 0.8), (2, 'b', (12, 11, 52, 50), 0.95), (3, 'c', (9, 10, 49, 52), 0.7),
             (4, 'd', (200, 10, 240, 50), 0.9), (5, 'e', (10, 10, 50, 50), 0.9), (6, 'a', (14, 12, 54, 52), 0.6)]
    for fid, sha, (x1, y1, x2, y2), det in faces:
        conn.execute("INSERT OR IGNORE INTO images (sha256, path) VALUES (?, ?)", (sha, f'/s/2019/01/2019-01-05/{sha}.jpg'))
        conn.execute("INSERT INTO faces (id, sha256, face_idx, x1, y1, x2, y2, det_score, cluster_id, embedding) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)", (fid, sha, fid, x1, y1, x2, y2, det, e))
    cat_path = tmp_path / 'catalog.db'
    cat = sqlite3.connect(cat_path)
    cat.execute("CREATE TABLE files (sha256 TEXT, taken_at TEXT)")
    cat.executemany("INSERT INTO files VALUES (?, ?)", [(s, '2019-01-05T10:00:00-08:00') for s in 'abcd']
                    + [('e', '2019-01-05T10:00:01-08:00')])
    cat.commit()
    cat.close()
    got = {f['id']: f['burst'] for f in ark_faces.cluster_detail(conn, 'c0', catalog_path=str(cat_path))}
    assert got[2] == [1, 3], "the best detection leads its burst"
    assert got[4] == [] and got[5] == [] and got[6] == [], "elsewhere, a second later, or the same photo"
    assert 1 not in got and 3 not in got


def test_ark_days_marked_wrong_are_redone_first_with_more_frames_and_the_note(tmp_path):
    from datetime import datetime, timedelta
    from photo_organizer.analysis import ark, days
    t0 = datetime(2018, 9, 22, 13, 0)
    d = tmp_path / '2018-09-22'
    d.mkdir()
    items = []
    for i in range(40):                                       # one 2.6-hour burst
        p = d / f'{i}.jpg'
        Image.new('RGB', (8, 8), (i, 0, 0)).save(p)
        items.append((f'g{i}', str(p), t0 + timedelta(minutes=4 * i)))
    found = {str(d): items}
    conn = ark.open_db(str(tmp_path / 'analysis.db'))
    prompts = []
    vlm = lambda path: {'caption': 'a ballpark'}

    def summary(prompt):
        prompts.append(prompt)
        return {'title': f'Game {len(prompts)}', 'summary': 'At a ballgame.'}

    assert days.run_days(conn, found, vlm, summary, log=lambda s: None) == 1
    first = conn.execute("SELECT bursts FROM days").fetchone()[0]
    assert len(json.loads(first)[0]['samples']) == 6          # 3 per two hours
    assert 'several descriptions agree' in prompts[0] and 'marked wrong' not in prompts[0]
    assert days.run_days(conn, found, vlm, summary, log=lambda s: None) == 0
    assert days.mark_wrong(conn, str(d), 'Busch Stadium, St. Louis')
    assert not days.mark_wrong(conn, '/elsewhere')
    assert days.run_days(conn, found, vlm, summary, log=lambda s: None) == 1
    assert '"Game 1: At a ballgame."' in prompts[1] and 'Busch Stadium, St. Louis' in prompts[1]
    row = conn.execute("SELECT * FROM days").fetchone()
    assert row['title'] == 'Game 2' and row['attempt'] == 1 and row['wrong_at'] is None
    assert row['wrong_note'] == 'Busch Stadium, St. Louis' and row['prev_summary'] == 'Game 1: At a ballgame.'
    assert len(json.loads(row['bursts'])[0]['samples']) == 12   # twice the frames on a redo

    days.set_context(conn, str(d), ' Cardinals vs Cubs with Grandpa ')    # context: redone, same frames
    assert days.run_days(conn, found, vlm, summary, log=lambda s: None) == 1
    assert 'Context from the person who took the photos' in prompts[2] and 'Cardinals vs Cubs' in prompts[2]
    assert 'Busch Stadium, St. Louis' in prompts[2], "the earlier correction is kept"
    row = conn.execute("SELECT * FROM days").fetchone()
    assert row['attempt'] == 1 and len(json.loads(row['bursts'])[0]['samples']) == 12
    assert row['context_used'] == 'Cardinals vs Cubs with Grandpa'
    days.set_context(conn, str(d), 'Cardinals vs Cubs with Grandpa')       # unchanged: nothing to do
    assert days.run_days(conn, found, vlm, summary, log=lambda s: None) == 0
    days.set_context(conn, str(d), '')                                    # removed: redone without it
    assert days.run_days(conn, found, vlm, summary, log=lambda s: None) == 1
    assert 'Context from' not in prompts[3] and days.get_context(conn, str(d)) is None


def test_a_failed_claim_never_leaves_same_content_workers_waiting(env, monkeypatch):
    """A catalog write failing (e.g. 'database is locked') after a worker took the in-flight slot for
    some content must still release it; workers holding the same content once waited forever."""
    import shutil
    import sqlite3 as sq
    import threading
    import photo_organizer.transfer as transfer
    src, dest, go, rows = env
    a = make_jpeg(f"{src}/a/IMG_1.jpg", "2019:03:02 10:00:00")
    for d in 'bcd':
        (src / d).mkdir()
        shutil.copy2(a, src / d / 'IMG_1.jpg')
    real, calls = transfer._claim_dest, []

    def flaky(ctx, fid, planned, src_hash):
        calls.append(fid)
        if src_hash and len(calls) == 1:
            raise sq.OperationalError('database is locked')
        return real(ctx, fid, planned, src_hash)
    monkeypatch.setattr(transfer, '_claim_dest', flaky)
    t = threading.Thread(target=go, kwargs={'workers': 4}, daemon=True)
    t.start()
    t.join(60)
    assert not t.is_alive(), "transfer hung"
    assert files_under(dest) == ['2019/03/2019-03-02/IMG_1.jpg']
    statuses = sorted(r['status'] for r in rows().values())
    assert statuses.count('error') == 1 and statuses.count('done') == 1


def test_names_differing_only_in_case_get_separate_files(env):
    src, dest, go, rows = env
    make_jpeg(f"{src}/a/IMG_1.HEIC.jpg", "2019:03:02 10:00:00", (1, 2, 3))
    make_jpeg(f"{src}/b/IMG_1.heic.jpg", "2019:03:02 10:00:00", (9, 8, 7))
    go(workers=2)
    assert len(files_under(dest)) == 2
    assert all(r['status'] == 'done' for r in rows().values())
