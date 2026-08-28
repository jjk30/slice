"""Compatibility shim for the slice CLI.

The CLI now lives in the top-level ``slice_cli`` module so it can ship as the
``slice-gateway`` wheel (typer and httpx only) without pulling in the server
package. This shim keeps ``python -m app.cli`` and ``from app.cli import ...``
working unchanged inside the repo. See ``slice_cli.py`` for the real code.
"""

from __future__ import annotations

from slice_cli import (  # noqa: F401
    app,
    base_url,
    init,
    load_config,
    login,
    main,
    save_config,
    use,
)

if __name__ == "__main__":
    main()
