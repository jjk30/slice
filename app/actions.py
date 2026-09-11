"""Human in the loop approvals for MCP writes (phase 32).

An agent talking to slice over MCP never writes a rule itself. It proposes a change
(``POST /actions/propose``), which lands as a ``pending_actions`` row and one email to
the account owner with an Approve link and a Reject link. Only the approval applies the
write, through the very same functions the direct ``/admin/rules`` endpoints use
(``app.admin.apply_add_rule`` / ``apply_delete_rule``), after the very same validation
(``app.admin.validate_rule_fields`` / ``validate_rule_id``).

The token in the link is the whole authorization for the decision: 32 random bytes,
shown once in the email, stored only as a SHA-256, compared in constant time, and good
for one decision inside ten minutes. Fail closed everywhere: an invalid payload creates
no row; an email that cannot be sent expires the row on the spot (no email means no
pending action); a decision on a row that is not pending changes nothing; an approval
whose write fails reopens the row so the person can click again before it expires.

Routes:

- ``POST /actions/propose``            slice key auth. ``{kind, payload}`` in, ``{action_id, status, expires_at}`` out.
- ``GET  /actions/{id}/approve?t=``    no session, the token is the auth. Applies the write.
- ``GET  /actions/{id}/reject?t=``     no session, the token is the auth. Applies nothing.
- ``GET  /actions/{id}``               slice key auth, same account only. Status and the applied rule id.

Every propose and every decision logs one structured line (action id, account, kind,
decision, via). The token never appears in a log line, a response body, or a row.
"""

from __future__ import annotations

import hashlib
import hmac
import html as html_lib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import config
from app.admin import (
    RuleExistsError,
    RuleWriteError,
    apply_add_rule,
    apply_delete_rule,
    find_duplicate_rule,
    validate_rule_fields,
    validate_rule_id,
)
from app.alerts.channels import FOOTER_AI_SETUP, DeliveryResult, ResendEmailChannel, format_time
from app.auth.middleware import read_account

logger = logging.getLogger("slice.gateway")

router = APIRouter(prefix="/actions", tags=["actions"])

KIND_ADD = "add_rule"
KIND_DELETE = "delete_rule"
KINDS = (KIND_ADD, KIND_DELETE)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"

VIA_EMAIL = "email"

# How long a proposal stays open. Short on purpose: the email is read now or never.
TTL = timedelta(minutes=10)
# The link token: 32 random bytes, url-safe. Only its SHA-256 is ever stored.
TOKEN_BYTES = 32

SUBJECT = "slice: approve a change to your routing rules"
HEADING = "An agent wants to change a routing rule"
NOTE = (
    "Each link works once, and both stop working at {expires}. "
    "If you did not expect this, reject it or ignore it."
)
# The plain-text fallback (phase 32b): the same words as the HTML, one line per card row.
EMAIL_TEXT = """\
{heading}

{sentence}

Team: {team}
Route: {route}
Asked by: {asked_by}
Expires: {expires}

Approve change: {approve_url}

Reject: {reject_url}

{note}

{footer}

Sent {sent}"""

# Phase 32b: the HTML email. Table layout and inline styles only, since mail clients
# strip stylesheets; Helvetica/Arial, since Gmail strips web fonts; the cake logo as the
# hosted image the alert emails already use, never base64. Colors are the site palette.
EMAIL_FONT = "Helvetica, Arial, sans-serif"
LOGO_URL = "https://sliceapp.dev/logo.png"
INK, PAPER, PAPER2, EDGE, MUTED, GREEN, DOT = (
    "#1C1815", "#FBF8F1", "#FFFDF8", "#EFE7D7", "#736A5D", "#0F6E56", "#CFC5B2"
)


def _card_row(label: str, value: str, last: bool = False) -> str:
    border = "" if last else f"border-bottom:1px solid {EDGE};"
    return (
        "<tr>"
        f'<td style="padding:12px 16px;{border}font-family:{EMAIL_FONT};font-size:13px;'
        f'color:{MUTED};width:96px;vertical-align:top;">{html_lib.escape(label)}</td>'
        f'<td style="padding:12px 16px;{border}font-family:{EMAIL_FONT};font-size:15px;'
        f'color:{INK};vertical-align:top;">{html_lib.escape(value)}</td>'
        "</tr>"
    )


def _button(href: str, label: str, *, primary: bool) -> str:
    if primary:
        cell = f"border-radius:8px;background:{GREEN};"
        link = f"color:#FFFFFF;background:{GREEN};"
    else:
        cell = f"border-radius:8px;background:#FFFFFF;border:1px solid {DOT};"
        link = f"color:{INK};background:#FFFFFF;"
    return (
        f'<td style="{cell}">'
        f'<a href="{html_lib.escape(href, quote=True)}" style="display:inline-block;padding:12px 22px;'
        f"font-family:{EMAIL_FONT};font-size:15px;font-weight:bold;line-height:1;"
        f'text-decoration:none;border-radius:8px;{link}">{html_lib.escape(label)}</a>'
        "</td>"
    )


def render_email_html(
    *, sentence: str, team: str, route: str, asked_by: str, expires: str,
    approve_url: str, reject_url: str, note: str, footer: str,
) -> str:
    """The approval email as HTML: heading, one sentence, the change card, two buttons, the note."""
    text = f"font-family:{EMAIL_FONT};color:{INK};"
    rows = (
        _card_row("Team", team)
        + _card_row("Route", route)
        + _card_row("Asked by", asked_by)
        + _card_row("Expires", expires, last=True)
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{PAPER};padding:32px 16px;">'
        '<tr><td align="center">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        f'style="max-width:600px;width:100%;background:{PAPER2};border:1px solid {EDGE};border-radius:12px;">'
        f'<tr><td style="padding:28px 32px 0;{text}">'
        f'<img src="{LOGO_URL}" width="40" height="40" alt="slice" '
        'style="width:40px;height:40px;vertical-align:middle;border:0;">'
        '<span style="font-size:18px;font-weight:bold;margin-left:10px;vertical-align:middle;">slice</span>'
        "</td></tr>"
        f'<tr><td style="padding:24px 32px 0;{text}font-size:24px;font-weight:bold;line-height:1.25;">'
        f"{html_lib.escape(HEADING)}</td></tr>"
        f'<tr><td style="padding:12px 32px 0;{text}font-size:16px;line-height:1.5;">'
        f"{html_lib.escape(sentence)}</td></tr>"
        '<tr><td style="padding:24px 32px 0;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border:1px solid {EDGE};border-radius:10px;background:{PAPER};">{rows}</table>'
        "</td></tr>"
        '<tr><td style="padding:28px 32px 0;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f"{_button(approve_url, 'Approve change', primary=True)}"
        '<td style="width:12px;"></td>'
        f"{_button(reject_url, 'Reject', primary=False)}"
        "</tr></table>"
        "</td></tr>"
        f'<tr><td style="padding:24px 32px 32px;font-family:{EMAIL_FONT};font-size:13px;line-height:1.5;color:{MUTED};">'
        f"{html_lib.escape(note)}<br><br>{html_lib.escape(footer)}</td></tr>"
        "</table>"
        "</td></tr></table>"
    )


# --- errors and helpers ----------------------------------------------------------------


def _anthropic_error(status_code: int, error_type: str, message: str) -> JSONResponse:
    """The same Anthropic-shaped error body the /v1 proxy returns (app/main.py)."""
    return JSONResponse(
        status_code=status_code,
        content={"type": "error", "error": {"type": error_type, "message": message}},
    )


def _unauthorized() -> JSONResponse:
    return _anthropic_error(
        401, "authentication_error", "Missing slice key. Send it as 'Authorization: Bearer slk_...'."
    )


def _get_db(request: Request):
    return getattr(request.app.state, "db", None)


def _get_rules(request: Request):
    return getattr(request.app.state, "rules", None)


def _db_ready(db) -> bool:
    return db is not None and getattr(db, "enabled", False)


def mint_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(presented: str | None, stored_hash: str | None) -> bool:
    """Constant-time compare of a presented link token against the stored hash."""
    if not isinstance(presented, str) or not presented or not isinstance(stored_hash, str):
        return False
    return hmac.compare_digest(hash_token(presented), stored_hash)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value) -> datetime | None:
    """A tz-aware datetime from a row value (asyncpg gives aware ones; fakes may not)."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _iso(value) -> str | None:
    aware = _aware(value)
    return aware.isoformat() if aware is not None else None


def is_expired(row: dict, now: datetime | None = None) -> bool:
    expires_at = _aware(row.get("expires_at"))
    return expires_at is None or expires_at <= (now or _now())


def _link(action_id: int, decision: str, token: str) -> str:
    return f"{config.PUBLIC_BASE_URL}/actions/{action_id}/{decision}?t={token}"


def change_sentence(kind: str, payload: dict, rule: dict | None = None) -> str:
    """What will change, in one plain sentence, for the email and the result page."""
    if kind == KIND_ADD:
        return (
            f"slice will add a rule for team {payload['team']} that sends "
            f"{payload['from_model']} requests to {payload['to_model']} instead."
        )
    rule_id = payload.get("rule_id")
    if rule:
        return (
            f"slice will delete rule #{rule_id} for team {rule.get('team')}, which sends "
            f"{rule.get('from_model')} requests to {rule.get('to_model')}."
        )
    return f"slice will delete rule #{rule_id}."


def _route_text(kind: str, payload: dict, rule: dict | None) -> str:
    """The Route row: ``from -> to`` for the rule being added or deleted."""
    if kind == KIND_ADD:
        return f"{payload['from_model']} -> {payload['to_model']}"
    if rule:
        return f"{rule.get('from_model')} -> {rule.get('to_model')} (rule #{payload.get('rule_id')}, to be deleted)"
    return f"rule #{payload.get('rule_id')} (to be deleted)"


ASKED_BY_DEFAULT = "An agent using your slice key"


async def _asked_by(db, account) -> str:
    """The Asked by row: the key that made the request, the way the dashboard's key card
    shows it (its display prefix, plus the device name the key was minted with, as
    ``slk_live_ab12... on JJs-Macbook-Pro``). With auth off there is no key behind the
    request, so the plain line stays."""
    key_id = getattr(account, "key_id", None)
    if key_id is None or account.id is None or not _db_ready(db):
        return ASKED_BY_DEFAULT
    try:
        rows = await db.list_keys(account.id)
    except Exception:  # noqa: BLE001  # a name is a courtesy, never a blocker.
        return ASKED_BY_DEFAULT
    for row in rows:
        if row.get("id") == key_id:
            prefix = row.get("key_prefix") or ASKED_BY_DEFAULT
            name = (row.get("name") or "").strip()
            return f"{prefix} on {name}" if name else prefix
    return ASKED_BY_DEFAULT


def _log(event: str, row: dict, **extra) -> None:
    logger.info(
        json.dumps(
            {
                "event": event,
                "action_id": row.get("id"),
                "account_id": row.get("account_id"),
                "kind": row.get("kind"),
                **extra,
            }
        )
    )


# --- email -------------------------------------------------------------------------


async def send_email(*, to: str, subject: str, text: str, html: str | None = None) -> DeliveryResult:
    """One email through the existing Resend channel. Never raises. Tests replace this."""
    channel = ResendEmailChannel(
        api_key=config.RESEND_API_KEY or "", sender=config.ALERT_FROM, to=None
    )
    return await channel.send_email(to=to, subject=subject, text=text, html=html)


async def _recipient(db, account) -> str | None:
    """Where the approval email goes: the account's saved profile email, read fresh from
    the database so a just-changed address is honoured; the operator's ALERT_EMAIL_TO
    for the local single-tenant account (no row to read). None means nobody to ask."""
    if account.id is not None and _db_ready(db):
        try:
            row = await db.get_account(account.id)
        except Exception:  # noqa: BLE001  # an unreadable account is nobody to ask.
            row = None
        email = (row or {}).get("email")
        if isinstance(email, str) and email.strip():
            return email.strip()
        return None
    if account.id is None and config.ALERT_EMAIL_TO:
        return config.ALERT_EMAIL_TO.split(",")[0].strip() or None
    return None


# --- result pages ------------------------------------------------------------------
# A centered card in the site's own fonts and colors, with the cake favicon. One icon
# (a green check, a red cross, or a muted mark), a title, one sentence, one quiet note.
# No emoji, no script, nothing to click.

FAVICON_URL = "https://sliceapp.dev/favicon.png"
_FONTS_URL = (
    "https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,600"
    "&family=Inter:wght@400;500&display=swap"
)

_ICONS = {
    "check": (
        GREEN,
        '<path d="M9 18.5 15 24.5 27 12" fill="none" stroke="#FFFFFF" stroke-width="3.2" '
        'stroke-linecap="round" stroke-linejoin="round"/>',
    ),
    "cross": (
        "#C24A44",
        '<path d="M12 12 24 24 M24 12 12 24" fill="none" stroke="#FFFFFF" stroke-width="3.2" '
        'stroke-linecap="round"/>',
    ),
    "mark": (
        "#A79C8B",
        '<path d="M11 18 25 18" fill="none" stroke="#FFFFFF" stroke-width="3.2" stroke-linecap="round"/>',
    ),
}

_PAGE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title}</title>
<link rel="icon" type="image/png" href="{favicon}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="{fonts}" rel="stylesheet">
<style>
  :root{{--ink:#1C1815;--paper:#FBF8F1;--paper2:#FFFDF8;--edge:#EFE7D7;--muted:#736A5D;
        --green:#0F6E56;--red:#C24A44;
        --display:'Bricolage Grotesque',sans-serif;--body:'Inter',sans-serif}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:
      radial-gradient(120% 90% at 82% 4%, #FFFDF8 0%, rgba(255,253,248,0) 52%),
      radial-gradient(90% 70% at 6% 102%, #F3ECDE 0%, rgba(243,236,222,0) 58%),
      var(--paper);color:var(--ink);font-family:var(--body);line-height:1.6;
      -webkit-font-smoothing:antialiased;min-height:100vh;display:flex;align-items:center;
      justify-content:center;padding:24px}}
  .card{{max-width:460px;width:100%;background:var(--paper2);border:1px solid var(--edge);
        border-radius:16px;padding:40px 36px;text-align:center}}
  .brand{{font-family:var(--display);font-weight:600;font-size:15px;color:var(--muted);margin-bottom:24px}}
  .icon{{width:56px;height:56px;border-radius:50%;margin:0 auto 20px;display:block}}
  h1{{font-family:var(--display);font-weight:600;font-size:28px;line-height:1.15;margin-bottom:12px;color:var(--ink)}}
  p{{font-size:16px;color:var(--ink);margin-bottom:10px}}
  p.note{{color:var(--muted);font-size:14px;margin-bottom:0}}
</style>
</head>
<body>
<main class="card">
  <div class="brand">slice</div>
  <svg class="icon" viewBox="0 0 36 36" aria-hidden="true"><circle cx="18" cy="18" r="18" fill="{icon_fill}"/>{icon_path}</svg>
  <h1>{title}</h1>
  <p>{message}</p>
  <p class="note">{note}</p>
</main>
</body>
</html>
"""


def _page(status: int, title: str, message: str, note: str = "", icon: str = "mark") -> HTMLResponse:
    fill, path = _ICONS.get(icon, _ICONS["mark"])
    body = _PAGE.format(
        title=html_lib.escape(title),
        message=html_lib.escape(message),
        note=html_lib.escape(note),
        icon_fill=fill,
        icon_path=path,
        favicon=FAVICON_URL,
        fonts=_FONTS_URL,
    )
    return HTMLResponse(status_code=status, content=body)


def _page_not_found() -> HTMLResponse:
    return _page(
        404, "Nothing here", "There is no pending change with that id.",
        "Open the link from the email slice sent you.", icon="cross",
    )


def _page_bad_token() -> HTMLResponse:
    return _page(
        403, "This link is not valid",
        "It does not match the change it points at. Nothing was changed.",
        "Open the link from the email slice sent you, without editing it.", icon="cross",
    )


def _page_expired() -> HTMLResponse:
    return _page(
        410, "This link has expired",
        "Approval links stop working ten minutes after the agent asks, and this one did. "
        "Nothing was changed.",
        "If you still want the change, ask the agent to propose it again.",
    )


def _page_already(row: dict) -> HTMLResponse:
    status = row.get("status")
    return _page(
        409, "This link was already used",
        f"This change was already {status}. Each link works once, so nothing else happened.",
        "You can close this tab.",
    )


def _page_unavailable() -> HTMLResponse:
    return _page(
        503, "slice could not do that right now",
        "The change is still pending. Try the link again in a moment, before it expires.",
        icon="cross",
    )


# --- routes ------------------------------------------------------------------------


@router.post("/propose")
async def propose(request: Request):
    """Record a proposed write and email the owner an approve and a reject link.

    Validation runs first and with the same rules the direct endpoints use: a bad payload
    creates nothing. The row is created, then the email is sent; if the send fails the
    row is expired on the spot and the caller gets a 502, so no pending action ever
    exists that nobody was asked about.
    """
    account = read_account(request)
    if account is None:
        return _unauthorized()

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _anthropic_error(400, "invalid_request_error", "Request body is not valid JSON.")
    if not isinstance(body, dict):
        return _anthropic_error(400, "invalid_request_error", "Request body must be a JSON object.")

    kind = body.get("kind")
    if kind not in KINDS:
        return _anthropic_error(
            400, "invalid_request_error", f"'kind' must be one of: {', '.join(KINDS)}."
        )
    raw_payload = body.get("payload")
    if not isinstance(raw_payload, dict):
        return _anthropic_error(400, "invalid_request_error", "'payload' must be a JSON object.")

    rules = _get_rules(request)
    rule = None
    if kind == KIND_ADD:
        payload, message = validate_rule_fields(raw_payload)
        if payload is None:
            return _anthropic_error(400, "invalid_request_error", message)
    else:
        rule_id, message = validate_rule_id(raw_payload.get("rule_id"))
        if rule_id is None:
            return _anthropic_error(400, "invalid_request_error", message)
        payload = {"rule_id": rule_id}
        # The rule must exist and be the caller's, the same answer DELETE /admin/rules
        # would give, so nobody is emailed about a rule that is not there.
        if rules is not None:
            owned = [r for r in await rules.all(account.id) if r.id == rule_id]
            if not owned:
                return _anthropic_error(404, "not_found_error", f"No rule with id {rule_id}.")
            found = owned[0]
            rule = {"team": found.team, "from_model": found.from_model, "to_model": found.to_model}

    db = _get_db(request)
    if not _db_ready(db):
        return _anthropic_error(
            503, "api_error", "Action storage is unavailable (database not connected)."
        )

    if kind == KIND_ADD:
        # Phase 32c: a rule the account already has is refused here, before any row or
        # email, with the same answer the direct endpoint gives.
        try:
            existing = await find_duplicate_rule(db, account.id, payload)
        except RuleWriteError as exc:
            return _anthropic_error(exc.status, "api_error", exc.message)
        if existing is not None:
            return _anthropic_error(409, "invalid_request_error", RuleExistsError(existing).message)

    to = await _recipient(db, account)
    if to is None:
        return _anthropic_error(
            400, "invalid_request_error",
            "This account has no email to send the approval to. Set one in Settings first.",
        )

    token = mint_token()
    expires_at = _now() + TTL
    try:
        row = await db.create_pending_action(account.id, kind, payload, hash_token(token), expires_at)
    except Exception:  # noqa: BLE001
        return _anthropic_error(503, "api_error", "Could not store the action.")

    action_id = row["id"]
    _log("action_proposed", row, decision=None, via=None)

    expires_text = format_time(expires_at)
    fields = {
        "sentence": change_sentence(kind, payload, rule),
        "team": payload["team"] if kind == KIND_ADD else (rule or {}).get("team") or "unknown",
        "route": _route_text(kind, payload, rule),
        "asked_by": await _asked_by(db, account),
        "expires": expires_text,
        "approve_url": _link(action_id, "approve", token),
        "reject_url": _link(action_id, "reject", token),
        "note": NOTE.format(expires=expires_text),
        "footer": FOOTER_AI_SETUP,
    }
    del token  # never needed again in this process; the hash is what the row holds.
    text = EMAIL_TEXT.format(heading=HEADING, sent=format_time(_now()), **fields)
    html = render_email_html(**fields)

    try:
        result = await send_email(to=to, subject=SUBJECT, text=text, html=html)
    except Exception as exc:  # noqa: BLE001  # the channel promises not to raise; belt and braces.
        result = DeliveryResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    if not result.ok:
        # Fail closed: nobody was asked, so nothing may stay pending.
        try:
            await db.set_pending_action_status(
                action_id, from_status=STATUS_PENDING, to_status=STATUS_EXPIRED, decided_via=VIA_EMAIL
            )
        except Exception:  # noqa: BLE001  # the 502 below is the truth either way.
            pass
        _log("action_decided", row, decision=STATUS_EXPIRED, via="email_send_failed", error=result.error)
        return _anthropic_error(
            502, "api_error", "The approval email could not be sent, so the action was not created."
        )

    return JSONResponse(
        status_code=201,
        content={"action_id": action_id, "status": STATUS_PENDING, "expires_at": _iso(expires_at)},
    )


async def _load_for_decision(request: Request, action_id: int, token: str | None):
    """The pending row a decision may act on, or the page that says why not.

    Order matters: an unknown id is a 404 before the token is looked at; a wrong token is
    refused before the status is disclosed; a decided row is "already decided"; a row
    past its expiry is expired right here (one use, one window) and refused.
    """
    db = _get_db(request)
    if not _db_ready(db):
        return None, _page_unavailable()
    try:
        row = await db.get_pending_action(action_id)
    except Exception:  # noqa: BLE001
        return None, _page_unavailable()
    if row is None:
        return None, _page_not_found()
    if not token_matches(token, row.get("token_hash")):
        return None, _page_bad_token()
    if row.get("status") != STATUS_PENDING:
        return None, _page_already(row)
    if is_expired(row):
        try:
            await db.set_pending_action_status(
                action_id, from_status=STATUS_PENDING, to_status=STATUS_EXPIRED, decided_via=VIA_EMAIL
            )
        except Exception:  # noqa: BLE001  # the refusal stands either way.
            pass
        _log("action_decided", row, decision=STATUS_EXPIRED, via=VIA_EMAIL)
        return None, _page_expired()
    return row, None


@router.get("/{action_id}/approve", response_class=HTMLResponse)
async def approve(request: Request, action_id: int, t: str | None = None):
    """Apply the proposed write, once. The token in ``t`` is the whole authorization."""
    row, refusal = await _load_for_decision(request, action_id, t)
    if refusal is not None:
        return refusal

    db = _get_db(request)
    # Claim the row first (pending to approved, atomically) so two clicks can never both
    # apply; the write follows, and a failed write hands the row back as pending.
    try:
        claimed = await db.set_pending_action_status(
            action_id, from_status=STATUS_PENDING, to_status=STATUS_APPROVED, decided_via=VIA_EMAIL
        )
    except Exception:  # noqa: BLE001
        return _page_unavailable()
    if claimed is None:
        latest = await db.get_pending_action(action_id)
        return _page_already(latest or {**row, "status": "decided"})

    kind, payload = row["kind"], row.get("payload") or {}
    try:
        if kind == KIND_ADD:
            written = await apply_add_rule(db, _get_rules(request), row.get("account_id"), payload)
            applied_id = written.get("id")
        else:
            applied_id = await apply_delete_rule(
                db, _get_rules(request), row.get("account_id"), int(payload["rule_id"])
            )
    except RuleExistsError as exc:
        # The rule turned up between the proposal and the click (a direct write, or a
        # second approval of the same change): the end state is what the person asked
        # for, so the approval stands and points at the rule that already exists.
        existing_id = exc.rule.get("id")
        try:
            await db.set_pending_action_status(
                action_id, from_status=STATUS_APPROVED, to_status=STATUS_APPROVED,
                decided_via=VIA_EMAIL, applied_rule_id=existing_id,
            )
        except Exception:  # noqa: BLE001
            pass
        _log("action_decided", row, decision=STATUS_APPROVED, via=VIA_EMAIL, applied_rule_id=existing_id, note="rule_already_exists")
        return _page(
            200, "Nothing to add", "This rule already exists, so nothing changed.",
            "You can close this tab.",
        )
    except RuleWriteError as exc:
        if exc.status == 404:
            # The rule is already gone: nothing to apply, but the approval stands as
            # decided (the person said yes, and the end state is what they asked for).
            _log("action_decided", row, decision=STATUS_APPROVED, via=VIA_EMAIL, note="rule_already_gone")
            return _page(
                200, "Change approved", "That rule was already gone, so there was nothing left to delete.",
                "You can close this tab.", icon="check",
            )
        try:
            await db.set_pending_action_status(
                action_id, from_status=STATUS_APPROVED, to_status=STATUS_PENDING
            )
        except Exception:  # noqa: BLE001
            pass
        _log("action_decided", row, decision="apply_failed", via=VIA_EMAIL, error=exc.message)
        return _page_unavailable()

    try:
        await db.set_pending_action_status(
            action_id, from_status=STATUS_APPROVED, to_status=STATUS_APPROVED,
            decided_via=VIA_EMAIL, applied_rule_id=applied_id,
        )
    except Exception:  # noqa: BLE001  # the write landed; the id is bookkeeping.
        pass
    _log("action_decided", row, decision=STATUS_APPROVED, via=VIA_EMAIL, applied_rule_id=applied_id)

    if kind == KIND_ADD:
        message = (
            f"Rule #{applied_id} is live: team {payload.get('team')} now sends "
            f"{payload.get('from_model')} requests to {payload.get('to_model')}."
        )
    else:
        message = f"Rule #{applied_id} was deleted."
    return _page(200, "Change approved", message, "You can close this tab.", icon="check")


@router.get("/{action_id}/reject", response_class=HTMLResponse)
async def reject(request: Request, action_id: int, t: str | None = None):
    """Close the proposal without applying anything, once."""
    row, refusal = await _load_for_decision(request, action_id, t)
    if refusal is not None:
        return refusal

    db = _get_db(request)
    try:
        claimed = await db.set_pending_action_status(
            action_id, from_status=STATUS_PENDING, to_status=STATUS_REJECTED, decided_via=VIA_EMAIL
        )
    except Exception:  # noqa: BLE001
        return _page_unavailable()
    if claimed is None:
        latest = await db.get_pending_action(action_id)
        return _page_already(latest or {**row, "status": "decided"})

    _log("action_decided", row, decision=STATUS_REJECTED, via=VIA_EMAIL)
    return _page(
        200, "Change rejected", "Nothing was changed. The agent will be told the change was rejected.",
        "You can close this tab.", icon="cross",
    )


@router.get("/{action_id}")
async def status(request: Request, action_id: int):
    """The caller's own action: its status and, once approved, the rule id it applied."""
    account = read_account(request)
    if account is None:
        return _unauthorized()
    db = _get_db(request)
    if not _db_ready(db):
        return _anthropic_error(
            503, "api_error", "Action storage is unavailable (database not connected)."
        )
    try:
        row = await db.get_pending_action(action_id)
    except Exception:  # noqa: BLE001
        return _anthropic_error(503, "api_error", "Could not read the action.")
    # Another account's action is a 404, not a 403: its existence is not the caller's business.
    if row is None or row.get("account_id") != account.id:
        return _anthropic_error(404, "not_found_error", f"No action with id {action_id}.")

    status_value = row.get("status")
    if status_value == STATUS_PENDING and is_expired(row):
        # Lazily expire on read so a stale pending row never reads as still open.
        try:
            updated = await db.set_pending_action_status(
                action_id, from_status=STATUS_PENDING, to_status=STATUS_EXPIRED, decided_via=VIA_EMAIL
            )
        except Exception:  # noqa: BLE001
            updated = None
        if updated is not None:
            row = updated
            _log("action_decided", row, decision=STATUS_EXPIRED, via=VIA_EMAIL)
        status_value = row.get("status")

    return {
        "action_id": row.get("id"),
        "kind": row.get("kind"),
        "status": status_value,
        "applied_rule_id": row.get("applied_rule_id"),
        "expires_at": _iso(row.get("expires_at")),
        "decided_at": _iso(row.get("decided_at")),
        "decided_via": row.get("decided_via"),
    }
