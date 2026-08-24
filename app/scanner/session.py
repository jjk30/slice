"""The boto3 session factory (phase 18a), kept apart so boto3 stays a lazy import.

``make_session`` is the only place the scanner constructs AWS clients. It imports boto3
*inside* the function, so importing the scanner package (which ``app.main`` does at
startup to register the router and the daily task) never pulls boto3 in — the heavy SDK
loads only when a scan actually runs. On the box the session picks up the instance
role's credentials automatically; locally, with none, the checks simply find nothing.
"""

from __future__ import annotations

from app import config

# Cost Explorer is a global service but its endpoint lives in us-east-1; the client must
# be built there regardless of the session's default region.
COST_EXPLORER_REGION = "us-east-1"


def make_session():
    """A boto3 Session, region from config (or boto3's own resolution chain). Lazy import."""
    import boto3  # noqa: PLC0415 — lazy on purpose: never import boto3 at module load.

    kwargs = {}
    if config.AWS_REGION:
        kwargs["region_name"] = config.AWS_REGION
    return boto3.session.Session(**kwargs)
