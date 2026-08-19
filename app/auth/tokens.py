"""Dashboard-session JWTs (phase 12): mint at login, verify on ``/auth/me``.

HS256 over ``JWT_SECRET`` from the environment. The subject is the account id; the
token carries an issuer tag and an expiry (``JWT_TTL_SECONDS``). Verification is
closed by construction: no secret configured, a bad signature, a tampered payload, the
wrong issuer, or an expired token all come back as None. Nothing here touches the
database — the caller turns the account id into an account.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt

from app import config

ALGORITHM = "HS256"
ISSUER = "slice"


def mint_jwt(
    account_id: int,
    *,
    secret: str | None = None,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> str | None:
    """A signed session token for ``account_id``, or None when no secret is configured.

    None (rather than an unsigned token or a random secret) is the closed choice: the
    login still hands out a slice key, the dashboard bridge works on the key, and JWTs
    simply do not exist until ``JWT_SECRET`` is set.
    """
    secret = secret if secret is not None else config.JWT_SECRET
    if not secret:
        return None
    ttl = ttl_seconds if ttl_seconds is not None else config.JWT_TTL_SECONDS
    issued = now or datetime.now(timezone.utc)
    claims = {
        "sub": str(int(account_id)),
        "iss": ISSUER,
        "iat": issued,
        "exp": issued + timedelta(seconds=max(1, int(ttl))),
    }
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def verify_jwt(token: str | None, *, secret: str | None = None) -> int | None:
    """The account id a valid token was minted for, or None for anything else.

    "Anything else" is every failure mode: no secret configured, an expired token, a
    forged or tampered token, a token signed with a different secret or algorithm, the
    wrong issuer, or a subject that is not an integer. Never raises.
    """
    secret = secret if secret is not None else config.JWT_SECRET
    if not secret or not isinstance(token, str) or not token:
        return None
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["exp", "sub", "iss"]},
        )
    except jwt.PyJWTError:
        return None
    except Exception:  # noqa: BLE001 — a malformed token is a rejected token, never a 500.
        return None
    try:
        return int(claims.get("sub"))
    except (TypeError, ValueError):
        return None
