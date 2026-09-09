"""Shell-level tests for infra/ec2/scripts/fetch_judge_model.sh.

The script is run for real under bash with a fake `aws` on PATH (a tiny shim that
records its call and writes the destination file), so no network and no real model
are touched. Covers: it skips the download when the model file already exists, and it
downloads when the file is missing. Skipped entirely where bash is unavailable.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "infra" / "ec2" / "scripts" / "fetch_judge_model.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")

# A fake `aws` CLI: append the args to $AWS_CALLS_LOG and, for `s3 cp SRC DEST`, write
# a nonempty file at DEST so the script's download path produces a real file.
FAKE_AWS = """#!/usr/bin/env bash
echo "$@" >> "$AWS_CALLS_LOG"
if [ "$1" = "s3" ] && [ "$2" = "cp" ]; then
  printf 'fake-model-bytes' > "$4"
fi
"""


def _run(tmp_path: Path, *args: str):
    """Run the script with a fake aws on PATH; return (CompletedProcess, calls_log)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_aws = bindir / "aws"
    fake_aws.write_text(FAKE_AWS)
    fake_aws.chmod(fake_aws.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    calls_log = tmp_path / "aws_calls.log"
    calls_log.write_text("")

    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "AWS_CALLS_LOG": str(calls_log),
        "JUDGE_MODELS_DIR": str(tmp_path / "models"),
        "JUDGE_WEIGHTS_BUCKET": "slice-judge-weights-test",
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
    )
    return proc, calls_log


def test_skips_when_model_already_present(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    dest = models / "judge_q4.gguf"
    dest.write_text("already here")  # nonempty existing file

    proc, calls_log = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert calls_log.read_text() == ""  # aws never called: the download was skipped.
    assert dest.read_text() == "already here"  # existing file left untouched.
    assert "skipping" in proc.stdout


def test_downloads_when_model_missing(tmp_path):
    proc, calls_log = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    calls = calls_log.read_text()
    assert "s3 cp s3://slice-judge-weights-test/judge_q4.gguf" in calls
    dest = tmp_path / "models" / "judge_q4.gguf"
    assert dest.exists() and dest.stat().st_size > 0


def test_missing_bucket_fails_loud(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "JUDGE_MODELS_DIR": str(tmp_path / "models"),
    }
    env.pop("JUDGE_WEIGHTS_BUCKET", None)
    # No bucket argument and no env var: the guard must fail nonzero, not download.
    proc = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)

    assert proc.returncode != 0
    assert "JUDGE_WEIGHTS_BUCKET" in proc.stderr
