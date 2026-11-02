"""Face detection + Gemma scene description for files in the canonical store (photoArk).

Results live in ~/.photo-organizer/analysis.db, keyed by content hash (sha256 from the
organizer's catalog), so they follow a file wherever it sits and survive renames.

  photo-ark-analyze run --year 2010 [--no-vlm | --no-faces]   analyze stored images (resumable)
  photo-ark-analyze seed-names                                  suggest names from ~/gemma labels
  photo-ark-analyze cluster                                     group unnamed faces (DBSCAN)
  photo-ark-analyze days [--year 2010]                          summarize random day folders
  photo-ark-analyze stats

Names attach to faces (faces.person_id -> persons), never to cluster ids, so re-clustering
never loses a label. person_source records how a name got there: 'manual' (a person typed
it), 'suggested' (seed-names matched it to the centroid of faces named earlier) or 'cluster'.
"""

import argparse
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from photo_organizer.constants import PHOTO_EXTENSIONS, RAW_EXTENSIONS
from photo_organizer.db import DEFAULT_DB_PATH

DEFAULT_ANALYSIS_DB = os.path.expanduser('~/.photo-organizer/analysis.db')
DEFAULT_VLM = "mlx-community/gemma-4-31b-it-4bit"
FACE_MODEL = "insightface/buffalo_l@640"
VLM_MAX_SIDE = 1536
EMBED_DIM = 512

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    sha256 TEXT PRIMARY KEY,
    path TEXT NOT NULL,             -- dest_path in the store when analyzed
    width INTEGER, height INTEGER,  -- of the EXIF-oriented image that faces were found in
    source TEXT,                    -- 'file' | 'raw-preview'
    caption TEXT, scene TEXT, setting TEXT, text_visible TEXT,
    keywords TEXT, subjects TEXT,   -- JSON arrays
    vlm_model TEXT, vlm_raw TEXT,   -- vlm_raw: the reply when it wasn't valid JSON
    face_model TEXT,
    faces_at TEXT, vlm_at TEXT,     -- set when each stage finished (resume points)
    error TEXT
);
CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS faces (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL REFERENCES images(sha256),
    face_idx INTEGER NOT NULL,
    x1 REAL, y1 REAL, x2 REAL, y2 REAL,     -- pixels of the EXIF-oriented image
    det_score REAL, age INTEGER, gender TEXT,
    embedding BLOB,                          -- float32 x 512, L2-normalized (ArcFace)
    cluster_id INTEGER,
    person_id INTEGER REFERENCES persons(id),
    person_source TEXT,                      -- 'manual' | 'suggested' | 'cluster'
    person_score REAL,
    UNIQUE (sha256, face_idx)
);
CREATE TABLE IF NOT EXISTS face_not_person (   -- "No, this isn't them" answers in the face UI
    face_id INTEGER NOT NULL,
    person_id INTEGER NOT NULL,
    PRIMARY KEY (face_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);
CREATE INDEX IF NOT EXISTS idx_faces_cluster ON faces(cluster_id);
CREATE INDEX IF NOT EXISTS idx_images_path ON images(path);
"""

NOW = "strftime('%Y-%m-%dT%H:%M:%S', 'now')"


def open_db(path=DEFAULT_ANALYSIS_DB):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def select_images(catalog_path, year=None, prefix=None, dest=None):
    """[(sha256, dest_path)] of stored images under the year folder or path prefix, one per hash."""
    if year:
        root = dest or _store_root(catalog_path)
        prefix = os.path.join(root, str(year))
    prefix = prefix.rstrip('/') + '/'
    cat = sqlite3.connect(f"file:{catalog_path}?mode=ro", uri=True, timeout=30)
    try:
        rows = cat.execute(
            "SELECT sha256, dest_path FROM files WHERE status = 'done' AND sha256 IS NOT NULL "
            "AND substr(dest_path, 1, ?) = ? ORDER BY dest_path", (len(prefix), prefix)).fetchall()
    finally:
        cat.close()
    seen, out = set(), []
    for sha, path in rows:
        if os.path.splitext(path)[1].lower() in PHOTO_EXTENSIONS and sha not in seen:
            seen.add(sha)
            out.append((sha, path))
    return out


def _store_root(catalog_path):
    cat = sqlite3.connect(f"file:{catalog_path}?mode=ro", uri=True)
    try:
        row = cat.execute("SELECT value FROM progress WHERE key = 'run'").fetchone()
    finally:
        cat.close()
    if not row or not json.loads(row[0]).get('dest'):
        sys.exit("Can't tell the store root from the catalog; pass --prefix instead of --year")
    return json.loads(row[0])['dest']


# --- image loading ---------------------------------------------------------------------------

def _exiftool_orientation(path):
    out = subprocess.run(['exiftool', '-n', '-s3', '-Orientation', path], capture_output=True, text=True).stdout
    try:
        return int(out.strip())
    except ValueError:
        return 1


_ORIENT_OPS = {2: [Image.Transpose.FLIP_LEFT_RIGHT], 3: [Image.Transpose.ROTATE_180],
               4: [Image.Transpose.FLIP_TOP_BOTTOM], 5: [Image.Transpose.TRANSPOSE],
               6: [Image.Transpose.ROTATE_270], 7: [Image.Transpose.TRANSVERSE],
               8: [Image.Transpose.ROTATE_90]}


def load_image(path):
    """(RGB image as displayed, source). RAW files use their embedded JPEG, turned per the RAW's
    Orientation tag; everything else is decoded by PIL (HEIC via pillow_heif) and EXIF-transposed."""
    if os.path.splitext(path)[1].lower() in RAW_EXTENSIONS:
        for tag in ('-JpgFromRaw', '-PreviewImage'):
            data = subprocess.run(['exiftool', '-b', tag, path], capture_output=True).stdout
            if len(data) > 10_000:
                im = Image.open(io.BytesIO(data))
                im.load()
                for op in _ORIENT_OPS.get(_exiftool_orientation(path), []):
                    im = im.transpose(op)
                return im.convert('RGB'), 'raw-preview'
        raise ValueError("RAW file has no embedded preview")
    if os.path.splitext(path)[1].lower() in ('.heic', '.heif'):
        import pillow_heif
        pillow_heif.register_heif_opener()
    with Image.open(path) as im:
        return ImageOps.exif_transpose(im).convert('RGB'), 'file'


# --- models ----------------------------------------------------------------------------------

class FaceModel:
    """InsightFace buffalo_l (RetinaFace detector + ArcFace 512-d embedding + age/gender)."""

    def __init__(self):
        from insightface.app import FaceAnalysis
        self.app = FaceAnalysis(name="buffalo_l", providers=["CoreMLExecutionProvider", "CPUExecutionProvider"])
        self.app.prepare(ctx_id=0, det_size=(640, 640))

    def __call__(self, rgb):
        bgr = np.ascontiguousarray(np.asarray(rgb)[:, :, ::-1])
        out = []
        for f in self.app.get(bgr):
            g = getattr(f, 'gender', None)
            out.append({'bbox': [float(x) for x in f.bbox.tolist()], 'det_score': float(f.det_score),
                        'age': int(f.age) if getattr(f, 'age', None) is not None else None,
                        'gender': 'M' if g == 1 else ('F' if g == 0 else None),
                        'embedding': np.asarray(f.normed_embedding, dtype=np.float32)})
        return out


class VisionModel:
    """Gemma 4 through mlx-vlm, with the JSON prompt from analyze.py."""

    def __init__(self, name=DEFAULT_VLM):
        from mlx_vlm import load
        from mlx_vlm.utils import load_config
        self.name = name
        self.model, self.processor = load(name)
        self.config = load_config(name)

    def __call__(self, jpeg_path):
        from photo_organizer.analysis.analyze import analyze_with_gemma
        return analyze_with_gemma(self.model, self.processor, self.config, jpeg_path)

    def text(self, prompt, max_tokens=500):
        """Text-only reply parsed as JSON (or the raw reply), for the day summaries."""
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template
        formatted = apply_chat_template(self.processor, self.config, prompt, num_images=0)
        out = generate(self.model, self.processor, formatted, max_tokens=max_tokens, verbose=False)
        text = out.text if hasattr(out, 'text') else str(out)
        try:
            return json.loads(text.strip().strip('`').removeprefix('json').strip())
        except json.JSONDecodeError:
            return {'_raw_response': text, '_parse_error': True}


# --- analysis --------------------------------------------------------------------------------

def _store_faces(conn, sha, faces):
    conn.execute("DELETE FROM faces WHERE sha256 = ? AND person_source IS NOT 'manual'", (sha,))
    for i, f in enumerate(faces):
        x1, y1, x2, y2 = f['bbox']
        conn.execute(
            "INSERT OR IGNORE INTO faces (sha256, face_idx, x1, y1, x2, y2, det_score, age, gender, embedding) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sha, i, x1, y1, x2, y2, f['det_score'], f['age'], f['gender'],
             np.asarray(f['embedding'], dtype=np.float32).tobytes()))


def _list_json(v):
    return json.dumps([x for x in v if isinstance(x, str)]) if isinstance(v, list) else None


def analyze_one(conn, sha, path, face_fn=None, vlm_fn=None, vlm_name=None, tmpdir=None):
    """Run the stages not yet done for this image. The models run before anything is written, so
    the write lock is held only for the short final transaction (the face UI writes to this DB
    too). Returns faces found."""
    row = conn.execute("SELECT faces_at, vlm_at FROM images WHERE sha256 = ?", (sha,)).fetchone()
    need_faces = face_fn is not None and not (row and row['faces_at'])
    need_vlm = vlm_fn is not None and not (row and row['vlm_at'])
    if not (need_faces or need_vlm):
        return None
    upsert = "INSERT INTO images (sha256, path) VALUES (?, ?) ON CONFLICT(sha256) DO UPDATE SET path = ?"
    try:
        im, source = load_image(path)
    except Exception as e:
        conn.execute(upsert, (sha, path, path))
        conn.execute("UPDATE images SET error = ? WHERE sha256 = ?", (f"load: {e}", sha))
        conn.commit()
        return None
    faces = face_fn(im) if need_faces else None
    r = None
    if need_vlm:
        small = im.copy()
        small.thumbnail((VLM_MAX_SIDE, VLM_MAX_SIDE))
        tmp = os.path.join(tmpdir or tempfile.gettempdir(), f"{sha}.jpg")
        small.save(tmp, 'JPEG', quality=90)
        try:
            r = vlm_fn(tmp)
        finally:
            os.unlink(tmp)
    conn.execute(upsert, (sha, path, path))
    conn.execute("UPDATE images SET width = ?, height = ?, source = ?, error = NULL WHERE sha256 = ?",
                 (im.width, im.height, source, sha))
    if faces is not None:
        _store_faces(conn, sha, faces)
        conn.execute(f"UPDATE images SET face_model = ?, faces_at = {NOW} WHERE sha256 = ?", (FACE_MODEL, sha))
    if r is not None:
        raw = r.get('_raw_response') if r.get('_parse_error') else None
        conn.execute(
            f"UPDATE images SET caption = ?, scene = ?, setting = ?, text_visible = ?, keywords = ?, subjects = ?, "
            f"vlm_model = ?, vlm_raw = ?, vlm_at = {NOW} WHERE sha256 = ?",
            (r.get('caption'), r.get('scene'), r.get('setting'), r.get('text_visible'),
             _list_json(r.get('keywords')), _list_json(r.get('subjects')), vlm_name, raw, sha))
    conn.commit()
    return None if faces is None else len(faces)


def run(conn, images, face_fn=None, vlm_fn=None, vlm_name=None, log=print):
    done = conn.execute("SELECT sha256, faces_at, vlm_at FROM images").fetchall()
    finished = {r['sha256'] for r in done
                if (face_fn is None or r['faces_at']) and (vlm_fn is None or r['vlm_at'])}
    todo = [(s, p) for s, p in images if s not in finished]
    log(f"{len(images):,} images, {len(images) - len(todo):,} already done, {len(todo):,} to analyze")
    start = time.time()
    with tempfile.TemporaryDirectory(prefix='ark-analyze-') as tmpdir:
        for i, (sha, path) in enumerate(todo, 1):
            t = time.time()
            try:
                n = analyze_one(conn, sha, path, face_fn, vlm_fn, vlm_name, tmpdir)
            except Exception as e:
                conn.rollback()
                conn.execute("INSERT INTO images (sha256, path, error) VALUES (?, ?, ?) "
                             "ON CONFLICT(sha256) DO UPDATE SET error = excluded.error",
                             (sha, path, f"{type(e).__name__}: {e}"))
                conn.commit()
                log(f"[{i}/{len(todo)}] ERROR {path}: {e}")
                continue
            rate = i / (time.time() - start)
            eta = (len(todo) - i) / rate if rate else 0
            cap = conn.execute("SELECT caption FROM images WHERE sha256 = ?", (sha,)).fetchone()[0]
            log(f"[{i}/{len(todo)}] {os.path.basename(path)}  {'' if n is None else f'{n} faces  '}"
                f"{time.time() - t:.1f}s  ({rate:.2f}/s, {eta / 60:.0f} min left)"
                + (f"\n    {cap}" if vlm_fn and cap else ''))
    return len(todo)


# --- names -----------------------------------------------------------------------------------

def _unpack(blob):
    return np.frombuffer(blob, dtype=np.float32)


def load_reference_faces(data_dir):
    """{name: {'cluster': [emb, ...], 'manual': [emb, ...]}} from the earlier Apple Photos analysis
    in data_dir (~/gemma): faces named through a cluster label, and faces named one by one
    ('face:UUID:IDX' labels, which override the cluster's name)."""
    data_dir = Path(data_dir).expanduser()
    labels = json.loads((data_dir / 'face_labels.json').read_text())
    clusters = json.loads((data_dir / 'face_clusters.json').read_text())
    owner = {}
    for cid, members in clusters.items():
        if cid in labels:
            for f in members:
                owner[(f['uuid'], f['face_idx'])] = (labels[cid], 'cluster')
    for key, name in labels.items():
        if key.startswith('face:'):
            _, uuid, idx = key.split(':')
            owner[(uuid, int(idx))] = (name, 'manual')
    refs = {}
    with (data_dir / 'photo_analysis.jsonl').open() as f:
        for line in f:
            if '"embedding"' not in line:
                continue
            rec = json.loads(line)
            for idx, face in enumerate(rec.get('faces') or []):
                hit = owner.get((rec.get('uuid'), idx))
                if hit and face.get('embedding'):
                    refs.setdefault(hit[0], {'cluster': [], 'manual': []})[hit[1]].append(
                        np.asarray(face['embedding'], dtype=np.float32))
    return refs


def _unit_rows(vs):
    M = np.stack(vs).astype(np.float32)
    return M / np.linalg.norm(M, axis=1, keepdims=True)


def _centroid(M):
    c = M.mean(axis=0)
    return c / np.linalg.norm(c)


def reference_centroids(refs, trim=0.45, min_coherent=0.5):
    """{name: (centroid, faces used, note)}. A cluster-labeled set is trimmed to the faces near its
    centre; if most of it isn't near (DBSCAN can chain many people into one cluster), the set is
    not trusted and only the faces named one by one are used."""
    out = {}
    for name, sets in refs.items():
        manual = sets.get('manual', [])
        cluster = sets.get('cluster', [])
        note = ''
        if cluster:
            M = _unit_rows(cluster + manual)
            c = _centroid(M)
            for _ in range(3):
                keep = M @ c >= trim
                if not keep.any():
                    break
                c = _centroid(M[keep])
            keep = M @ c >= trim
            if keep.mean() >= min_coherent:
                out[name] = (c, int(keep.sum()), f"{int(keep.sum())} of {len(M)} faces")
                continue
            note = f"cluster set too mixed ({keep.mean():.0%} coherent); "
        if manual:
            out[name] = (_centroid(_unit_rows(manual)), len(manual), note + f"{len(manual)} hand-named faces")
        else:
            out[name] = (None, 0, note + "skipped")
    return out


def seed_names(conn, centroids, threshold=0.5, margin=0.08, min_score=0.6):
    """Suggest a person for each confidently detected face not named by hand: the reference
    person whose centroid is most similar (cosine), if it reaches threshold and beats the
    runner-up by margin. Earlier suggestions are replaced. Returns {name: count}."""
    conn.execute("UPDATE faces SET person_id = NULL, person_source = NULL, person_score = NULL "
                 "WHERE person_source = 'suggested'")
    usable = sorted(n for n, (c, _, _) in centroids.items() if c is not None)
    rows = conn.execute("SELECT id, embedding FROM faces WHERE det_score >= ? "
                        "AND (person_source IS NULL OR person_source = 'cluster')", (min_score,)).fetchall()
    counts = {}
    if not rows or not usable:
        conn.commit()
        return counts
    A = _unit_rows([_unpack(r['embedding']) for r in rows])
    C = np.stack([centroids[n][0] for n in usable])
    S = A @ C.T
    order = np.argsort(-S, axis=1)
    for i, r in enumerate(rows):
        best = S[i, order[i, 0]]
        second = S[i, order[i, 1]] if len(usable) > 1 else -1.0
        if best >= threshold and best - second >= margin:
            name = usable[order[i, 0]]
            conn.execute("UPDATE faces SET person_id = ?, person_source = 'suggested', person_score = ? WHERE id = ?",
                         (_person_id(conn, name), float(best), r['id']))
            counts[name] = counts.get(name, 0) + 1
    conn.commit()
    return counts


def _person_id(conn, name):
    conn.execute("INSERT OR IGNORE INTO persons (name) VALUES (?)", (name,))
    return conn.execute("SELECT id FROM persons WHERE name = ?", (name,)).fetchone()[0]


def _coherent(M, trim=0.45, min_frac=0.75):
    """True when most faces sit near the group's centre. One person's faces measure ~0.9 here;
    a DBSCAN chain of look-alikes (A~B~C~...) measures far lower (0.28 for a 14k-face chain)."""
    return float(np.mean(M @ _centroid(M) >= trim)) >= min_frac


def _dbscan_groups(X, idx, eps, min_samples, step=0.05, floor=0.3):
    """DBSCAN (cosine) over X[idx]; any incoherent cluster is clustered again with a tighter eps."""
    from sklearn.cluster import DBSCAN
    labels = DBSCAN(eps=eps, min_samples=min_samples, metric='cosine').fit_predict(X[idx])
    out = []
    for c in set(labels) - {-1}:
        members = idx[labels == c]
        if eps - step >= floor - 1e-9 and not _coherent(X[members]):
            out += _dbscan_groups(X, members, eps - step, min_samples, step, floor)
        else:
            out.append(members)
    return out


def cluster_faces(conn, eps=0.5, min_samples=3, min_score=0.6):
    """Group confidently detected faces (DBSCAN, cosine, chains split apart) and write cluster_id,
    numbered biggest first. Computes before writing, so the DB isn't locked meanwhile. Returns the
    cluster count."""
    rows = conn.execute("SELECT id, embedding FROM faces WHERE det_score >= ?", (min_score,)).fetchall()
    groups = []
    if len(rows) >= min_samples:
        X = _unit_rows([_unpack(r['embedding']) for r in rows])
        groups = sorted(_dbscan_groups(X, np.arange(len(rows)), eps, min_samples), key=len, reverse=True)
    conn.execute("UPDATE faces SET cluster_id = NULL")
    conn.executemany("UPDATE faces SET cluster_id = ? WHERE id = ?",
                     [(c, rows[i]['id']) for c, g in enumerate(groups) for i in g])
    conn.commit()
    return len(groups)


def stats(conn):
    q = lambda sql: conn.execute(sql).fetchone()[0]
    print(f"images: {q('SELECT COUNT(*) FROM images'):,}  faces done: {q('SELECT COUNT(*) FROM images WHERE faces_at IS NOT NULL'):,}"
          f"  described: {q('SELECT COUNT(*) FROM images WHERE vlm_at IS NOT NULL'):,}"
          f"  errors: {q('SELECT COUNT(*) FROM images WHERE error IS NOT NULL'):,}")
    print(f"faces: {q('SELECT COUNT(*) FROM faces'):,}  in clusters: {q('SELECT COUNT(*) FROM faces WHERE cluster_id IS NOT NULL'):,}"
          f"  clusters: {q('SELECT COUNT(DISTINCT cluster_id) FROM faces'):,}")
    for r in conn.execute("SELECT p.name, f.person_source, COUNT(*) n, ROUND(AVG(f.person_score), 3) s "
                          "FROM faces f JOIN persons p ON p.id = f.person_id GROUP BY 1, 2 ORDER BY n DESC"):
        print(f"  {r['name']:<24} {r['person_source']:<10} {r['n']:>6,}  avg score {r['s']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', default=DEFAULT_ANALYSIS_DB, help='analysis database')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('run', help='analyze stored images')
    p.add_argument('--catalog', default=DEFAULT_DB_PATH)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--year', type=int)
    g.add_argument('--prefix', help='store folder to analyze, e.g. /Volumes/photoArk/2010/06')
    p.add_argument('--dest', help='store root (default: from the catalog)')
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--no-faces', action='store_true')
    p.add_argument('--no-vlm', action='store_true')
    p.add_argument('--model', default=DEFAULT_VLM)
    p = sub.add_parser('seed-names', help='suggest names from the earlier Apple Photos labels')
    p.add_argument('--data-dir', default=os.environ.get('PHOTO_ANALYSIS_DIR', '~/gemma'))
    p.add_argument('--threshold', type=float, default=0.5)
    p.add_argument('--margin', type=float, default=0.08, help='lead over the second-best person')
    p = sub.add_parser('cluster', help='group faces (DBSCAN, cosine)')
    p.add_argument('--eps', type=float, default=0.5)
    p.add_argument('--min-samples', type=int, default=3)
    p = sub.add_parser('days', help='summarize day folders from sampled frames, in random order')
    p.add_argument('--catalog', default=DEFAULT_DB_PATH)
    g = p.add_mutually_exclusive_group()
    g.add_argument('--year', type=int)
    g.add_argument('--prefix', help='store folder, e.g. /Volumes/photoArk/2010 (default: whole store)')
    p.add_argument('--dest', help='store root (default: from the catalog)')
    p.add_argument('--limit', type=int, default=0, help='days to summarize this run')
    p.add_argument('--gap', type=float, default=45, help='minutes between shots that start a new burst')
    p.add_argument('--per-burst', type=int, default=3, help='frames captioned per burst at most')
    p.add_argument('--max-samples', type=int, default=9, help='frames captioned per day at most')
    p.add_argument('--seed', type=int, help='fix the random day order')
    p.add_argument('--model', default=DEFAULT_VLM)
    sub.add_parser('stats')
    args = parser.parse_args()

    conn = open_db(args.db)
    if args.cmd == 'run':
        images = select_images(args.catalog, args.year, args.prefix, args.dest)
        if args.limit:
            images = images[:args.limit]
        face_fn = None if args.no_faces else FaceModel()
        vlm_fn = None if args.no_vlm else VisionModel(args.model)
        if not face_fn and not vlm_fn:
            sys.exit("Nothing to do: both stages disabled")
        run(conn, images, face_fn, vlm_fn, None if args.no_vlm else args.model,
            log=lambda s: print(s, flush=True))
    elif args.cmd == 'days':
        from photo_organizer.analysis import days
        found = days.select_days(args.catalog, args.year, args.prefix, args.dest)
        vlm = VisionModel(args.model)
        days.run_days(conn, found, vlm, vlm.text, args.model, seed=args.seed, limit=args.limit,
                      gap_minutes=args.gap, per_burst=args.per_burst, max_samples=args.max_samples,
                      log=lambda s: print(s, flush=True))
    elif args.cmd == 'seed-names':
        t = time.time()
        centroids = reference_centroids(load_reference_faces(args.data_dir))
        print(f"Reference people ({time.time() - t:.0f}s):")
        for name, (_, _, note) in sorted(centroids.items()):
            print(f"  {name:<24} {note}")
        counts = seed_names(conn, centroids, args.threshold, args.margin)
        print(f"Suggested names for {sum(counts.values()):,} faces: {counts}")
    elif args.cmd == 'cluster':
        print(f"{cluster_faces(conn, args.eps, args.min_samples):,} clusters")
    stats(conn)
    conn.close()


if __name__ == '__main__':
    main()
