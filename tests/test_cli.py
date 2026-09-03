"""The ``slice`` CLI's gateway address and ``--version`` (slice_cli.py).

The address order is fixed and every test here pins one rung of it: the saved config
wins, then ``SLICE_BASE_URL``, then the hosted default. 0.2.1 made the hosted gateway
the default so a fresh ``pip install slice-gateway && slice login`` reaches
api.sliceapp.dev without a flag; a self-hosted box is reached by the flag or the variable.
The config path is pointed at a temp directory, so nothing here reads or writes the real
``~/.slice/config.json``. No network: nothing invokes login/init/use against a gateway.
"""

from __future__ import annotations

import json
from importlib import metadata

import pytest
from typer.testing import CliRunner

import slice_cli

HOSTED = "https://api.sliceapp.dev"
LOCAL = "http://localhost:8080"

runner = CliRunner()


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    """An isolated config file: absent by default, and the env variable unset."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(slice_cli, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(slice_cli, "CONFIG_PATH", path)
    monkeypatch.delenv("SLICE_BASE_URL", raising=False)
    return path


# --- base URL order -----------------------------------------------------------


def test_default_base_url_is_the_hosted_gateway(config_path):
    assert slice_cli.DEFAULT_BASE_URL == HOSTED
    assert slice_cli.base_url() == HOSTED


def test_env_variable_wins_over_the_default(config_path, monkeypatch):
    monkeypatch.setenv("SLICE_BASE_URL", LOCAL + "/")
    assert slice_cli.base_url() == LOCAL


def test_saved_config_wins_over_the_env_variable(config_path, monkeypatch):
    config_path.write_text(json.dumps({"base_url": "https://gateway.example/"}))
    monkeypatch.setenv("SLICE_BASE_URL", LOCAL)
    assert slice_cli.base_url() == "https://gateway.example"


def test_saved_config_without_an_address_falls_through(config_path, monkeypatch):
    config_path.write_text(json.dumps({"slice_key": "slk_live_x"}))
    assert slice_cli.base_url() == HOSTED
    monkeypatch.setenv("SLICE_BASE_URL", LOCAL)
    assert slice_cli.base_url() == LOCAL


def test_login_help_names_the_hosted_default(config_path):
    result = runner.invoke(slice_cli.app, ["login", "--help"])
    assert result.exit_code == 0
    assert "api.sliceapp.dev" in result.output
    assert "localhost" not in result.output


# --- --version ---------------------------------------------------------------


def test_version_prints_the_installed_distribution_version(config_path, monkeypatch):
    monkeypatch.setattr(slice_cli.metadata, "version", lambda name: {"slice-gateway": "9.9.9"}[name])
    result = runner.invoke(slice_cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == "slice-gateway 9.9.9"


def test_version_falls_back_to_unknown_when_not_installed(config_path, monkeypatch):
    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(slice_cli.metadata, "version", missing)
    result = runner.invoke(slice_cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == "slice-gateway unknown"


def test_version_matches_the_real_metadata_when_installed(config_path):
    """Against the real distribution: the string is the dist name plus its version."""
    result = runner.invoke(slice_cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == slice_cli.version_string()
    assert result.output.startswith("slice-gateway ")


def test_version_does_not_break_the_subcommands(config_path):
    """The eager root option must leave ``slice <command>`` working as before."""
    result = runner.invoke(slice_cli.app, ["use", "--help"])
    assert result.exit_code == 0
    assert "claude-code" in result.output
