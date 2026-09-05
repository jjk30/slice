"""``POST /email/inbound`` (phase 23b): Resend's inbound-email webhook. Public, no slice key.

The route does only what must happen before Resend's timeout: it verifies the Svix
signature on the raw body (401 and stop on anything else), drops every event that is not
``email.received`` (200), skips an ``email_id`` already recorded (200, a Resend retry),
then answers 202 and hands the event to a detached task, exactly the shape the scanner's
``/scanner/run`` uses. With ``EMAIL_ASSISTANT_ENABLED`` off the route answers 200 and
does nothing at all, signature or not.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config
from app.email_assistant import service
from app.email_assistant.signature import verify_signature

logger = logging.getLogger("slice.gateway")

router = APIRouter(prefix="/email", tags=["email"])


def _status(code: int, status: str) -> JSONResponse:
    return JSONResponse(status_code=code, content={"status": status})


def get_assistant(app) -> "service.EmailAssistant | None":
    """The assistant lifespan built (or a test installed); None means it is off."""
    return getattr(app.state, "email_assistant", None)


@router.post("/inbound")
async def inbound(request: Request):
    if not config.EMAIL_ASSISTANT_ENABLED:
        return _status(200, "disabled")

    body = await request.body()
    # a. The signature, on the raw bytes, before anything is parsed.
    if not verify_signature(request.headers, body, config.RESEND_WEBHOOK_SECRET):
        logger.warning(json.dumps({"event": "email_assistant", "step": "signature", "verdict": "rejected"}))
        return JSONResponse(status_code=401, content={"error": {"message": "Invalid webhook signature."}})

    try:
        payload = json.loads(body)
    except ValueError:
        logger.warning(json.dumps({"event": "email_assistant", "step": "parse", "verdict": "ignored", "reason": "not_json"}))
        return _status(200, "ignored")

    # b. Only inbound mail. Every other Resend event (sent, delivered, bounced...) is a 200 no-op.
    event_type = payload.get("type") if isinstance(payload, dict) else None
    if event_type != service.EVENT_RECEIVED:
        logger.info(json.dumps({"event": "email_assistant", "step": "event_type", "verdict": "ignored", "type": event_type}))
        return _status(200, "ignored")

    event = service.parse_event(payload)
    if event is None:
        logger.warning(json.dumps({"event": "email_assistant", "step": "parse", "verdict": "ignored", "reason": "malformed_event"}))
        return _status(200, "ignored")

    # Resend retries on a non-2xx or a timeout; an email_id already on file is done with.
    db = getattr(request.app.state, "db", None)
    if db is not None and getattr(db, "enabled", False):
        try:
            if await db.email_reply_seen(event.email_id):
                logger.info(json.dumps({"event": "email_assistant", "step": "dedupe", "email_id": event.email_id, "verdict": "ignored", "reason": "duplicate_email_id"}))
                return _status(200, "duplicate")
        except Exception as exc:  # noqa: BLE001  # the claim inside the task settles it either way.
            logger.warning(json.dumps({"event": "email_assistant", "step": "dedupe", "email_id": event.email_id, "verdict": None, "reason": f"lookup_failed: {exc}"}))

    assistant = get_assistant(request.app)
    if assistant is None:
        logger.warning(json.dumps({"event": "email_assistant", "step": "accept", "email_id": event.email_id, "verdict": "error", "reason": "assistant_unavailable"}))
        return JSONResponse(status_code=503, content={"error": {"message": "Email assistant is unavailable."}})

    # c. 202 now; everything else in a detached task so Resend never waits on a model call.
    if service.spawn(assistant.handle(event)) is None:
        return JSONResponse(status_code=503, content={"error": {"message": "Email assistant is unavailable (no running event loop)."}})
    logger.info(json.dumps({"event": "email_assistant", "step": "accept", "email_id": event.email_id, "verdict": None}))
    return _status(202, "accepted")
