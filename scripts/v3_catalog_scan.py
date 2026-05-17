#!/usr/bin/env python3
"""Scan a directory of v3 score_record sidecars into a catalog JSONL.

Usage:
    python scripts/v3_catalog_scan.py \
        --score-dir /path/to/score_records \
        --out /path/to/catalog.jsonl \
        [--score-uri-prefix s3://bucket/prefix] \
        [--bundle-uri-prefix s3://bucket/prefix]

Reads every ``*.score_record.json`` in --score-dir, builds a catalog
row per record, and appends to --out. Records that fail validation are
skipped with a warning on stderr; a summary is printed at the end.
Exit code is non-zero if zero records were found.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running this script directly without the package install: we
# walk up from scripts/ to the repo root and add src/ to sys.path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cathedral.v3.datasets.catalog import (  # noqa: E402
    CatalogValidationError,
    append_catalog_row,
    build_catalog_row,
)


def _derive_score_uri(path: Path, prefix: str | None) -> str:
    if prefix:
        # Trim trailing slash on prefix, prepend with '/' to the filename.
        return prefix.rstrip("/") + "/" + path.name
    return path.resolve().as_uri()


def _derive_bundle_uri(record: dict, prefix: str | None, eval_run_id: str) -> str | None:
    # Prefer an explicit pointer stashed by the publisher.
    explicit = record.get("bundle_url")
    if isinstance(explicit, str) and explicit:
        return explicit
    if prefix:
        return prefix.rstrip("/") + f"/{eval_run_id}.tar.gz.enc"
    return None


def _derive_manifest_uri(record: dict, prefix: str | None, eval_run_id: str) -> str | None:
    explicit = record.get("manifest_url")
    if isinstance(explicit, str) and explicit:
        return explicit
    if prefix:
        return prefix.rstrip("/") + f"/{eval_run_id}.manifest.json"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--score-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--score-uri-prefix", type=str, default=None)
    parser.add_argument("--bundle-uri-prefix", type=str, default=None)
    args = parser.parse_args(argv)

    score_dir: Path = args.score_dir
    if not score_dir.is_dir():
        print(f"score-dir does not exist or is not a directory: {score_dir}", file=sys.stderr)
        return 2

    files = sorted(score_dir.glob("*.score_record.json"))
    written = 0
    skipped = 0
    for path in files:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"skip {path.name}: cannot read/parse: {exc}", file=sys.stderr)
            skipped += 1
            continue

        eval_run_id = (
            record.get("eval_run_id")
            if isinstance(record, dict) and isinstance(record.get("eval_run_id"), str)
            else path.stem.replace(".score_record", "")
        )

        score_uri = _derive_score_uri(path, args.score_uri_prefix)
        bundle_uri = _derive_bundle_uri(record, args.bundle_uri_prefix, eval_run_id)
        manifest_uri = _derive_manifest_uri(record, args.bundle_uri_prefix, eval_run_id)

        try:
            row = build_catalog_row(
                score_record=record,
                score_uri=score_uri,
                bundle_uri=bundle_uri,
                manifest_uri=manifest_uri,
            )
        except CatalogValidationError as exc:
            print(f"skip {path.name}: {exc}", file=sys.stderr)
            skipped += 1
            continue

        append_catalog_row(args.out, row)
        written += 1

    print(
        f"v3_catalog_scan: wrote={written} skipped={skipped} out={args.out}",
        file=sys.stderr,
    )

    if written == 0:
        print("v3_catalog_scan: no records written; exiting non-zero", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
