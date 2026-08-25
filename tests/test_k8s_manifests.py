"""Manifest checks for the phase-19 Kubernetes deploy (k8s/).

Two layers, both light and dependency-free beyond PyYAML (already a dep):

1. Structural YAML checks that always run — every manifest parses, and the key
   invariants the report promises hold (2 gateway replicas, probes, resources,
   HPA bounds, secret wiring, no real secrets committed).
2. A `kubectl kustomize` lint that runs only when kubectl is installed — it proves
   the whole kustomization renders and the secretGenerator references get
   rewritten. It manufactures a throwaway secrets.env from the example when the
   real one is absent (e.g. in CI) and removes only what it created.

Nothing here needs a cluster.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
K8S = REPO_ROOT / "k8s"
BASE = K8S / "base"


def _load_all(path: Path) -> list[dict]:
    with path.open() as f:
        return [d for d in yaml.safe_load_all(f) if d is not None]


def _docs() -> list[dict]:
    """Every k8s object across the base manifests, flattened."""
    out: list[dict] = []
    for f in sorted(BASE.glob("*.yaml")):
        out.extend(_load_all(f))
    return out


def _by_kind(name: str, kind: str) -> dict:
    for d in _docs():
        if d.get("kind") == kind and d.get("metadata", {}).get("name") == name:
            return d
    raise AssertionError(f"no {kind}/{name} found in k8s/base")


# ---------------------------------------------------------------------------
# YAML validity
# ---------------------------------------------------------------------------

def test_all_yaml_files_parse():
    files = list(BASE.glob("*.yaml")) + [
        K8S / "kustomization.yaml",
        K8S / "kind" / "kind-config.yaml",
    ]
    assert files, "expected manifests under k8s/"
    for f in files:
        docs = _load_all(f)
        assert docs, f"{f} produced no documents"
        for d in docs:
            assert "kind" in d and "apiVersion" in d, f"{f}: doc missing kind/apiVersion"


def test_expected_objects_exist():
    kinds = {(d["kind"], d["metadata"]["name"]) for d in _docs()}
    for expected in [
        ("Namespace", "slice"),
        ("ConfigMap", "slice-config"),
        ("StatefulSet", "postgres"),
        ("Service", "postgres"),
        ("Deployment", "redis"),
        ("Service", "redis"),
        ("Deployment", "gateway"),
        ("Service", "gateway"),
        ("HorizontalPodAutoscaler", "gateway"),
    ]:
        assert expected in kinds, f"missing object: {expected}"


def test_everything_is_in_the_slice_namespace():
    # Namespaced objects must declare namespace: slice (kustomize also enforces it,
    # but the raw manifests should be correct on their own).
    for d in _docs():
        if d["kind"] == "Namespace":
            continue
        assert d["metadata"].get("namespace") == "slice", (
            f"{d['kind']}/{d['metadata']['name']} not in namespace slice"
        )


# ---------------------------------------------------------------------------
# Gateway deployment
# ---------------------------------------------------------------------------

def test_gateway_has_two_replicas():
    assert _by_kind("gateway", "Deployment")["spec"]["replicas"] == 2


def test_gateway_probes_hit_docs():
    container = _by_kind("gateway", "Deployment")["spec"]["template"]["spec"]["containers"][0]
    for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
        assert probe in container, f"gateway missing {probe}"
        assert container[probe]["httpGet"]["path"] == "/docs"
        assert container[probe]["httpGet"]["port"] == 8080


def test_gateway_resources():
    container = _by_kind("gateway", "Deployment")["spec"]["template"]["spec"]["containers"][0]
    res = container["resources"]
    assert res["requests"] == {"memory": "750Mi", "cpu": "250m"}
    assert res["limits"] == {"memory": "1.5Gi", "cpu": "1000m"}


def test_gateway_image_is_overridable_local_tag():
    container = _by_kind("gateway", "Deployment")["spec"]["template"]["spec"]["containers"][0]
    # Base pins the local image and never pulls it from a registry.
    assert container["image"] == "slice-gateway:local"
    assert container["imagePullPolicy"] == "IfNotPresent"


def test_gateway_secrets_wired_from_secret():
    container = _by_kind("gateway", "Deployment")["spec"]["template"]["spec"]["containers"][0]
    secret_env = {
        e["name"]: e["valueFrom"]["secretKeyRef"]["key"]
        for e in container["env"]
        if "valueFrom" in e and "secretKeyRef" in e["valueFrom"]
    }
    for key in (
        "DATABASE_URL",
        "ANTHROPIC_API_KEY",
        "JWT_SECRET",
        "RESEND_API_KEY",
        "GITHUB_OAUTH_CLIENT_ID",
    ):
        assert secret_env.get(key) == key, f"gateway {key} not sourced from Secret"
    # Non-secret config comes from the ConfigMap.
    froms = [ref.get("configMapRef", {}).get("name") for ref in container.get("envFrom", [])]
    assert "slice-config" in froms


def test_gateway_service_is_clusterip_on_8080():
    svc = _by_kind("gateway", "Service")
    assert svc["spec"]["type"] == "ClusterIP"
    ports = svc["spec"]["ports"]
    assert any(p["port"] == 8080 and p["targetPort"] == 8080 for p in ports)


# ---------------------------------------------------------------------------
# Postgres / Redis
# ---------------------------------------------------------------------------

def test_postgres_password_from_secret_and_pvc():
    sts = _by_kind("postgres", "StatefulSet")
    assert sts["spec"]["replicas"] == 1
    container = sts["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "postgres:16"
    pw = next(e for e in container["env"] if e["name"] == "POSTGRES_PASSWORD")
    assert pw["valueFrom"]["secretKeyRef"]["key"] == "POSTGRES_PASSWORD"
    # 1Gi PVC from the default storageclass (no storageClassName set).
    vct = sts["spec"]["volumeClaimTemplates"][0]
    assert vct["spec"]["resources"]["requests"]["storage"] == "1Gi"
    assert "storageClassName" not in vct["spec"]


def test_redis_is_deployment_v7():
    dep = _by_kind("redis", "Deployment")
    assert dep["spec"]["template"]["spec"]["containers"][0]["image"] == "redis:7"


# ---------------------------------------------------------------------------
# HPA
# ---------------------------------------------------------------------------

def test_hpa_bounds_and_target():
    hpa = _by_kind("gateway", "HorizontalPodAutoscaler")
    spec = hpa["spec"]
    assert spec["minReplicas"] == 2
    assert spec["maxReplicas"] == 4
    assert spec["scaleTargetRef"]["name"] == "gateway"
    cpu = next(m for m in spec["metrics"] if m["resource"]["name"] == "cpu")
    assert cpu["resource"]["target"]["averageUtilization"] == 70


# ---------------------------------------------------------------------------
# Secrets hygiene
# ---------------------------------------------------------------------------

def test_no_real_secrets_committed():
    # The example is committed; the real env file must not be.
    assert (K8S / "secrets.env.example").exists()
    # secrets.env is gitignored; if a developer created one locally that's fine,
    # but it must be listed in .gitignore so it can never be committed.
    gitignore = (REPO_ROOT / ".gitignore").read_text().splitlines()
    assert "k8s/secrets.env" in gitignore


def test_example_secrets_have_no_live_looking_values():
    text = (K8S / "secrets.env.example").read_text()
    # Placeholder Anthropic key only — never a full-length real one.
    for line in text.splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            val = line.split("=", 1)[1]
            assert val == "" or "xxxx" in val, "example must not carry a real key"


# ---------------------------------------------------------------------------
# kustomize lint (only when kubectl is available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl not installed")
def test_kustomize_build_renders():
    secrets = K8S / "secrets.env"
    created = False
    if not secrets.exists():
        shutil.copy(K8S / "secrets.env.example", secrets)
        created = True
    try:
        out = subprocess.run(
            ["kubectl", "kustomize", str(K8S)],
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0, f"kustomize build failed:\n{out.stderr}"
        docs = [d for d in yaml.safe_load_all(out.stdout) if d]
        kinds = {(d["kind"], d["metadata"]["name"]) for d in docs}
        # The generated Secret gets a hash-suffixed name; find it.
        secret = next(d for d in docs if d["kind"] == "Secret")
        assert secret["metadata"]["name"].startswith("slice-secrets-"), (
            "secretGenerator should hash-suffix the name"
        )
        # Every reference to the secret in the workloads must point at the hashed
        # name, not the bare 'slice-secrets'.
        rendered = out.stdout
        assert "name: slice-secrets\n" not in rendered, (
            "a raw slice-secrets reference was left un-rewritten"
        )
        assert ("Namespace", "slice") in kinds
    finally:
        if created:
            secrets.unlink()
