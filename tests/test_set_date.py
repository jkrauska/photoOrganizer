"""Undated files can be given an EXIF date per folder and then filed."""

import shutil

import pytest

from photo_organizer.db import init_db
from photo_organizer.work_queue import set_folder_date
from test_organizer import env, files_under, make_jpeg  # noqa: F401  (env is a fixture)

pytestmark = pytest.mark.skipif(not shutil.which('exiftool'), reason="exiftool not installed")


def test_set_date_files_a_folders_undated_files_on_the_next_run(env, tmp_path):
    src, dest, go, rows = env
    make_jpeg(f"{src}/wedding/a.jpeg")
    make_jpeg(f"{src}/wedding_other/b.jpeg", color=(9, 9, 9))       # prefix lookalike: untouched
    go()
    assert all(r['status'] == 'review' for r in rows().values())

    conn = init_db(str(tmp_path / 'local' / 'catalog.db'))
    import os
    mtime = os.path.getmtime(src / 'wedding' / 'a.jpeg')
    dated, failed = set_folder_date(conn, str(src / 'wedding'), '2024-01-13')
    assert [os.path.basename(p) for p in dated] == ['a.jpeg'] and failed == []
    assert int(os.path.getmtime(src / 'wedding' / 'a.jpeg')) == int(mtime)   # exiftool -P keeps seconds
    with pytest.raises(ValueError):
        set_folder_date(conn, str(src / 'wedding'), '2024-13-01')
    go()
    assert files_under(dest) == ['2024/01/2024-01-13/a.jpeg']
    by_name = {r['filename']: r for r in rows().values()}
    assert by_name['a.jpeg']['date_source'] == 'exif'
    assert by_name['b.jpeg']['status'] == 'review'
