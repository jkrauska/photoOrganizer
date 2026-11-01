"""Local web app to browse face clusters and label them.

Run:   uv run photo-face-browser
Then:  http://localhost:8000/ (opens automatically)

Reads face_clusters.json + photo_analysis.jsonl.
Writes labels to face_labels.json (cluster_id -> person name).
Caches face crops in face_crops/.
"""

import argparse
import json
import webbrowser
from pathlib import Path
from threading import Timer

import pillow_heif
import uvicorn
from PIL import Image
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from photo_organizer.analysis.paths import add_data_dir_arg, data_paths

pillow_heif.register_heif_opener()

# Set from --data-dir in main() before the server starts.
CLUSTERS_FILE: Path
ANALYSIS_FILE: Path
LABELS_FILE: Path
CROP_DIR: Path
CROP_THUMB_SIZE = 256

face_index: dict[str, list[dict]] = {}   # uuid -> [{bbox, embedding, ...}, ...]
photo_paths: dict[str, str] = {}         # uuid -> path used at analysis time
cluster_data: dict[str, list[dict]] = {} # cluster_id -> [{uuid, filename, face_idx}, ...]


def load_data():
    if not ANALYSIS_FILE.exists():
        raise SystemExit(f"{ANALYSIS_FILE} not found. Run photo-analyze first.")
    if not CLUSTERS_FILE.exists():
        raise SystemExit(f"{CLUSTERS_FILE} not found. Run photo-cluster-faces first.")

    with ANALYSIS_FILE.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            face_index[rec["uuid"]] = rec.get("faces", [])
            # Prefer analysis_path (e.g. JPEG derivative for RAW); fall back to path.
            src = rec.get("analysis_path") or rec.get("path")
            if src:
                photo_paths[rec["uuid"]] = src

    cluster_data.update(json.loads(CLUSTERS_FILE.read_text()))


def load_labels() -> dict:
    return json.loads(LABELS_FILE.read_text()) if LABELS_FILE.exists() else {}


def save_labels(labels: dict):
    LABELS_FILE.write_text(json.dumps(labels, indent=2, sort_keys=True))


def ensure_crop(uuid: str, face_idx: int) -> Path:
    out = CROP_DIR / f"{uuid}_{face_idx}.jpg"
    if out.exists():
        return out

    faces = face_index.get(uuid, [])
    if face_idx >= len(faces):
        raise HTTPException(404, "face not found")
    path = photo_paths.get(uuid)
    if not path or not Path(path).exists():
        raise HTTPException(404, f"source photo missing: {path}")

    try:
        with Image.open(path) as im:
            img = im.convert("RGB")
            w, h = img.size
            x1, y1, x2, y2 = faces[face_idx]["bbox"]
            pad = max(10, (x2 - x1) // 6)
            x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
            x2, y2 = min(w, x2 + pad), min(h, y2 + pad)
            if x2 <= x1 or y2 <= y1:
                raise HTTPException(500, "empty crop region")
            crop = img.crop((x1, y1, x2, y2))
            crop.thumbnail((CROP_THUMB_SIZE, CROP_THUMB_SIZE), Image.LANCZOS)
            CROP_DIR.mkdir(exist_ok=True)
            crop.save(out, "JPEG", quality=85)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"could not crop {path}: {e}")
    return out


app = FastAPI()


@app.get("/api/clusters")
def list_clusters():
    labels = load_labels()
    items = []
    unclustered = None
    for cid, faces in cluster_data.items():
        if not faces:
            continue
        entry = {
            "id": cid,
            "count": len(faces),
            "label": labels.get(cid, ""),
            "sample": f"/face/{faces[0]['uuid']}/{faces[0]['face_idx']}.jpg",
        }
        if cid == "unclustered":
            unclustered = entry
        else:
            items.append(entry)
    items.sort(key=lambda x: (-x["count"], x["id"]))
    if unclustered:
        items.append(unclustered)
    return items


@app.get("/api/cluster/{cid}")
def cluster_detail(cid: str, limit: int = 200):
    if cid not in cluster_data:
        raise HTTPException(404, "cluster not found")
    labels = load_labels()
    return [
        {
            **f,
            "face_url": f"/face/{f['uuid']}/{f['face_idx']}.jpg",
            "face_label": labels.get(f"face:{f['uuid']}:{f['face_idx']}", ""),
        }
        for f in cluster_data[cid][:limit]
    ]


@app.get("/face/{uuid}/{face_idx}.jpg")
def get_face(uuid: str, face_idx: int):
    return FileResponse(ensure_crop(uuid, face_idx), media_type="image/jpeg")


class LabelIn(BaseModel):
    cluster_id: str
    label: str


class FaceLabelIn(BaseModel):
    uuid: str
    face_idx: int
    label: str


@app.post("/api/label")
def set_label(body: LabelIn):
    labels = load_labels()
    label = body.label.strip()
    if label:
        labels[body.cluster_id] = label
    else:
        labels.pop(body.cluster_id, None)
    save_labels(labels)
    return {"ok": True, "labels": labels}


@app.post("/api/label_face")
def set_face_label(body: FaceLabelIn):
    labels = load_labels()
    key = f"face:{body.uuid}:{body.face_idx}"
    label = body.label.strip()
    if label:
        labels[key] = label
    else:
        labels.pop(key, None)
    save_labels(labels)
    return {"ok": True}


@app.get("/api/labels")
def get_labels():
    return load_labels()


HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Face Browser</title>
<style>
  body { font: 14px -apple-system, system-ui, sans-serif; margin: 0; display: flex; height: 100vh; }
  #sidebar { width: 300px; border-right: 1px solid #ddd; overflow-y: auto; padding: 8px; box-sizing: border-box; }
  #main { flex: 1; padding: 16px; overflow-y: auto; }
  .item { padding: 6px; cursor: pointer; border-radius: 6px; display: flex; align-items: center; gap: 8px; }
  .item:hover { background: #f0f0f0; }
  .item.active { background: #def; }
  .item img { width: 44px; height: 44px; object-fit: cover; border-radius: 4px; background: #eee; }
  .item .meta { flex: 1; min-width: 0; }
  .item .label { font-weight: 600; color: #333; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .item .count { color: #888; font-size: 12px; }
  #header { position: sticky; top: 0; background: white; padding: 8px 0 12px; margin-bottom: 16px; border-bottom: 1px solid #eee; }
  #header h2 { margin: 0 0 4px; font-size: 13px; color: #555; font-weight: 500; }
  #header .row { display: flex; gap: 8px; align-items: center; }
  #header input { flex: 1; padding: 12px; font-size: 18px; border: 2px solid #2a7; border-radius: 6px; outline: none; }
  #header input:focus { border-color: #1a5; box-shadow: 0 0 0 3px rgba(42,170,119,0.15); }
  #header button { padding: 12px 16px; font-size: 14px; cursor: pointer; border-radius: 6px; border: 1px solid #ccc; background: white; }
  #header button.primary { background: #2a7; color: white; border-color: #2a7; font-weight: 600; }
  .item .label.unlabeled { color: #888; font-weight: 400; font-style: italic; }
  #grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; }
  #grid figure { margin: 0; display: flex; flex-direction: column; gap: 4px; }
  #grid img { width: 100%; aspect-ratio: 1; object-fit: cover; border-radius: 6px; background: #eee; }
  #grid figcaption { font-size: 11px; color: #666; text-align: center; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  #grid .face-name { width: 100%; padding: 6px 8px; font-size: 13px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; }
  #grid .face-name:focus { outline: none; border-color: #2a7; box-shadow: 0 0 0 2px rgba(42,170,119,0.15); }
  #grid .face-name.saved { background: #e8f7ee; border-color: #2a7; }
  .empty { color: #999; padding: 40px; text-align: center; }
  #stats { color: #888; font-size: 12px; padding: 6px 4px; }
</style></head><body>
<div id="sidebar"><div class="empty">Loading…</div></div>
<div id="main"><div class="empty"><h2 style="margin:0 0 12px">How to label a face cluster</h2><ol style="text-align:left; display:inline-block; line-height:1.8"><li>Click a cluster in the left sidebar.</li><li>Type the person's name in the green-bordered input that appears at the top.</li><li>Press <kbd>Enter</kbd> (or click <b>Save &amp; Next</b>) to save and jump to the next unlabeled cluster.</li></ol></div></div>
<script>
let clusters = [];
let currentId = null;

async function loadClusters() {
  clusters = await fetch('/api/clusters').then(r => r.json());
  renderSidebar();
}

function renderSidebar() {
  const sb = document.getElementById('sidebar');
  const labeled = clusters.filter(c => c.label).length;
  sb.innerHTML = `<div id="stats">${labeled}/${clusters.length} clusters labeled · click one to name it</div>` +
    clusters.map(c => `
      <div class="item ${c.id===currentId?'active':''}" onclick="selectCluster('${c.id}')">
        <img src="${c.sample}" loading="lazy">
        <div class="meta">
          <div class="label ${c.label?'':'unlabeled'}">${escapeHtml(c.label) || '(unnamed) ' + c.id}</div>
          <div class="count">${c.count} face${c.count===1?'':'s'}${c.label?' · '+c.id:''}</div>
        </div>
      </div>
    `).join('');
}

function escapeHtml(s) {
  return (s || '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}

async function selectCluster(id) {
  currentId = id;
  renderSidebar();
  const c = clusters.find(x => x.id === id);
  const main = document.getElementById('main');
  const isUnclustered = id === 'unclustered';
  const headerHtml = isUnclustered
    ? `<h2>Unclustered faces — these didn't group automatically. Name each one below (Enter to save and move to the next).</h2>`
    : `<h2>Name this person (${c.count} face${c.count===1?'':'s'} in ${c.id}). Or override individual faces below.</h2>
       <div class="row">
         <input id="labelInput" placeholder="Bulk name for the whole cluster…" value="${escapeHtml(c.label)}" autofocus>
         <button onclick="saveLabel(false)">Save</button>
         <button class="primary" onclick="saveLabel(true)">Save &amp; Next ⏎</button>
       </div>`;
  main.innerHTML = `
    <div id="header">${headerHtml}</div>
    <div id="grid"><div class="empty">Loading faces…</div></div>
  `;
  const input = document.getElementById('labelInput');
  if (input) {
    input.focus();
    input.select();
    input.addEventListener('keydown', e => { if (e.key === 'Enter') saveLabel(true); });
  }
  const faces = await fetch(`/api/cluster/${id}`).then(r => r.json());
  document.getElementById('grid').innerHTML = faces.map(f => `
    <figure>
      <img src="${f.face_url}" loading="lazy">
      <figcaption>${escapeHtml(f.filename)}</figcaption>
      <input class="face-name ${f.face_label?'saved':''}" data-uuid="${f.uuid}" data-idx="${f.face_idx}"
             value="${escapeHtml(f.face_label)}" placeholder="name…">
    </figure>
  `).join('');
  document.querySelectorAll('#grid .face-name').forEach(inp => {
    inp.addEventListener('keydown', e => {
      if (e.key === 'Enter') {
        e.preventDefault();
        saveFaceLabel(inp);
        const next = inp.parentElement.nextElementSibling?.querySelector('.face-name');
        if (next) next.focus();
      }
    });
    inp.addEventListener('blur', () => saveFaceLabel(inp));
    inp.dataset.prev = inp.value.trim();
  });
  if (isUnclustered) {
    const firstEmpty = document.querySelector('#grid .face-name:not(.saved)');
    if (firstEmpty) firstEmpty.focus();
  }
}

async function saveFaceLabel(inp) {
  const uuid = inp.dataset.uuid;
  const idx = parseInt(inp.dataset.idx, 10);
  const label = inp.value.trim();
  const prev = inp.dataset.prev || '';
  if (label === prev) return;
  await fetch('/api/label_face', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({uuid, face_idx: idx, label}),
  });
  inp.dataset.prev = label;
  inp.classList.toggle('saved', !!label);
}

async function saveLabel(advance) {
  const input = document.getElementById('labelInput');
  const label = input.value.trim();
  await fetch('/api/label', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({cluster_id: currentId, label}),
  });
  const c = clusters.find(x => x.id === currentId);
  if (c) c.label = label;
  renderSidebar();
  if (advance) {
    const idx = clusters.findIndex(x => x.id === currentId);
    const next = clusters.slice(idx + 1).find(x => !x.label && x.id !== 'unclustered');
    if (next) selectCluster(next.id);
  }
}

loadClusters();
</script></body></html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


def main():
    parser = argparse.ArgumentParser(description="Browse and label face clusters.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address; use 0.0.0.0 to expose on the LAN (no auth)")
    parser.add_argument("--no-open", action="store_true")
    add_data_dir_arg(parser)
    args = parser.parse_args()

    global CLUSTERS_FILE, ANALYSIS_FILE, LABELS_FILE, CROP_DIR
    paths = data_paths(args)
    CLUSTERS_FILE, ANALYSIS_FILE = paths.clusters, paths.analysis
    LABELS_FILE, CROP_DIR = paths.labels, paths.crops

    load_data()
    CROP_DIR.mkdir(exist_ok=True)

    n_named = sum(1 for k in cluster_data if k != "unclustered" and cluster_data[k])
    n_faces = sum(len(v) for v in cluster_data.values())
    browser_host = "localhost" if args.host in ("0.0.0.0", "::") else args.host
    print(f"Loaded {n_named} clusters ({n_faces} faces) → http://{browser_host}:{args.port}/")
    if args.host == "0.0.0.0":
        print(f"(also reachable on the LAN at http://<this-machine>:{args.port}/)")

    if not args.no_open:
        Timer(0.6, lambda: webbrowser.open(f"http://{browser_host}:{args.port}/")).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
