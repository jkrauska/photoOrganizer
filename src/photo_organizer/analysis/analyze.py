"""Analyze Apple Photos with Gemma 4 + InsightFace. Output: JSONL sidecar."""

import argparse
import json
import time
from pathlib import Path

import numpy as np
import osxphotos
import pillow_heif
from PIL import Image
from insightface.app import FaceAnalysis
from mlx_vlm import load, generate
from mlx_vlm.prompt_utils import apply_chat_template
from mlx_vlm.utils import load_config

from photo_organizer.analysis.paths import add_data_dir_arg, data_paths, load_done_uuids

pillow_heif.register_heif_opener()  # PIL can now open HEIC/HEIF

# RAW formats PIL can't decode; we fall back to Apple's JPEG derivative.
RAW_EXTS = {".arw", ".cr2", ".cr3", ".nef", ".dng", ".raf", ".raw", ".orf", ".rw2", ".pef", ".srw"}

DEFAULT_MODEL = "mlx-community/gemma-4-31b-it-4bit"
MAX_TOKENS = 400

PROMPT = """Analyze this photo and respond with ONLY a valid JSON object, no other text.

{
  "caption": "A natural one-sentence description of what's happening",
  "keywords": ["5-10 lowercase tags useful for search"],
  "scene": "indoor | outdoor | mixed | unclear",
  "subjects": ["primary subjects, e.g. 'two people', 'golden retriever'"],
  "setting": "brief phrase locating the photo",
  "text_visible": "any readable text in the image, or null"
}"""


def resolve_image_path(photo) -> str | None:
    """Return a path mlx-vlm + PIL can open: HEIC directly, RAW via Apple's largest JPEG derivative."""
    if not photo.path:
        return None
    ext = Path(photo.path).suffix.lower()
    if ext in RAW_EXTS:
        derivs = photo.path_derivatives or []
        return derivs[0] if derivs else None
    return photo.path


def analyze_faces(face_app: FaceAnalysis, image_path: str) -> list:
    """Run InsightFace; return list of face dicts."""
    try:
        with Image.open(image_path) as im:
            arr = np.array(im.convert("RGB"))
    except Exception:
        return []
    # InsightFace expects BGR (OpenCV ordering).
    arr_bgr = np.ascontiguousarray(arr[:, :, ::-1])
    faces = face_app.get(arr_bgr)
    return [
        {
            "bbox": [int(x) for x in f.bbox.tolist()],
            "det_score": float(f.det_score),
            "age": int(f.age) if hasattr(f, "age") else None,
            "gender": "M" if getattr(f, "gender", None) == 1 else ("F" if getattr(f, "gender", None) == 0 else None),
            "embedding": f.normed_embedding.tolist(),  # 512 floats
        }
        for f in faces
    ]


def analyze_with_gemma(model, processor, config, image_path: str) -> dict:
    """Run Gemma 4 vision; return parsed JSON or raw fallback."""
    formatted = apply_chat_template(processor, config, PROMPT, num_images=1)
    response = generate(
        model, processor, formatted,
        image=[image_path], max_tokens=MAX_TOKENS, verbose=False,
    )
    text = response.text if hasattr(response, "text") else str(response)
    try:
        return json.loads(text.strip().strip("`").removeprefix("json").strip())
    except json.JSONDecodeError:
        return {"_raw_response": text, "_parse_error": True}


def main():
    parser = argparse.ArgumentParser(description="Analyze Apple Photos with Gemma 4 + InsightFace.")
    parser.add_argument("-n", "--limit", type=int, default=None,
                        help="Process at most N photos (default: all unprocessed).")
    parser.add_argument("--album", default=None,
                        help="Restrict to a Photos album by name.")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"MLX vision model (default: {DEFAULT_MODEL})")
    add_data_dir_arg(parser)
    args = parser.parse_args()
    output_file = data_paths(args).analysis

    print(f"Loading vision model: {args.model}...")
    model, processor = load(args.model)
    config = load_config(args.model)

    print("Loading InsightFace (buffalo_l)...")
    face_app = FaceAnalysis(
        name="buffalo_l",
        providers=["CoreMLExecutionProvider", "CPUExecutionProvider"],
    )
    face_app.prepare(ctx_id=0, det_size=(640, 640))

    print("Reading Photos library...")
    db = osxphotos.PhotosDB()
    photos = db.photos(albums=[args.album]) if args.album else db.photos()
    photos = [p for p in photos if not p.ismovie and p.path]
    # Photos with faces (per Apple's own detection) first, then newest first
    # within each bucket. Photos without a date land at the very end.
    photos.sort(key=lambda p: (
        not bool(p.face_info),
        -(p.date.timestamp() if p.date else 0),
    ))
    n_with_faces = sum(1 for p in photos if p.face_info)

    done = load_done_uuids(output_file)
    todo = [p for p in photos if p.uuid not in done]
    if args.limit:
        todo = todo[:args.limit]

    print(f"Total: {len(photos)} ({n_with_faces} with detected faces) "
          f"| Done: {len(done)} | To process: {len(todo)}")

    with output_file.open("a") as out:
        for i, photo in enumerate(todo, 1):
            start = time.time()
            analysis_path = resolve_image_path(photo)
            record = {
                "uuid": photo.uuid,
                "filename": photo.original_filename,
                "date": photo.date.isoformat() if photo.date else None,
                "path": photo.path,
            }
            if analysis_path and analysis_path != photo.path:
                record["analysis_path"] = analysis_path

            if not analysis_path:
                record["skipped"] = "no readable image (RAW with no derivative)"
                out.write(json.dumps(record) + "\n")
                out.flush()
                print(f"[{i}/{len(todo)}] {photo.original_filename} — skipped (no derivative)")
                continue

            try:
                record["faces"] = analyze_faces(face_app, analysis_path)
            except Exception as e:
                record["faces_error"] = str(e)

            try:
                record.update(analyze_with_gemma(model, processor, config, analysis_path))
            except Exception as e:
                record["gemma_error"] = str(e)

            out.write(json.dumps(record) + "\n")
            out.flush()
            print(f"[{i}/{len(todo)}] {photo.original_filename} "
                  f"({len(record.get('faces', []))} faces, {time.time()-start:.1f}s)")
            if record.get("caption"):
                print(f'  "caption": "{record["caption"]}"')


if __name__ == "__main__":
    main()
