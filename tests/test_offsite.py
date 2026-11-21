import datetime as dt
import sqlite3

from photo_organizer import offsite
from photo_organizer.db import init_db


def test_big_years_split_by_month_and_big_months_into_capped_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(offsite, 'UNIT_MAX_BYTES', 100)
    for rel, size in [('2019/04/2019-04-01/a.mov', 60), ('2019/04/2019-04-01/b.mov', 30),
                      ('2019/04/2019-04-02/c.mov', 60), ('2019/05/2019-05-01/d.jpg', 10), ('2020/01/2020-01-01/e.jpg', 5)]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b'x' * size)
    units = {u.name: [p for p, _ in u.files] for u in offsite.plan_units(str(tmp_path))}
    assert units == {'2019-04.1': ['2019/04/2019-04-01/a.mov', '2019/04/2019-04-01/b.mov'],
                     '2019-04.2': ['2019/04/2019-04-02/c.mov'], '2019-05': ['2019/05/2019-05-01/d.jpg'],
                     '2020': ['2020/01/2020-01-01/e.jpg']}
    assert [u.name for u in offsite.plan_units(str(tmp_path), {'2019-04'})] == ['2019-04.1', '2019-04.2']
    assert offsite.is_open('2026-11.2', offsite.dt.date(2026, 11, 21))


def test_verify_and_light_track_s3_staleness_and_files_added_after_backup(tmp_path):
    ark, state = '/ark', tmp_path / 'state'
    (state / 'manifests').mkdir(parents=True)
    offsite.write_index({u: {'unit': u, 's3_uri': f's3://b/tars/{u}.tar', 'source': ark, 'files': 1, 'bytes': 100,
                             'sha256': 'x', 'uploaded_at': '2026-01-01T00:00:00'} for u in ('1999', '2000')}, state)
    for u in ('1999', '2000'):
        with offsite.gzip.open(state / 'manifests' / f'{u}.tsv.gz', 'wt') as f:
            f.write(f'path\tsize\tmtime\tsha256\ttar\n{u}/01/{u}-01-01/a.jpg\t100\t0\tx\ts3://b/tars/{u}.tar\n')
    catalog = str(tmp_path / 'catalog.db')
    init_db(catalog).close()
    with sqlite3.connect(catalog) as conn:
        conn.executemany("INSERT INTO files (filename, original_path, dest_path, status) VALUES ('a', ?, ?, 'done')",
                         [(p, f'{ark}/{p}') for p in ('1999/01/1999-01-01/a.jpg', '2000/01/2000-01-01/a.jpg',
                                                      '2001/05/2001-05-05/b.jpg')])
    today = dt.date.today()
    good = [{'k': f'tars/{u}.tar', 's': 100, 'c': 'DEEP_ARCHIVE'} for u in ('1999', '2000')] + \
           [{'k': f'manifests/{u}.tsv.gz', 's': 1, 'c': 'STANDARD'} for u in ('1999', '2000')] + \
           [{'k': 'index.tsv', 's': 1, 'c': 'STANDARD'}]

    assert offsite.summary(catalog, state, today)['light'] == 'yellow', 'never verified'
    s = offsite.summary(catalog, state, today)
    assert s['buckets'] == [{'bucket': 'b', 'region': None, 'index_class': None, 'units': 2, 'bytes': 200}]
    offsite.verify(state, lister=lambda b: good, region_of=lambda b: 'us-west-2')
    s = offsite.summary(catalog, state, today)
    assert s['light'] == 'green', s['reasons']
    assert s['buckets'][0]['region'] == 'us-west-2' and s['buckets'][0]['index_class'] == 'STANDARD'
    assert {(u['tar_class'], u['manifest_class']) for u in s['units']} == {('DEEP_ARCHIVE', 'STANDARD')}
    assert s['not_backed_up'] == [{'year': '2001', 'files': 1, 'open': False, 'uploading': False}]

    dead = offsite.subprocess.Popen(['true'])
    dead.wait()
    (state / 'uploading').mkdir()
    for unit, pid in [('2001', offsite.os.getpid()), ('2002', dead.pid)]:
        offsite.progress_path(unit, state).write_text(offsite.json.dumps(
            {'unit': unit, 'pid': pid, 'started_at': '2026-01-02T00:00:00', 'files': 1, 'bytes': 100,
             'files_done': 0, 'bytes_done': 40}))
    s = offsite.summary(catalog, state, today)
    assert s['light'] == 'green', 'an upload in progress is not a problem'
    assert [(u['unit'], u['status']) for u in s['units']] == [('1999', 'ok'), ('2000', 'ok'), ('2001', 'in progress')]
    assert s['not_backed_up'][0]['uploading']
    offsite.progress_path('2001', state).unlink()
    assert offsite.summary(catalog, state, today + dt.timedelta(days=offsite.STALE_DAYS + 1))['light'] == 'yellow'

    with sqlite3.connect(catalog) as conn:
        conn.execute("INSERT INTO files (filename, original_path, dest_path, status) VALUES ('c', 'c', ?, 'done')",
                     (f'{ark}/1999/02/1999-02-02/c.jpg',))
    s = offsite.summary(catalog, state, today)
    assert s['light'] == 'yellow' and s['units'][0]['new_files'] == 1

    offsite.verify(state, lister=lambda b: [o for o in good if o['k'] != 'tars/2000.tar']
                   + [{'k': 'tars/1999.tar', 's': 99, 'c': 'DEEP_ARCHIVE'}], region_of=lambda b: 'us-west-2')
    s = offsite.summary(catalog, state, today)
    assert s['light'] == 'red' and [u['status'] for u in s['units']] == ['size', 'missing']

    offsite.verify(state, lister=lambda b: (_ for _ in ()).throw(RuntimeError('no network')),
                   region_of=lambda b: 'us-west-2')
    assert offsite.summary(catalog, state, today)['light'] == 'yellow', "unreachable S3 isn't proof of loss"
