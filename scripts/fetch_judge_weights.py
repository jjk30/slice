"""Fetch the trained Qwen routing-judge weights from S3 onto the box.

    JUDGE_WEIGHTS_BUCKET=slice-judge-weights-... python -m scripts.fetch_judge_weights

Downloads judge_lora.zip and judge_merged.zip from the private judge-weights
bucket (see infra/ec2/main.tf) into a local directory and unzips each into its
own subfolder:

    <dir>/judge_lora.zip    -> <dir>/judge_lora/
    <dir>/judge_merged.zip  -> <dir>/judge_merged/

The directory defaults to ./judge_weights and JUDGE_WEIGHTS_DIR overrides it.
A file is skipped when its unzipped subfolder already exists, unless --force.
The merged model is about 741 MB, so a small box that only needs the LoRA
adapter can pass --lora-only and never pull it.

This is a fetch tool, not part of the server: it loads no model and imports
nothing heavy. boto3 (already a dependency) does the download.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import zipfile
from pathlib import Path

# The two artifacts the Colab notebook produces (see colab/README.md). Each name
# is the zip in the bucket; its unzipped subfolder is the name without ".zip".
LORA_ZIP = "judge_lora.zip"
MERGED_ZIP = "judge_merged.zip"

DEFAULT_DIR = "judge_weights"


def _subfolder(dest_dir: Path, zip_name: str) -> Path:
    """The folder a zip unzips into: the zip name without its .zip suffix."""
    return dest_dir / zip_name[: -len(".zip")]


def _fetch_one(client, bucket: str, zip_name: str, dest_dir: Path, force: bool) -> str:
    """Download and unzip one file. Returns a one-line status: fetched or skipped."""
    subfolder = _subfolder(dest_dir, zip_name)

    if subfolder.exists() and not force:
        return f"skipped {zip_name} ({subfolder}/ already exists)"

    # On --force, drop any existing unzipped folder so the refetch is clean.
    if subfolder.exists():
        shutil.rmtree(subfolder)

    dest_dir.mkdir(parents=True, exist_ok=True)
    local_zip = dest_dir / zip_name
    client.download_file(bucket, zip_name, str(local_zip))

    subfolder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(local_zip) as zf:
        zf.extractall(subfolder)

    return f"fetched {zip_name} -> {subfolder}/"


def run(argv=None, client=None) -> int:
    parser = argparse.ArgumentParser(
        prog="fetch_judge_weights",
        description="Fetch the trained Qwen routing-judge weights from S3.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refetch even when the unzipped subfolder already exists.",
    )
    parser.add_argument(
        "--lora-only",
        action="store_true",
        help="Fetch only judge_lora.zip, skipping the ~741 MB merged model.",
    )
    args = parser.parse_args(argv)

    bucket = os.getenv("JUDGE_WEIGHTS_BUCKET")
    if not bucket:
        print(
            "error: set JUDGE_WEIGHTS_BUCKET to the judge-weights bucket name "
            "(see the judge_weights_bucket Terraform output).",
            file=sys.stderr,
        )
        return 1

    dest_dir = Path(os.getenv("JUDGE_WEIGHTS_DIR") or DEFAULT_DIR)

    zips = [LORA_ZIP] if args.lora_only else [LORA_ZIP, MERGED_ZIP]

    if client is None:
        import boto3  # local import so the script loads fast and tests can inject a fake.

        client = boto3.client("s3")

    for zip_name in zips:
        print(_fetch_one(client, bucket, zip_name, dest_dir, args.force))

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
