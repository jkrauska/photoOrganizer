"""Enrich photo_analysis.jsonl with ffprobe, exiftool, and Nominatim reverse-geocoding.

Writes a parallel metadata.jsonl keyed by uuid. Resumes on rerun.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from photo_organizer.analysis.paths import add_data_dir_arg, data_paths, load_done_uuids

# Nominatim usage policy: <=1 req/sec and a unique, contactable User-Agent. There is no
# honest default for the contact, so geocoding asks for one.
# https://operations.osmfoundation.org/policies/nominatim/
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_UA = "photo-organizer/0.1 ({contact})"
NOMINATIM_DELAY = 1.1
GEO_CACHE_PRECISION = 3  # decimal places (~110m) for cache key


def run_ffprobe(path: str) -> dict | None:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return {"_error": result.stderr.strip()[:500]}
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def run_exiftool(path: str) -> dict | None:
    try:
        result = subprocess.run(
            ["exiftool", "-json", "-n", "-coordFormat", "%.6f", path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return {"_error": result.stderr.strip()[:500]}
        data = json.loads(result.stdout)
        return data[0] if data else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def extract_latlon(exif: dict) -> tuple[float, float] | None:
    if not exif or "_error" in exif:
        return None
    lat = exif.get("GPSLatitude")
    lon = exif.get("GPSLongitude")
    if lat is None or lon is None:
        return None
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def reverse_geocode(lat: float, lon: float, cache: dict, user_agent: str) -> dict | None:
    key = (round(lat, GEO_CACHE_PRECISION), round(lon, GEO_CACHE_PRECISION))
    if key in cache:
        return cache[key]

    params = urllib.parse.urlencode({
        "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
        "format": "json", "zoom": 14, "addressdetails": 1,
    })
    req = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}",
        headers={"User-Agent": user_agent, "Accept-Language": "en"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        result = {
            "display_name": data.get("display_name"),
            "address": data.get("address", {}),
        }
    except Exception as e:
        result = {"_error": f"{type(e).__name__}: {e}"}
    cache[key] = result
    time.sleep(NOMINATIM_DELAY)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", "--limit", type=int, default=None,
                        help="Process at most N records (default: all unprocessed).")
    parser.add_argument("--no-geocode", action="store_true",
                        help="Skip Nominatim reverse-geocoding.")
    parser.add_argument("--contact", default=os.environ.get("PHOTO_ORGANIZER_CONTACT"),
                        help="Your real email or project URL for the Nominatim User-Agent, as its "
                             "usage policy requires; placeholders are answered with 403 "
                             "(default: $PHOTO_ORGANIZER_CONTACT).")
    add_data_dir_arg(parser)
    args = parser.parse_args()
    paths = data_paths(args)

    if not args.no_geocode and not args.contact:
        sys.exit("Reverse-geocoding needs a contact for the Nominatim User-Agent: pass --contact "
                 "with your real email or project URL (or set $PHOTO_ORGANIZER_CONTACT), or pass "
                 "--no-geocode. Nominatim answers 403 to placeholder contacts.")
    user_agent = NOMINATIM_UA.format(contact=args.contact)

    if not shutil.which("exiftool"):
        sys.exit("exiftool not found in PATH. Install with: brew install exiftool")
    if not shutil.which("ffprobe"):
        sys.exit("ffprobe not found in PATH. Install with: brew install ffmpeg")
    if not paths.analysis.exists():
        sys.exit(f"{paths.analysis} not found. Run photo-analyze first.")

    done = load_done_uuids(paths.metadata)
    print(f"Already enriched: {len(done)}")

    geo_cache: dict = {}
    processed = 0

    with paths.analysis.open() as src, paths.metadata.open("a") as out:
        for line in src:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            uuid = rec.get("uuid")
            path = rec.get("path")
            if not uuid or not path or uuid in done:
                continue
            if args.limit is not None and processed >= args.limit:
                break

            start = time.time()
            entry = {"uuid": uuid, "filename": rec.get("filename"), "path": path}
            entry["ffprobe"] = run_ffprobe(path)
            exif = run_exiftool(path)
            entry["exif"] = exif

            latlon = extract_latlon(exif)
            if latlon:
                entry["latitude"], entry["longitude"] = latlon
                if not args.no_geocode:
                    entry["location"] = reverse_geocode(*latlon, geo_cache, user_agent)

            out.write(json.dumps(entry) + "\n")
            out.flush()
            processed += 1
            loc_note = ""
            if entry.get("location") and entry["location"].get("display_name"):
                loc_note = f" — {entry['location']['display_name'][:60]}"
            print(f"[{processed}] {entry['filename']} ({time.time()-start:.1f}s){loc_note}")

    print(f"\nDone. Wrote {processed} new entries to {paths.metadata}.")
    print(f"Geocode cache hits saved ~{len(geo_cache)} unique lookups.")


if __name__ == "__main__":
    main()
