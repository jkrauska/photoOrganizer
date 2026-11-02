"""Face labeling over the photoArk analysis DB (see ark.py): the data side of `photo-face-browser --ark`.

Clusters are faces.cluster_id; faces with no cluster form 'unclustered'. Naming writes persons +
faces.person_id with person_source='manual'. A 'suggested' name (from seed-names) can be accepted
(it becomes 'manual') or rejected (person cleared, person_source='rejected', which seed-names skips,
so it isn't suggested again). Strangers and false detections can be forgotten: their rows
and cached crops are deleted. Kept free of FastAPI so it can be tested without the analysis extra.
"""

import io
import os
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from photo_organizer.analysis.ark import _centroid, _person_id, _unit_rows, _unpack, load_image, open_db

UNCLUSTERED = 'unclustered'
CROP_THUMB_SIZE = 256
FULL_MAX_SIDE = 2048
# "Is this X?" is asked only for a clear match: cosine between the cluster's centre and the centre
# of X's hand-named faces, and its lead over the next person. Measured on the 2010-2017 data, true
# matches scored 0.6-0.7 while sisters' clusters reached 0.49 against each other.
GUESS_MIN = 0.55
GUESS_MARGIN = 0.15


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_db(db_path):
    """Create the schema if needed (ark.open_db), so later per-request connections can skip it."""
    open_db(db_path).close()


def _cluster_where(cid):
    if cid == UNCLUSTERED:
        return "f.cluster_id IS NULL", ()
    if not cid.startswith('c') or not cid[1:].lstrip('-').isdigit():
        raise KeyError(cid)
    return "f.cluster_id = ?", (int(cid[1:]),)


def _cluster_name(conn, cid):
    """The name given to a whole cluster: a manual name held by most of its faces (so a few faces
    named one by one as someone else don't count as naming the cluster)."""
    where, params = _cluster_where(cid)
    total = conn.execute(f"SELECT COUNT(*) FROM faces f WHERE {where}", params).fetchone()[0]
    row = conn.execute(f"SELECT p.id, p.name FROM faces f JOIN persons p ON p.id = f.person_id "
                       f"WHERE {where} AND f.person_source = 'manual' "
                       f"GROUP BY p.id HAVING COUNT(*) * 2 > ? ORDER BY COUNT(*) DESC LIMIT 1",
                       (*params, total)).fetchone()
    return (row['id'], row['name']) if row else (None, '')


def list_clusters(conn):
    """Clusters biggest first, then 'unclustered' last."""
    rows = conn.execute(
        "SELECT cluster_id, COUNT(*) n, SUM(person_source = 'manual') named, "
        "SUM(person_source = 'suggested') suggested, "
        "(SELECT id FROM faces g WHERE g.cluster_id IS f.cluster_id ORDER BY det_score DESC LIMIT 1) sample "
        "FROM faces f GROUP BY cluster_id").fetchall()
    items, tail = [], None
    for r in rows:
        cid = UNCLUSTERED if r['cluster_id'] is None else f"c{r['cluster_id']}"
        entry = {'id': cid, 'count': r['n'], 'named': r['named'] or 0, 'suggested': r['suggested'] or 0,
                 'label': '' if cid == UNCLUSTERED else _cluster_name(conn, cid)[1],
                 'sample': f"/face/{r['sample']}.jpg"}
        if cid == UNCLUSTERED:
            tail = entry
        else:
            items.append(entry)
    items.sort(key=lambda x: (-x['count'], x['id']))
    return items + ([tail] if tail else [])


def cluster_detail(conn, cid, limit=500):
    where, params = _cluster_where(cid)
    rows = conn.execute(
        f"SELECT f.id, f.sha256, f.face_idx, f.det_score, f.person_source, f.person_score, "
        f"p.name, i.path FROM faces f JOIN images i ON i.sha256 = f.sha256 "
        f"LEFT JOIN persons p ON p.id = f.person_id WHERE {where} "
        f"ORDER BY f.det_score DESC, f.id LIMIT ?", (*params, limit)).fetchall()
    return [{'id': r['id'], 'filename': os.path.basename(r['path']), 'path': r['path'],
             'det_score': r['det_score'], 'name': r['name'] or '', 'source': r['person_source'] or '',
             'score': r['person_score'], 'face_url': f"/face/{r['id']}.jpg"} for r in rows]


def persons(conn):
    return [r[0] for r in conn.execute("SELECT name FROM persons ORDER BY name")]


def name_faces(conn, face_ids, name):
    """Name these faces by hand; an empty name clears them. Returns faces changed."""
    name = name.strip()
    marks = ','.join('?' * len(face_ids))
    if name:
        cur = conn.execute(f"UPDATE faces SET person_id = ?, person_source = 'manual', person_score = NULL "
                           f"WHERE id IN ({marks})", (_person_id(conn, name), *face_ids))
    else:
        cur = conn.execute(f"UPDATE faces SET person_id = NULL, person_source = NULL, person_score = NULL "
                           f"WHERE id IN ({marks})", face_ids)
    conn.commit()
    return cur.rowcount


def name_cluster(conn, cid, name):
    """Name every face in a cluster, except faces named by hand as someone else (those overrides
    stay). Faces carrying the cluster's previous name follow the rename. Returns faces changed."""
    if cid == UNCLUSTERED:
        raise ValueError("name unclustered faces one by one")
    where, params = _cluster_where(cid)
    old_pid, _ = _cluster_name(conn, cid)
    ids = [r[0] for r in conn.execute(
        f"SELECT f.id FROM faces f WHERE {where} AND (f.person_source IS NOT 'manual' OR f.person_id IS ?)",
        (*params, old_pid))]
    return name_faces(conn, ids, name) if ids else 0


def accept_suggestions(conn, face_ids):
    marks = ','.join('?' * len(face_ids))
    cur = conn.execute(f"UPDATE faces SET person_source = 'manual' "
                       f"WHERE id IN ({marks}) AND person_source = 'suggested'", face_ids)
    conn.commit()
    return cur.rowcount


def reject_suggestions(conn, face_ids):
    marks = ','.join('?' * len(face_ids))
    cur = conn.execute(f"UPDATE faces SET person_id = NULL, person_source = 'rejected', person_score = NULL "
                       f"WHERE id IN ({marks}) AND person_source = 'suggested'", face_ids)
    conn.commit()
    return cur.rowcount


def forget_faces(conn, face_ids, crop_dir=None):
    """Delete these faces (embedding and box) and their cached crops: for strangers and false
    detections. An image is only scanned for faces once, so normal runs don't bring them back."""
    marks = ','.join('?' * len(face_ids))
    rows = conn.execute(f"SELECT sha256, face_idx, x1, y1, x2, y2 FROM faces WHERE id IN ({marks})",
                        face_ids).fetchall()
    cur = conn.execute(f"DELETE FROM faces WHERE id IN ({marks})", face_ids)
    conn.execute(f"DELETE FROM face_not_person WHERE face_id IN ({marks})", face_ids)
    conn.commit()
    if crop_dir:
        for r in rows:
            _crop_path(crop_dir, r).unlink(missing_ok=True)
    return cur.rowcount


def forget_cluster(conn, cid, crop_dir=None):
    """Forget the faces in a cluster that have no name (manual or suggested). Returns faces deleted."""
    where, params = _cluster_where(cid)
    ids = [r[0] for r in conn.execute(f"SELECT f.id FROM faces f WHERE {where} AND f.person_id IS NULL", params)]
    return forget_faces(conn, ids, crop_dir) if ids else 0


def _crop_path(crop_dir, r):
    x1, y1, x2, y2 = (int(round(v)) for v in (r['x1'], r['y1'], r['x2'], r['y2']))
    return Path(crop_dir) / f"{r['sha256']}_{r['face_idx']}_{x1}_{y1}_{x2}_{y2}.jpg"


def full_image(conn, face_id, max_side=FULL_MAX_SIDE):
    """JPEG bytes of the whole photo a face is in (EXIF-oriented, scaled down), with the face boxed."""
    r = conn.execute("SELECT f.x1, f.y1, f.x2, f.y2, i.path FROM faces f JOIN images i ON i.sha256 = f.sha256 "
                     "WHERE f.id = ?", (face_id,)).fetchone()
    if r is None:
        raise KeyError(face_id)
    img, _ = load_image(r['path'])
    scale = min(1.0, max_side / max(img.size))
    if scale < 1:
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    box = [v * scale for v in (r['x1'], r['y1'], r['x2'], r['y2'])]
    ImageDraw.Draw(img).rectangle(box, outline=(255, 210, 0), width=max(3, img.width // 300))
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=88)
    return buf.getvalue()


def _person_centroids(conn, min_faces=3):
    """(person ids, names, unit centroid rows) of people with enough hand-named faces."""
    by_person = {}
    for r in conn.execute("SELECT person_id, embedding FROM faces "
                          "WHERE person_source = 'manual' AND embedding IS NOT NULL"):
        by_person.setdefault(r['person_id'], []).append(_unpack(r['embedding']))
    pids = sorted(p for p, es in by_person.items() if len(es) >= min_faces)
    if not pids:
        return [], [], None
    names = dict(conn.execute("SELECT id, name FROM persons").fetchall())
    return pids, [names[p] for p in pids], np.stack([_centroid(_unit_rows(by_person[p])) for p in pids])


def _unnamed_in_cluster(conn, cid):
    where, params = _cluster_where(cid)
    return conn.execute(f"SELECT f.id, f.embedding FROM faces f WHERE {where} AND f.person_id IS NULL "
                        f"AND f.embedding IS NOT NULL", params).fetchall()


def guess_cluster(conn, cid, min_score=GUESS_MIN, margin=GUESS_MARGIN):
    """{'name', 'score'} when an unnamed cluster clearly looks like someone already named, else None.
    People that most of the cluster's unnamed faces were said not to be are skipped."""
    if cid == UNCLUSTERED or _cluster_name(conn, cid)[1]:
        return None
    rows = _unnamed_in_cluster(conn, cid)
    pids, names, C = _person_centroids(conn)
    if not rows or C is None:
        return None
    marks = ','.join('?' * len(rows))
    said_no = dict(conn.execute(f"SELECT person_id, COUNT(*) FROM face_not_person WHERE face_id IN ({marks}) "
                                f"GROUP BY person_id", [r['id'] for r in rows]).fetchall())
    s = C @ _centroid(_unit_rows([_unpack(r['embedding']) for r in rows]))
    for i, p in enumerate(pids):
        if said_no.get(p, 0) * 2 >= len(rows):
            s[i] = -1.0
    order = np.argsort(-s)
    best = float(s[order[0]])
    second = float(s[order[1]]) if len(order) > 1 else -1.0
    if best >= min_score and best - second >= margin:
        return {'name': names[order[0]], 'score': round(best, 3)}
    return None


def not_this_person(conn, cid, name):
    """Record that the cluster's unnamed faces aren't this person (kept per face, so it outlasts
    re-clustering). Returns faces recorded."""
    row = conn.execute("SELECT id FROM persons WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise KeyError(name)
    rows = _unnamed_in_cluster(conn, cid)
    conn.executemany("INSERT OR IGNORE INTO face_not_person (face_id, person_id) VALUES (?, ?)",
                     [(r['id'], row['id']) for r in rows])
    conn.commit()
    return len(rows)


def ensure_crop(conn, face_id, crop_dir):
    """Path of a cached JPEG crop of this face, cut from the EXIF-oriented image the bbox refers to.
    The cache name includes hash, index and bbox, so a re-detected face never shows a stale crop."""
    r = conn.execute("SELECT f.sha256, f.face_idx, f.x1, f.y1, f.x2, f.y2, i.path FROM faces f "
                     "JOIN images i ON i.sha256 = f.sha256 WHERE f.id = ?", (face_id,)).fetchone()
    if r is None:
        raise KeyError(face_id)
    x1, y1, x2, y2 = (int(round(v)) for v in (r['x1'], r['y1'], r['x2'], r['y2']))
    out = _crop_path(crop_dir, r)
    if out.exists():
        return out
    img, _ = load_image(r['path'])
    w, h = img.size
    pad = max(10, (x2 - x1) // 6)
    box = (max(0, x1 - pad), max(0, y1 - pad), min(w, x2 + pad), min(h, y2 + pad))
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError(f"empty crop region {box} in {w}x{h}")
    crop = img.crop(box)
    crop.thumbnail((CROP_THUMB_SIZE, CROP_THUMB_SIZE), Image.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix('.tmp')
    crop.save(tmp, 'JPEG', quality=85)
    os.replace(tmp, out)
    return out
