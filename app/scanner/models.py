"""The finding shape shared across the scanner (phase 18a).

A ``Finding`` is one thing the scanner noticed about the AWS account: which check
raised it, the resource it points at, how bad it is, a one-sentence plain summary, and
a small free-form ``detail`` dict with the specifics (the open port, the key's age, the
bucket's grantee). Frozen and hashable-by-fields so tests can compare them directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The three severities, most to least urgent. ``high`` is what the alert path watches.
SEVERITY_HIGH = "high"
SEVERITY_MED = "med"
SEVERITY_LOW = "low"

# Check names (the ``check`` field), one per check function.
# Phase 18a security checks.
CHECK_S3_PUBLIC = "s3_public"
CHECK_SG_OPEN = "sg_open"
CHECK_UNENCRYPTED = "unencrypted"
CHECK_IAM_RISK = "iam_risk"
# Phase 18c cost-waste checks. These carry ``est_monthly_usd`` (float, nullable) in detail.
CHECK_EBS_WASTE = "ebs_waste"
CHECK_EIP_WASTE = "eip_waste"
CHECK_SNAPSHOT_WASTE = "snapshot_waste"
CHECK_IDLE_INSTANCES = "idle_instances"
# Phase 18b: not a check but a finding kind — recorded when slice cannot assume a user's
# role, so the failure is visible in their findings rather than silently swallowed.
CHECK_CONNECTION = "connection"


@dataclass(frozen=True)
class Finding:
    check: str
    resource_id: str
    severity: str
    summary: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "check": self.check,
            "resource_id": self.resource_id,
            "severity": self.severity,
            "summary": self.summary,
            "detail": dict(self.detail),
        }
