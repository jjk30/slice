"""Phase-18a AWS scanner tests. Stubbed boto3 (botocore Stubber) and fakes only — no real
AWS, no real Redis, no real database.

Layers:

- **Check tests** drive each of the four checks against a stubbed boto3 client: a public
  bucket is flagged and a closed one is not; a world-open security group on a sensitive
  port is flagged; an unencrypted bucket/volume is flagged; an old access key and a
  direct AdministratorAccess attachment are flagged.
- **Graph tests** prove the supervisor StateGraph fans out to all four checks and that one
  check raising records an error but never kills the others.
- **Cost tests** parse a canned get_cost_and_usage response.
- **Service tests** prove the alert fires on a *new* high and not on a repeat (the diff),
  and that the cooldown latch collapses repeats.
- **Endpoint tests** cover the findings/cost shapes and that /scanner is auth-locked.
- **Import test** proves importing the scanner never pulls boto3 in (lazy import).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import boto3
import pytest
from botocore.stub import Stubber

from app import config
from app.alerts import engine as alerts_engine
from app.scanner import checks, cost, graph, service
from app.scanner.checks import (
    check_iam_risk,
    check_s3_public,
    check_sg_open,
    check_unencrypted,
)
from app.scanner.models import (
    CHECK_IAM_RISK,
    CHECK_S3_PUBLIC,
    CHECK_SG_OPEN,
    CHECK_UNENCRYPTED,
    SEVERITY_HIGH,
    SEVERITY_MED,
    Finding,
)
from app.main import app

ALL_USERS = "http://acs.amazonaws.com/groups/global/AllUsers"


# --- boto3 stubbing helpers -------------------------------------------------


class FakeSession:
    """A stand-in for boto3.Session: returns pre-stubbed clients by service name.

    A service with no stub raises when built, which the checks catch and fail open on —
    exactly what a denied or absent client does in production.
    """

    def __init__(self, clients: dict):
        self._clients = clients

    def client(self, name, **kwargs):
        if name not in self._clients:
            raise RuntimeError(f"no stubbed client for {name!r}")
        return self._clients[name]


def _client(service: str):
    return boto3.client(
        service,
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


# --- a. S3 public -----------------------------------------------------------


async def test_s3_public_flags_public_bucket_not_closed():
    s3 = _client("s3")
    stub = Stubber(s3)
    stub.add_response(
        "list_buckets",
        {"Buckets": [{"Name": "public-bucket"}, {"Name": "closed-bucket"}]},
    )
    # public-bucket: an AllUsers ACL grant, no public policy.
    stub.add_response(
        "get_bucket_acl",
        {"Owner": {"ID": "o"}, "Grants": [{"Grantee": {"Type": "Group", "URI": ALL_USERS}, "Permission": "READ"}]},
        {"Bucket": "public-bucket"},
    )
    stub.add_response(
        "get_bucket_policy_status", {"PolicyStatus": {"IsPublic": False}}, {"Bucket": "public-bucket"}
    )
    # closed-bucket: no public grant, not public.
    stub.add_response(
        "get_bucket_acl",
        {"Owner": {"ID": "o"}, "Grants": [{"Grantee": {"Type": "CanonicalUser", "ID": "o"}, "Permission": "FULL_CONTROL"}]},
        {"Bucket": "closed-bucket"},
    )
    stub.add_response(
        "get_bucket_policy_status", {"PolicyStatus": {"IsPublic": False}}, {"Bucket": "closed-bucket"}
    )
    stub.activate()

    findings = check_s3_public(FakeSession({"s3": s3}))
    stub.assert_no_pending_responses()

    flagged = [f for f in findings if f.check == CHECK_S3_PUBLIC]
    assert [f.resource_id for f in flagged] == ["public-bucket"]
    assert flagged[0].severity == SEVERITY_HIGH
    assert flagged[0].detail["kind"] == "public_acl"


async def test_s3_public_flags_public_policy():
    s3 = _client("s3")
    stub = Stubber(s3)
    stub.add_response("list_buckets", {"Buckets": [{"Name": "policy-bucket"}]})
    stub.add_response("get_bucket_acl", {"Owner": {"ID": "o"}, "Grants": []}, {"Bucket": "policy-bucket"})
    stub.add_response(
        "get_bucket_policy_status", {"PolicyStatus": {"IsPublic": True}}, {"Bucket": "policy-bucket"}
    )
    stub.activate()

    findings = check_s3_public(FakeSession({"s3": s3}))
    stub.assert_no_pending_responses()
    assert [f.detail["kind"] for f in findings] == ["public_policy"]


async def test_s3_public_no_policy_is_not_flagged():
    s3 = _client("s3")
    stub = Stubber(s3)
    stub.add_response("list_buckets", {"Buckets": [{"Name": "b"}]})
    stub.add_response("get_bucket_acl", {"Owner": {"ID": "o"}, "Grants": []}, {"Bucket": "b"})
    stub.add_client_error(
        "get_bucket_policy_status", service_error_code="NoSuchBucketPolicy", expected_params={"Bucket": "b"}
    )
    stub.activate()

    assert check_s3_public(FakeSession({"s3": s3})) == []
    stub.assert_no_pending_responses()


# --- b. Security groups -----------------------------------------------------


async def test_sg_open_flags_world_open_ssh_not_closed():
    ec2 = _client("ec2")
    stub = Stubber(ec2)
    stub.add_response(
        "describe_security_groups",
        {
            "SecurityGroups": [
                {
                    "GroupId": "sg-open",
                    "GroupName": "open",
                    "IpPermissions": [
                        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                         "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
                    ],
                },
                {
                    "GroupId": "sg-closed",
                    "GroupName": "closed",
                    "IpPermissions": [
                        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                         "IpRanges": [{"CidrIp": "10.0.0.0/8"}]}
                    ],
                },
            ]
        },
    )
    stub.activate()

    findings = check_sg_open(FakeSession({"ec2": ec2}))
    stub.assert_no_pending_responses()

    assert [f.resource_id for f in findings] == ["sg-open"]
    assert findings[0].severity == SEVERITY_HIGH
    assert findings[0].detail["port"] == 22 and findings[0].detail["service"] == "SSH"


async def test_sg_open_all_protocols_flags_every_sensitive_port():
    ec2 = _client("ec2")
    stub = Stubber(ec2)
    stub.add_response(
        "describe_security_groups",
        {
            "SecurityGroups": [
                {
                    "GroupId": "sg-all",
                    "IpPermissions": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
                }
            ]
        },
    )
    stub.activate()
    findings = check_sg_open(FakeSession({"ec2": ec2}))
    assert {f.detail["port"] for f in findings} == set(checks.SENSITIVE_PORTS)


# --- c. Unencrypted ---------------------------------------------------------


async def test_unencrypted_flags_bucket_and_volume():
    s3 = _client("s3")
    s3_stub = Stubber(s3)
    s3_stub.add_response("list_buckets", {"Buckets": [{"Name": "enc"}, {"Name": "unenc"}]})
    s3_stub.add_response(
        "get_bucket_encryption",
        {"ServerSideEncryptionConfiguration": {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}},
        {"Bucket": "enc"},
    )
    s3_stub.add_client_error(
        "get_bucket_encryption",
        service_error_code="ServerSideEncryptionConfigurationNotFoundError",
        expected_params={"Bucket": "unenc"},
    )
    s3_stub.activate()

    ec2 = _client("ec2")
    ec2_stub = Stubber(ec2)
    ec2_stub.add_response(
        "describe_volumes",
        {"Volumes": [
            {"VolumeId": "vol-enc", "Encrypted": True, "Size": 8, "State": "in-use"},
            {"VolumeId": "vol-plain", "Encrypted": False, "Size": 20, "State": "available"},
        ]},
    )
    ec2_stub.activate()

    findings = check_unencrypted(FakeSession({"s3": s3, "ec2": ec2}))
    s3_stub.assert_no_pending_responses()
    ec2_stub.assert_no_pending_responses()

    ids = {f.resource_id for f in findings}
    assert ids == {"unenc", "vol-plain"}
    assert all(f.severity == SEVERITY_MED for f in findings)


# --- d. IAM risk ------------------------------------------------------------


async def test_iam_risk_flags_old_key_and_direct_admin():
    iam = _client("iam")
    stub = Stubber(iam)
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    recent = datetime.now(timezone.utc) - timedelta(days=1)

    stub.add_response("list_users", {"Users": [
        {"UserName": "admin-user", "UserId": "AIDAEXAMPLEADMIN1", "Arn": "arn:aws:iam::1:user/admin-user",
         "Path": "/", "CreateDate": old},
        {"UserName": "normal", "UserId": "AIDAEXAMPLENORMAL", "Arn": "arn:aws:iam::1:user/normal",
         "Path": "/", "CreateDate": recent},
    ]})
    # admin-user: one old key + AdministratorAccess.
    stub.add_response(
        "list_access_keys",
        {"AccessKeyMetadata": [{"UserName": "admin-user", "AccessKeyId": "AKIAEXAMPLEOLD01", "Status": "Active", "CreateDate": old}]},
        {"UserName": "admin-user"},
    )
    stub.add_response(
        "get_access_key_last_used",
        {"UserName": "admin-user", "AccessKeyLastUsed": {"ServiceName": "s3", "Region": "us-east-1", "LastUsedDate": recent}},
        {"AccessKeyId": "AKIAEXAMPLEOLD01"},
    )
    stub.add_response(
        "list_attached_user_policies",
        {"AttachedPolicies": [{"PolicyName": "AdministratorAccess", "PolicyArn": "arn:aws:iam::aws:policy/AdministratorAccess"}]},
        {"UserName": "admin-user"},
    )
    # normal: one recent key, no admin.
    stub.add_response(
        "list_access_keys",
        {"AccessKeyMetadata": [{"UserName": "normal", "AccessKeyId": "AKIAEXAMPLENEW01", "Status": "Active", "CreateDate": recent}]},
        {"UserName": "normal"},
    )
    stub.add_response("list_attached_user_policies", {"AttachedPolicies": []}, {"UserName": "normal"})
    stub.activate()

    findings = check_iam_risk(FakeSession({"iam": iam}))
    stub.assert_no_pending_responses()

    by_resource = {f.resource_id: f for f in findings}
    assert "AKIAEXAMPLEOLD01" in by_resource and by_resource["AKIAEXAMPLEOLD01"].severity == SEVERITY_MED
    assert "admin-user" in by_resource and by_resource["admin-user"].severity == SEVERITY_HIGH
    assert "AKIAEXAMPLENEW01" not in by_resource  # recent key not flagged


async def test_iam_risk_key_age_threshold_is_config(monkeypatch):
    monkeypatch.setattr(config, "SCANNER_IAM_KEY_MAX_AGE_DAYS", 3650)  # 10 years
    iam = _client("iam")
    stub = Stubber(iam)
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    stub.add_response("list_users", {"Users": [
        {"UserName": "u", "UserId": "AIDAEXAMPLEUSER01", "Arn": "arn:aws:iam::1:user/u", "Path": "/", "CreateDate": old}
    ]})
    stub.add_response(
        "list_access_keys",
        {"AccessKeyMetadata": [{"UserName": "u", "AccessKeyId": "AKIAEXAMPLEKEY001", "Status": "Active", "CreateDate": old}]},
        {"UserName": "u"},
    )
    stub.add_response("list_attached_user_policies", {"AttachedPolicies": []}, {"UserName": "u"})
    stub.activate()

    # A ~6-year-old key is under a 10-year threshold: not flagged, and no last_used call.
    assert check_iam_risk(FakeSession({"iam": iam})) == []
    stub.assert_no_pending_responses()


# --- Graph: supervisor fan-out and per-node fail-open -----------------------


def _fake_checks(behaviors):
    """(name, fn) tuples from a {name: findings-or-Exception} spec."""
    out = []
    for name, spec in behaviors.items():
        def make(spec):
            def fn(session):
                if isinstance(spec, Exception):
                    raise spec
                return spec
            return fn
        out.append((name, make(spec)))
    return tuple(out)


async def test_supervisor_runs_all_four_checks(monkeypatch):
    f = Finding(check="a", resource_id="r1", severity="high", summary="s")
    monkeypatch.setattr(
        graph, "CHECKS",
        _fake_checks({"a": [f], "b": [], "c": [], "d": []}),
    )
    g = graph._build_graph()
    final = await g.ainvoke({"session": object(), "findings": [], "ran": [], "errors": []})

    assert set(final["ran"]) == {"a", "b", "c", "d"}
    assert final["findings"] == [f]
    assert final.get("errors", []) == []


async def test_one_check_raising_does_not_kill_the_others(monkeypatch):
    f_b = Finding(check="b", resource_id="r2", severity="med", summary="s")
    monkeypatch.setattr(
        graph, "CHECKS",
        _fake_checks({
            "a": RuntimeError("a exploded"),
            "b": [f_b],
            "c": [],
            "d": [],
        }),
    )
    g = graph._build_graph()
    final = await g.ainvoke({"session": object(), "findings": [], "ran": [], "errors": []})

    # Every node still ran; the survivor's finding is present; the failure is recorded.
    assert set(final["ran"]) == {"a", "b", "c", "d"}
    assert final["findings"] == [f_b]
    assert [e["check"] for e in final["errors"]] == ["a"]


async def test_run_scan_graph_sorts_by_severity(monkeypatch):
    low = Finding(check="z", resource_id="z1", severity="low", summary="s")
    high = Finding(check="a", resource_id="a1", severity="high", summary="s")
    med = Finding(check="m", resource_id="m1", severity="med", summary="s")

    async def fake_ainvoke(state, *a, **k):
        return {"findings": [low, high, med], "ran": ["x"], "errors": []}

    monkeypatch.setattr(graph._GRAPH, "ainvoke", fake_ainvoke)
    ordered = await graph.run_scan_graph(object())
    assert [f.severity for f in ordered] == ["high", "med", "low"]


# --- Cost parse -------------------------------------------------------------


def test_parse_cost_response():
    response = {
        "ResultsByTime": [
            {"TimePeriod": {"Start": "2026-08-20", "End": "2026-08-21"},
             "Total": {"UnblendedCost": {"Amount": "1.23", "Unit": "USD"}}},
            {"TimePeriod": {"Start": "2026-08-21", "End": "2026-08-22"},
             "Total": {"UnblendedCost": {"Amount": "2.50", "Unit": "USD"}}},
        ]
    }
    report = cost.parse_cost_response(response, yesterday=date(2026, 8, 21))
    assert report.yesterday == Decimal("2.50")
    assert report.month_to_date == Decimal("3.73")
    assert report.currency == "USD"
    assert report.daily[0] == (date(2026, 8, 20), Decimal("1.23"))


def test_parse_cost_response_tolerates_garbage():
    response = {"ResultsByTime": [
        {"TimePeriod": {"Start": "bad-date"}, "Total": {"UnblendedCost": {"Amount": "1.0"}}},
        {"TimePeriod": {"Start": "2026-08-21"}, "Total": {"UnblendedCost": {"Amount": "notanumber"}}},
    ]}
    report = cost.parse_cost_response(response, yesterday=date(2026, 8, 21))
    # The bad date is skipped; the bad number counts as 0.
    assert report.month_to_date == Decimal("0")
    assert report.yesterday == Decimal("0")


def test_fetch_costs_via_stub():
    ce = _client("ce")
    stub = Stubber(ce)
    stub.add_response(
        "get_cost_and_usage",
        {"ResultsByTime": [
            {"TimePeriod": {"Start": "2026-08-22", "End": "2026-08-23"},
             "Total": {"UnblendedCost": {"Amount": "4.00", "Unit": "USD"}}},
        ]},
        {
            "TimePeriod": {"Start": "2026-08-01", "End": "2026-08-23"},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
        },
    )
    stub.activate()
    now = datetime(2026, 8, 23, 6, 0, tzinfo=timezone.utc)
    report = cost.fetch_costs(FakeSession({"ce": ce}), now=now)
    stub.assert_no_pending_responses()
    assert report.yesterday == Decimal("4.00")


# --- Service: new-high alert + cooldown -------------------------------------


class FakeScannerDB:
    enabled = True

    def __init__(self):
        self.runs: dict[str, list[Finding]] = {}
        self.order: list[str] = []

    async def record_findings(self, run_id, findings):
        self.runs[run_id] = list(findings)
        self.order.append(run_id)

    async def previous_run_id(self, current):
        prev = [r for r in self.order if r != current]
        return prev[-1] if prev else None

    async def high_resource_ids(self, run_id):
        return {f.resource_id for f in self.runs.get(run_id, []) if f.severity == SEVERITY_HIGH}

    async def latest_run_id(self):
        return self.order[-1] if self.order else None

    async def findings_for_run(self, run_id):
        return [f.as_dict() | {"created_at": None} for f in self.runs.get(run_id, [])]


class FakeChannel:
    name = "fake"

    def __init__(self):
        self.sent = []

    async def send(self, alert):
        self.sent.append(alert)
        from app.alerts.channels import DeliveryResult

        return DeliveryResult(ok=True)


@pytest.fixture
def scan_alerts_on(monkeypatch):
    import fakeredis.aioredis

    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    monkeypatch.setattr(config, "ALERT_COOLDOWN_SECONDS", 3600)
    channel = FakeChannel()
    engine = alerts_engine.AlertEngine(
        channels=[channel], redis=fakeredis.aioredis.FakeRedis(), database=None
    )
    alerts_engine.configure(engine)
    yield channel
    alerts_engine.configure(None)


async def test_new_high_fires_alert(monkeypatch, scan_alerts_on):
    channel = scan_alerts_on
    highs = [Finding(check=CHECK_SG_OPEN, resource_id="sg-1", severity=SEVERITY_HIGH, summary="world-open ssh")]
    monkeypatch.setattr(service, "run_scan_graph", _async_return(highs))

    db = FakeScannerDB()
    result = await service.run_scan(object(), db, None, run_id="run1")
    await alerts_engine.drain()

    assert [f.resource_id for f in result.new_highs] == ["sg-1"]
    assert len(channel.sent) == 1
    alert = channel.sent[0]
    assert alert.kind == alerts_engine.KIND_SCAN
    assert alert.detail["count"] == 1
    from app.alerts.channels import subject_for

    assert subject_for(alert) == "slice found 1 high-risk AWS issue"


async def test_repeat_high_does_not_fire(monkeypatch, scan_alerts_on):
    channel = scan_alerts_on
    highs = [Finding(check=CHECK_SG_OPEN, resource_id="sg-1", severity=SEVERITY_HIGH, summary="world-open ssh")]
    monkeypatch.setattr(service, "run_scan_graph", _async_return(highs))

    db = FakeScannerDB()
    await service.run_scan(object(), db, None, run_id="run1")  # first: new -> fires
    await service.run_scan(object(), db, None, run_id="run2")  # same high -> not new
    await alerts_engine.drain()

    # sg-1 was in run1's highs, so run2 sees no *new* high: exactly one alert total.
    assert len(channel.sent) == 1


async def test_cooldown_collapses_repeated_new_highs(monkeypatch, scan_alerts_on):
    channel = scan_alerts_on

    async def graph_for(session):
        return graph_for.value

    # run1 has sg-1; run2 introduces a genuinely new high sg-2 — but inside the cooldown.
    monkeypatch.setattr(service, "run_scan_graph", graph_for)
    db = FakeScannerDB()

    graph_for.value = [Finding(check=CHECK_SG_OPEN, resource_id="sg-1", severity=SEVERITY_HIGH, summary="a")]
    await service.run_scan(object(), db, None, run_id="run1")
    graph_for.value = [
        Finding(check=CHECK_SG_OPEN, resource_id="sg-1", severity=SEVERITY_HIGH, summary="a"),
        Finding(check=CHECK_SG_OPEN, resource_id="sg-2", severity=SEVERITY_HIGH, summary="b"),
    ]
    await service.run_scan(object(), db, None, run_id="run2")
    await alerts_engine.drain()

    # Both runs fired (each had a new high), but the per-hour cooldown let one reach the channel.
    assert len(channel.sent) == 1


def _async_return(value):
    async def fn(session):
        return value
    return fn


# --- Endpoints --------------------------------------------------------------


@pytest.fixture
async def client():
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
        yield c


async def test_findings_endpoint_shape(client):
    db = FakeScannerDB()
    db.runs["run1"] = [
        Finding(check=CHECK_S3_PUBLIC, resource_id="b", severity=SEVERITY_HIGH, summary="public", detail={"kind": "public_acl"})
    ]
    db.order = ["run1"]

    previous = getattr(app.state, "db", None)
    app.state.db = db
    try:
        r = await client.get("/scanner/findings")
    finally:
        app.state.db = previous

    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == "run1"
    assert len(body["findings"]) == 1
    finding = body["findings"][0]
    assert set(finding) == {"check", "resource_id", "severity", "summary", "detail", "created_at"}
    assert finding["check"] == CHECK_S3_PUBLIC and finding["detail"]["kind"] == "public_acl"


async def test_findings_endpoint_no_db_is_empty(client):
    previous = getattr(app.state, "db", None)
    app.state.db = None
    try:
        r = await client.get("/scanner/findings")
    finally:
        app.state.db = previous
    assert r.status_code == 200 and r.json() == {"run_id": None, "findings": []}


async def test_run_endpoint_returns_run_id_without_blocking(client, monkeypatch):
    started = {}

    async def fake_run_scan(session, db, redis, *, run_id=None, alert=True):
        started["run_id"] = run_id

    monkeypatch.setattr(service, "run_scan", fake_run_scan)
    # No real boto3 session needed; make_session is not called until run_scan, which we faked
    # — but the route builds the session before run_scan, so stub it to something cheap.
    monkeypatch.setattr("app.scanner.routes.make_session", lambda: object())

    r = await client.post("/scanner/run")
    assert r.status_code == 202
    body = r.json()
    assert "run_id" in body and body["status"] == "started"
    # Let the detached task run.
    import asyncio

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert started.get("run_id") == body["run_id"]


async def test_cost_endpoint_shape(client):
    class CostDB:
        enabled = True

        async def aws_cost_rows_since(self, since):
            return [
                {"date": date(2026, 8, 22), "amount_usd": Decimal("2.00"),
                 "fetched_at": datetime(2026, 8, 23, 1, tzinfo=timezone.utc)},
                {"date": date(2026, 8, 21), "amount_usd": Decimal("1.50"),
                 "fetched_at": datetime(2026, 8, 22, 1, tzinfo=timezone.utc)},
            ]

    previous = getattr(app.state, "db", None)
    app.state.db = CostDB()
    try:
        r = await client.get("/scanner/cost")
    finally:
        app.state.db = previous

    body = r.json()
    assert body["yesterday"] == "2.00"  # newest day first
    assert body["month_to_date"] == "3.50"
    assert body["currency"] == "USD"
    assert len(body["daily"]) == 2


async def test_scanner_endpoints_require_auth(client, monkeypatch):
    """With auth on and no slice key, every /scanner path is a 401."""
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    for method, path in [("GET", "/scanner/findings"), ("GET", "/scanner/cost"), ("POST", "/scanner/run")]:
        r = await client.request(method, path)
        assert r.status_code == 401, (method, path)


# --- Lazy boto3 import ------------------------------------------------------


def test_importing_scanner_does_not_import_boto3():
    """Importing the scanner package (as app.main does at startup) must not pull boto3 in."""
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import app.scanner\n"
        "import app.scanner.checks, app.scanner.graph, app.scanner.service, app.scanner.cost\n"
        "import app.scanner.routes, app.scanner.session\n"
        "assert 'boto3' not in sys.modules, 'boto3 was imported at scanner import time'\n"
        "assert 'botocore' not in sys.modules, 'botocore was imported at scanner import time'\n"
        "print('ok')\n"
    )
    import os

    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": os.getcwd()}
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=os.getcwd()
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "ok"
