"""Guards the runtime image's file layout.

GUARDRAILS_CONFIG_DIR defaults to the relative "guardrails" path, so the config
tree has to be copied into the image or RailsConfig.from_path fails and guardrails
silently turn off in every container. This test pins the COPY line so that copy
cannot be dropped again.
"""

from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"


def test_dockerfile_copies_guardrails_config():
    lines = [line.strip() for line in DOCKERFILE.read_text().splitlines()]
    assert "COPY guardrails/ ./guardrails/" in lines
