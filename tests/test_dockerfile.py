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


def test_dockerfile_installs_cpu_torch_before_requirements():
    # sentence-transformers pulls torch; on this arm64 CPU box the default wheel bundles
    # unusable NVIDIA CUDA libs and bloats the image by gigabytes. The CPU-only wheel must
    # be installed from the PyTorch CPU index BEFORE `pip install -r requirements.txt`, so
    # the requirements install finds torch already satisfied and never pulls the CUDA build.
    lines = [line.strip() for line in DOCKERFILE.read_text().splitlines()]
    cpu_torch = "RUN pip install torch --index-url https://download.pytorch.org/whl/cpu"
    requirements = "RUN pip install -r requirements.txt"
    assert cpu_torch in lines, "CPU-only torch install line is missing from the Dockerfile"
    assert requirements in lines, "requirements install line is missing from the Dockerfile"
    assert lines.index(cpu_torch) < lines.index(requirements), (
        "CPU torch must be installed before the requirements install, or the requirements "
        "resolve would pull the CUDA torch build"
    )
