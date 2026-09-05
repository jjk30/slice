"""Svix webhook signature verification for Resend's inbound webhook (phase 23b).

Resend signs every webhook post the Svix way. Three headers arrive with the request:
``svix-id``, ``svix-timestamp`` (unix seconds) and ``svix-signature``. The signed content
is ``"{svix-id}.{svix-timestamp}.{raw body}"``; the signature is HMAC-SHA256 of that with
the base64-decoded secret (the endpoint secret is ``whsec_<base64>``), itself base64
encoded. The signature header holds space-separated ``v1,<sig>`` entries (several during a
secret rotation); the post is valid if ANY of them matches. Done by hand with the standard
library, no svix dependency, and every failure path answers False, never raises.

Two guards on top of the HMAC: a timestamp more than ``TOLERANCE_SECONDS`` away from now
is rejected (replay), and the comparison is constant-time (``hmac.compare_digest``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

SECRET_PREFIX = "whsec_"
SIGNATURE_VERSION = "v1"
# How far the svix-timestamp may sit from our clock, either way. Svix's own tolerance.
TOLERANCE_SECONDS = 5 * 60


def _key(secret: str) -> bytes | None:
    raw = secret[len(SECRET_PREFIX):] if secret.startswith(SECRET_PREFIX) else secret
    try:
        return base64.b64decode(raw, validate=True)
    except (ValueError, TypeError):
        return None


def signature_for(secret: str, svix_id: str, timestamp: str | int, body: bytes) -> str | None:
    """The ``v1`` signature value for these inputs, or None when the secret is unusable."""
    key = _key(secret)
    if key is None:
        return None
    signed = f"{svix_id}.{timestamp}.".encode() + body
    return base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()


def verify_signature(headers, body: bytes, secret: str | None, *, now: float | None = None) -> bool:
    """True only when a ``v1`` entry matches and the timestamp is within tolerance.

    ``headers`` is anything with ``.items()`` (Starlette's Headers, or a plain dict);
    names are matched case-insensitively. A missing secret, a missing or malformed
    header, a bad timestamp, or an undecodable secret all answer False.
    """
    if not secret:
        return False
    lowered = {str(name).lower(): value for name, value in headers.items()}
    svix_id = lowered.get("svix-id")
    timestamp = lowered.get("svix-timestamp")
    signature_header = lowered.get("svix-signature")
    if not (svix_id and timestamp and signature_header):
        return False

    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - sent_at) > TOLERANCE_SECONDS:
        return False

    expected = signature_for(secret, svix_id, timestamp, body)
    if expected is None:
        return False
    for entry in signature_header.split():
        version, _, value = entry.partition(",")
        if version == SIGNATURE_VERSION and value and hmac.compare_digest(value, expected):
            return True
    return False
