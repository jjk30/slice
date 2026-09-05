"""The AWS scanner's checks: four security (phase 18a) and four cost-waste (phase 18c).

slice is a cost gateway, so the scanner catches money being wasted as well as security
risk. The cost-waste checks flag unattached EBS volumes and gp2-that-should-be-gp3
(``ebs_waste``), unassociated Elastic IPs (``eip_waste``), stale snapshots
(``snapshot_waste``), and idle EC2 instances (``idle_instances``). Each cost finding
carries an ``est_monthly_usd`` (float, nullable) in its ``detail`` and takes its severity
from that estimate (see ``severity_for_waste``).

Each check is a small, self-contained function taking a boto3 session (anything with a
``.client(name)`` method, a real ``boto3.Session`` in production, a fake wrapping
``botocore.stub.Stubber`` clients in tests) and returning a list of ``Finding``. A check
builds only the clients it needs and lists only the resources it inspects, so all eight
are independent and run in parallel on the same supervisor graph.

Scope: the scanner is **single-region**: every client is built in the session's region
(``AWS_REGION``). Resources in other regions are not seen. Multi-region scanning (iterating
``ec2:DescribeRegions``) is future work; the cost estimates here are therefore per-region.
All prices are us-east-1 on-demand list prices, coarse by design: the point is to rank
waste, not to reconcile a bill.

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
from datetime import datetime, timedelta, timezone

from app import config
from app.scanner.models import (
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
    denied it is skipped silently: the per-bucket findings still stand.
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415  # lazy: no SDK at import.

    findings: list[Finding] = []
    s3 = session.client("s3")

    # Account-level Block Public Access (best effort, skipped on any denial).
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
            # NoSuchBucketPolicy simply means there is no policy, not public.
            if _error_code(exc) != "NoSuchBucketPolicy":
                _debug(CHECK_S3_PUBLIC, f"policy_status:{name}", exc)

    return findings


def _account_public_access_block(session, ClientError) -> list[Finding]:
    """One HIGH finding when the account's S3 Block Public Access is missing or partial.

    Needs the account id (sts) and s3control. Any denial or unexpected error skips the
    probe entirely (returns nothing): the per-bucket findings are the real signal, and
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
    except Exception as exc:  # noqa: BLE001  # no credentials, s3control absent, etc.: skip.
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

    Our own box's group is flagged like any other if it matches: honesty over vanity.
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


# ===========================================================================
# Cost-waste checks (phase 18c). All prices are us-east-1 on-demand list prices,
# single-region (see the module docstring). Coarse on purpose: they rank waste.
# ===========================================================================

# EBS $/GB-month by volume type. Unknown types get no estimate (None), never a wrong one.
EBS_GB_MONTH = {"gp3": 0.08, "gp2": 0.10, "io1": 0.125, "io2": 0.125, "standard": 0.05}
# Moving a gp2 volume to gp3 saves ~20% of its per-GB cost (0.10 -> 0.08).
GP2_TO_GP3_SAVING_PER_GB = EBS_GB_MONTH["gp2"] - EBS_GB_MONTH["gp3"]  # 0.02

# An unassociated Elastic IP is billed hourly; ~$3.60/mo (0.005/h * 730).
EIP_MONTHLY_USD = 3.60

# Snapshots are billed at ~$0.05/GB-month, but they are *incremental*: a snapshot only
# stores blocks changed since the previous one, so VolumeSize is a true upper bound. The
# summaries say so. Snapshots older than this many days are flagged.
SNAPSHOT_GB_MONTH = 0.05
SNAPSHOT_AGE_DAYS = 90
# Above this many stale snapshots, collapse to one grouped finding instead of spamming.
SNAPSHOT_GROUP_THRESHOLD = 10

# Idle = average CPU below this over the trailing window.
IDLE_CPU_PERCENT = 5.0
IDLE_WINDOW_DAYS = 7
_HOURS_PER_MONTH = 730

# On-demand $/hour for common instance types. Unknown types get no estimate (None) but are
# still flagged when idle: we just can't price the waste.
INSTANCE_HOURLY = {
    "t2.micro": 0.0116, "t2.small": 0.023, "t2.medium": 0.0464, "t2.large": 0.0928,
    "t3.micro": 0.0104, "t3.small": 0.0208, "t3.medium": 0.0416, "t3.large": 0.0832,
    "t4g.micro": 0.0084, "t4g.small": 0.0168, "t4g.medium": 0.0336, "t4g.large": 0.0672,
    "m5.large": 0.096, "m6i.large": 0.096, "m6g.large": 0.077, "c5.large": 0.085,
}


def severity_for_waste(est: float | None) -> str:
    """Severity from a monthly-waste estimate: high >= $10, med >= $1, else low (None -> low)."""
    if est is not None and est >= 10:
        return SEVERITY_HIGH
    if est is not None and est >= 1:
        return SEVERITY_MED
    return SEVERITY_LOW


def _money(est: float | None) -> float | None:
    return None if est is None else round(est, 2)


def _instance_monthly(instance_type: str | None) -> float | None:
    hourly = INSTANCE_HOURLY.get(instance_type)
    return None if hourly is None else round(hourly * _HOURS_PER_MONTH, 2)


# --- e. EBS waste: unattached volumes and gp2-that-should-be-gp3 -------------


def check_ebs_waste(session) -> list[Finding]:
    """Unattached EBS volumes (full monthly cost wasted) and attached gp2 (switch to gp3)."""
    from botocore.exceptions import ClientError  # noqa: PLC0415

    findings: list[Finding] = []
    ec2 = session.client("ec2")
    try:
        volumes = ec2.describe_volumes().get("Volumes", []) or []
    except ClientError as exc:
        _debug(CHECK_EBS_WASTE, "describe_volumes", exc)
        return findings

    for volume in volumes:
        vid = volume.get("VolumeId") or "?"
        vtype = volume.get("VolumeType")
        size = volume.get("Size") or 0
        state = volume.get("State")

        if state == "available":
            # Unattached: the whole volume is paid for and used by nothing.
            rate = EBS_GB_MONTH.get(vtype)
            est = _money(rate * size) if rate is not None else None
            cost_txt = f"~${est:.2f}/mo" if est is not None else "an unknown amount/mo"
            findings.append(
                Finding(
                    check=CHECK_EBS_WASTE,
                    resource_id=vid,
                    severity=severity_for_waste(est),
                    summary=f"EBS volume {vid} ({size} GiB {vtype}) is unattached, wasting {cost_txt}.",
                    detail={
                        "kind": "unattached",
                        "volume_type": vtype,
                        "size_gib": size,
                        "est_monthly_usd": est,
                    },
                )
            )
        elif vtype == "gp2":
            # Attached gp2: gp3 is cheaper (and faster). Suggestion, capped at med severity.
            est = _money(GP2_TO_GP3_SAVING_PER_GB * size)
            sev = severity_for_waste(est)
            if sev == SEVERITY_HIGH:
                sev = SEVERITY_MED
            findings.append(
                Finding(
                    check=CHECK_EBS_WASTE,
                    resource_id=vid,
                    severity=sev,
                    summary=(
                        f"EBS volume {vid} ({size} GiB) is gp2; switching to gp3 saves "
                        f"~${est:.2f}/mo."
                    ),
                    detail={
                        "kind": "gp2_to_gp3",
                        "volume_type": vtype,
                        "size_gib": size,
                        "est_monthly_usd": est,
                    },
                )
            )
    return findings


# --- f. EIP waste: Elastic IPs associated with nothing ----------------------


def check_eip_waste(session) -> list[Finding]:
    """Elastic IPs with no AssociationId, billed hourly while doing nothing."""
    from botocore.exceptions import ClientError  # noqa: PLC0415

    findings: list[Finding] = []
    ec2 = session.client("ec2")
    try:
        addresses = ec2.describe_addresses().get("Addresses", []) or []
    except ClientError as exc:
        _debug(CHECK_EIP_WASTE, "describe_addresses", exc)
        return findings

    for addr in addresses:
        if addr.get("AssociationId"):
            continue
        public_ip = addr.get("PublicIp")
        alloc = addr.get("AllocationId") or public_ip or "?"
        est = _money(EIP_MONTHLY_USD)
        findings.append(
            Finding(
                check=CHECK_EIP_WASTE,
                resource_id=alloc,
                severity=severity_for_waste(est),
                summary=(
                    f"Elastic IP {public_ip} ({alloc}) is not associated with anything, "
                    f"wasting ~${est:.2f}/mo."
                ),
                detail={"kind": "unassociated_eip", "public_ip": public_ip, "est_monthly_usd": est},
            )
        )
    return findings


# --- g. Snapshot waste: stale self-owned snapshots --------------------------


def check_snapshot_waste(session) -> list[Finding]:
    """Self-owned EBS snapshots older than 90 days.

    The estimate uses VolumeSize at $0.05/GB-mo, an *upper bound*, snapshots are
    incremental, so the real cost is usually far less; the summaries say so. Above
    SNAPSHOT_GROUP_THRESHOLD stale snapshots, one grouped finding stands in for the lot to
    avoid finding-spam.
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415

    findings: list[Finding] = []
    ec2 = session.client("ec2")
    now = datetime.now(timezone.utc)
    try:
        snapshots = ec2.describe_snapshots(OwnerIds=["self"]).get("Snapshots", []) or []
    except ClientError as exc:
        _debug(CHECK_SNAPSHOT_WASTE, "describe_snapshots", exc)
        return findings

    stale = []
    for snap in snapshots:
        age = _age_days(snap.get("StartTime"), now)
        if age is not None and age > SNAPSHOT_AGE_DAYS:
            stale.append((snap, age))

    if not stale:
        return findings

    if len(stale) > SNAPSHOT_GROUP_THRESHOLD:
        total_size = sum((s.get("VolumeSize") or 0) for s, _ in stale)
        est = _money(total_size * SNAPSHOT_GB_MONTH)
        return [
            Finding(
                check=CHECK_SNAPSHOT_WASTE,
                resource_id="account",
                severity=severity_for_waste(est),
                summary=(
                    f"{len(stale)} EBS snapshots are older than {SNAPSHOT_AGE_DAYS} days "
                    f"(~{total_size} GiB total). Deleting unneeded ones could save up to "
                    f"~${est:.2f}/mo (upper bound; snapshots are incremental)."
                ),
                detail={
                    "kind": "grouped",
                    "count": len(stale),
                    "total_size_gib": total_size,
                    "est_monthly_usd": est,
                },
            )
        ]

    for snap, age in stale:
        sid = snap.get("SnapshotId") or "?"
        size = snap.get("VolumeSize") or 0
        est = _money(size * SNAPSHOT_GB_MONTH)
        findings.append(
            Finding(
                check=CHECK_SNAPSHOT_WASTE,
                resource_id=sid,
                severity=severity_for_waste(est),
                summary=(
                    f"EBS snapshot {sid} is {age} days old (~{size} GiB); up to ~${est:.2f}/mo "
                    f"(upper bound; snapshots are incremental)."
                ),
                detail={
                    "kind": "snapshot",
                    "age_days": age,
                    "volume_size_gib": size,
                    "est_monthly_usd": est,
                },
            )
        )
    return findings


# --- h. Idle instances: running EC2 with near-zero CPU ----------------------


def check_idle_instances(session) -> list[Finding]:
    """Running EC2 instances averaging under 5% CPU over the last 7 days.

    The slice box's own instance is scanned like any other (honesty over vanity), its
    CPU won't be idle anyway, so it is not special-cased. A type absent from the price map
    is still flagged idle; its waste estimate is just null.
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415

    findings: list[Finding] = []
    ec2 = session.client("ec2")
    try:
        reservations = ec2.describe_instances(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
        ).get("Reservations", []) or []
    except ClientError as exc:
        _debug(CHECK_IDLE_INSTANCES, "describe_instances", exc)
        return findings

    cw = session.client("cloudwatch")
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=IDLE_WINDOW_DAYS)

    for reservation in reservations:
        for inst in reservation.get("Instances", []) or []:
            iid = inst.get("InstanceId") or "?"
            itype = inst.get("InstanceType")
            try:
                stats = cw.get_metric_statistics(
                    Namespace="AWS/EC2",
                    MetricName="CPUUtilization",
                    Dimensions=[{"Name": "InstanceId", "Value": iid}],
                    StartTime=start,
                    EndTime=now,
                    Period=86400,
                    Statistics=["Average"],
                )
            except ClientError as exc:
                _debug(CHECK_IDLE_INSTANCES, f"metrics:{iid}", exc)
                continue

            points = [p.get("Average") for p in stats.get("Datapoints", []) or [] if p.get("Average") is not None]
            if not points:
                continue  # no data to judge on: say nothing rather than guess.
            avg = sum(points) / len(points)
            if avg >= IDLE_CPU_PERCENT:
                continue

            est = _instance_monthly(itype)
            cost_txt = f"~${est:.2f}/mo on-demand" if est is not None else "cost unknown for this type"
            findings.append(
                Finding(
                    check=CHECK_IDLE_INSTANCES,
                    resource_id=iid,
                    severity=severity_for_waste(est),
                    summary=(
                        f"EC2 instance {iid} ({itype}) averaged {avg:.1f}% CPU over "
                        f"{IDLE_WINDOW_DAYS} days (idle); {cost_txt}."
                    ),
                    detail={
                        "kind": "idle_instance",
                        "instance_type": itype,
                        "avg_cpu_percent": round(avg, 2),
                        "est_monthly_usd": est,
                    },
                )
            )
    return findings


# The registry the supervisor graph fans out over: (check name, function). Eight nodes:
# the four phase-18a security checks, then the four phase-18c cost-waste checks.
CHECKS = (
    (CHECK_S3_PUBLIC, check_s3_public),
    (CHECK_SG_OPEN, check_sg_open),
    (CHECK_UNENCRYPTED, check_unencrypted),
    (CHECK_IAM_RISK, check_iam_risk),
    (CHECK_EBS_WASTE, check_ebs_waste),
    (CHECK_EIP_WASTE, check_eip_waste),
    (CHECK_SNAPSHOT_WASTE, check_snapshot_waste),
    (CHECK_IDLE_INSTANCES, check_idle_instances),
)
