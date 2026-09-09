"""Tests for scripts/fetch_judge_weights.py.

A fake S3 client stands in for boto3: its download_file writes a prebuilt zip to
the target path, so the script's real download-then-unzip flow runs end to end
with no network and no real weights. Covers: fetch when missing, skip when the
unzipped folder is already present, --force refetches, --lora-only skips the
merged file, and a missing JUDGE_WEIGHTS_BUCKET exits clean.
"""

import io
import pathlib
import sys
import zipfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import fetch_judge_weights as fjw  # noqa: E402


# --- helpers --------------------------------------------------------------- #


def _zip_bytes(members: dict) -> bytes:
    """A real in-memory zip of {name: text} so extractall has something to open."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


class FakeS3:
    """Stand-in for boto3.client('s3'): download_file writes a known zip to disk."""

    def __init__(self, blobs: dict):
        self._blobs = blobs
        self.calls = []

    def download_file(self, bucket, key, filename):
        self.calls.append((bucket, key))
        pathlib.Path(filename).write_bytes(self._blobs[key])


@pytest.fixture
def s3():
    return FakeS3(
        {
            fjw.LORA_ZIP: _zip_bytes({"adapter_config.json": "{}"}),
            fjw.MERGED_ZIP: _zip_bytes({"config.json": "{}"}),
        }
    )


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("JUDGE_WEIGHTS_BUCKET", "slice-judge-weights-test")
    monkeypatch.setenv("JUDGE_WEIGHTS_DIR", str(tmp_path))
    return tmp_path


# --- tests ----------------------------------------------------------------- #


def test_fetch_when_missing(env, s3):
    rc = fjw.run([], client=s3)

    assert rc == 0
    assert (env / "judge_lora" / "adapter_config.json").exists()
    assert (env / "judge_merged" / "config.json").exists()
    assert {key for _, key in s3.calls} == {fjw.LORA_ZIP, fjw.MERGED_ZIP}


def test_skip_when_present(env, s3):
    # Both unzipped folders already there: nothing is downloaded.
    (env / "judge_lora").mkdir()
    (env / "judge_merged").mkdir()

    rc = fjw.run([], client=s3)

    assert rc == 0
    assert s3.calls == []


def test_force_refetches(env, s3):
    # A stale folder with a leftover file that a clean refetch must remove.
    stale = env / "judge_lora"
    stale.mkdir()
    (stale / "leftover.txt").write_text("old")

    rc = fjw.run(["--force"], client=s3)

    assert rc == 0
    assert not (stale / "leftover.txt").exists()
    assert (stale / "adapter_config.json").exists()
    assert {key for _, key in s3.calls} == {fjw.LORA_ZIP, fjw.MERGED_ZIP}


def test_lora_only_skips_merged(env, s3):
    rc = fjw.run(["--lora-only"], client=s3)

    assert rc == 0
    assert (env / "judge_lora" / "adapter_config.json").exists()
    assert not (env / "judge_merged").exists()
    assert [key for _, key in s3.calls] == [fjw.LORA_ZIP]


def test_missing_env_exits_clean(monkeypatch, tmp_path, s3, capsys):
    monkeypatch.delenv("JUDGE_WEIGHTS_BUCKET", raising=False)
    monkeypatch.setenv("JUDGE_WEIGHTS_DIR", str(tmp_path))

    rc = fjw.run([], client=s3)

    assert rc == 1
    assert "JUDGE_WEIGHTS_BUCKET" in capsys.readouterr().err
    assert s3.calls == []
