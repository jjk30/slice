"""The four AWS security checks (phase 18a).

Each check is a small, self-contained function taking a boto3 session (anything with a
``.client(name)`` method — a real ``boto3.Session`` in production, a fake wrapping
``botocore.stub.Stubber`` clients in tests) and returning a list of ``Finding``. A check
builds only the clients it needs and lists only the resources it inspects, so the four
are independent and can run in parallel.

Fail-open, at two levels:

- The graph node that calls a check catches anything the check itself raises (see
  ``app.scanner.graph``), so a completely broken check drops zero findings, never the run.
- Inside a check, every *per-resource* call is wrapped too, so one unreadable bucket or
  one denied describe doesn't cost the findings from every other resource. A denied
  permission yields no finding rather than a false "clean".

``botocore`` is imported lazily inside the functions (only ``ClientError`` is needed, and
only when a scan runs) so this module import never pulls the AWS SDK in.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app import config
from app.scanner.models import (
    CHECK_IAM_RISK,
    CHECK_S3_PUBLIC,
    CHECK_SG_OPEN,
    CHECK_UNENCRYPTED,
    SEVERITY_HIGH,
    SEVERITY_MED,
    Finding,
)

logger = logging.getLogger("slice.gateway")

# Ports where 0.0.0.0/0 is almost never intentional: SSH, RDP, Postgres, Redis, MySQL.
SENSITIVE_PORTS = (22, 3389, 5432, 6379, 3306)
_PORT_NAMES = {22: "SSH", 3389: "RDP", 5432: "PostgreSQL", 6379: "Redis", 3306: "MySQL"}

# The public-ACL grantee URIs S3 uses for "anyone" and "any AWS account".
_PUBLIC_ACL_URIS = {
    "http://acs.amazonaws.com/groups/global/AllUsers": "public (AllUsers)",
    "http://acs.amazonaws.com/groups/global/AuthenticatedUsers": "any AWS account (AuthenticatedUsers)",
}


def _error_code(exc) -> str:
    """The AWS error code from a ClientError (e.g. 'AccessDenied'), or '' for anything else."""
    return (getattr(exc, "response", {}) or {}).get("Error", {}).get("Code", "")


def _debug(check: str, resource: str, exc: Exception) -> None:
    logger.debug(
        json.dumps(
            {"event": "scanner_skip", "check": check, "resource": resource, "error": str(exc)}
        )
    )


# --- a. S3 public exposure --------------------------------------------------


def check_s3_public(session) -> list[Finding]:
    """Buckets exposed by a public ACL or a public policy, plus account-level BPA gaps.

    Per bucket: a public ACL grant (AllUsers / AuthenticatedUsers) and a policy whose
    status is ``IsPublic`` each raise a HIGH finding. Separately, the account-level S3
    Block Public Access setting is checked: if it is missing or not fully on, that is one
    HIGH finding for the account (a single toggle that would otherwise let a future bucket
    go public). The account-level probe needs the account id and s3control; if either is
    denied it is skipped silently — the per-bucket findings still stand.
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415 — lazy: no SDK at import.

    findings: list[Finding] = []
    s3 = session.client("s3")

    # Account-level Block Public Access (best effort — skipped on any denial).
    findings.extend(_account_public_access_block(session, ClientError))

    try:
        buckets = s3.list_buckets().get("Buckets", []) or []
    except ClientError as exc:
        _debug(CHECK_S3_PUBLIC, "list_buckets", exc)
        return findings

    for bucket in buckets:
        name = bucket.get("Name")
        if not name:
            continue

        # Public ACL grants.
        try:
            acl = s3.get_bucket_acl(Bucket=name)
            for grant in acl.get("Grants", []) or []:
                uri = (grant.get("Grantee") or {}).get("URI")
                label = _PUBLIC_ACL_URIS.get(uri)
                if label is not None:
                    findings.append(
                        Finding(
                            check=CHECK_S3_PUBLIC,
                            resource_id=name,
                            severity=SEVERITY_HIGH,
                            summary=f"S3 bucket {name} grants {label} access via its ACL.",
                            detail={
                                "kind": "public_acl",
                                "grantee": uri,
                                "permission": grant.get("Permission"),
                            },
                        )
                    )
        except ClientError as exc:
            _debug(CHECK_S3_PUBLIC, f"acl:{name}", exc)

        # Public bucket policy.
        try:
            status = s3.get_bucket_policy_status(Bucket=name)
            if (status.get("PolicyStatus") or {}).get("IsPublic"):
                findings.append(
                    Finding(
                        check=CHECK_S3_PUBLIC,
                        resource_id=name,
                        severity=SEVERITY_HIGH,
                        summary=f"S3 bucket {name} has a bucket policy that makes it public.",
                        detail={"kind": "public_policy"},
                    )
                )
        except ClientError as exc:
            # NoSuchBucketPolicy simply means there is no policy — not public.
            if _error_code(exc) != "NoSuchBucketPolicy":
                _debug(CHECK_S3_PUBLIC, f"policy_status:{name}", exc)

    return findings


def _account_public_access_block(session, ClientError) -> list[Finding]:
    """One HIGH finding when the account's S3 Block Public Access is missing or partial.

    Needs the account id (sts) and s3control. Any denial or unexpected error skips the
    probe entirely (returns nothing) — the per-bucket findings are the real signal, and
    honesty over a false clean means we say nothing here rather than assert "fine".
    """
    try:
        account_id = session.client("sts").get_caller_identity().get("Account")
        if not account_id:
            return []
        cfg = session.client("s3control").get_public_access_block(AccountId=account_id)
        block = cfg.get("PublicAccessBlockConfiguration", {}) or {}
    except ClientError as exc:
        if _error_code(exc) == "NoSuchPublicAccessBlockConfiguration":
            return [
                Finding(
                    check=CHECK_S3_PUBLIC,
                    resource_id="account",
                    severity=SEVERITY_HIGH,
                    summary="Account-level S3 Block Public Access is not configured.",
                    detail={"kind": "account_public_access_block", "configured": False},
                )
            ]
        _debug(CHECK_S3_PUBLIC, "account_public_access_block", exc)
        return []
    except Exception as exc:  # noqa: BLE001 — no credentials, s3control absent, etc.: skip.
        _debug(CHECK_S3_PUBLIC, "account_public_access_block", exc)
        return []

    flags = (
        "BlockPublicAcls",
        "IgnorePublicAcls",
        "BlockPublicPolicy",
        "RestrictPublicBuckets",
    )
    off = [f for f in flags if not block.get(f)]
    if off:
        return [
            Finding(
                check=CHECK_S3_PUBLIC,
                resource_id="account",
                severity=SEVERITY_HIGH,
                summary="Account-level S3 Block Public Access is not fully enabled.",
                detail={"kind": "account_public_access_block", "disabled_flags": off},
            )
        ]
    return []


# --- b. Security groups open to the world on sensitive ports ----------------


def check_sg_open(session) -> list[Finding]:
    """Security groups allowing 0.0.0.0/0 (or ::/0) to a sensitive port.

    Our own box's group is flagged like any other if it matches — honesty over vanity.
    A rule with protocol ``-1`` (all traffic) opens every sensitive port at once.
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415

    findings: list[Finding] = []
    ec2 = session.client("ec2")
    try:
        groups = ec2.describe_security_groups().get("SecurityGroups", []) or []
    except ClientError as exc:
        _debug(CHECK_SG_OPEN, "describe_security_groups", exc)
        return findings

    for group in groups:
        gid = group.get("GroupId") or "?"
        gname = group.get("GroupName")
        for perm in group.get("IpPermissions", []) or []:
            open_v4 = any(r.get("CidrIp") == "0.0.0.0/0" for r in perm.get("IpRanges", []) or [])
            open_v6 = any(r.get("CidrIpv6") == "::/0" for r in perm.get("Ipv6Ranges", []) or [])
            if not (open_v4 or open_v6):
                continue

            proto = perm.get("IpProtocol")
            if proto == "-1":
                hit = list(SENSITIVE_PORTS)
            else:
                frm, to = perm.get("FromPort"), perm.get("ToPort")
                if frm is None or to is None:
                    continue
                hit = [p for p in SENSITIVE_PORTS if frm <= p <= to]

            for port in hit:
                cidr = "0.0.0.0/0" if open_v4 else "::/0"
                svc = _PORT_NAMES.get(port, str(port))
                findings.append(
                    Finding(
                        check=CHECK_SG_OPEN,
                        resource_id=gid,
                        severity=SEVERITY_HIGH,
                        summary=(
                            f"Security group {gid} allows the world ({cidr}) to reach "
                            f"{svc} (port {port})."
                        ),
                        detail={
                            "group_name": gname,
                            "port": port,
                            "protocol": proto,
                            "cidr": cidr,
                            "service": svc,
                        },
                    )
                )
    return findings


# --- c. Unencrypted storage -------------------------------------------------


def check_unencrypted(session) -> list[Finding]:
    """S3 buckets without default encryption and EBS volumes that are not encrypted."""
    from botocore.exceptions import ClientError  # noqa: PLC0415

    findings: list[Finding] = []
    s3 = session.client("s3")

    try:
        buckets = s3.list_buckets().get("Buckets", []) or []
    except ClientError as exc:
        _debug(CHECK_UNENCRYPTED, "list_buckets", exc)
        buckets = []

    for bucket in buckets:
        name = bucket.get("Name")
        if not name:
            continue
        try:
            s3.get_bucket_encryption(Bucket=name)
        except ClientError as exc:
            if _error_code(exc) == "ServerSideEncryptionConfigurationNotFoundError":
                findings.append(
                    Finding(
                        check=CHECK_UNENCRYPTED,
                        resource_id=name,
                        severity=SEVERITY_MED,
                        summary=f"S3 bucket {name} has no default encryption configured.",
                        detail={"resource_type": "s3_bucket"},
                    )
                )
            else:
                _debug(CHECK_UNENCRYPTED, f"encryption:{name}", exc)

    ec2 = session.client("ec2")
    try:
        volumes = ec2.describe_volumes().get("Volumes", []) or []
    except ClientError as exc:
        _debug(CHECK_UNENCRYPTED, "describe_volumes", exc)
        volumes = []

    for volume in volumes:
        if not volume.get("Encrypted"):
            vid = volume.get("VolumeId") or "?"
            findings.append(
                Finding(
                    check=CHECK_UNENCRYPTED,
                    resource_id=vid,
                    severity=SEVERITY_MED,
                    summary=f"EBS volume {vid} is not encrypted at rest.",
                    detail={
                        "resource_type": "ebs_volume",
                        "size_gib": volume.get("Size"),
                        "state": volume.get("State"),
                    },
                )
            )
    return findings


# --- d. IAM risk: old keys, direct AdministratorAccess ----------------------

_ADMIN_POLICY_ARN = "arn:aws:iam::aws:policy/AdministratorAccess"


def check_iam_risk(session) -> list[Finding]:
    """Access keys older than the configured age, and users with AdministratorAccess attached directly."""
    from botocore.exceptions import ClientError  # noqa: PLC0415

    findings: list[Finding] = []
    iam = session.client("iam")
    now = datetime.now(timezone.utc)
    max_age = config.SCANNER_IAM_KEY_MAX_AGE_DAYS

    try:
        users = iam.list_users().get("Users", []) or []
    except ClientError as exc:
        _debug(CHECK_IAM_RISK, "list_users", exc)
        return findings

    for user in users:
        name = user.get("UserName")
        if not name:
            continue

        # Old access keys.
        try:
            keys = iam.list_access_keys(UserName=name).get("AccessKeyMetadata", []) or []
        except ClientError as exc:
            _debug(CHECK_IAM_RISK, f"list_access_keys:{name}", exc)
            keys = []
        for key in keys:
            created = key.get("CreateDate")
            key_id = key.get("AccessKeyId") or "?"
            age_days = _age_days(created, now)
            if age_days is not None and age_days > max_age:
                detail = {"user": name, "age_days": age_days, "status": key.get("Status")}
                try:
                    last = iam.get_access_key_last_used(AccessKeyId=key_id)
                    used = (last.get("AccessKeyLastUsed") or {}).get("LastUsedDate")
                    detail["last_used"] = used.isoformat() if hasattr(used, "isoformat") else used
                except ClientError as exc:
                    _debug(CHECK_IAM_RISK, f"last_used:{key_id}", exc)
                findings.append(
                    Finding(
                        check=CHECK_IAM_RISK,
                        resource_id=key_id,
                        severity=SEVERITY_MED,
                        summary=(
                            f"IAM access key {key_id} for user {name} is {age_days} days old "
                            f"(over {max_age})."
                        ),
                        detail=detail,
                    )
                )

        # AdministratorAccess attached directly to the user.
        try:
            attached = iam.list_attached_user_policies(UserName=name).get(
                "AttachedPolicies", []
            ) or []
        except ClientError as exc:
            _debug(CHECK_IAM_RISK, f"list_attached_user_policies:{name}", exc)
            attached = []
        for policy in attached:
            if (
                policy.get("PolicyArn") == _ADMIN_POLICY_ARN
                or policy.get("PolicyName") == "AdministratorAccess"
            ):
                findings.append(
                    Finding(
                        check=CHECK_IAM_RISK,
                        resource_id=name,
                        severity=SEVERITY_HIGH,
                        summary=f"IAM user {name} has AdministratorAccess attached directly.",
                        detail={"policy": policy.get("PolicyName"), "arn": policy.get("PolicyArn")},
                    )
                )
    return findings


def _age_days(created, now: datetime) -> int | None:
    """Whole days between ``created`` and ``now``, or None when ``created`` is unusable."""
    if not hasattr(created, "tzinfo"):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (now - created).days


# The registry the supervisor graph fans out over: (check name, function).
CHECKS = (
    (CHECK_S3_PUBLIC, check_s3_public),
    (CHECK_SG_OPEN, check_sg_open),
    (CHECK_UNENCRYPTED, check_unencrypted),
    (CHECK_IAM_RISK, check_iam_risk),
)
