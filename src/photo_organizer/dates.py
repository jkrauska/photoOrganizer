"""Date extraction from EXIF, filenames, directory paths, and mtime."""

import datetime
import os
import re
import struct

from photo_organizer.constants import FILENAME_PATTERNS

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


def _parse_exif_datetime(val):
    """Parse EXIF datetime string like '2013:01:18 15:21:17' to date."""
    if not val or not isinstance(val, str):
        return None
    val = val.strip().rstrip('\x00')
    if len(val) < 10:
        return None
    try:
        # EXIF format: "YYYY:MM:DD HH:MM:SS"
        return datetime.date(int(val[0:4]), int(val[5:7]), int(val[8:10]))
    except (ValueError, IndexError):
        return None


def extract_exif_date_jpg(filepath):
    """Extract date from JPG/JPEG/PNG using Pillow."""
    if not HAS_PILLOW:
        return None, None
    try:
        with Image.open(filepath) as img:
            exif_data = img.getexif()
            if not exif_data:
                return None, None

            # Try EXIF sub-IFD for DateTimeOriginal (36867) and DateTimeDigitized (36868)
            exif_ifd = exif_data.get_ifd(0x8769)
            if exif_ifd:
                for tag in (36867, 36868):
                    val = exif_ifd.get(tag)
                    dt = _parse_exif_datetime(val)
                    if dt:
                        return dt, val

            # Fallback: DateTime (306) from main IFD
            val = exif_data.get(306)
            dt = _parse_exif_datetime(val)
            if dt:
                return dt, val

    except Exception:
        pass
    return None, None


def extract_exif_date_arw(filepath):
    """Extract date from Sony ARW (TIFF-based) using struct parsing."""
    try:
        with open(filepath, 'rb') as f:
            header = f.read(8)
            if len(header) < 8:
                return None, None

            if header[:2] == b'II':
                endian = '<'
            elif header[:2] == b'MM':
                endian = '>'
            else:
                return None, None

            magic = struct.unpack(endian + 'H', header[2:4])[0]
            if magic != 42:
                return None, None

            ifd_offset = struct.unpack(endian + 'I', header[4:8])[0]

            exif_ifd_offset = None
            datetime_val = None

            # Walk main IFD
            f.seek(ifd_offset)
            num_entries = struct.unpack(endian + 'H', f.read(2))[0]
            for _ in range(num_entries):
                entry = f.read(12)
                if len(entry) < 12:
                    break
                tag, typ, count = struct.unpack(endian + 'HHI', entry[:8])
                raw = entry[8:12]

                if tag == 34665:  # EXIF sub-IFD pointer
                    exif_ifd_offset = struct.unpack(endian + 'I', raw)[0]
                elif tag == 306:  # DateTime
                    off = struct.unpack(endian + 'I', raw)[0]
                    pos = f.tell()
                    f.seek(off)
                    datetime_val = f.read(19).decode('ascii', errors='ignore')
                    f.seek(pos)

            # Read EXIF sub-IFD
            if exif_ifd_offset:
                f.seek(exif_ifd_offset)
                num_entries = struct.unpack(endian + 'H', f.read(2))[0]
                for _ in range(num_entries):
                    entry = f.read(12)
                    if len(entry) < 12:
                        break
                    tag, typ, count = struct.unpack(endian + 'HHI', entry[:8])
                    raw = entry[8:12]

                    if tag in (36867, 36868):  # DateTimeOriginal, DateTimeDigitized
                        off = struct.unpack(endian + 'I', raw)[0]
                        pos = f.tell()
                        f.seek(off)
                        val = f.read(19).decode('ascii', errors='ignore')
                        f.seek(pos)
                        dt = _parse_exif_datetime(val)
                        if dt:
                            return dt, val

            # Fallback to main IFD DateTime
            dt = _parse_exif_datetime(datetime_val)
            if dt:
                return dt, datetime_val

    except Exception:
        pass
    return None, None


def extract_date_from_filename(filename):
    """Try to parse a date from the filename using known patterns."""
    for pattern in FILENAME_PATTERNS:
        m = pattern.search(filename)
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 1990 <= y <= 2030 and 1 <= mo <= 12 and 1 <= d <= 31:
                    return datetime.date(y, mo, d)
            except (ValueError, IndexError):
                continue
    return None


def extract_date_from_dirpath(filepath, source_root):
    """Try to parse a date from parent directory names."""
    rel = os.path.relpath(filepath, source_root)
    parts = rel.split(os.sep)

    # Check for date-like directory names: "2017-06-17", "2022-10-01"
    for part in parts[:-1]:  # skip the filename itself
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', part)
        if m:
            try:
                return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass

    # Check for YEAR/MONTH structure: "2017/05" -> 2017-05-01
    if len(parts) >= 2:
        year_match = re.match(r'^(\d{4})$', parts[0])
        if year_match:
            y = int(year_match.group(1))
            if 1990 <= y <= 2030:
                if len(parts) >= 3:
                    month_match = re.match(r'^(\d{1,2})$', parts[1])
                    if month_match:
                        mo = int(month_match.group(1))
                        if 1 <= mo <= 12:
                            return datetime.date(y, mo, 1)
                return datetime.date(y, 1, 1)

    return None


def extract_date(filepath, source_root):
    """Extract the best date for a file. Returns (date, exif_datetime_str, source_method)."""
    ext = os.path.splitext(filepath)[1].lower()
    filename = os.path.basename(filepath)

    # 1. EXIF (for images and RAW)
    dt, exif_str = None, None
    if ext in ('.jpg', '.jpeg', '.png'):
        dt, exif_str = extract_exif_date_jpg(filepath)
    elif ext == '.arw':
        dt, exif_str = extract_exif_date_arw(filepath)

    if dt:
        return dt, exif_str, 'exif'

    # 2. Filename patterns
    dt = extract_date_from_filename(filename)
    if dt:
        return dt, None, 'filename'

    # 3. Directory path
    dt = extract_date_from_dirpath(filepath, source_root)
    if dt:
        return dt, None, 'dirname'

    # 4. File modification time
    try:
        mtime = os.path.getmtime(filepath)
        dt = datetime.date.fromtimestamp(mtime)
        return dt, None, 'mtime'
    except OSError:
        pass

    return None, None, 'unknown'
