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
CHECK_S3_PUBLIC = "s3_public"
CHECK_SG_OPEN = "sg_open"
CHECK_UNENCRYPTED = "unencrypted"
CHECK_IAM_RISK = "iam_risk"


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
