"""The boto3 session factory (phase 18a/b), kept apart so boto3 stays a lazy import.

``make_session`` is the only place the scanner constructs AWS clients. It imports boto3
*inside* the function, so importing the scanner package (which ``app.main`` does at
startup to register the router and the daily task) never pulls boto3 in: the heavy SDK
loads only when a scan actually runs.

Two modes:

- **Own account** (``make_session()``): the box's own instance-role credentials, as in
  part A, used only for slice's operator account scanning slice's own infrastructure.
- **Assumed role** (``make_session(role_arn, external_id)``, phase 18b): sts:AssumeRole
  into a *user's* account, always with their External ID (confused-deputy protection),
  short-lived (15-minute) credentials. Raises on failure (expired trust, deleted role,
  wrong external id) so the caller can mark the connection as errored: it never returns
  the box's own session as a fallback, which would scan the wrong account.
"""

from __future__ import annotations

from app import config

# Cost Explorer is a global service but its endpoint lives in us-east-1; the client must
# be built there regardless of the session's default region.
COST_EXPLORER_REGION = "us-east-1"


def _region_kwargs() -> dict:
    return {"region_name": config.AWS_REGION} if config.AWS_REGION else {}


def make_session(role_arn: str | None = None, external_id: str | None = None, *, sts_client=None):
    """A boto3 Session. Lazy import.

    With no ``role_arn`` it is the box's own session (part A). With one, it assumes that
    role, always passing ``external_id`` when given, and builds a session from the
    temporary credentials. ``sts_client`` is a seam for tests (a stubbed STS client);
    production builds its own from the box's credentials. Assume-role failures propagate.
    """
    import boto3  # noqa: PLC0415  # lazy on purpose: never import boto3 at module load.

    if not role_arn:
        return boto3.session.Session(**_region_kwargs())

    sts = sts_client if sts_client is not None else boto3.client("sts", **_region_kwargs())
    params = {
        "RoleArn": role_arn,
        "RoleSessionName": config.SCANNER_ASSUME_ROLE_SESSION_NAME,
        "DurationSeconds": config.SCANNER_ASSUME_ROLE_DURATION_SECONDS,
    }
    if external_id:
        params["ExternalId"] = external_id
    creds = sts.assume_role(**params)["Credentials"]
    return boto3.session.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        **_region_kwargs(),
    )


def test_role(role_arn: str, external_id: str, *, sts_client=None) -> tuple[bool, str]:
    """Live-verify a connection: assume the role and make one cheap read. Never raises.

    Returns ``(True, account_id)`` on success (the assumed identity's account), or
    ``(False, message)`` on any failure, a clear, human-readable reason the connect
    endpoint returns to the user. This is the only place the connect flow touches AWS.
    """
    try:
        session = make_session(role_arn, external_id, sts_client=sts_client)
        identity = session.client("sts", **_region_kwargs()).get_caller_identity()
        return True, str(identity.get("Account", ""))
    except Exception as exc:  # noqa: BLE001  # every failure is a clear message, never a crash.
        return False, _clean_error(exc)


def _clean_error(exc: Exception) -> str:
    """A short, user-facing reason from a boto/STS error (its message, not a traceback)."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        err = response.get("Error", {}) or {}
        code, message = err.get("Code"), err.get("Message")
        if code or message:
            return f"{code}: {message}" if code and message else (message or code)
    return f"{type(exc).__name__}: {exc}"
