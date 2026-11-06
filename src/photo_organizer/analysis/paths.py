"""Locations of the analysis sidecar files, all under one data directory."""

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = Path(os.environ.get("PHOTO_ANALYSIS_DIR", "~/.photo-organizer/analysis")).expanduser()


@dataclass(frozen=True)
class DataPaths:
    root: Path

    @property
    def analysis(self) -> Path:
        return self.root / "photo_analysis.jsonl"

    @property
    def clusters(self) -> Path:
        return self.root / "face_clusters.json"

    @property
    def labels(self) -> Path:
        return self.root / "face_labels.json"

    @property
    def metadata(self) -> Path:
        return self.root / "metadata.jsonl"

    @property
    def db(self) -> Path:
        return self.root / "photos.db"

    @property
    def crops(self) -> Path:
        return self.root / "face_crops"


def add_data_dir_arg(parser: argparse.ArgumentParser):
    parser.add_argument(
        "--data-dir", type=Path, default=DEFAULT_DATA_DIR,
        help=f"Directory holding the sidecar files (default: $PHOTO_ANALYSIS_DIR or {DEFAULT_DATA_DIR})",
    )


def data_paths(args) -> DataPaths:
    root = args.data_dir.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return DataPaths(root)


def load_done_uuids(path: Path) -> set:
    if not path.exists():
        return set()
    done = set()
    with path.open() as f:
        for line in f:
            try:
                done.add(json.loads(line)["uuid"])
            except (json.JSONDecodeError, KeyError):
                continue
    return done
