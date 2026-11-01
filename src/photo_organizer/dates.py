"""Capture-date extraction: ExifTool for embedded metadata, then filename and folder patterns."""

import datetime
import json
import os
import re
import shutil
import subprocess

from photo_organizer.constants import FILENAME_PATTERNS

# Most precise first. With -api QuickTimeUTC, video CreateDate comes back in local time with offset.
EXIF_DATE_TAGS = ('SubSecDateTimeOriginal', 'DateTimeOriginal', 'CreateDate', 'MediaCreateDate')
EXIFTOOL_CHUNK = 200


def _plausible_year(y):
    return 1970 <= y <= datetime.date.today().year + 1


def parse_exif_datetime(val):
    """Parse '2013:01:18 15:21:17[.123][-07:00]' to a datetime (aware when an offset is present)."""
    if not val or not isinstance(val, str):
        return None
    m = re.match(r'(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?',
                 val.strip())
    if not m:
        return None
    y, mo, d, hh, mm, ss = (int(g) for g in m.groups()[:6])
    if not _plausible_year(y):
        return None
    tz = None
    if m.group(7):
        off = m.group(7)
        if off == 'Z':
            tz = datetime.timezone.utc
        else:
            sign = -1 if off[0] == '-' else 1
            digits = off[1:].replace(':', '')
            tz = datetime.timezone(sign * datetime.timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
    try:
        return datetime.datetime(y, mo, d, hh, mm, ss, tzinfo=tz)
    except ValueError:
        return None


def require_exiftool():
    if not shutil.which('exiftool'):
        raise SystemExit("exiftool not found in PATH. Install with: brew install exiftool")


def read_exif_dates(paths):
    """Return {path: (datetime, raw_string)} for files with a usable embedded capture date."""
    out = {}
    for i in range(0, len(paths), EXIFTOOL_CHUNK):
        chunk = paths[i:i + EXIFTOOL_CHUNK]
        cmd = ['exiftool', '-json', '-q', '-q', '-fast', '-api', 'QuickTimeUTC',
               '-charset', 'filename=utf8']
        cmd += [f'-{t}' for t in EXIF_DATE_TAGS]
        cmd += ['-@', '-']
        # exiftool exits non-zero if any file in the chunk is unreadable; the rest are still reported.
        result = subprocess.run(cmd, input='\n'.join(chunk), capture_output=True, text=True)
        try:
            records = json.loads(result.stdout) if result.stdout.strip() else []
        except json.JSONDecodeError:
            records = []
        for rec in records:
            for tag in EXIF_DATE_TAGS:
                dt = parse_exif_datetime(rec.get(tag))
                if dt:
                    out[rec['SourceFile']] = (dt, rec[tag])
                    break
    return out


def date_from_filename(filename):
    for pattern in FILENAME_PATTERNS:
        m = pattern.search(filename)
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if _plausible_year(y):
                    return datetime.date(y, mo, d)
            except ValueError:
                continue
    return None


def date_from_dirpath(filepath, source_root):
    """Date from the nearest 'YYYY-MM-DD' folder. Year- or month-only folders are too vague to file by."""
    parts = os.path.relpath(filepath, source_root).split(os.sep)[:-1]
    for part in reversed(parts):
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', part)
        if m:
            try:
                d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if _plausible_year(d.year):
                    return d
            except ValueError:
                pass
    return None
