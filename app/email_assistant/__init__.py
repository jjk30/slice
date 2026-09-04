"""Phase 23b: the reply-by-email assistant.

A user replies to a slice alert email with a question. Resend delivers the inbound mail
as an ``email.received`` webhook to ``POST /email/inbound`` (``routes``): the raw body is
Svix-signature-checked, the 202 goes back at once, and everything else runs in a detached
task (``service``): a loop guard, sender-to-account matching, fetching the body from
Resend's receiving API, a per-account daily reply limit (phase 26), the email-channel
guardrails (the topic rail in ``guardrails/prompts.yml``, NeMo prompting mode "email",
which since phase 26 sorts the question into own_data / general / blocked), a plain-text
context built from the same read paths the dashboard and MCP tools use (``context``, for
own-data questions only), one model call (the context model for own data, the general
model with a disclaimer first for general advice), the output rail, and a threaded reply
through the existing Resend channel.

Every step fails CLOSED. A bad signature is a 401; an unknown sender gets nothing (and
never learns whether an address exists); a blocked or errored rail gets the fixed line
"Sorry, I can't help with that here."; a failed model call sends nothing. Nothing here
ever writes to AWS, changes a rule or a cap, or stores the body or the answer.

Only the router is re-exported here; ``app.main`` imports ``service`` by module.
"""

from __future__ import annotations

from app.email_assistant.routes import router

__all__ = ["router"]
