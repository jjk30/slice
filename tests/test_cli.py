"""The ``slice`` CLI's gateway address and ``--version`` (slice_cli.py).

The address order is fixed and every test here pins one rung of it: the saved config
wins, then ``SLICE_BASE_URL``, then the hosted default. 0.2.1 made the hosted gateway
the default so a fresh ``pip install slice-gateway && slice login`` reaches
api.sliceapp.dev without a flag; a self-hosted box is reached by the flag or the variable.
The config path is pointed at a temp directory, so nothing here reads or writes the real
``~/.slice/config.json``. No network: nothing invokes login/init/use against a gateway;
the ``slice use`` tests only read the saved key and print lines.
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


# --- dashboard link after login ---------------------------------------------------


def test_dashboard_url_for_the_hosted_gateway_is_on_the_main_site():
    assert slice_cli.dashboard_url(HOSTED) == "https://sliceapp.dev/dashboard"
    assert slice_cli.dashboard_url(HOSTED + "/") == "https://sliceapp.dev/dashboard"


def test_dashboard_url_for_a_self_hosted_gateway_is_under_the_gateway():
    assert slice_cli.dashboard_url(LOCAL) == "http://localhost:8080/dashboard"
    assert slice_cli.dashboard_url("https://gateway.example/") == "https://gateway.example/dashboard"


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeClient:
    """Stands in for httpx.Client: the device flow starts, then the first poll is
    authorized. Records every URL that was posted to."""

    posted: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None):
        _FakeClient.posted.append(url)
        if url.endswith("/auth/device/start"):
            return _Response(200, {
                "session_id": "sess", "user_code": "WXYZ-1234",
                "verification_uri": "https://github.com/login/device", "interval": 1,
            })
        return _Response(200, {
            "status": "authorized", "slice_key": "slk_live_new", "jwt": "j",
            "account": {"login": "jjk30", "id": 14},
        })


@pytest.fixture
def device_flow(config_path, monkeypatch):
    _FakeClient.posted = []
    opened: list[str] = []
    monkeypatch.setattr(slice_cli.httpx, "Client", _FakeClient)
    monkeypatch.setattr(slice_cli.time, "sleep", lambda _s: None)
    monkeypatch.setattr(slice_cli.webbrowser, "open", lambda url: opened.append(url))
    return opened


def test_login_prints_the_dashboard_link_and_opens_it(device_flow, config_path):
    result = runner.invoke(slice_cli.app, ["login"])
    assert result.exit_code == 0, result.output
    assert "Logged in as jjk30" in result.output
    assert "Your dashboard: https://sliceapp.dev/dashboard" in result.output
    assert result.output.index("Logged in as jjk30") < result.output.index("Your dashboard:")
    # The device link first, then the dashboard, and nothing else.
    assert device_flow == ["https://github.com/login/device", "https://sliceapp.dev/dashboard"]
    assert _FakeClient.posted[0] == HOSTED + "/auth/device/start"
    assert json.loads(config_path.read_text())["slice_key"] == "slk_live_new"


def test_login_self_hosted_points_at_the_gateway_dashboard(device_flow):
    result = runner.invoke(slice_cli.app, ["login", "--base-url", LOCAL + "/"])
    assert result.exit_code == 0, result.output
    assert "Your dashboard: http://localhost:8080/dashboard" in result.output
    assert device_flow[-1] == "http://localhost:8080/dashboard"


def test_login_no_open_prints_the_dashboard_link_without_opening_it(device_flow):
    result = runner.invoke(slice_cli.app, ["login", "--no-open"])
    assert result.exit_code == 0, result.output
    assert "Your dashboard: https://sliceapp.dev/dashboard" in result.output
    assert device_flow == []


def test_login_dashboard_survives_a_browser_that_cannot_open(device_flow, monkeypatch):
    def broken(url):
        raise RuntimeError("no display")

    monkeypatch.setattr(slice_cli.webbrowser, "open", broken)
    result = runner.invoke(slice_cli.app, ["login"])
    assert result.exit_code == 0, result.output
    assert "Your dashboard: https://sliceapp.dev/dashboard" in result.output


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


# --- slice use: shell dialect ---------------------------------------------------


@pytest.fixture
def logged_in(config_path):
    config_path.write_text(json.dumps({"slice_key": "slk_live_x"}))
    return config_path


@pytest.mark.parametrize("tool", ["claude-code", "anthropic"])
def test_use_prints_export_lines_by_default(logged_in, monkeypatch, tool):
    monkeypatch.setattr(slice_cli.os, "name", "posix")
    result = runner.invoke(slice_cli.app, ["use", tool])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("export ")
    assert "$env:" not in result.output


@pytest.mark.parametrize("tool", ["claude-code", "anthropic"])
def test_use_prints_powershell_lines_on_windows(logged_in, monkeypatch, tool):
    monkeypatch.setattr(slice_cli.os, "name", "nt")
    result = runner.invoke(slice_cli.app, ["use", tool])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("$env:")
    assert "export " not in result.output


def test_use_powershell_quotes_values_and_keeps_the_comments(logged_in, monkeypatch):
    monkeypatch.setattr(slice_cli.os, "name", "nt")
    result = runner.invoke(slice_cli.app, ["use", "claude-code"])
    lines = result.output.splitlines()
    assert lines[0] == '$env:ANTHROPIC_BASE_URL="https://api.sliceapp.dev"'
    assert lines[2].startswith('$env:ANTHROPIC_AUTH_TOKEN="slk_live_x"')
    assert "# your slice key" in lines[2]


def test_use_anthropic_comment_names_the_powershell_variable(logged_in, monkeypatch):
    monkeypatch.setattr(slice_cli.os, "name", "nt")
    result = runner.invoke(slice_cli.app, ["use", "anthropic"])
    assert "#   Authorization: Bearer $env:SLICE_KEY" in result.output
    assert "$SLICE_KEY\n" not in result.output


def test_use_curl_is_the_same_on_windows(logged_in, monkeypatch):
    monkeypatch.setattr(slice_cli.os, "name", "posix")
    posix = runner.invoke(slice_cli.app, ["use", "curl"]).output
    monkeypatch.setattr(slice_cli.os, "name", "nt")
    windows = runner.invoke(slice_cli.app, ["use", "curl"]).output
    assert posix == windows
    assert posix.startswith("curl https://api.sliceapp.dev/v1/messages")
