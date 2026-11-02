"""Camera and lens tags of stored files, read in batches (on the NAS in real runs)."""

import sqlite3

from photo_organizer.cameras import backfill, camera_name, unread
from photo_organizer.db import init_db


def test_friendly_camera_names():
    assert camera_name('SONY', 'ILCE-1') == 'Sony A1'
    assert camera_name('SONY', 'ILCE-9') == 'Sony A9'
    assert camera_name('SONY', 'ILCE-9M3') == 'Sony A9 III'
    assert camera_name('SONY', 'ILCE-7RM4') == 'Sony A7R IV'
    assert camera_name('SONY', 'ILCE-7M2') == 'Sony A7 II'
    assert camera_name('SONY', 'ILCE-1M2') == 'Sony A1 II'
    assert camera_name('SONY', 'SLT-A65V') == 'Sony A65'
    assert camera_name('SONY', 'DSC-RX100M3') == 'Sony RX100 III'
    assert camera_name('Apple', 'iPhone 17 Pro') == 'iPhone 17 Pro'
    assert camera_name('Canon', 'Canon PowerShot S3 IS') == 'Canon PowerShot S3 IS'
    assert camera_name('NIKON CORPORATION', 'NIKON D750') == 'Nikon D750'
    assert camera_name('FUJIFILM', 'X-T4') == 'Fujifilm X-T4'


class FakeFS:
    target = 'nas'

    def remote(self, path):
        return path.replace('/Volumes/photoArk', '/mnt/pool/photoArk')


class FakeReader:
    def __init__(self, tags):
        self.tags, self.batches, self.closed = tags, [], False

    def read(self, paths):
        self.batches.append(list(paths))
        return {p: {'SourceFile': p, **self.tags[p]} for p in paths if p in self.tags}

    def close(self):
        self.closed = True


def test_backfill_reads_in_batches_records_every_file_and_resumes(tmp_path):
    conn = init_db(str(tmp_path / 'catalog.db'))
    names = [f"IMG_{i}.JPG" for i in range(45)] + ['IMG_0.JPG.xmp']
    for n in names:
        conn.execute("INSERT INTO files (filename, original_path, status, dest_path) VALUES (?, ?, 'done', ?)",
                     (n, f"/src/{n}", f"/Volumes/photoArk/2024/01/2024-01-01/{n}"))
    conn.commit()
    tags = {f"/mnt/pool/photoArk/2024/01/2024-01-01/IMG_{i}.JPG":
            {'Make': 'SONY', 'Model': 'ILCE-1', 'LensModel': 'FE 70-200mm F2.8 GM OSS'} for i in range(40)}
    reader = FakeReader(tags)
    lines = []
    assert backfill(conn, FakeFS(), batch=20, readers=[reader], out=lines.append) == (45, 40)
    assert [len(b) for b in reader.batches] == [20, 20, 5] and reader.closed     # sidecars aren't read
    assert sum('/45]' in line for line in lines) == 3
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT camera, lens_model, camera_read_at FROM files WHERE filename LIKE '%.JPG'").fetchall()
    assert sum(r['camera'] == 'Sony A1' for r in rows) == 40
    assert all(r['camera_read_at'] for r in rows)                  # untagged files are not re-read
    assert unread(conn) == [] and backfill(conn, FakeFS(), readers=[FakeReader({})], out=lines.append) == (0, 0)
