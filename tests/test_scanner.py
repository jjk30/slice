"""Phase-18a/b AWS scanner tests. Stubbed boto3 (botocore Stubber) and fakes only: no real
AWS, no real Redis, no real database.

Layers:

- **Check tests** (18a) drive each of the four checks against a stubbed boto3 client.
- **Graph tests** (18a) prove the supervisor StateGraph fans out to all four checks and
  that one check raising records an error but never kills the others.
- **Cost tests** (18a) parse a canned get_cost_and_usage response.
- **Service tests** cover the per-account new-high alert + cooldown, external-id issuance
  (once, stable, never reused), assume-role with the External ID, connected vs
  not-connected targeting, and an assume failure marking the account errored with no
  fallback to the own account (and not blocking other accounts' daily runs).
- **Connect/scan/read endpoint tests** cover the connect flow and strict per-account
  isolation of findings, cost, and connection, plus that /scanner is auth-locked.
- **CloudFormation tests** parse the onboarding template and check the External ID
  condition, slice's principal, and that its action set matches part A's policy exactly.
- **Import test** proves importing the scanner never pulls boto3 in (lazy import).
"""

from __future__ import annotations

import pathlib
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote_plus

import boto3
import pytest
import yaml
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from app import config
from app.alerts import engine as alerts_engine
from app.auth.resolver import Account
from app.scanner import checks, cost, graph, routes, service
from app.scanner import session as sess
from app.scanner.checks import (
    check_ebs_waste,
    check_eip_waste,
    check_iam_risk,
    check_idle_instances,
    check_s3_public,
    check_sg_open,
    check_snapshot_waste,
    check_unencrypted,
)
from app.scanner.models import (
    CHECK_CONNECTION,
    CHECK_EBS_WASTE,
    CHECK_EIP_WASTE,
    CHECK_IAM_RISK,
    CHECK_IDLE_INSTANCES,
    CHECK_S3_PUBLIC,
    CHECK_SG_OPEN,
    CHECK_SNAPSHOT_WASTE,
    CHECK_UNENCRYPTED,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MED,
    Finding,
)
from app.main import app

ALL_USERS = "http://acs.amazonaws.com/groups/global/AllUsers"
TEMPLATE_FILE = "infra/user-onboarding/slice-readonly-role.yaml"

# Fake access key ids, built at runtime so secret scanners never match the literal shape.
OLD_KEY_ID = "AKIA" + "EXAMPLEOLD01"
NEW_KEY_ID = "AKIA" + "EXAMPLENEW01"
DEPLOY_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
STALE_KEY_ID = "AKIA" + "EXAMPLEKEY001"
TEMP_KEY_ID = "ASIA" + "EXAMPLE000001"


def _client_error(code: str, message: str, op: str = "AssumeRole") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, op)


# --- boto3 stubbing helpers -------------------------------------------------


class FakeSession:
    """A stand-in for boto3.Session: returns pre-stubbed clients by service name.

    A service with no stub raises when built, which the checks catch and fail open on,
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
        {"AccessKeyMetadata": [{"UserName": "admin-user", "AccessKeyId": OLD_KEY_ID, "Status": "Active", "CreateDate": old}]},
        {"UserName": "admin-user"},
    )
    stub.add_response(
        "get_access_key_last_used",
        {"UserName": "admin-user", "AccessKeyLastUsed": {"ServiceName": "s3", "Region": "us-east-1", "LastUsedDate": recent}},
        {"AccessKeyId": OLD_KEY_ID},
    )
    stub.add_response(
        "list_attached_user_policies",
        {"AttachedPolicies": [{"PolicyName": "AdministratorAccess", "PolicyArn": "arn:aws:iam::aws:policy/AdministratorAccess"}]},
        {"UserName": "admin-user"},
    )
    # normal: one recent key, no admin.
    stub.add_response(
        "list_access_keys",
        {"AccessKeyMetadata": [{"UserName": "normal", "AccessKeyId": NEW_KEY_ID, "Status": "Active", "CreateDate": recent}]},
        {"UserName": "normal"},
    )
    stub.add_response("list_attached_user_policies", {"AttachedPolicies": []}, {"UserName": "normal"})
    stub.activate()

    findings = check_iam_risk(FakeSession({"iam": iam}))
    stub.assert_no_pending_responses()

    by_resource = {f.resource_id: f for f in findings}
    assert OLD_KEY_ID in by_resource and by_resource[OLD_KEY_ID].severity == SEVERITY_MED
    assert "admin-user" in by_resource and by_resource["admin-user"].severity == SEVERITY_HIGH
    assert NEW_KEY_ID not in by_resource  # recent key not flagged


async def test_iam_key_finding_is_about_age_and_titled_as_a_key(monkeypatch):
    """Phase 26: the key finding names the key (not the user) as its resource, carries the
    age and the owner in its detail, and its email/dashboard title is the key wording, never
    "full admin access"."""
    from app.alerts.channels import SCAN_KEY_COPY, finding_title, is_access_key_id

    monkeypatch.setattr(config, "SCANNER_IAM_KEY_MAX_AGE_DAYS", 90)
    iam = _client("iam")
    stub = Stubber(iam)
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    stub.add_response("list_users", {"Users": [
        {"UserName": "deploy", "UserId": "AIDAEXAMPLEDEPLOY", "Arn": "arn:aws:iam::1:user/deploy", "Path": "/", "CreateDate": old}
    ]})
    stub.add_response(
        "list_access_keys",
        {"AccessKeyMetadata": [{"UserName": "deploy", "AccessKeyId": DEPLOY_KEY_ID, "Status": "Active", "CreateDate": old}]},
        {"UserName": "deploy"},
    )
    stub.add_response(
        "get_access_key_last_used",
        {"UserName": "deploy", "AccessKeyLastUsed": {"ServiceName": "s3", "Region": "us-east-1", "LastUsedDate": old}},
        {"AccessKeyId": DEPLOY_KEY_ID},
    )
    stub.add_response("list_attached_user_policies", {"AttachedPolicies": []}, {"UserName": "deploy"})
    stub.activate()

    findings = check_iam_risk(FakeSession({"iam": iam}))
    stub.assert_no_pending_responses()

    assert len(findings) == 1
    finding = findings[0]
    assert finding.resource_id == DEPLOY_KEY_ID and is_access_key_id(finding.resource_id)
    assert finding.severity == SEVERITY_MED
    assert finding.detail["user"] == "deploy" and finding.detail["age_days"] > 90
    assert finding.detail["status"] == "Active" and finding.detail["last_used"].startswith("2020-01-01")
    assert "days old" in finding.summary and "Administrator" not in finding.summary

    title = finding_title(finding.check, finding.resource_id, config.AWS_REGION)
    assert title == f"The access key {DEPLOY_KEY_ID} is more than 90 days old."
    assert "full admin access" not in title
    assert SCAN_KEY_COPY["iam_risk"]["doc"].startswith("https://docs.aws.amazon.com/IAM/")


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
        {"AccessKeyMetadata": [{"UserName": "u", "AccessKeyId": STALE_KEY_ID, "Status": "Active", "CreateDate": old}]},
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
            "Filter": {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}},
        },
    )
    stub.activate()
    now = datetime(2026, 8, 23, 6, 0, tzinfo=timezone.utc)
    report = cost.fetch_costs(FakeSession({"ce": ce}), now=now)
    stub.assert_no_pending_responses()
    assert report.yesterday == Decimal("4.00")


# --- Fakes for service / route tests ----------------------------------------


class FakeScannerDB:
    """Account-aware in-memory store for findings, costs, and connections (phase 18b)."""

    enabled = True

    def __init__(self):
        # run_id -> (account_id, [Finding])
        self.runs: dict[str, tuple] = {}
        self.order: list[str] = []
        self.connections: dict[int, dict] = {}
        self.costs: dict = {}  # (account_id, date) -> amount
        # Phase 24b: (scope, check, resource_id) -> {note, created_at, removed_at}; soft
        # delete like the real table, so the re-arm read can see when one was undone.
        self.expectations: dict = {}
        self.run_times: dict[str, datetime] = {}
        self.fail_expectation_writes = False

    # --- findings ---
    async def record_findings(self, account_id, run_id, findings):
        acct, existing = self.runs.get(run_id, (account_id, []))
        self.runs[run_id] = (account_id, list(existing) + list(findings))
        if run_id not in self.order:
            self.order.append(run_id)
        self.run_times.setdefault(run_id, datetime.now(timezone.utc))

    # --- expectations (phase 24b) ---
    async def list_expectations(self, account_id):
        return [
            {"check": c, "resource_id": r, "note": v["note"], "created_at": v["created_at"]}
            for (a, c, r), v in self.expectations.items()
            if a == account_id and v["removed_at"] is None
        ]

    async def add_expectation(self, account_id, check, resource_id, note=None):
        if self.fail_expectation_writes:
            raise RuntimeError("database is down")
        row = {"note": note, "created_at": datetime.now(timezone.utc), "removed_at": None}
        self.expectations[(account_id, check, resource_id)] = row
        return {"check": check, "resource_id": resource_id, "note": note, "created_at": row["created_at"]}

    async def remove_expectation(self, account_id, check, resource_id):
        if self.fail_expectation_writes:
            raise RuntimeError("database is down")
        row = self.expectations.get((account_id, check, resource_id))
        if row is None or row["removed_at"] is not None:
            return False
        row["removed_at"] = datetime.now(timezone.utc)
        return True

    async def rearmed_expectations_since(self, account_id, run_id):
        since = self.run_times.get(run_id)
        if since is None:
            return set()
        return {
            (c, r)
            for (a, c, r), v in self.expectations.items()
            if a == account_id and v["removed_at"] is not None and v["removed_at"] >= since
        }

    async def previous_run_id(self, account_id, current):
        prev = [r for r in self.order if r != current and self.runs[r][0] == account_id]
        return prev[-1] if prev else None

    async def high_resource_ids(self, account_id, run_id):
        acct, fs = self.runs.get(run_id, (None, []))
        if acct != account_id:
            return set()
        return {f.resource_id for f in fs if f.severity == SEVERITY_HIGH}

    async def latest_run_id(self, account_id):
        for r in reversed(self.order):
            if self.runs[r][0] == account_id:
                return r
        return None

    async def findings_for_run(self, account_id, run_id):
        acct, fs = self.runs.get(run_id, (None, []))
        if acct != account_id:
            return []
        return [f.as_dict() | {"created_at": None} for f in fs]

    # --- costs ---
    async def record_aws_costs(self, account_id, rows):
        for d, a in rows:
            self.costs[(account_id, d)] = a

    async def aws_cost_rows_since(self, account_id, since):
        rows = [
            {"date": d, "amount_usd": a, "fetched_at": None}
            for (acct, d), a in self.costs.items()
            if acct == account_id and d >= since
        ]
        return sorted(rows, key=lambda r: r["date"], reverse=True)

    # --- connections ---
    async def get_connection(self, account_id):
        row = self.connections.get(int(account_id))
        return dict(row) if row else None

    async def create_connection(self, account_id, external_id):
        row = self.connections.get(int(account_id))
        if row is None:
            row = {
                "id": len(self.connections) + 1, "account_id": int(account_id),
                "role_arn": None, "external_id": external_id, "status": "pending",
                "last_error": None, "connected_at": None, "created_at": None,
            }
            self.connections[int(account_id)] = row
        return dict(row)

    async def set_connection_status(self, account_id, status, *, role_arn=None, last_error=None):
        row = self.connections.setdefault(
            int(account_id),
            {"id": len(self.connections) + 1, "account_id": int(account_id),
             "role_arn": None, "external_id": "seed", "status": "pending",
             "last_error": None, "connected_at": None, "created_at": None},
        )
        row["status"] = status
        row["role_arn"] = role_arn
        row["last_error"] = last_error
        if status == "connected":
            row["connected_at"] = datetime.now(timezone.utc)
        return dict(row)

    async def disconnect(self, account_id):
        row = self.connections.get(int(account_id))
        if row is None:
            return False
        row.update(role_arn=None, status="pending", last_error=None, connected_at=None)
        return True

    async def connected_accounts(self):
        return [
            {"account_id": r["account_id"], "role_arn": r["role_arn"], "external_id": r["external_id"]}
            for r in self.connections.values()
            if r["status"] == "connected" and r["role_arn"]
        ]

    def connect(self, account_id, role_arn, external_id="ext"):
        """Test helper: seed a live connection."""
        self.connections[int(account_id)] = {
            "id": len(self.connections) + 1, "account_id": int(account_id),
            "role_arn": role_arn, "external_id": external_id, "status": "connected",
            "last_error": None, "connected_at": None, "created_at": None,
        }


class FakeChannel:
    name = "fake"

    def __init__(self):
        self.sent = []

    async def send(self, alert):
        self.sent.append(alert)
        from app.alerts.channels import DeliveryResult

        return DeliveryResult(ok=True)


def _async_return(value):
    async def fn(session):
        return value
    return fn


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


@pytest.fixture
def set_db():
    prev = getattr(app.state, "db", None)

    def _set(db):
        app.state.db = db
        return db

    yield _set
    app.state.db = prev


@pytest.fixture
async def client():
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
        yield c


def _as_account(account):
    return lambda request: account


# --- Service: new-high alert + cooldown (per account) -----------------------


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
    # The detail carries structured findings (check/resource/region/severity), and the older
    # summaries list is kept alongside.
    assert alert.detail["findings"] == [
        {"check": CHECK_SG_OPEN, "resource": "sg-1", "region": config.AWS_REGION, "severity": SEVERITY_HIGH}
    ]
    assert alert.detail["summaries"] == ["world-open ssh"]
    from app.alerts.channels import subject_for

    assert subject_for(alert) == "slice found 1 thing to check in your AWS account"


async def test_scan_email_renders_s3_public_doc_link(monkeypatch, scan_alerts_on):
    """A new s3_public high renders its friendly block, naming the bucket and its AWS doc link."""
    channel = scan_alerts_on
    highs = [Finding(check=CHECK_S3_PUBLIC, resource_id="acme-invoices", severity=SEVERITY_HIGH, summary="public")]
    monkeypatch.setattr(service, "run_scan_graph", _async_return(highs))

    db = FakeScannerDB()
    await service.run_scan(object(), db, None, run_id="run1")
    await alerts_engine.drain()

    from app.alerts.channels import body_for

    body = body_for(channel.sent[0])
    assert "Your S3 storage bucket acme-invoices" in body
    assert (
        "Read more: https://docs.aws.amazon.com/AmazonS3/latest/userguide/"
        "access-control-block-public-access.html" in body
    )
    assert "\u2014" not in body  # no em dash anywhere


# --- Phase 24b: expectations ------------------------------------------------


async def test_expected_high_is_skipped_in_the_new_high_diff(monkeypatch, scan_alerts_on):
    """An expected (check, resource) is recorded like any finding but never alerted."""
    channel = scan_alerts_on
    highs = [Finding(check=CHECK_SG_OPEN, resource_id="sg-1", severity=SEVERITY_HIGH, summary="world-open ssh")]
    monkeypatch.setattr(service, "run_scan_graph", _async_return(highs))

    db = FakeScannerDB()
    await db.add_expectation(None, CHECK_SG_OPEN, "sg-1", "bastion, on purpose")
    result = await service.run_scan(object(), db, None, run_id="run1")
    await alerts_engine.drain()

    assert result.new_highs == []
    assert result.expected_skipped == 1
    assert channel.sent == []
    # Still recorded: the record is untouched by expectations.
    assert [f.resource_id for f in db.runs["run1"][1]] == ["sg-1"]


async def test_expectation_matches_check_and_resource_together(monkeypatch, scan_alerts_on):
    """Expecting sg-1 for one check does not silence a different check on the same resource."""
    channel = scan_alerts_on
    highs = [Finding(check=CHECK_SG_OPEN, resource_id="sg-1", severity=SEVERITY_HIGH, summary="open")]
    monkeypatch.setattr(service, "run_scan_graph", _async_return(highs))

    db = FakeScannerDB()
    await db.add_expectation(None, CHECK_S3_PUBLIC, "sg-1")
    result = await service.run_scan(object(), db, None, run_id="run1")
    await alerts_engine.drain()

    assert [f.resource_id for f in result.new_highs] == ["sg-1"]
    assert result.expected_skipped == 0
    assert len(channel.sent) == 1


async def test_undoing_an_expectation_brings_the_finding_back_once(monkeypatch, scan_alerts_on):
    """Expected: quiet. Un-expected: the next scan alerts even though the previous run had it. Then quiet."""
    channel = scan_alerts_on
    highs = [Finding(check=CHECK_SG_OPEN, resource_id="sg-1", severity=SEVERITY_HIGH, summary="open")]
    monkeypatch.setattr(service, "run_scan_graph", _async_return(highs))

    db = FakeScannerDB()
    await db.add_expectation(None, CHECK_SG_OPEN, "sg-1")
    await service.run_scan(object(), db, None, run_id="run1")
    await alerts_engine.drain()
    assert channel.sent == []

    assert await db.remove_expectation(None, CHECK_SG_OPEN, "sg-1") is True
    result = await service.run_scan(object(), db, None, run_id="run2")
    await alerts_engine.drain()
    assert [f.resource_id for f in result.new_highs] == ["sg-1"]
    assert len(channel.sent) == 1

    # The run after that sees it in the previous run and stays quiet, as before.
    result3 = await service.run_scan(object(), db, None, run_id="run3")
    await alerts_engine.drain()
    assert result3.new_highs == []
    assert len(channel.sent) == 1


async def test_email_says_how_many_expected_findings_were_skipped(monkeypatch, scan_alerts_on):
    channel = scan_alerts_on
    highs = [
        Finding(check=CHECK_SG_OPEN, resource_id="sg-1", severity=SEVERITY_HIGH, summary="a"),
        Finding(check=CHECK_S3_PUBLIC, resource_id="acme-site", severity=SEVERITY_HIGH, summary="b"),
    ]
    monkeypatch.setattr(service, "run_scan_graph", _async_return(highs))

    db = FakeScannerDB()
    await db.add_expectation(None, CHECK_S3_PUBLIC, "acme-site", "public website bucket")
    await service.run_scan(object(), db, None, run_id="run1")
    await alerts_engine.drain()

    from app.alerts.channels import body_for, subject_for

    assert len(channel.sent) == 1
    alert = channel.sent[0]
    assert alert.detail["count"] == 1
    assert alert.detail["expected_skipped"] == 1
    assert subject_for(alert) == "slice found 1 thing to check in your AWS account"
    body = body_for(alert)
    assert "1 expected finding not shown. Manage them on the dashboard." in body
    assert "acme-site" not in body
    assert "\u2014" not in body


async def test_expectations_read_failure_skips_nothing(monkeypatch, scan_alerts_on):
    """A broken expectations read fails open: the finding is alerted rather than lost."""
    channel = scan_alerts_on
    highs = [Finding(check=CHECK_SG_OPEN, resource_id="sg-1", severity=SEVERITY_HIGH, summary="a")]
    monkeypatch.setattr(service, "run_scan_graph", _async_return(highs))

    db = FakeScannerDB()

    async def broken(account_id):
        raise RuntimeError("db down")

    db.list_expectations = broken
    result = await service.run_scan(object(), db, None, run_id="run1")
    await alerts_engine.drain()
    assert [f.resource_id for f in result.new_highs] == ["sg-1"]
    assert len(channel.sent) == 1


async def test_repeat_high_does_not_fire(monkeypatch, scan_alerts_on):
    channel = scan_alerts_on
    highs = [Finding(check=CHECK_SG_OPEN, resource_id="sg-1", severity=SEVERITY_HIGH, summary="world-open ssh")]
    monkeypatch.setattr(service, "run_scan_graph", _async_return(highs))

    db = FakeScannerDB()
    await service.run_scan(object(), db, None, run_id="run1")  # first: new -> fires
    await service.run_scan(object(), db, None, run_id="run2")  # same high -> not new
    await alerts_engine.drain()
    assert len(channel.sent) == 1


async def test_cooldown_collapses_repeated_new_highs(monkeypatch, scan_alerts_on):
    channel = scan_alerts_on

    async def graph_for(session):
        return graph_for.value

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
    assert len(channel.sent) == 1


async def test_alert_cooldown_is_per_account(monkeypatch, scan_alerts_on):
    """A new high in account A and in account B each alert: cooldown is keyed per account."""
    channel = scan_alerts_on
    monkeypatch.setattr(
        service, "run_scan_graph",
        _async_return([Finding(check=CHECK_SG_OPEN, resource_id="sg-x", severity=SEVERITY_HIGH, summary="s")]),
    )
    db = FakeScannerDB()
    await service.run_scan(object(), db, None, run_id="a1", account_id=2)
    await service.run_scan(object(), db, None, run_id="b1", account_id=3)
    await alerts_engine.drain()
    # Two accounts, two independent cooldown keys -> two alerts.
    assert len(channel.sent) == 2


# --- External id: generated once, stable, never reused -----------------------


async def test_external_id_generated_once_stable_and_unique():
    db = FakeScannerDB()
    a1 = await service.get_or_create_external_id(db, 5)
    a2 = await service.get_or_create_external_id(db, 5)
    assert a1 == a2 and len(a1) == 32  # secrets.token_hex(16) -> 32 hex chars
    b1 = await service.get_or_create_external_id(db, 6)
    assert b1 != a1  # never reused across accounts


# --- make_session assume-role passes the External ID -------------------------


def test_make_session_assumes_with_external_id():
    sts = _client("sts")
    stub = Stubber(sts)
    stub.add_response(
        "assume_role",
        {
            "Credentials": {
                "AccessKeyId": TEMP_KEY_ID, "SecretAccessKey": "secret",
                "SessionToken": "token", "Expiration": datetime(2030, 1, 1, tzinfo=timezone.utc),
            },
            "AssumedRoleUser": {"AssumedRoleId": "AROAEXAMPLE:slice", "Arn": "arn:aws:sts::111111111111:assumed-role/x/slice"},
        },
        {
            "RoleArn": "arn:aws:iam::111111111111:role/slice-scanner/r",
            "RoleSessionName": config.SCANNER_ASSUME_ROLE_SESSION_NAME,
            "DurationSeconds": config.SCANNER_ASSUME_ROLE_DURATION_SECONDS,
            "ExternalId": "ext-123",
        },
    )
    stub.activate()
    session = sess.make_session(
        "arn:aws:iam::111111111111:role/slice-scanner/r", "ext-123", sts_client=sts
    )
    stub.assert_no_pending_responses()  # proves assume_role was called with ExternalId=ext-123
    creds = session.get_credentials()
    assert creds.access_key == TEMP_KEY_ID


def test_test_role_success(monkeypatch):
    identity = _client("sts")
    istub = Stubber(identity)
    istub.add_response(
        "get_caller_identity",
        {"Account": "999999999999", "Arn": "arn:aws:sts::999999999999:assumed-role/x/slice", "UserId": "AROA:slice"},
    )
    istub.activate()
    monkeypatch.setattr(sess, "make_session", lambda role, ext, sts_client=None: FakeSession({"sts": identity}))
    ok, info = sess.test_role("arn:aws:iam::999999999999:role/slice-scanner/r", "ext")
    assert ok is True and info == "999999999999"


def test_test_role_failure_returns_clear_message(monkeypatch):
    def boom(role, ext, sts_client=None):
        raise _client_error("AccessDenied", "not authorized to assume")

    monkeypatch.setattr(sess, "make_session", boom)
    ok, info = sess.test_role("arn:aws:iam::1:role/slice-scanner/r", "ext")
    assert ok is False and "AccessDenied" in info


# --- Scan targeting: connected / not-connected / assume failure --------------


async def test_scan_connected_uses_assumed_session(monkeypatch):
    """A connected account's scan builds an assumed session for its role + external id."""
    calls = []

    def spy_make_session(role_arn=None, external_id=None):
        calls.append((role_arn, external_id))
        return object()

    monkeypatch.setattr(service, "make_session", spy_make_session)
    monkeypatch.setattr(service, "run_scan_graph", _async_return([]))

    db = FakeScannerDB()
    db.connect(5, "arn:aws:iam::555555555555:role/slice-scanner/r", external_id="ext-5")
    result = await service.run_scan_for_account(db, None, 5, run_id="r1")

    assert result.status == "ok"
    assert calls == [("arn:aws:iam::555555555555:role/slice-scanner/r", "ext-5")]
    # Findings stored under account 5, not the own (None) scope.
    assert db.runs["r1"][0] == 5


async def test_scan_not_connected_writes_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(service, "run_scan_graph", lambda s: called.append(s) or _async_return([])(s))
    db = FakeScannerDB()  # account 7 has no connection
    result = await service.run_scan_for_account(db, None, 7, run_id="r1")
    assert result.status == "not_connected"
    assert db.runs == {}  # nothing scanned or stored
    assert called == []  # the graph never ran


async def test_assume_failure_marks_error_no_fallback(monkeypatch):
    """An assume failure marks the connection errored, stores a visible error finding for THAT
    account, and never falls back to scanning slice's own account."""
    graph_calls = []

    def boom(role_arn=None, external_id=None):
        raise _client_error("AccessDenied", "role trust does not allow slice")

    async def graph_spy(session):
        graph_calls.append(session)
        return [Finding(check=CHECK_SG_OPEN, resource_id="own", severity=SEVERITY_HIGH, summary="own infra")]

    monkeypatch.setattr(service, "make_session", boom)
    monkeypatch.setattr(service, "run_scan_graph", graph_spy)

    db = FakeScannerDB()
    db.connect(5, "arn:aws:iam::555555555555:role/slice-scanner/r", external_id="ext-5")
    result = await service.run_scan_for_account(db, None, 5, run_id="r1")

    assert result.status == "error" and "AccessDenied" in result.error
    assert graph_calls == []  # no fallback own-account scan happened
    # The connection is marked error, and a visible error finding is stored under account 5.
    assert db.connections[5]["status"] == "error"
    acct, findings_stored = db.runs["r1"]
    assert acct == 5
    assert [f.check for f in findings_stored] == [CHECK_CONNECTION]
    assert findings_stored[0].severity == SEVERITY_HIGH


async def test_daily_one_account_failure_does_not_block_others(monkeypatch):
    import fakeredis.aioredis

    redis = fakeredis.aioredis.FakeRedis()
    scanned_sessions = []

    def make_session_spy(role_arn=None, external_id=None):
        if role_arn and "bad" in role_arn:
            raise _client_error("AccessDenied", "bad role")
        return ("session", role_arn)

    async def graph_spy(session):
        scanned_sessions.append(session)
        return []

    monkeypatch.setattr(service, "make_session", make_session_spy)
    monkeypatch.setattr(service, "run_scan_graph", graph_spy)
    # Cost pull would call boto; make it a no-op report.
    monkeypatch.setattr(service, "fetch_costs", lambda session, now=None: cost.CostReport())

    db = FakeScannerDB()
    db.connect(2, "arn:aws:iam::222222222222:role/slice-scanner/good", external_id="e2")
    db.connect(3, "arn:aws:iam::333333333333:role/slice-scanner/bad", external_id="e3")

    await service.run_daily_once(db, redis)

    # Own account (None role) + the good account (2) both scanned; the bad one (3) did not.
    assert ("session", None) in scanned_sessions  # operator own
    assert ("session", "arn:aws:iam::222222222222:role/slice-scanner/good") in scanned_sessions
    assert db.connections[3]["status"] == "error"  # the bad one recorded its failure
    # Account 3 has only the connection-error finding; account 2 ran clean.
    assert any(v[0] == 3 and v[1] and v[1][0].check == CHECK_CONNECTION for v in db.runs.values())


# --- verify_connection persists status --------------------------------------


async def test_verify_connection_good_marks_connected(monkeypatch):
    db = FakeScannerDB()
    monkeypatch.setattr(service, "test_role", lambda role, ext: (True, "444444444444"))
    ok, info = await service.verify_connection(db, 5, "arn:aws:iam::444444444444:role/slice-scanner/r")
    assert ok is True and info == "444444444444"
    assert db.connections[5]["status"] == "connected"
    assert db.connections[5]["role_arn"] == "arn:aws:iam::444444444444:role/slice-scanner/r"


async def test_verify_connection_bad_marks_error(monkeypatch):
    db = FakeScannerDB()
    monkeypatch.setattr(service, "test_role", lambda role, ext: (False, "AccessDenied: nope"))
    ok, info = await service.verify_connection(db, 5, "arn:aws:iam::444444444444:role/slice-scanner/r")
    assert ok is False
    assert db.connections[5]["status"] == "error" and db.connections[5]["status"] != "connected"


# --- Endpoints: connect flow -------------------------------------------------


async def test_connect_get_returns_external_id_and_quick_create_url(client, monkeypatch, set_db):
    monkeypatch.setattr(
        config, "SCANNER_TEMPLATE_URL",
        "https://raw.githubusercontent.com/x/slice/main/infra/user-onboarding/slice-readonly-role.yaml",
    )
    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=5, login="u")))
    set_db(FakeScannerDB())

    r = await client.get("/scanner/connect")
    body = r.json()
    ext = body["external_id"]
    assert ext and len(ext) == 32
    assert body["slice_aws_account_id"] == "194133064379"
    assert body["template_path"] == TEMPLATE_FILE
    assert ext in body["quick_create_url"]  # external id prefilled
    assert quote_plus(config.SCANNER_TEMPLATE_URL) in body["quick_create_url"]  # template location

    # A second call returns the SAME external id (issued once).
    r2 = await client.get("/scanner/connect")
    assert r2.json()["external_id"] == ext


async def test_connect_get_operator_needs_no_connection(client, monkeypatch, set_db):
    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=1, login="operator")))
    set_db(FakeScannerDB())
    r = await client.get("/scanner/connect")
    body = r.json()
    assert body["status"] == "operator" and body["external_id"] is None


async def test_connect_post_good_assume(client, monkeypatch, set_db):
    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=5, login="u")))
    db = set_db(FakeScannerDB())
    monkeypatch.setattr(service, "test_role", lambda role, ext: (True, "444444444444"))

    r = await client.post(
        "/scanner/connect", json={"role_arn": "arn:aws:iam::444444444444:role/slice-scanner/r"}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "connected" and r.json()["aws_account_id"] == "444444444444"
    assert db.connections[5]["status"] == "connected"


async def test_connect_post_bad_assume_400_and_not_connected(client, monkeypatch, set_db):
    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=5, login="u")))
    db = set_db(FakeScannerDB())
    monkeypatch.setattr(service, "test_role", lambda role, ext: (False, "AccessDenied: no trust"))

    r = await client.post(
        "/scanner/connect", json={"role_arn": "arn:aws:iam::444444444444:role/slice-scanner/r"}
    )
    assert r.status_code == 400
    assert db.connections[5]["status"] == "error"
    assert db.connections[5]["status"] != "connected"


async def test_connect_post_rejects_bad_arn(client, monkeypatch, set_db):
    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=5, login="u")))
    set_db(FakeScannerDB())
    r = await client.post("/scanner/connect", json={"role_arn": "not-an-arn"})
    assert r.status_code == 400


async def test_disconnect_keeps_external_id(client, monkeypatch, set_db):
    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=5, login="u")))
    db = set_db(FakeScannerDB())
    db.connect(5, "arn:aws:iam::444444444444:role/slice-scanner/r", external_id="reserved-ext")

    r = await client.delete("/scanner/connect")
    assert r.status_code == 200 and r.json()["status"] == "disconnected"
    assert db.connections[5]["status"] == "pending"
    assert db.connections[5]["role_arn"] is None
    assert db.connections[5]["external_id"] == "reserved-ext"  # reserved


# --- Endpoints: run / findings / cost, per account ---------------------------


async def test_run_not_connected_account_is_refused(client, monkeypatch, set_db):
    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=9, login="u")))
    set_db(FakeScannerDB())  # account 9 has no connection
    r = await client.post("/scanner/run")
    assert r.status_code == 409 and r.json()["status"] == "not_connected"


async def test_run_connected_account_kicks_scan(client, monkeypatch, set_db):
    started = {}

    async def fake_run_scan_for_account(db, redis, account_id, *, run_id=None, alert=True):
        started["account_id"] = account_id
        started["run_id"] = run_id

    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=5, login="u")))
    db = set_db(FakeScannerDB())
    db.connect(5, "arn:aws:iam::555555555555:role/slice-scanner/r")
    monkeypatch.setattr(service, "run_scan_for_account", fake_run_scan_for_account)

    r = await client.post("/scanner/run")
    assert r.status_code == 202
    import asyncio

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert started == {"account_id": 5, "run_id": r.json()["run_id"]}


async def test_run_operator_scans_own(client, monkeypatch, set_db):
    started = {}

    async def fake(db, redis, account_id, *, run_id=None, alert=True):
        started["account_id"] = account_id

    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=1, login="operator")))
    set_db(FakeScannerDB())
    monkeypatch.setattr(service, "run_scan_for_account", fake)
    r = await client.post("/scanner/run")
    assert r.status_code == 202
    import asyncio

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert started["account_id"] == 1


async def test_findings_isolation_between_accounts(client, monkeypatch, set_db):
    db = set_db(FakeScannerDB())
    await db.record_findings(
        2, "runA",
        [Finding(check=CHECK_SG_OPEN, resource_id="x", severity=SEVERITY_HIGH, summary="s")],
    )

    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=2, login="a")))
    r2 = await client.get("/scanner/findings")
    assert r2.json()["run_id"] == "runA" and len(r2.json()["findings"]) == 1

    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=3, login="b")))
    r3 = await client.get("/scanner/findings")
    assert r3.json() == {"run_id": None, "findings": [], "estimated_monthly_waste_usd": 0.0}  # B sees nothing of A's


async def test_operator_findings_are_own_scope(client, monkeypatch, set_db):
    db = set_db(FakeScannerDB())
    await db.record_findings(
        None, "own1",
        [Finding(check=CHECK_S3_PUBLIC, resource_id="b", severity=SEVERITY_HIGH, summary="public")],
    )
    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=1, login="operator")))
    r = await client.get("/scanner/findings")
    assert r.json()["run_id"] == "own1" and len(r.json()["findings"]) == 1


async def test_cost_isolation_between_accounts(client, monkeypatch, set_db):
    db = set_db(FakeScannerDB())
    # The /scanner/cost route sums the current UTC month, so anchor the cost to the
    # first of this month on that same clock rather than a hardcoded calendar date.
    first = datetime.now(timezone.utc).date().replace(day=1)
    await db.record_aws_costs(2, [(first, Decimal("5.00"))])

    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=2, login="a")))
    r2 = await client.get("/scanner/cost")
    assert r2.json()["month_to_date"] == "5.00"

    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=3, login="b")))
    r3 = await client.get("/scanner/cost")
    assert r3.json()["month_to_date"] == "0"  # B sees only its own (empty) costs, never A's
    assert r3.json()["daily"] == []


async def test_findings_json_carries_title_and_expected(client, monkeypatch, set_db):
    """Each finding carries the email's plain-words line and its expected flag; expected rows stay listed."""
    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=5, login="u")))
    db = set_db(FakeScannerDB())
    await db.record_findings(5, "r1", [
        Finding(check=CHECK_S3_PUBLIC, resource_id="acme-site", severity=SEVERITY_HIGH, summary="public"),
        Finding(check=CHECK_SG_OPEN, resource_id="sg-1", severity=SEVERITY_HIGH, summary="open"),
    ])
    await db.add_expectation(5, CHECK_S3_PUBLIC, "acme-site", "website")

    r = await client.get("/scanner/findings")
    assert r.status_code == 200
    rows = {f["resource_id"]: f for f in r.json()["findings"]}
    assert set(rows) == {"acme-site", "sg-1"}
    assert rows["acme-site"]["expected"] is True
    assert rows["sg-1"]["expected"] is False
    assert rows["acme-site"]["title"] == (
        f"Your S3 storage bucket acme-site in {config.AWS_REGION} is open to the internet."
    )
    assert rows["sg-1"]["title"].startswith("A firewall rule on sg-1")


async def test_expectations_post_and_delete_round_trip(client, monkeypatch, set_db):
    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=5, login="u")))
    db = set_db(FakeScannerDB())

    r = await client.post(
        "/scanner/expectations",
        json={"check": CHECK_S3_PUBLIC, "resource_id": " acme-site ", "note": "website"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["expected"] is True
    assert body["check"] == CHECK_S3_PUBLIC and body["resource_id"] == "acme-site"
    assert body["note"] == "website"
    assert (5, CHECK_S3_PUBLIC, "acme-site") in db.expectations  # the caller's own scope

    r = await client.request(
        "DELETE", "/scanner/expectations", json={"check": CHECK_S3_PUBLIC, "resource_id": "acme-site"}
    )
    assert r.status_code == 200
    assert r.json() == {"expected": False, "check": CHECK_S3_PUBLIC, "resource_id": "acme-site", "removed": True}
    assert await db.list_expectations(5) == []

    # Removing it again is a no-op, said plainly.
    r = await client.request(
        "DELETE", "/scanner/expectations", json={"check": CHECK_S3_PUBLIC, "resource_id": "acme-site"}
    )
    assert r.status_code == 200 and r.json()["removed"] is False


async def test_expectations_unknown_check_is_rejected(client, monkeypatch, set_db):
    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=5, login="u")))
    db = set_db(FakeScannerDB())
    for body in (
        {"check": "made_up", "resource_id": "x"},
        {"check": "connection", "resource_id": "x"},  # a finding kind, not an expectable check
        {"check": CHECK_S3_PUBLIC, "resource_id": ""},
        {"check": CHECK_S3_PUBLIC},
        ["not", "an", "object"],
    ):
        r = await client.post("/scanner/expectations", json=body)
        assert r.status_code == 400, body
    assert db.expectations == {}


async def test_expectations_are_scoped_to_the_signed_in_account(client, monkeypatch, set_db):
    """Account B cannot see, flag, or remove account A's expectation; each writes its own scope."""
    db = set_db(FakeScannerDB())
    for acct in (2, 3):
        await db.record_findings(acct, f"r{acct}", [
            Finding(check=CHECK_S3_PUBLIC, resource_id="shared-name", severity=SEVERITY_HIGH, summary="p"),
        ])

    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=2, login="a")))
    r = await client.post("/scanner/expectations", json={"check": CHECK_S3_PUBLIC, "resource_id": "shared-name"})
    assert r.status_code == 200

    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=3, login="b")))
    assert (await client.get("/scanner/findings")).json()["findings"][0]["expected"] is False
    r = await client.request(
        "DELETE", "/scanner/expectations", json={"check": CHECK_S3_PUBLIC, "resource_id": "shared-name"}
    )
    assert r.status_code == 200 and r.json()["removed"] is False

    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=2, login="a")))
    assert (await client.get("/scanner/findings")).json()["findings"][0]["expected"] is True
    assert (2, CHECK_S3_PUBLIC, "shared-name") in db.expectations
    assert (3, CHECK_S3_PUBLIC, "shared-name") not in db.expectations


async def test_expectations_operator_writes_under_the_own_scope(client, monkeypatch, set_db):
    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=1, login="operator")))
    db = set_db(FakeScannerDB())
    r = await client.post("/scanner/expectations", json={"check": CHECK_SG_OPEN, "resource_id": "sg-1"})
    assert r.status_code == 200
    assert (None, CHECK_SG_OPEN, "sg-1") in db.expectations


async def test_expectations_write_failure_is_a_500(client, monkeypatch, set_db):
    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=5, login="u")))
    db = set_db(FakeScannerDB())
    db.fail_expectation_writes = True
    r = await client.post("/scanner/expectations", json={"check": CHECK_SG_OPEN, "resource_id": "sg-1"})
    assert r.status_code == 500 and r.json() == {"error": {"message": "Could not save the expectation."}}
    r = await client.request("DELETE", "/scanner/expectations", json={"check": CHECK_SG_OPEN, "resource_id": "sg-1"})
    assert r.status_code == 500 and r.json() == {"error": {"message": "Could not remove the expectation."}}


async def test_expectations_without_a_database_are_a_503(client, monkeypatch, set_db):
    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=5, login="u")))
    set_db(None)
    r = await client.post("/scanner/expectations", json={"check": CHECK_SG_OPEN, "resource_id": "sg-1"})
    assert r.status_code == 503


async def test_findings_endpoint_no_db_is_empty(client, monkeypatch, set_db):
    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=5, login="u")))
    set_db(None)
    r = await client.get("/scanner/findings")
    assert r.status_code == 200 and r.json() == {"run_id": None, "findings": [], "estimated_monthly_waste_usd": 0.0}


async def test_scanner_endpoints_require_auth(client, monkeypatch):
    """With auth on and no slice key, every /scanner path is a 401."""
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    calls = [
        ("GET", "/scanner/findings"), ("GET", "/scanner/cost"), ("POST", "/scanner/run"),
        ("GET", "/scanner/connect"), ("POST", "/scanner/connect"), ("DELETE", "/scanner/connect"),
        ("POST", "/scanner/expectations"), ("DELETE", "/scanner/expectations"),
    ]
    for method, path in calls:
        r = await client.request(method, path)
        assert r.status_code == 401, (method, path)


# --- CloudFormation template -------------------------------------------------


def test_cloudformation_template_valid_and_matches_part_a():
    text = pathlib.Path(TEMPLATE_FILE).read_text()
    doc = yaml.safe_load(text)  # parses as valid YAML

    role = doc["Resources"]["SliceScannerRole"]["Properties"]
    trust = role["AssumeRolePolicyDocument"]["Statement"][0]
    # Principal is slice's account; the External ID condition guards the assume.
    assert trust["Principal"]["AWS"] == "arn:aws:iam::194133064379:root"
    assert trust["Action"] == "sts:AssumeRole"
    assert trust["Condition"]["StringEquals"]["sts:ExternalId"] == {"Ref": "ExternalId"}

    # The permission set matches part A's scanner policy actions exactly.
    cfn_actions = set(role["Policies"][0]["PolicyDocument"]["Statement"][0]["Action"])

    tf = pathlib.Path("infra/ec2/main.tf").read_text()
    start = tf.index('data "aws_iam_policy_document" "scanner"')
    end = tf.index('data "aws_iam_policy_document" "scanner_assume"')
    scanner_doc = tf[start:end]
    tf_actions = set(re.findall(r'"([a-z0-9]+:[A-Za-z]+)"', scanner_doc))

    assert cfn_actions == tf_actions
    assert "sts:AssumeRole" not in cfn_actions  # that lives in the box's own policy, not the role
    # Phase 18c cost-waste checks are present in lockstep on both sides.
    new_cost_actions = {
        "ec2:DescribeAddresses", "ec2:DescribeSnapshots", "ec2:DescribeInstances",
        "cloudwatch:GetMetricStatistics",
    }
    assert new_cost_actions <= cfn_actions
    assert new_cost_actions <= tf_actions


def test_cloudformation_template_has_role_path_and_external_id_param():
    doc = yaml.safe_load(pathlib.Path(TEMPLATE_FILE).read_text())
    assert doc["Parameters"]["ExternalId"]["Type"] == "String"
    # The fixed path lets the box scope sts:AssumeRole to arn:aws:iam::*:role/slice-scanner/*.
    assert doc["Resources"]["SliceScannerRole"]["Properties"]["Path"] == "/slice-scanner/"


def test_box_assume_policy_is_scoped_to_slice_scanner_path():
    tf = pathlib.Path("infra/ec2/main.tf").read_text()
    assert "arn:aws:iam::*:role/slice-scanner/*" in tf
    assert '"sts:AssumeRole"' in tf


# --- Cost-waste checks (phase 18c) ------------------------------------------


async def test_ebs_waste_unattached_gp2_and_gp2_to_gp3():
    ec2 = _client("ec2")
    stub = Stubber(ec2)
    stub.add_response(
        "describe_volumes",
        {"Volumes": [
            {"VolumeId": "vol-idle", "VolumeType": "gp2", "Size": 100, "State": "available"},
            {"VolumeId": "vol-gp2", "VolumeType": "gp2", "Size": 200, "State": "in-use"},
            {"VolumeId": "vol-gp3", "VolumeType": "gp3", "Size": 50, "State": "in-use"},
        ]},
    )
    stub.activate()
    findings = check_ebs_waste(FakeSession({"ec2": ec2}))
    stub.assert_no_pending_responses()

    by_id = {f.resource_id: f for f in findings}
    assert set(by_id) == {"vol-idle", "vol-gp2"}  # the attached gp3 is fine
    # Unattached 100 GiB gp2: full monthly cost 100 * $0.10 = $10.00 -> high.
    assert by_id["vol-idle"].detail["est_monthly_usd"] == 10.0
    assert by_id["vol-idle"].severity == SEVERITY_HIGH
    assert by_id["vol-idle"].detail["kind"] == "unattached"
    # Attached gp2 -> gp3 suggestion: 200 * $0.02 = $4.00, capped at med.
    assert by_id["vol-gp2"].detail["est_monthly_usd"] == 4.0
    assert by_id["vol-gp2"].severity == SEVERITY_MED
    assert by_id["vol-gp2"].detail["kind"] == "gp2_to_gp3"


async def test_ebs_gp2_to_gp3_capped_at_med_even_for_large_volume():
    ec2 = _client("ec2")
    stub = Stubber(ec2)
    # 1000 GiB gp2 -> gp3 saving is $20/mo, which would be "high" by the raw rule; the
    # gp2->gp3 suggestion is capped at med.
    stub.add_response(
        "describe_volumes",
        {"Volumes": [{"VolumeId": "vol-big", "VolumeType": "gp2", "Size": 1000, "State": "in-use"}]},
    )
    stub.activate()
    findings = check_ebs_waste(FakeSession({"ec2": ec2}))
    assert findings[0].detail["est_monthly_usd"] == 20.0
    assert findings[0].severity == SEVERITY_MED


async def test_eip_waste_flags_unassociated_not_associated():
    ec2 = _client("ec2")
    stub = Stubber(ec2)
    stub.add_response(
        "describe_addresses",
        {"Addresses": [
            {"PublicIp": "1.2.3.4", "AllocationId": "eipalloc-idle"},
            {"PublicIp": "5.6.7.8", "AllocationId": "eipalloc-used", "AssociationId": "eipassoc-1"},
        ]},
    )
    stub.activate()
    findings = check_eip_waste(FakeSession({"ec2": ec2}))
    stub.assert_no_pending_responses()

    assert [f.resource_id for f in findings] == ["eipalloc-idle"]
    assert findings[0].detail["est_monthly_usd"] == 3.6
    assert findings[0].severity == SEVERITY_MED


async def test_snapshot_waste_single_old_snapshot_upper_bound_wording():
    ec2 = _client("ec2")
    stub = Stubber(ec2)
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    stub.add_response(
        "describe_snapshots",
        {"Snapshots": [
            {"SnapshotId": "snap-old", "VolumeSize": 100, "StartTime": old, "State": "completed", "OwnerId": "1"},
            {"SnapshotId": "snap-new", "VolumeSize": 100, "StartTime": datetime.now(timezone.utc), "State": "completed", "OwnerId": "1"},
        ]},
        {"OwnerIds": ["self"]},
    )
    stub.activate()
    findings = check_snapshot_waste(FakeSession({"ec2": ec2}))
    stub.assert_no_pending_responses()

    assert [f.resource_id for f in findings] == ["snap-old"]  # the recent one is not flagged
    assert findings[0].detail["est_monthly_usd"] == 5.0  # 100 * $0.05 upper bound
    assert "upper bound" in findings[0].summary
    assert findings[0].detail["kind"] == "snapshot"


async def test_snapshot_waste_collapses_when_many():
    ec2 = _client("ec2")
    stub = Stubber(ec2)
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    snaps = [
        {"SnapshotId": f"snap-{i}", "VolumeSize": 10, "StartTime": old, "State": "completed", "OwnerId": "1"}
        for i in range(11)  # > 10 -> grouped
    ]
    stub.add_response("describe_snapshots", {"Snapshots": snaps}, {"OwnerIds": ["self"]})
    stub.activate()
    findings = check_snapshot_waste(FakeSession({"ec2": ec2}))
    stub.assert_no_pending_responses()

    assert len(findings) == 1
    f = findings[0]
    assert f.detail["kind"] == "grouped" and f.detail["count"] == 11
    assert f.detail["est_monthly_usd"] == 5.5  # 110 GiB * $0.05
    assert "upper bound" in f.summary and "11" in f.summary


async def test_idle_instances_flags_low_cpu_priced_and_unknown_type():
    ec2 = _client("ec2")
    ec2_stub = Stubber(ec2)
    ec2_stub.add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [
            {"InstanceId": "i-known", "InstanceType": "t3.large", "State": {"Name": "running"}},
            {"InstanceId": "i-unknown", "InstanceType": "x9.mega", "State": {"Name": "running"}},
        ]}]},
    )
    ec2_stub.activate()

    cw = _client("cloudwatch")
    cw_stub = Stubber(cw)
    for _ in range(2):  # one metrics call per instance, in order
        cw_stub.add_response(
            "get_metric_statistics",
            {"Label": "CPUUtilization", "Datapoints": [
                {"Timestamp": datetime(2026, 8, 20, tzinfo=timezone.utc), "Average": 2.0, "Unit": "Percent"},
            ]},
        )
    cw_stub.activate()

    findings = check_idle_instances(FakeSession({"ec2": ec2, "cloudwatch": cw}))
    ec2_stub.assert_no_pending_responses()
    cw_stub.assert_no_pending_responses()

    by_id = {f.resource_id: f for f in findings}
    assert set(by_id) == {"i-known", "i-unknown"}  # both idle, both flagged
    # t3.large priced from the map: 0.0832 * 730 = $60.74/mo -> high.
    assert by_id["i-known"].detail["est_monthly_usd"] == 60.74
    assert by_id["i-known"].severity == SEVERITY_HIGH
    assert by_id["i-known"].detail["avg_cpu_percent"] == 2.0
    # Unknown type: still flagged, but no price -> est null, severity low.
    assert by_id["i-unknown"].detail["est_monthly_usd"] is None
    assert by_id["i-unknown"].severity == SEVERITY_LOW


async def test_idle_instances_busy_instance_not_flagged():
    ec2 = _client("ec2")
    ec2_stub = Stubber(ec2)
    ec2_stub.add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [
            {"InstanceId": "i-busy", "InstanceType": "t3.large", "State": {"Name": "running"}},
        ]}]},
    )
    ec2_stub.activate()
    cw = _client("cloudwatch")
    cw_stub = Stubber(cw)
    cw_stub.add_response(
        "get_metric_statistics",
        {"Datapoints": [{"Timestamp": datetime(2026, 8, 20, tzinfo=timezone.utc), "Average": 42.0, "Unit": "Percent"}]},
    )
    cw_stub.activate()
    findings = check_idle_instances(FakeSession({"ec2": ec2, "cloudwatch": cw}))
    assert findings == []  # 42% CPU is not idle


# --- Waste estimate in the findings response ---------------------------------


async def test_findings_response_sums_estimated_waste(client, monkeypatch, set_db):
    db = set_db(FakeScannerDB())
    await db.record_findings(
        None, "run-w",
        [
            Finding(check=CHECK_EBS_WASTE, resource_id="vol-1", severity=SEVERITY_HIGH,
                    summary="unattached", detail={"kind": "unattached", "est_monthly_usd": 10.0}),
            Finding(check=CHECK_EIP_WASTE, resource_id="eip-1", severity=SEVERITY_MED,
                    summary="idle eip", detail={"kind": "unassociated_eip", "est_monthly_usd": 3.6}),
            # A security finding with no est counts as 0 toward the sum.
            Finding(check=CHECK_S3_PUBLIC, resource_id="b", severity=SEVERITY_HIGH,
                    summary="public", detail={"kind": "public_acl"}),
        ],
    )
    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=1, login="operator")))
    r = await client.get("/scanner/findings")
    body = r.json()
    assert body["estimated_monthly_waste_usd"] == 13.6
    assert len(body["findings"]) == 3


async def test_findings_response_waste_is_zero_when_empty(client, monkeypatch, set_db):
    monkeypatch.setattr(routes, "read_account", _as_account(Account(id=1, login="operator")))
    set_db(FakeScannerDB())
    r = await client.get("/scanner/findings")
    assert r.json()["estimated_monthly_waste_usd"] == 0


# --- Supervisor graph now has eight nodes ------------------------------------


def test_registry_has_all_eight_checks():
    names = [n for n, _ in checks.CHECKS]
    assert names == [
        CHECK_S3_PUBLIC, CHECK_SG_OPEN, CHECK_UNENCRYPTED, CHECK_IAM_RISK,
        CHECK_EBS_WASTE, CHECK_EIP_WASTE, CHECK_SNAPSHOT_WASTE, CHECK_IDLE_INSTANCES,
    ]


async def test_supervisor_runs_all_eight_nodes(monkeypatch):
    names = list("abcdefgh")
    monkeypatch.setattr(graph, "CHECKS", _fake_checks({n: [] for n in names}))
    g = graph._build_graph()
    final = await g.ainvoke({"session": object(), "findings": [], "ran": [], "errors": []})
    assert set(final["ran"]) == set(names) and len(final["ran"]) == 8


async def test_one_of_eight_raising_kills_nothing(monkeypatch):
    survivor = Finding(check="h", resource_id="r", severity="med", summary="s")
    behaviors = {n: [] for n in list("abcdefgh")}
    behaviors["c"] = RuntimeError("c exploded")
    behaviors["h"] = [survivor]
    monkeypatch.setattr(graph, "CHECKS", _fake_checks(behaviors))
    g = graph._build_graph()
    final = await g.ainvoke({"session": object(), "findings": [], "ran": [], "errors": []})
    assert len(final["ran"]) == 8
    assert final["findings"] == [survivor]
    assert [e["check"] for e in final["errors"]] == ["c"]


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
