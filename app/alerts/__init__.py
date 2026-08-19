"""Phase 11 alerts: tell someone when a team crosses its budget warn line or its cap.

Two moments already detected by the Redis layer feed this package: the once-per-month
warn latch in ``redis_layer.add_cost`` (kind ``warn``) and the blocked decision in
``redis_layer.check_budget`` (kind ``block``). Both call ``fire``, which does
``asyncio.create_task(send_alert(...))`` and returns — nothing here is ever awaited
by a request, and every failure inside is caught and logged.

``engine`` holds the cooldown (a Redis key per team per kind, fail open when Redis
is down), fans out to channels, and records one ``alerts`` row per attempt.
``channels`` holds the channel interface and the one implementation for now, email
via Resend; Slack and WhatsApp plug in there without touching the engine.

``ALERTS_ENABLED`` (default: on only when ``RESEND_API_KEY`` is set) is the whole
switch: off means ``fire`` returns before creating a task.
"""

from __future__ import annotations

from app.alerts.channels import (
    KIND_BLOCK,
    KIND_WARN,
    Alert,
    AlertChannel,
    DeliveryResult,
    ResendEmailChannel,
    build_default_channels,
)
from app.alerts.engine import (
    AlertEngine,
    build_engine,
    configure,
    cooldown_key,
    drain,
    fire,
    get_engine,
    send_alert,
)
from app.alerts.whatsapp import TwilioWhatsAppChannel, send_whatsapp_message

__all__ = [
    "KIND_BLOCK",
    "KIND_WARN",
    "Alert",
    "AlertChannel",
    "AlertEngine",
    "DeliveryResult",
    "ResendEmailChannel",
    "TwilioWhatsAppChannel",
    "build_default_channels",
    "build_engine",
    "configure",
    "cooldown_key",
    "drain",
    "fire",
    "get_engine",
    "send_alert",
    "send_whatsapp_message",
]
