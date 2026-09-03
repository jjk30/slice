"""The ``slice`` CLI (phase 12): log in from the terminal, then point tools at slice.

Installed as a console entry point via pyproject.toml, so ``pip install -e .`` gives the
``slice`` command. Three subcommands:

- ``slice login``, runs the GitHub device flow against the gateway's ``/auth/device/*``
  endpoints (the gateway holds the OAuth client id and talks to GitHub; the CLI never
  does). It shows the user code and URL, opens the browser, polls on the interval, and on
  success saves the slice key and JWT to ``~/.slice/config.json`` (chmod 600).
- ``slice init``, confirms the saved config, records the default base URL, and prints
  the current account status (a ``/auth/me`` call with the saved key).
- ``slice use <tool>``, prints the env lines that point a tool at slice. For tools where
  the caller controls headers (SDK, curl) that is the whole story; for ``claude-code`` it
  prints the three variables (base URL, your Anthropic key in ANTHROPIC_API_KEY, your slice
  key in ANTHROPIC_AUTH_TOKEN) that run it end to end through slice.
- ``slice --version``, prints ``slice-gateway <version>`` from the installed distribution.

The gateway address is the saved config, then ``SLICE_BASE_URL``, then the hosted
``https://api.sliceapp.dev``; self-hosted users pass ``--base-url`` once or set the variable.

The CLI talks only to the gateway over HTTP; it holds no secrets of its own. The saved
slice key is a bearer credential, so the config file is written 0600.
"""

from __future__ import annotations

import json
import os
import platform
import stat
import time
import webbrowser
from importlib import metadata
from pathlib import Path

import httpx
import typer

app = typer.Typer(add_completion=False, help="slice. Log in and point your tools at the gateway.")

CONFIG_DIR = Path.home() / ".slice"
CONFIG_PATH = CONFIG_DIR / "config.json"
# The hosted gateway. A saved config or SLICE_BASE_URL overrides it (see base_url), which
# is how a self-hosted box (http://localhost:8080 in docker compose) is reached.
DEFAULT_BASE_URL = "https://api.sliceapp.dev"

# The distribution the CLI ships in; its installed version is what --version prints.
DIST_NAME = "slice-gateway"

# How the device flow gives up if the user never authorizes: the gateway returns the
# expiry, but cap the CLI's own patience too so a wedged terminal doesn't spin forever.
POLL_CEILING_SECONDS = 900

# The gateway names the minted key after this so one machine keeps one live key: a repeat
# login from here replaces this device's key, while another machine's key is left alone.
MAX_DEVICE_CHARS = 64


def device_name() -> str:
    """This machine's name for the poll request, trimmed. Empty if the host has none."""
    try:
        return (platform.node() or "").strip()[:MAX_DEVICE_CHARS]
    except Exception:  # noqa: BLE001 — a nameless host just logs in without a device.
        return ""


# --- config file ------------------------------------------------------------


def base_url() -> str:
    """The gateway base URL: the saved one, else ``SLICE_BASE_URL``, else the hosted default."""
    saved = load_config().get("base_url")
    return (saved or os.getenv("SLICE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def version_string() -> str:
    """``slice-gateway <version>`` from the installed distribution, or ``unknown``."""
    try:
        version = metadata.version(DIST_NAME)
    except metadata.PackageNotFoundError:
        version = "unknown"
    return f"{DIST_NAME} {version}"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(version_string())
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", help="Print the version and exit.", callback=_version_callback, is_eager=True
    ),
) -> None:
    """slice. Log in and point your tools at the gateway."""


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_config(data: dict) -> None:
    """Write ``~/.slice/config.json`` as 0600 (it holds a bearer credential)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600


# --- commands ---------------------------------------------------------------


@app.command()
def login(
    base: str = typer.Option(
        None,
        "--base-url",
        help="Gateway base URL (default: saved config, then SLICE_BASE_URL, then https://api.sliceapp.dev).",
    ),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the verification URL in a browser."),
) -> None:
    """Log in to slice via GitHub (device flow) and save the slice key."""
    target = (base or base_url()).rstrip("/")
    existing = load_config()

    with httpx.Client(timeout=30.0) as http:
        try:
            started = http.post(f"{target}/auth/device/start")
        except httpx.HTTPError as exc:
            _fail(f"Could not reach the gateway at {target}: {exc}")
        if started.status_code != 200:
            _fail(f"Login could not start: {_message(started)}")
        data = started.json()
        session_id = data["session_id"]
        user_code = data["user_code"]
        verification_uri = data["verification_uri"]
        interval = int(data.get("interval") or 5)

        typer.echo("")
        typer.secho("  To finish signing in, open:", bold=True)
        typer.secho(f"    {verification_uri}", fg=typer.colors.CYAN)
        typer.secho("  and enter this code:", bold=True)
        typer.secho(f"    {user_code}", fg=typer.colors.GREEN, bold=True)
        typer.echo("")
        if open_browser:
            try:
                webbrowser.open(verification_uri)
            except Exception:  # noqa: BLE001, a headless box just shows the URL above.
                pass

        typer.echo("Waiting for you to authorize in the browser…")
        device = device_name()
        poll_body = {"session_id": session_id}
        if device:
            poll_body["device"] = device
        deadline = time.monotonic() + POLL_CEILING_SECONDS
        while time.monotonic() < deadline:
            time.sleep(max(1, interval))
            try:
                polled = http.post(f"{target}/auth/device/poll", json=poll_body)
            except httpx.HTTPError as exc:
                _fail(f"Lost contact with the gateway: {exc}")
            if polled.status_code != 200:
                _fail(f"Login failed: {_message(polled)}")
            body = polled.json()
            status = body.get("status")
            if status == "authorized":
                _save_login(target, existing, body)
                account = body.get("account") or {}
                typer.secho(f"Logged in as {account.get('login')}", fg=typer.colors.GREEN, bold=True)
                return
            if status == "slow_down":
                interval = int(body.get("interval") or interval + 5)
                continue
            if status == "pending":
                interval = int(body.get("interval") or interval)
                continue
            if status == "expired":
                _fail("The login request expired before you authorized it. Run `slice login` again.")
            if status == "denied":
                _fail("Login was denied on the GitHub page.")
            _fail(f"Unexpected login status: {status!r}")
        _fail("Timed out waiting for authorization. Run `slice login` again.")


def _save_login(target: str, existing: dict, body: dict) -> None:
    account = body.get("account") or {}
    save_config(
        {
            **existing,
            "base_url": target,
            "slice_key": body.get("slice_key"),
            "jwt": body.get("jwt"),
            "login": account.get("login"),
            "account_id": account.get("id"),
        }
    )


@app.command()
def init(
    base: str = typer.Option(None, "--base-url", help="Set the default gateway base URL."),
) -> None:
    """Confirm the saved login, set the default base URL, and show account status."""
    config = load_config()
    if base:
        config["base_url"] = base.rstrip("/")
        save_config(config)
    target = (config.get("base_url") or base_url()).rstrip("/")
    key = config.get("slice_key")

    typer.echo(f"Gateway:  {target}")
    typer.echo(f"Config:   {CONFIG_PATH}")
    if not key:
        typer.secho("Not logged in. Run `slice login` first.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    with httpx.Client(timeout=15.0) as http:
        try:
            me = http.get(f"{target}/auth/me", headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as exc:
            _fail(f"Could not reach the gateway at {target}: {exc}")
    if me.status_code != 200:
        typer.secho(f"Saved key was rejected: {_message(me)}", fg=typer.colors.RED)
        typer.echo("Run `slice login` to get a fresh key.")
        raise typer.Exit(code=1)
    account = me.json().get("account") or {}
    typer.secho(f"Logged in as {account.get('login')} (account {account.get('id')}).", fg=typer.colors.GREEN)


@app.command()
def use(tool: str = typer.Argument(..., help="Which tool: anthropic | openai | curl | claude-code")) -> None:
    """Print the environment lines that point a tool at slice."""
    config = load_config()
    target = base_url()
    key = config.get("slice_key")
    if not key:
        typer.secho("Not logged in. Run `slice login` first.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    name = tool.strip().lower()
    if name in {"anthropic", "sdk", "python"}:
        typer.echo(f"export ANTHROPIC_BASE_URL={target}")
        typer.echo(f"export SLICE_KEY={key}")
        typer.echo("# Send the slice key as the Authorization header, your provider key as x-api-key:")
        typer.echo('#   Authorization: Bearer $SLICE_KEY')
        typer.echo('#   x-api-key: <your Anthropic key>')
    elif name in {"openai", "codex"}:
        typer.echo(f"export OPENAI_BASE_URL={target}/v1")
        typer.echo(f"export SLICE_KEY={key}")
        typer.echo("# Authorization: Bearer $SLICE_KEY, and your provider key in x-api-key.")
    elif name in {"curl", "http"}:
        typer.echo(f'curl {target}/v1/messages \\')
        typer.echo(f'  -H "Authorization: Bearer {key}" \\')
        typer.echo('  -H "x-api-key: $ANTHROPIC_API_KEY" \\')
        typer.echo('  -H "content-type: application/json" \\')
        typer.echo('  -d \'{"model":"claude-sonnet-5","max_tokens":64,"messages":[{"role":"user","content":"hi"}]}\'')
    elif name in {"claude-code", "claudecode", "cc"}:
        typer.echo(f"export ANTHROPIC_BASE_URL={target}")
        typer.echo("export ANTHROPIC_API_KEY=sk-ant-api...     # your own Anthropic key")
        typer.echo(f"export ANTHROPIC_AUTH_TOKEN={key}   # your slice key, from slice login")
        typer.echo("# ANTHROPIC_AUTH_TOKEN goes out as Authorization: Bearer, where slice reads its key.")
        typer.echo("# ANTHROPIC_API_KEY stays your own Anthropic key in x-api-key; slice forwards it.")
        typer.echo("# Claude Code notes that env auth takes precedence over your claude.ai login while")
        typer.echo("# these are set, which is expected; unset the three variables to go back to normal.")
    else:
        _fail(f"Unknown tool {tool!r}. Try: anthropic | openai | curl | claude-code")


# --- helpers ----------------------------------------------------------------


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"].get("message") or f"HTTP {response.status_code}"
    return f"HTTP {response.status_code}"


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
