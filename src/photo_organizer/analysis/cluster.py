"""Cluster face embeddings from photo_analysis.jsonl into person groups (DBSCAN, cosine)."""

import argparse
import json
from collections import defaultdict

import numpy as np
from sklearn.cluster import DBSCAN

from photo_organizer.analysis.paths import add_data_dir_arg, data_paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eps", type=float, default=0.5,
                        help="Cosine distance threshold; lower = stricter (default: 0.5)")
    parser.add_argument("--min-samples", type=int, default=3,
                        help="Min faces to call something a person (default: 3)")
    add_data_dir_arg(parser)
    args = parser.parse_args()
    paths = data_paths(args)

    embeddings, refs = [], []
    with paths.analysis.open() as f:
        for line in f:
            rec = json.loads(line)
            for idx, face in enumerate(rec.get("faces", [])):
                embeddings.append(face["embedding"])
                refs.append({"uuid": rec["uuid"], "filename": rec["filename"], "face_idx": idx})

    X = np.array(embeddings)
    labels = DBSCAN(eps=args.eps, min_samples=args.min_samples, metric="cosine").fit_predict(X)

    clusters = defaultdict(list)
    for ref, label in zip(refs, labels):
        clusters[f"person_{label}" if label >= 0 else "unclustered"].append(ref)

    paths.clusters.write_text(json.dumps(dict(clusters), indent=2))
    print(f"Found {len([k for k in clusters if k != 'unclustered'])} person clusters "
          f"from {len(embeddings)} faces across {len(set(r['uuid'] for r in refs))} photos.")


if __name__ == "__main__":
    main()
