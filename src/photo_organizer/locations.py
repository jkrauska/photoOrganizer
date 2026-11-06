"""Where each stored photo was taken: GPS from its own metadata, places from an offline gazetteer.

Sources, most trusted first (the `source` and `confidence` of each locations row):
  gps            the file's own GPS (EXIF / QuickTime), read by exiftool on the NAS       1.0
  day_gps        borrowed from the GPS photo nearest in time in the same day folder       0.5-0.8
  image_setting  a place named in the photo's Gemma description (analysis.db images)       0.2-0.5
  day_setting    a place named in the day's Gemma summary (analysis.db days)               0.15-0.4
Coordinates are stored only for gps and day_gps rows; Gemma rows name a place, not a point.

Place names come from GeoNames (cities1000 + admin codes), downloaded once into
~/.photo-organizer/geonames; nothing is sent anywhere per photo. Regions ("Tahoe", "South Bay",
"Marin") come from ~/.photo-organizer/regions.json, written from the defaults below on first use:
each region is a circle (center + radius_km), a polygon ([[lat, lon], ...]) or a list of counties,
plus optional aliases matched in Gemma text. The first region that matches wins, so put small
ones first. After editing it, `regions --apply` relabels every row without rereading files.

  photo-organizer-locations read --dest /Volumes/photoArk [--batch 20] [--procs 4] [--limit N]
  photo-organizer-locations infer          # day borrowing + Gemma fallback for files without GPS
  photo-organizer-locations regions [--apply]
  photo-organizer-locations stats
"""

import argparse
import bisect
import collections
import io
import json
import math
import os
import re
import signal
import sqlite3
import sys
import threading
import time
import urllib.request
import zipfile
from datetime import datetime

from photo_organizer.cameras import SKIP_EXTENSIONS, StayOpen, nas_exiftool_command
from photo_organizer.db import DEFAULT_DB_PATH, NOW_SQL, init_db
from photo_organizer.state import shutdown_requested, signal_handler

GEO_DIR = os.path.expanduser('~/.photo-organizer/geonames')
GEONAMES_URL = 'https://download.geonames.org/export/dump/'
GEONAMES_FILES = ('cities1000.zip', 'admin1CodesASCII.txt', 'admin2Codes.txt', 'countryInfo.txt')
REGIONS_PATH = os.path.expanduser('~/.photo-organizer/regions.json')
ANALYSIS_DB = os.path.expanduser('~/.photo-organizer/analysis.db')
GPS_TAGS = ('Composite:GPSLatitude', 'Composite:GPSLongitude', 'Composite:GPSAltitude')

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    file_id INTEGER PRIMARY KEY,     -- files.id
    lat REAL, lon REAL, alt REAL,    -- gps and day_gps rows only
    country_code TEXT, country TEXT, state TEXT, county TEXT, city TEXT, region TEXT,
    source TEXT,                     -- gps | day_gps | image_setting | day_setting | NULL: unknown
    confidence REAL,
    detail TEXT,                     -- how it was found: 'IMG_1.HEIC, 12 min apart', the Gemma text
    gps_read_at TEXT,                -- the file's own GPS was looked for (resume point of `read`)
    located_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_locations_source ON locations(source);
CREATE INDEX IF NOT EXISTS idx_locations_region ON locations(region);
CREATE INDEX IF NOT EXISTS idx_locations_city ON locations(city);
"""

# Public areas, not anyone's addresses; edit ~/.photo-organizer/regions.json to add your own.
DEFAULT_REGIONS = [
    {'name': 'Tahoe', 'center': [39.10, -120.04], 'radius_km': 38,   # takes in Truckee and Donner
     'aliases': ['Lake Tahoe', 'Tahoe', 'South Lake Tahoe', 'Truckee']},
    {'name': 'Yosemite', 'polygon': [[38.19, -119.89], [38.19, -119.20], [37.49, -119.20], [37.49, -119.89]],
     'aliases': ['Yosemite']},
    {'name': 'San Francisco', 'counties': ['San Francisco'], 'state': 'California'},
    {'name': 'Marin', 'counties': ['Marin'], 'state': 'California', 'aliases': ['Marin', 'Sausalito', 'Mill Valley']},
    {'name': 'Peninsula', 'counties': ['San Mateo'], 'state': 'California'},
    {'name': 'South Bay', 'counties': ['Santa Clara'], 'state': 'California', 'aliases': ['South Bay', 'Silicon Valley']},
    {'name': 'East Bay', 'counties': ['Alameda', 'Contra Costa'], 'state': 'California', 'aliases': ['East Bay']},
    {'name': 'Wine Country', 'counties': ['Napa', 'Sonoma'], 'state': 'California',
     'aliases': ['Napa Valley', 'Sonoma', 'Wine Country']},
    {'name': 'Santa Cruz', 'counties': ['Santa Cruz'], 'state': 'California'},
    {'name': 'Monterey', 'counties': ['Monterey'], 'state': 'California', 'aliases': ['Monterey', 'Carmel', 'Big Sur']},
]

# Day borrowing: confidence by how far apart in time the two photos were.
DAY_GPS_CONFIDENCE = ((3600, 0.8), (6 * 3600, 0.65), (None, 0.5))
TEXT_CONFIDENCE = {'image_setting': {'city': 0.5, 'region': 0.5, 'state': 0.3, 'country': 0.2},
                   'day_setting': {'city': 0.4, 'region': 0.4, 'state': 0.25, 'country': 0.15}}
# Single-word city names that are also ordinary words in Gemma's text.
COMMON_WORDS = {'Mobile', 'Orange', 'Reading', 'Bath', 'Nice', 'Victoria', 'Florence', 'Mesa', 'Garden',
                'Columbia', 'Lincoln', 'Jackson', 'Independence', 'Surprise', 'Normal', 'Paradise', 'Hollywood',
                'Phoenix', 'Aurora', 'Concord', 'Salem', 'Temple', 'Sale', 'Split', 'Hope', 'Union', 'Ocean',
                'Sunrise', 'Hayward', 'Mission', 'Liberty', 'Bend', 'Eagle', 'Center', 'George',
                'Golden Gate', 'National Park', 'South Beach'}
PREFIX_WORDS = {'Downtown', 'Uptown', 'Central', 'Old', 'Greater', 'Metropolitan', 'The', 'In', 'At', 'Near'}
# A city name followed by one of these is a landmark, not the city: 'Golden Gate Bridge', 'Central Park'.
LANDMARK_WORDS = {'Bridge', 'Park', 'Street', 'Avenue', 'Road', 'Boulevard', 'Drive', 'Lane', 'Square', 'Plaza',
                  'Center', 'Centre', 'Mall', 'Tower', 'Hotel', 'School', 'College', 'University', 'Church', 'Hall',
                  'Building', 'Museum', 'Trail', 'Lake', 'River', 'Mountain', 'Peak', 'Island', 'Valley'}
STATE_COUNTRIES = {'US', 'CA', 'AU', 'MX'}
COUNTRY_ALIASES = {'USA': 'US', 'U.S.': 'US', 'U.S.A.': 'US', 'United States': 'US', 'America': 'US',
                   'UK': 'GB', 'England': 'GB', 'Scotland': 'GB', 'Wales': 'GB', 'Britain': 'GB',
                   'Holland': 'NL'}


def county_name(name):
    """'Marin County' -> 'Marin', 'City and County of San Francisco' -> 'San Francisco'."""
    return re.sub(r'\s+(County|Parish|Borough)$', '', re.sub(r'^City and County of\s+', '', name))


def km_between(lat1, lon1, lat2, lon2):
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


# --- gazetteer ---------------------------------------------------------------------------------

def download_gazetteer(folder=GEO_DIR, out=print):
    """Fetch the GeoNames files once (about 12 MB); later runs read them from disk."""
    os.makedirs(folder, exist_ok=True)
    for name in GEONAMES_FILES:
        path = os.path.join(folder, name)
        if not os.path.exists(path):
            out(f"Downloading {GEONAMES_URL}{name}")
            with urllib.request.urlopen(GEONAMES_URL + name, timeout=120) as r:
                data = r.read()
            with open(path + '.tmp', 'wb') as f:
                f.write(data)
            os.replace(path + '.tmp', path)


class Gazetteer:
    """Nearest populated place for a point, and places named in free text.
    places: [(name, lat, lon, country_code, admin1_code, admin2_code, population)];
    admin1: {'US.CA': 'California'}; admin2: {'US.CA.041': 'Marin'}; countries: {'US': 'United States'}."""

    def __init__(self, places, admin1, admin2, countries):
        self.places, self.admin1, self.admin2, self.countries = places, admin1, admin2, countries
        self.grid = collections.defaultdict(list)
        self.by_name = collections.defaultdict(list)
        for i, (name, lat, lon, *_rest) in enumerate(places):
            self.grid[(math.floor(lat), math.floor(lon))].append(i)
            self.by_name[name].append(i)
        self.states = {}          # 'California' / 'CA' (US only) -> (cc, 'US.CA')
        for key, name in admin1.items():
            cc, code = key.split('.', 1)
            self.states.setdefault(name, (cc, key))
            if cc == 'US':
                self.states.setdefault(code, (cc, key))
        self.country_names = {name: cc for cc, name in countries.items()}
        self.country_names.update({k: v for k, v in COUNTRY_ALIASES.items() if v in countries})
        # Named on their own, only states of countries whose state names aren't ordinary words
        # elsewhere ('Central', 'Western', 'Capital' are provinces too).
        named = sorted((n for n, (cc, _) in self.states.items() if cc in STATE_COUNTRIES and len(n) > 2),
                       key=len, reverse=True)
        self.state_re = re.compile(rf"\b({'|'.join(map(re.escape, named))})\b" if named else r'(?!)')
        names = sorted(self.country_names, key=len, reverse=True)
        self.country_re = re.compile(rf"(?<![\w.])({'|'.join(map(re.escape, names))})(?!\w)" if names else r'(?!)')

    @classmethod
    def load(cls, folder=GEO_DIR):
        download_gazetteer(folder)

        def rows(name):
            with open(os.path.join(folder, name), encoding='utf-8') as f:
                return [line.rstrip('\n').split('\t') for line in f if line.strip() and not line.startswith('#')]
        admin1 = {r[0]: r[1] for r in rows('admin1CodesASCII.txt')}
        admin2 = {r[0]: county_name(r[1]) for r in rows('admin2Codes.txt')}
        countries = {r[0]: r[4] for r in rows('countryInfo.txt') if len(r) > 4}
        places = []
        with zipfile.ZipFile(os.path.join(folder, 'cities1000.zip')) as z, \
                io.TextIOWrapper(z.open('cities1000.txt'), encoding='utf-8') as f:
            for line in f:
                r = line.split('\t')
                if r[7] in ('PPLX', 'PPLH', 'PPLQ', 'PPLW'):   # neighborhoods, historical, abandoned, destroyed
                    continue
                places.append((r[1], float(r[4]), float(r[5]), r[8], r[10], r[11], int(r[14] or 0)))
        return cls(places, admin1, admin2, countries)

    def describe(self, i):
        name, lat, lon, cc, a1, a2, _pop = self.places[i]
        return {'country_code': cc, 'country': self.countries.get(cc, cc), 'state': self.admin1.get(f"{cc}.{a1}"),
                'county': self.admin2.get(f"{cc}.{a1}.{a2}"), 'city': name, 'place_lat': lat, 'place_lon': lon}

    def nearest(self, lat, lon, max_rings=4):
        """The nearest place as describe() plus 'km', or None when nothing is within a few hundred km."""
        cy, cx, best, found_at = math.floor(lat), math.floor(lon), None, None
        for ring in range(max_rings + 1):
            for y in range(cy - ring, cy + ring + 1):
                for x in range(cx - ring, cx + ring + 1):
                    if max(abs(y - cy), abs(x - cx)) != ring:
                        continue
                    for i in self.grid.get((y, ((x + 180) % 360) - 180), ()):
                        d = km_between(lat, lon, self.places[i][1], self.places[i][2])
                        if best is None or d < best[0]:
                            best = (d, i)
            if best is not None:
                found_at = ring if found_at is None else found_at
                if ring > found_at:            # one more ring: a closer place can sit in a neighboring cell
                    break
        if best is None:
            return None
        return {**self.describe(best[1]), 'km': round(best[0], 1)}

    def _city(self, name, cc=None, state_key=None, min_pop=0):
        ids = [i for i in self.by_name.get(name, ())
               if self.places[i][6] >= min_pop and (cc is None or self.places[i][3] == cc)
               and (state_key is None or f"{self.places[i][3]}.{self.places[i][4]}" == state_key)]
        return max(ids, key=lambda i: self.places[i][6]) if ids else None

    def match_text(self, text):
        """The most specific place named in text: {'level': 'city'|'state'|'country', ...describe()} or None.
        Case-sensitive, so 'mobile application interface' names no city."""
        if not text:
            return None
        cap = r"[A-Z][\w.'-]*"
        # 'New York City, NY', 'Downtown Chicago, Illinois', 'Paris, France'
        for m in re.finditer(rf"((?:{cap}\s+){{0,3}}{cap}),\s*({cap}(?:\s+{cap}){{0,2}})", text):
            words, where = m.group(1).split(), m.group(2)
            cc = state_key = None
            if where in self.states:
                cc, state_key = self.states[where]
            elif where in self.country_names:
                cc = self.country_names[where]
            else:
                continue
            for k in range(len(words)):
                i = self._city(' '.join(words[k:]), cc, state_key)
                if i is not None:
                    return {'level': 'city', **self.describe(i)}
        # A city named on its own: several words from small towns up, one word only for large cities.
        tokens = list(re.finditer(cap, text))
        best = None
        for a in range(len(tokens)):
            for b in range(min(len(tokens), a + 4), a, -1):
                if any(text[tokens[j].end():tokens[j + 1].start()].strip() for j in range(a, b - 1)):
                    continue                       # only words separated by spaces
                name = text[tokens[a].start():tokens[b - 1].end()]
                if name in COMMON_WORDS:
                    continue
                if a > 0 and tokens[a - 1].group() not in PREFIX_WORDS \
                        and not text[tokens[a - 1].end():tokens[a].start()].strip():
                    continue                       # inside a longer name: 'Mont Saint-Michel', 'Ronald M. George'
                if b < len(tokens) and tokens[b].group() in LANDMARK_WORDS \
                        and not text[tokens[b - 1].end():tokens[b].start()].strip():
                    continue
                i = self._city(name, min_pop=100_000 if b - a == 1 else 1_000)
                if i is not None and (best is None or (b - a, self.places[i][6]) > best[0]):
                    best = ((b - a, self.places[i][6]), i)
        if best is not None:
            return {'level': 'city', **self.describe(best[1])}
        m = self.state_re.search(text)
        if m:
            cc, key = self.states[m.group(1)]
            return {'level': 'state', 'country_code': cc, 'country': self.countries.get(cc, cc),
                    'state': self.admin1[key], 'county': None, 'city': None}
        m = self.country_re.search(text)
        if m:
            cc = self.country_names[m.group(1)]
            return {'level': 'country', 'country_code': cc, 'country': self.countries.get(cc, cc),
                    'state': None, 'county': None, 'city': None}
        return None


# --- regions -----------------------------------------------------------------------------------

def load_regions(path=REGIONS_PATH):
    """The region table, written from DEFAULT_REGIONS the first time so it can be edited."""
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'w') as f:
            json.dump(DEFAULT_REGIONS, f, indent=2)
    with open(path) as f:
        return json.load(f)


def _inside(lat, lon, polygon):
    inside, n = False, len(polygon)
    for k in range(n):
        (y1, x1), (y2, x2) = polygon[k], polygon[(k + 1) % n]
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def region_of(regions, lat=None, lon=None, state=None, county=None):
    for r in regions:
        if 'center' in r and lat is not None:
            if km_between(lat, lon, *r['center']) <= r['radius_km']:
                return r['name']
        elif 'polygon' in r and lat is not None:
            if _inside(lat, lon, r['polygon']):
                return r['name']
        elif 'counties' in r and county:
            if county in r['counties'] and (not r.get('state') or r['state'] == state):
                return r['name']
    return None


def region_alias_in(regions, text):
    for r in regions:
        for alias in r.get('aliases', ()):
            if text and re.search(rf"\b{re.escape(alias)}\b", text):
                return r
    return None


# --- reading GPS on the NAS --------------------------------------------------------------------

def unread(conn, limit=0):
    skip = ' AND '.join(f"lower(f.dest_path) NOT LIKE '%{e}'" for e in SKIP_EXTENSIONS)
    return conn.execute(
        f"SELECT f.id, f.dest_path FROM files f LEFT JOIN locations l ON l.file_id = f.id "
        f"WHERE f.status = 'done' AND f.dest_path IS NOT NULL AND l.gps_read_at IS NULL AND {skip} "
        f"ORDER BY f.id" + (f" LIMIT {int(limit)}" if limit else '')).fetchall()


def gps_of(tags):
    """(lat, lon, alt) from one exiftool record (run with -n), or None; 0,0 is a missing fix."""
    try:
        lat, lon = float(tags['GPSLatitude']), float(tags['GPSLongitude'])
    except (KeyError, TypeError, ValueError):
        return None
    if (lat == 0 and lon == 0) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    try:
        alt = round(float(tags['GPSAltitude']), 1)
    except (KeyError, TypeError, ValueError):
        alt = None
    return lat, lon, alt


def place_columns(gaz, regions, lat, lon):
    p = gaz.nearest(lat, lon) or {}
    return {'country_code': p.get('country_code'), 'country': p.get('country'), 'state': p.get('state'),
            'county': p.get('county'), 'city': p.get('city'),
            'region': region_of(regions, lat, lon, p.get('state'), p.get('county')),
            'detail': f"{p['km']} km from {p['city']}" if p else None}


COLUMNS = ('lat', 'lon', 'alt', 'country_code', 'country', 'state', 'county', 'city', 'region',
           'source', 'confidence', 'detail')


def _upsert(conn, file_id, row, read=False):
    vals = [row.get(c) for c in COLUMNS]
    conn.execute(
        f"INSERT INTO locations (file_id, {', '.join(COLUMNS)}, located_at{', gps_read_at' if read else ''}) "
        f"VALUES (?, {', '.join('?' * len(COLUMNS))}, {NOW_SQL}{', ' + NOW_SQL if read else ''}) "
        f"ON CONFLICT(file_id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in COLUMNS)}, "
        f"located_at = excluded.located_at" + (", gps_read_at = excluded.gps_read_at" if read else ''),
        (file_id, *vals))


def record(conn, rows, found, gaz, regions):
    """Store the GPS of rows ([(id, nas_path)]); a file without GPS gets an empty, read row."""
    places = []
    for fid, nas_path in rows:
        g = gps_of(found.get(nas_path, {}))
        if g:
            row = {'lat': g[0], 'lon': g[1], 'alt': g[2], 'source': 'gps', 'confidence': 1.0,
                   **place_columns(gaz, regions, g[0], g[1])}
            places.append(row['region'] or row['city'] or row['country'] or '?')
        else:
            row = {}
        _upsert(conn, fid, row, read=True)
    return places


def init_locations(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def read_gps(conn, fs, gaz, regions, batch=20, procs=4, limit=0, readers=None, out=print):
    """Read GPS of stored files not read yet, reverse-geocoding as each batch lands.
    readers: StayOpen-likes (tests). Returns (files read, files with GPS)."""
    todo = [(r['id'], fs.remote(r['dest_path'])) for r in unread(conn, limit)]
    if not todo:
        out("Every stored file's GPS has been read.")
        return 0, 0
    if readers is None:
        command = nas_exiftool_command(fs.target)
        readers = [GpsReader(fs.target, command) for _ in range(max(1, min(procs, len(todo) // batch + 1)))]
    batches = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    lock = threading.Lock()
    state = {'next': 0, 'done': 0, 'gps': 0, 'places': collections.Counter(), 'error': None}
    start = time.time()
    out(f"Reading GPS of {len(todo):,} files on the NAS: {len(readers)} exiftool processes, {batch} files per batch")

    def work(reader):
        while not shutdown_requested() and state['error'] is None:
            with lock:
                if state['next'] >= len(batches):
                    return
                rows = batches[state['next']]
                state['next'] += 1
            try:
                found = reader.read([p for _, p in rows])
            except Exception as e:
                state['error'] = e
                return
            with lock:
                places = record(conn, rows, found, gaz, regions)
                conn.commit()
                state['done'] += len(rows)
                state['gps'] += len(places)
                state['places'].update(places)
                rate = state['done'] / max(time.time() - start, 1e-6)
                eta = (len(todo) - state['done']) / rate if rate else 0
                top = ', '.join(f"{n} ×{c}" for n, c in collections.Counter(places).most_common(3)) or 'no GPS'
                out(f"  [{state['done']:,}/{len(todo):,}] {rate:.0f} files/s, ~{eta / 60:.0f} min left | "
                    f"GPS {len(places)}/{len(rows)} | {top}")

    threads = [threading.Thread(target=work, args=(r,), daemon=True) for r in readers]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        for r in readers:
            r.close()
    if state['error'] is not None:
        raise state['error']
    out(f"Read {state['done']:,} files, {state['gps']:,} with GPS. Most common: "
        + ', '.join(f"{n} ({c:,})" for n, c in state['places'].most_common(6)))
    return state['done'], state['gps']


class GpsReader(StayOpen):
    """The camera pass's exiftool -stay_open, asking for numeric (-n) GPS instead of camera tags."""

    def read(self, paths):
        return super().read(paths, GPS_TAGS, ('-n',))


# --- inference for files without GPS ----------------------------------------------------------

def _when(taken_at):
    try:
        return datetime.fromisoformat(taken_at).timestamp() if taken_at else None
    except ValueError:
        return None


def _text_place(gaz, regions, text, source):
    """A Gemma text's place as a locations row (no coordinates), or None."""
    hit = gaz.match_text(text)
    alias = region_alias_in(regions, text)
    if hit is None and alias is None:
        return None
    if hit is not None and alias is not None and hit['level'] == 'city' and region_of(
            regions, hit['place_lat'], hit['place_lon'], hit['state'], hit['county']) != alias['name']:
        hit = None                                 # a city outside the region the text names: trust the region
    if hit is None or (alias and hit['level'] != 'city'):
        level, region = 'region', alias['name']
        hit = {}
        if 'center' in alias or 'polygon' in alias:
            lat, lon = alias.get('center') or alias['polygon'][0]
            p = gaz.nearest(lat, lon) or {}
            hit = {'country_code': p.get('country_code'), 'country': p.get('country'), 'state': p.get('state')}
        elif alias.get('state'):
            cc, key = gaz.states.get(alias['state'], (None, None))
            hit = {'country_code': cc, 'country': gaz.countries.get(cc), 'state': alias['state'],
                   'county': alias['counties'][0] if len(alias.get('counties', ())) == 1 else None}
    else:
        level = hit['level']
        region = region_of(regions, hit.get('place_lat'), hit.get('place_lon'), hit.get('state'), hit.get('county'))
    return {'country_code': hit.get('country_code'), 'country': hit.get('country'), 'state': hit.get('state'),
            'county': hit.get('county'), 'city': hit.get('city'), 'region': region, 'source': source,
            'confidence': TEXT_CONFIDENCE[source][level], 'detail': text[:200]}


def infer(conn, gaz, regions, analysis_db=ANALYSIS_DB, chunk=2000, out=print):
    """Give every read file without GPS the best inferred place (or none). Recomputed from scratch
    each run, so it never goes stale; gps rows are untouched. Returns Counter of sources."""
    rows = conn.execute(
        "SELECT f.id, f.dest_path, f.sha256, f.taken_at, l.source, l.lat, l.lon, l.alt, l.country_code, l.country, "
        "l.state, l.county, l.city, l.region FROM files f JOIN locations l ON l.file_id = f.id "
        "WHERE l.gps_read_at IS NOT NULL").fetchall()
    settings, day_settings = {}, {}
    if analysis_db and os.path.exists(analysis_db):
        a = sqlite3.connect(f"file:{analysis_db}?mode=ro", uri=True, timeout=60)
        settings = {s: t for s, t in a.execute(
            "SELECT sha256, setting FROM images WHERE setting IS NOT NULL AND setting != ''")}
        day_settings = {d: t for d, t in a.execute(
            "SELECT day, setting FROM days WHERE setting IS NOT NULL AND setting != ''")}
        a.close()
    by_day = collections.defaultdict(list)          # day folder -> [(time, row)] of gps rows
    for r in rows:
        if r['source'] == 'gps':
            by_day[os.path.dirname(r['dest_path'])].append((_when(r['taken_at']), r))
    for v in by_day.values():
        v.sort(key=lambda t: (t[0] is None, t[0] or 0))
    day_cache, counts, pending = {}, collections.Counter(), []

    def flush():
        for fid, row in pending:
            _upsert(conn, fid, row)
        conn.commit()
        pending.clear()

    for r in rows:
        if r['source'] == 'gps':
            counts['gps'] += 1
            continue
        day = os.path.dirname(r['dest_path'])
        row = None
        if by_day.get(day):
            gps = by_day[day]
            t = _when(r['taken_at'])
            timed = [g for g in gps if g[0] is not None]
            if t is not None and timed:
                k = bisect.bisect_left([g[0] for g in timed], t)
                near = min((timed[j] for j in (k - 1, k) if 0 <= j < len(timed)), key=lambda g: abs(g[0] - t))
                gap = abs(near[0] - t)
            else:
                near, gap = gps[0], None
            conf = next(c for limit, c in DAY_GPS_CONFIDENCE if limit is None or (gap is not None and gap <= limit))
            g = near[1]
            row = {c: g[c] for c in ('lat', 'lon', 'alt', 'country_code', 'country', 'state', 'county', 'city',
                                     'region')}
            row.update(source='day_gps', confidence=conf,
                       detail=f"{os.path.basename(g['dest_path'])}"
                              + (f", {gap / 60:.0f} min apart" if gap is not None else ''))
        if row is None and r['sha256'] in settings:
            row = _text_place(gaz, regions, settings[r['sha256']], 'image_setting')
        if row is None and day in day_settings:
            if day not in day_cache:
                day_cache[day] = _text_place(gaz, regions, day_settings[day], 'day_setting')
            row = day_cache[day]
        counts[row['source'] if row else None] += 1
        if row is not None or r['source'] is not None:       # clear a stale inference too
            pending.append((r['id'], row or {}))
        if len(pending) >= chunk:
            flush()
    flush()
    out("Located: " + ', '.join(f"{k or 'unknown'} {v:,}" for k, v in counts.most_common()))
    return counts


def apply_regions(conn, regions, chunk=5000):
    """Relabel the region of every row with coordinates after the region table changed."""
    rows = conn.execute("SELECT file_id, lat, lon, state, county FROM locations WHERE source = 'gps'").fetchall()
    for k in range(0, len(rows), chunk):
        for r in rows[k:k + chunk]:
            conn.execute("UPDATE locations SET region = ? WHERE file_id = ?",
                         (region_of(regions, r['lat'], r['lon'], r['state'], r['county']), r['file_id']))
        conn.commit()
    return len(rows)


# --- report ------------------------------------------------------------------------------------

def coverage(conn):
    skip = ' AND '.join(f"lower(f.dest_path) NOT LIKE '%{e}'" for e in SKIP_EXTENSIONS)
    total = conn.execute(f"SELECT COUNT(*) FROM files f WHERE f.status = 'done' AND f.dest_path IS NOT NULL "
                         f"AND {skip}").fetchone()[0]
    by_source = dict(conn.execute(
        "SELECT COALESCE(source, 'none'), COUNT(*) FROM locations WHERE gps_read_at IS NOT NULL GROUP BY 1"))
    return {'stored': total, 'read': sum(by_source.values()), 'by_source': by_source}


def stats(conn, out=print):
    c = coverage(conn)
    out(f"  {c['stored']:,} stored files, {c['read']:,} read for GPS ({c['stored'] - c['read']:,} not yet)")
    for src in ('gps', 'day_gps', 'image_setting', 'day_setting', 'none'):
        n = c['by_source'].get(src, 0)
        out(f"  {n:>8,}  {src:<14} {100 * n / max(c['read'], 1):5.1f}%")
    for col in ('region', 'country', 'city'):
        top = conn.execute(f"SELECT {col}, COUNT(*) n FROM locations WHERE {col} IS NOT NULL GROUP BY 1 "
                           f"ORDER BY n DESC LIMIT 8").fetchall()
        out(f"  top {col}: " + ', '.join(f"{r[0]} ({r[1]:,})" for r in top))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', default=DEFAULT_DB_PATH)
    parser.add_argument('--regions', default=REGIONS_PATH)
    sub = parser.add_subparsers(dest='cmd', required=True)
    p_read = sub.add_parser('read', help="Read stored files' GPS on the NAS and name their places (resumable)")
    p_read.add_argument('--dest', required=True, help='root of the canonical store')
    p_read.add_argument('--batch', type=int, default=20, help='Files per batch (one progress line each)')
    p_read.add_argument('--procs', type=int, default=4, help='Parallel exiftool processes on the NAS')
    p_read.add_argument('--limit', type=int, default=0)
    p_infer = sub.add_parser('infer', help='Places for files without GPS: same-day GPS, then Gemma text')
    p_infer.add_argument('--analysis-db', default=ANALYSIS_DB)
    p_reg = sub.add_parser('regions', help='List the regions; --apply relabels rows after editing them')
    p_reg.add_argument('--apply', action='store_true')
    sub.add_parser('stats', help='Coverage: GPS, inferred, unknown')
    args = parser.parse_args()
    conn = init_db(args.db)
    init_locations(conn)
    if args.cmd == 'stats':
        stats(conn)
        return
    regions = load_regions(args.regions)
    if args.cmd == 'regions':
        for r in regions:
            shape = (f"within {r['radius_km']} km of {r['center']}" if 'center' in r else
                     f"polygon of {len(r['polygon'])} points" if 'polygon' in r else
                     f"counties {', '.join(r['counties'])}")
            n = conn.execute("SELECT COUNT(*) FROM locations WHERE region = ?", (r['name'],)).fetchone()[0]
            print(f"  {r['name']:<16} {n:>8,}  {shape}")
        print(f"  (edit {args.regions})")
        if args.apply:
            n = apply_regions(conn, regions)
            print(f"Relabeled {n:,} GPS rows")
            infer(conn, Gazetteer.load(), regions)
        return
    gaz = Gazetteer.load()
    if args.cmd == 'infer':
        infer(conn, gaz, regions, args.analysis_db)
        return
    from photo_organizer.nas import detect
    fs, note = detect([], os.path.abspath(args.dest))
    if not fs:
        sys.exit(f"Error: the store isn't reachable on its NAS over ssh ({note})")
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    try:
        read_gps(conn, fs, gaz, regions, args.batch, args.procs, args.limit, out=lambda s: print(s, flush=True))
    finally:
        fs.close()
        conn.close()
    if shutdown_requested():
        print("Stopped; run again to continue.")


if __name__ == '__main__':
    main()
