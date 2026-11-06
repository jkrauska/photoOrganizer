"""Where photos were taken: GPS read in batches (on the NAS in real runs), an offline gazetteer,
editable regions, and inferred places for files without GPS."""

import json
import sqlite3

from photo_organizer.db import init_db
from photo_organizer.locations import (DEFAULT_REGIONS, Gazetteer, apply_regions, county_name, coverage, gps_of,
                                       infer, init_locations, load_regions, read_gps, region_of, unread)
from test_cameras import FakeFS, FakeReader

PLACES = [  # name, lat, lon, cc, admin1, admin2, population
    ('San Francisco', 37.7749, -122.4194, 'US', 'CA', '075', 808_000),
    ('Mill Valley', 37.906, -122.545, 'US', 'CA', '041', 14_000),
    ('Mountain View', 37.386, -122.084, 'US', 'CA', '085', 82_000),
    ('Kings Beach', 39.238, -120.026, 'US', 'CA', '061', 3_800),
    ('Chicago', 41.85, -87.65, 'US', 'IL', '031', 2_700_000),
    ('New York City', 40.714, -74.006, 'US', 'NY', '061', 8_800_000),
    ('Mobile', 30.69, -88.04, 'US', 'AL', '097', 190_000),
    ('Golden Gate', 26.19, -81.69, 'US', 'FL', '021', 25_000),
    ('Paris', 48.853, 2.349, 'FR', '11', '75', 2_100_000),
    ('Paris', 33.66, -95.55, 'US', 'TX', '277', 25_000),
    ('National Park', 39.85, -75.2, 'US', 'NJ', '015', 3_000),
    ('Saint-Michel', 48.6, -1.5, 'FR', '28', '50', 2_000),
]
ADMIN1 = {'US.CA': 'California', 'US.IL': 'Illinois', 'US.NY': 'New York', 'US.AL': 'Alabama',
          'US.FL': 'Florida', 'US.TX': 'Texas', 'US.HI': 'Hawaii', 'FR.11': 'Île-de-France'}
ADMIN2 = {'US.CA.075': 'San Francisco', 'US.CA.041': 'Marin', 'US.CA.085': 'Santa Clara', 'US.CA.061': 'Placer'}
COUNTRIES = {'US': 'United States', 'FR': 'France', 'JP': 'Japan'}


def gazetteer():
    return Gazetteer(PLACES, ADMIN1, ADMIN2, COUNTRIES)


def test_nearest_place_and_regions():
    g = gazetteer()
    p = g.nearest(37.7737, -122.4332)
    assert (p['city'], p['county'], p['state'], p['country']) == ('San Francisco', 'San Francisco', 'California',
                                                                   'United States')
    assert p['km'] < 2
    assert g.nearest(-60, 0) is None                                  # nothing within a few hundred km
    assert g.nearest(37.39, -122.08)['city'] == 'Mountain View'
    regions = DEFAULT_REGIONS
    assert region_of(regions, 39.25, -120.03, 'California', 'Placer') == 'Tahoe'          # circle first
    assert region_of(regions, 37.39, -122.08, 'California', 'Santa Clara') == 'South Bay'  # by county
    assert region_of(regions, 37.9, -122.55, 'California', 'Marin') == 'Marin'
    assert region_of(regions, 37.75, -119.6, 'California', 'Mariposa') == 'Yosemite'     # polygon
    assert region_of(regions, 41.85, -87.65, 'Illinois', 'Cook') is None
    assert county_name('City and County of San Francisco') == 'San Francisco' and county_name('Marin County') == 'Marin'


def test_places_named_in_gemma_text():
    g = gazetteer()

    def where(text):
        m = g.match_text(text)
        return m and (m['level'], m['city'] or m['state'] or m['country'])
    assert where('New York City, NY') == ('city', 'New York City')
    assert where('Downtown Chicago, Illinois') == ('city', 'Chicago')
    assert where('a street in Paris, France') == ('city', 'Paris')
    assert g.match_text('a street in Paris, France')['country'] == 'France'
    assert g.match_text('Paris, TX')['state'] == 'Texas'
    assert where('a baseball stadium in San Francisco') == ('city', 'San Francisco')
    assert where('on a beach in Hawaii') == ('state', 'Hawaii')
    assert where('somewhere in France') == ('country', 'France')
    assert where('mobile application interface') is None              # case-sensitive
    assert where('Mobile app screen') is None                          # a word as well as a city
    assert where('the Golden Gate Bridge') is None                     # a landmark, not Golden Gate FL
    assert where('inside a home') is None
    assert where('Mont Saint-Michel, a tidal island') is None         # part of a longer name


def test_a_region_named_in_text_beats_a_town_elsewhere():
    from photo_organizer.locations import _text_place
    g = gazetteer()
    y = _text_place(g, DEFAULT_REGIONS, 'Yosemite National Park', 'day_setting')
    assert (y['region'], y['city'], y['state'], y['confidence']) == ('Yosemite', None, 'California', 0.4)
    m = _text_place(g, DEFAULT_REGIONS, 'a trail near Mill Valley in Marin', 'image_setting')
    assert (m['region'], m['city'], m['confidence']) == ('Marin', 'Mill Valley', 0.5)   # the city is in it


def test_gps_of_reads_numeric_exiftool_output():
    assert gps_of({'GPSLatitude': 37.77, 'GPSLongitude': -122.43, 'GPSAltitude': 59.33}) == (37.77, -122.43, 59.3)
    assert gps_of({'GPSLatitude': '37.77', 'GPSLongitude': '-122.43'}) == (37.77, -122.43, None)
    assert gps_of({'GPSLatitude': 0, 'GPSLongitude': 0}) is None     # no fix
    assert gps_of({}) is None


def catalog(tmp_path, files):
    conn = init_db(str(tmp_path / 'catalog.db'))
    init_locations(conn)
    for name, day, taken, sha in files:
        conn.execute("INSERT INTO files (filename, original_path, status, dest_path, taken_at, sha256) "
                     "VALUES (?, ?, 'done', ?, ?, ?)",
                     (name, f"/src/{day}/{name}", f"/Volumes/photoArk/{day[:4]}/{day[5:7]}/{day}/{name}", taken, sha))
    conn.commit()
    return conn


def nas(day, name):
    return f"/mnt/pool/photoArk/{day[:4]}/{day[5:7]}/{day}/{name}"


def test_read_gps_in_batches_then_infer_the_rest(tmp_path):
    files = [(f"IMG_{i}.HEIC", '2024-06-01', f"2024-06-01T{10 + i // 10:02d}:{i % 10:02d}:00", f"a{i}")
             for i in range(25)]                                            # SF in the morning, no GPS on some
    files += [('DSC_1.ARW', '2024-06-01', '2024-06-01T10:05:30', 'b1'),     # a RAW shot beside the iPhone
              ('DSC_2.ARW', '2024-07-04', '2024-07-04T12:00:00', 'b2'),     # Gemma: Chicago
              ('DSC_3.ARW', '2024-07-05', '2024-07-05T12:00:00', 'b3'),     # the day summary says Tahoe
              ('DSC_4.ARW', '2024-07-06', '2024-07-06T12:00:00', 'b4'),     # nothing known
              ('DSC_4.ARW.xmp', '2024-07-06', None, None)]
    conn = catalog(tmp_path, files)
    tags = {nas('2024-06-01', f"IMG_{i}.HEIC"): {'GPSLatitude': 37.7737 + i / 1e4, 'GPSLongitude': -122.4332,
                                                   'GPSAltitude': 12.0} for i in range(20)}
    reader = FakeReader(tags)
    lines = []
    g, regions = gazetteer(), DEFAULT_REGIONS
    assert read_gps(conn, FakeFS(), g, regions, batch=10, readers=[reader], out=lines.append) == (29, 20)
    assert [len(b) for b in reader.batches] == [10, 10, 9] and reader.closed   # the sidecar isn't read
    assert sum('/29]' in line for line in lines) == 3 and 'GPS 10/10 | San Francisco ×10' in lines[1]
    assert unread(conn) == []
    assert read_gps(conn, FakeFS(), g, regions, readers=[FakeReader({})], out=lines.append) == (0, 0)  # resumes

    analysis = tmp_path / 'analysis.db'
    a = sqlite3.connect(analysis)
    a.execute("CREATE TABLE images (sha256 TEXT PRIMARY KEY, path TEXT, setting TEXT)")
    a.execute("CREATE TABLE days (day TEXT PRIMARY KEY, setting TEXT)")
    a.execute("INSERT INTO images VALUES ('b2', '', 'Chicago, Illinois'), ('b4', '', 'inside a home')")
    a.execute("INSERT INTO days VALUES ('/Volumes/photoArk/2024/07/2024-07-05', 'skiing at Lake Tahoe'), "
              "('/Volumes/photoArk/2024/07/2024-07-06', 'a parking lot')")
    a.commit()
    a.close()
    counts = infer(conn, g, regions, str(analysis), out=lines.append)
    assert counts == {'gps': 20, 'day_gps': 6, 'image_setting': 1, 'day_setting': 1, None: 1}

    conn.row_factory = sqlite3.Row
    loc = {r['filename']: r for r in conn.execute(
        "SELECT f.filename, l.* FROM files f JOIN locations l ON l.file_id = f.id")}
    assert loc['IMG_0.HEIC']['source'] == 'gps' and loc['IMG_0.HEIC']['alt'] == 12.0
    assert loc['IMG_0.HEIC']['city'] == 'San Francisco' and loc['IMG_0.HEIC']['region'] == 'San Francisco'
    raw = loc['DSC_1.ARW']                                    # borrowed from IMG_5 (10:05), nearest in time
    assert raw['source'] == 'day_gps' and raw['confidence'] == 0.8 and raw['detail'] == 'IMG_5.HEIC, 0 min apart'
    assert raw['lat'] == loc['IMG_5.HEIC']['lat'] and raw['city'] == 'San Francisco'
    assert loc['IMG_24.HEIC']['source'] == 'day_gps' and loc['IMG_24.HEIC']['detail'].startswith('IMG_19.HEIC')
    chicago = loc['DSC_2.ARW']
    assert (chicago['source'], chicago['city'], chicago['state'], chicago['lat']) == ('image_setting', 'Chicago',
                                                                                      'Illinois', None)
    tahoe = loc['DSC_3.ARW']
    assert (tahoe['source'], tahoe['region'], tahoe['state'], tahoe['city']) == ('day_setting', 'Tahoe',
                                                                                 'California', None)
    assert loc['DSC_4.ARW']['source'] is None and loc['DSC_4.ARW']['gps_read_at']

    c = coverage(conn)
    assert c['stored'] == 29 and c['read'] == 29 and c['by_source']['none'] == 1
    assert infer(conn, g, regions, str(analysis), out=lines.append) == counts       # rerunning changes nothing


def test_edited_regions_relabel_without_rereading(tmp_path):
    conn = catalog(tmp_path, [('IMG_1.HEIC', '2024-06-01', '2024-06-01T10:00:00', 'a1')])
    g = gazetteer()
    read_gps(conn, FakeFS(), g, DEFAULT_REGIONS, readers=[FakeReader(
        {nas('2024-06-01', 'IMG_1.HEIC'): {'GPSLatitude': 37.7737, 'GPSLongitude': -122.4332}})], out=lambda s: None)
    path = tmp_path / 'regions.json'
    regions = load_regions(str(path))                         # written from the defaults on first use
    assert [r['name'] for r in regions] == [r['name'] for r in DEFAULT_REGIONS]
    path.write_text(json.dumps([{'name': 'Home', 'center': [37.7737, -122.4332], 'radius_km': 1}, *regions]))
    assert apply_regions(conn, load_regions(str(path))) == 1
    assert conn.execute("SELECT region FROM locations").fetchone()[0] == 'Home'
