"""Build photos.db from the JSONL sidecars for browsing with Datasette.

Inputs (all optional except photo_analysis.jsonl):
  photo_analysis.jsonl   — faces + Gemma fields per photo
  face_clusters.json     — DBSCAN cluster assignments
  face_labels.json       — cluster_id -> person name
  metadata.jsonl         — exiftool + ffprobe + reverse-geocode per photo

Output:
  photos.db              — SQLite, browse with `uv run datasette <data-dir>/photos.db -o`

Idempotent: drops and rebuilds all tables on each run.
"""

import argparse
import json
import sqlite3
from pathlib import Path

from photo_organizer.analysis.paths import DataPaths, add_data_dir_arg, data_paths


SCHEMA = """
DROP TABLE IF EXISTS photos;
DROP TABLE IF EXISTS keywords;
DROP TABLE IF EXISTS subjects;
DROP TABLE IF EXISTS faces;
DROP TABLE IF EXISTS photos_fts;

CREATE TABLE photos (
  uuid TEXT PRIMARY KEY,
  filename TEXT,
  path TEXT,
  date TEXT,
  caption TEXT,
  scene TEXT,
  setting TEXT,
  text_visible TEXT,
  face_count INTEGER,
  persons TEXT,
  latitude REAL,
  longitude REAL,
  location TEXT,
  city TEXT,
  state TEXT,
  country TEXT,
  camera_make TEXT,
  camera_model TEXT,
  iso INTEGER,
  fnumber REAL,
  exposure_time REAL,
  focal_length REAL,
  width INTEGER,
  height INTEGER,
  format TEXT,
  duration REAL
);

CREATE TABLE keywords (photo_uuid TEXT, keyword TEXT);
CREATE INDEX idx_keywords_keyword ON keywords(keyword);
CREATE INDEX idx_keywords_uuid ON keywords(photo_uuid);

CREATE TABLE subjects (photo_uuid TEXT, subject TEXT);
CREATE INDEX idx_subjects_subject ON subjects(subject);

CREATE TABLE faces (
  photo_uuid TEXT,
  face_idx INTEGER,
  cluster_id TEXT,
  person TEXT,
  age INTEGER,
  gender TEXT,
  det_score REAL,
  bbox_x1 INTEGER, bbox_y1 INTEGER, bbox_x2 INTEGER, bbox_y2 INTEGER,
  PRIMARY KEY (photo_uuid, face_idx)
);
CREATE INDEX idx_faces_person ON faces(person);
CREATE INDEX idx_faces_cluster ON faces(cluster_id);

CREATE VIRTUAL TABLE photos_fts USING fts5(
  uuid UNINDEXED,
  caption, keywords, subjects, persons,
  scene, setting, location, text_visible,
  tokenize='porter unicode61'
);
"""


def load_face_owner_map(paths: DataPaths) -> dict:
    """Return {(uuid, face_idx): (cluster_id, person_name_or_None)}.

    Per-face overrides (keys like 'face:UUID:IDX') in face_labels.json take
    precedence over cluster-level labels.
    """
    if not paths.clusters.exists():
        return {}
    clusters = json.loads(paths.clusters.read_text())
    labels = json.loads(paths.labels.read_text()) if paths.labels.exists() else {}
    out = {}
    for cid, faces in clusters.items():
        cluster_person = labels.get(cid)
        for f in faces:
            key = (f["uuid"], f["face_idx"])
            per_face = labels.get(f"face:{f['uuid']}:{f['face_idx']}")
            out[key] = (cid, per_face or cluster_person)
    return out


def load_metadata_map(metadata: Path) -> dict:
    """Return {uuid: parsed metadata dict} from metadata.jsonl."""
    if not metadata.exists():
        return {}
    out = {}
    with metadata.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
                out[rec["uuid"]] = rec
            except (json.JSONDecodeError, KeyError):
                continue
    return out


def first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def to_int(v):
    try:
        return int(float(v)) if v is not None else None
    except (TypeError, ValueError):
        return None


def to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def extract_meta_fields(meta: dict) -> dict:
    """Pull flat fields out of an enrichment record."""
    if not meta:
        return {}
    exif = meta.get("exif") or {}
    ffp = meta.get("ffprobe") or {}
    loc = meta.get("location") or {}
    addr = loc.get("address") or {}

    # ffprobe: pick first video stream for dimensions
    width = height = None
    for s in ffp.get("streams") or []:
        if s.get("codec_type") == "video":
            width = to_int(s.get("width"))
            height = to_int(s.get("height"))
            break
    if width is None:
        width = to_int(exif.get("ImageWidth") or exif.get("ExifImageWidth"))
        height = to_int(exif.get("ImageHeight") or exif.get("ExifImageHeight"))
    fmt = (ffp.get("format") or {}).get("format_name")
    duration = to_float((ffp.get("format") or {}).get("duration"))

    return {
        "latitude": to_float(meta.get("latitude")),
        "longitude": to_float(meta.get("longitude")),
        "location": loc.get("display_name") if "_error" not in loc else None,
        "city": first(addr, "city", "town", "village", "hamlet", "suburb"),
        "state": first(addr, "state", "region", "province"),
        "country": addr.get("country"),
        "camera_make": exif.get("Make"),
        "camera_model": exif.get("Model"),
        "iso": to_int(exif.get("ISO")),
        "fnumber": to_float(exif.get("FNumber")),
        "exposure_time": to_float(exif.get("ExposureTime")),
        "focal_length": to_float(exif.get("FocalLength")),
        "width": width,
        "height": height,
        "format": fmt,
        "duration": duration,
    }


def main():
    parser = argparse.ArgumentParser(description="Build photos.db from the analysis sidecars.")
    add_data_dir_arg(parser)
    paths = data_paths(parser.parse_args())
    out_db = paths.db

    if not paths.analysis.exists():
        raise SystemExit(f"{paths.analysis} not found. Run photo-analyze first.")

    face_owners = load_face_owner_map(paths)
    meta_map = load_metadata_map(paths.metadata)

    out_db.unlink(missing_ok=True)
    conn = sqlite3.connect(out_db)
    conn.executescript(SCHEMA)
    cur = conn.cursor()

    n_photos = n_faces = n_keywords = n_subjects = 0

    with paths.analysis.open() as src:
        for line in src:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            uuid = rec.get("uuid")
            if not uuid:
                continue

            faces_raw = rec.get("faces", [])
            persons = []
            for idx, f in enumerate(faces_raw):
                cid, person = face_owners.get((uuid, idx), (None, None))
                if person:
                    persons.append(person)
                bbox = f.get("bbox") or [None, None, None, None]
                cur.execute(
                    "INSERT INTO faces VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid, idx, cid, person, f.get("age"), f.get("gender"),
                     f.get("det_score"), bbox[0], bbox[1], bbox[2], bbox[3]),
                )
                n_faces += 1
            persons_str = ", ".join(sorted(set(persons))) if persons else None

            keywords = [k for k in (rec.get("keywords") or []) if isinstance(k, str)]
            for k in keywords:
                cur.execute("INSERT INTO keywords VALUES (?, ?)", (uuid, k.lower()))
                n_keywords += 1

            subjects = [s for s in (rec.get("subjects") or []) if isinstance(s, str)]
            for s in subjects:
                cur.execute("INSERT INTO subjects VALUES (?, ?)", (uuid, s.lower()))
                n_subjects += 1

            meta = extract_meta_fields(meta_map.get(uuid, {}))

            cur.execute(
                """INSERT INTO photos VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    uuid, rec.get("filename"), rec.get("path"), rec.get("date"),
                    rec.get("caption"), rec.get("scene"), rec.get("setting"),
                    rec.get("text_visible"),
                    len(faces_raw), persons_str,
                    meta.get("latitude"), meta.get("longitude"),
                    meta.get("location"), meta.get("city"),
                    meta.get("state"), meta.get("country"),
                    meta.get("camera_make"), meta.get("camera_model"),
                    meta.get("iso"), meta.get("fnumber"),
                    meta.get("exposure_time"), meta.get("focal_length"),
                    meta.get("width"), meta.get("height"),
                    meta.get("format"), meta.get("duration"),
                ),
            )

            cur.execute(
                "INSERT INTO photos_fts VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    uuid,
                    rec.get("caption") or "",
                    " ".join(keywords),
                    " ".join(subjects),
                    persons_str or "",
                    rec.get("scene") or "",
                    rec.get("setting") or "",
                    meta.get("location") or "",
                    rec.get("text_visible") or "",
                ),
            )
            n_photos += 1

    conn.commit()
    conn.close()

    size_mb = out_db.stat().st_size / (1024 * 1024)
    print(f"Wrote {out_db} ({size_mb:.1f} MB)")
    print(f"  {n_photos} photos · {n_faces} faces · "
          f"{n_keywords} keyword rows · {n_subjects} subject rows")
    print(f"  with metadata enrichment: {sum(1 for u in meta_map)} photos")
    print()
    print(f"Browse:  uv run datasette '{out_db}' -o")


if __name__ == "__main__":
    main()
