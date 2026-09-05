import os
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
PORT = int(os.getenv("PORT", "8080"))

# --- Redis layer (phase 4): cache, budget caps, rate limits. ---
# Unset or unreachable Redis makes every check fail open: the proxy still
# serves traffic, it just stops caching and enforcing caps until Redis returns.
REDIS_URL = os.getenv("REDIS_URL") or "redis://localhost:6379"

# Per-team requests allowed in a rolling 60s window.
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "60"))

# Per-team monthly spend ceiling and the fraction of it that trips a warning.
BUDGET_MONTHLY_USD = Decimal(os.getenv("BUDGET_MONTHLY_USD", "25"))
BUDGET_WARN_RATIO = float(os.getenv("BUDGET_WARN_RATIO", "0.8"))

# How long a cached non-streaming response body stays warm.
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "3600"))

# Unset means request logging is disabled; the proxy still serves traffic.
DATABASE_URL = os.getenv("DATABASE_URL") or None

# Server-side provider keys. Anthropic is the exception: it always uses the
# client's own x-api-key, so it has no server key here. A None here means the
# provider is unconfigured; a request that routes to it gets a clean 401 and
# never leaves the machine.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or None
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or None
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")

# NVIDIA NIM is OpenAI-compatible; only the base URL and key differ.
NIM_API_KEY = os.getenv("NIM_API_KEY") or None
NIM_BASE_URL = os.getenv("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- Router (phase 5): pin ▸ rule ▸ auto, all fail open. ---
# Auto-routing is the only stage a flag can turn off; pins and per-team switch
# rules always apply. With auto off, requests forward as asked unless a rule fires.
AUTO_ROUTE_ENABLED = _bool("AUTO_ROUTE_ENABLED", True)

# Where an "easy" verdict routes to. "hard" always keeps the client's model.
ROUTE_EASY_MODEL = os.getenv("ROUTE_EASY_MODEL", "claude-haiku-4-5-20251001")

# The classifier model, and the ceiling on how long slice waits for its verdict.
# Any failure, timeout, or unexpected output counts as "hard", never an error to
# the client. The judge only ever sees the last user message, truncated.
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "claude-haiku-4-5-20251001")
JUDGE_TIMEOUT_SECONDS = float(os.getenv("JUDGE_TIMEOUT_SECONDS", "3"))
JUDGE_MAX_INPUT_CHARS = int(os.getenv("JUDGE_MAX_INPUT_CHARS", "2000"))

# How often the in-memory switch-rules cache reloads from Postgres. Writes refresh
# it immediately; this is just the background staleness bound.
RULES_REFRESH_SECONDS = float(os.getenv("RULES_REFRESH_SECONDS", "30"))

# --- RAG retrieval (phase 6): semantic hint for the judge. ---
# Retrieval runs only on the auto path (no pin, no rule) and only feeds the judge a
# soft hint, never a hard rule. Off, or a missing index, leaves phase-5 behavior
# untouched. The index is per-team: RAG_INDEX_DIR holds one subdirectory per team
# (rag_store/<team>/), each with its own FAISS index and sidecar, so one team's
# history never feeds another team's hint. Built offline by build_rag_index.py.
RAG_ENABLED = _bool("RAG_ENABLED", True)
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "5"))
RAG_INDEX_DIR = os.getenv("RAG_INDEX_DIR", "rag_store")

# Prompt text is only logged when this is on: prompts can be sensitive, so storing
# them for the offline index build is opt-in. Off means prompt_text is never stored
# and (until it is turned on and an index is built) retrieval simply finds nothing.
RAG_STORE_PROMPTS = _bool("RAG_STORE_PROMPTS", False)

# The local sentence-transformers model used for embeddings, both at build time and
# at query time. Configurable so the model name isn't hardcoded; the two must match.
RAG_EMBED_MODEL = os.getenv("RAG_EMBED_MODEL", "all-MiniLM-L6-v2")

# Seconds the startup RAG warm-up may take before it is abandoned. The heavy embedding
# model (torch + sentence-transformers) is loaded once at startup, in a daemon thread
# bounded by this timeout, so a missing, broken, or pathologically slow dependency
# disables RAG (retrieval returns nothing) instead of hanging or crashing the server.
# A generous default: a healthy first load (which may fetch model weights) fits inside
# it, while a genuine hang is cut short so startup always completes.
RAG_WARM_TIMEOUT_SECONDS = float(os.getenv("RAG_WARM_TIMEOUT_SECONDS", "60"))

# --- Agent loop (phase 7): try cheap, check, escalate, stop at a ceiling. ---
# The loop extends the auto path only: it fires when auto-routing sent a request
# down to a cheaper model, the request is non-streaming, and this flag is on. Pin,
# rule, cache-hit, and gate-reject paths never loop. Every stage fails open.
AGENT_ENABLED = _bool("AGENT_ENABLED", True)

# The escalation ladder, cheap to strong, spanning providers. The loop's first try
# is always the routed cheap model; escalation walks up this ladder from the rung
# above it (or straight to the final rung if the cheap model isn't listed). The
# client's originally requested model is always the final rung, appended if absent,
# the last resort is giving them exactly what they asked for. No model names are
# hardcoded outside this default; every rung must be priceable for its cost estimate
# to be finite (an unknown price makes the estimate infinite and blocks the rung).
AGENT_LADDER = os.getenv(
    "AGENT_LADDER",
    "claude-haiku-4-5-20251001,gemini-3.6-flash,gpt-5.2,claude-sonnet-5",
)

# Hard cap on total attempts (the first try plus escalations) for one request.
AGENT_MAX_ATTEMPTS = int(os.getenv("AGENT_MAX_ATTEMPTS", "3"))

# The spend ceiling for one request, across every attempt and every checker call.
# Before each escalation the loop computes an upper-bound estimate of the next
# attempt; if spend-so-far plus that estimate would cross this, the attempt is not
# made and the best answer already in hand is served. The ceiling is never crossed.
AGENT_COST_CEILING_USD = Decimal(os.getenv("AGENT_COST_CEILING_USD", "0.25"))

# The model the checker uses to judge pass/fail on an answer. Defaults to the same
# small judge model the router uses. Its cost counts toward the ceiling.
AGENT_CHECK_MODEL = os.getenv("AGENT_CHECK_MODEL", JUDGE_MODEL)

# Output-token count assumed for the cost estimate when a request omits max_tokens.
AGENT_DEFAULT_MAX_TOKENS = int(os.getenv("AGENT_DEFAULT_MAX_TOKENS", "1024"))

# --- Evaluation (phase 8): score a sample of routed-down answers, fire-and-forget. ---
# When a request was routed down to a cheaper model (or the agent loop passed on a
# cheap rung), a fraction of those responses are scored with RAGAS in a detached
# background task, never on the request path, never awaited, every failure swallowed.
# The score is written to the eval_scores table and surfaced at /admin/eval/summary.
#
# EVAL_SAMPLE_RATE is the whole on/off switch: 0 disables evaluation entirely (no
# sampling, no scorer built at startup, ragas never imported), and must not break
# boot. 1 scores every qualifying request; anything between is the sampled fraction.
EVAL_SAMPLE_RATE = float(os.getenv("EVAL_SAMPLE_RATE", "0.05"))

# A score at or above this passes; below fails. Both go in the row's `passed` column,
# and the pass rate is what /admin/eval/summary reports.
EVAL_PASS_THRESHOLD = float(os.getenv("EVAL_PASS_THRESHOLD", "0.7"))

# The model RAGAS uses as its judge, wired through langchain-anthropic. Defaults to the
# same small model the router judge uses. Its cost is external (billed by the provider
# on the judge call), not counted against the team budget: evaluation is out-of-band.
EVAL_JUDGE_MODEL = os.getenv("EVAL_JUDGE_MODEL", JUDGE_MODEL)

# Ceiling on how long a single RAGAS metric call may run before it is abandoned. A
# timeout is swallowed like any other failure: the score is simply not recorded.
EVAL_TIMEOUT_SECONDS = float(os.getenv("EVAL_TIMEOUT_SECONDS", "30"))

# --- LangSmith tracing (phase 8): observability for the LangGraph router and loop. ---
# Tracing is entirely env-var driven by LangChain itself. With LANGCHAIN_TRACING_V2
# false (the default) or no LANGCHAIN_API_KEY, LangChain no-ops and slice runs exactly
# as before: no code path here requires LangSmith. When tracing is on, runs are
# grouped under LANGCHAIN_PROJECT (default "slice"); configure_tracing() sets that
# default at startup without ever touching the key.
LANGCHAIN_TRACING_V2 = _bool("LANGCHAIN_TRACING_V2", False)
LANGCHAIN_API_KEY = os.getenv("LANGCHAIN_API_KEY") or None
LANGCHAIN_PROJECT = os.getenv("LANGCHAIN_PROJECT", "slice")

# --- Guardrails (phase 9): NeMo self-check rails around the agent loop only. ---
# Two rails, a self-check on the incoming prompt and a self-check on the assembled
# final answer, wrap the phase-7 agent loop and ONLY the agent loop. Plain proxy
# traffic and the router path never touch them; they run exactly where the loop runs
# (the non-streaming auto-routed-down path). Everything fails open: any error or
# timeout inside the rails engine is logged and the loop proceeds as if the rail
# passed, so a broken rail can never block or crash a request.
#
# GUARDRAILS_ENABLED is the whole kill switch. False means zero rails code runs and
# the loop behaves exactly as phase 7, and because nemoguardrails is imported lazily
# (only when the engine is built, which only happens when this is on), the server
# starts fine even if nemoguardrails is broken or absent while the switch is off.
GUARDRAILS_ENABLED = _bool("GUARDRAILS_ENABLED", True)

# The model the rails LLM uses for its self-checks, wired through langchain-anthropic
# (ChatAnthropic), the same integration the phase-8 eval judge uses. Defaults to the
# router judge model. Its calls are slice's own infrastructure cost, out-of-band from
# the team budget, exactly like the eval judge.
GUARDRAILS_MODEL = os.getenv("GUARDRAILS_MODEL", JUDGE_MODEL)

# Ceiling on how long a single rail check may run before it is abandoned. A timeout is
# swallowed like any other rails failure: fail open, log, the loop continues.
GUARDRAILS_TIMEOUT_SECONDS = float(os.getenv("GUARDRAILS_TIMEOUT_SECONDS", "5"))

# The NeMo Guardrails config directory (config.yml plus the self-check prompts). Only
# the two built-in self-check rails are enabled there, with custom slice-specific
# prompts and no feature that needs embeddings or downloads a model at runtime.
GUARDRAILS_CONFIG_DIR = os.getenv("GUARDRAILS_CONFIG_DIR", "guardrails")

# --- Alerts (phase 11): email when a team crosses its budget warn line or its cap. ---
# The Redis layer already detects both moments (the once-per-month warn latch in
# add_cost, the blocked decision in check_budget); the alerts engine hangs off those
# exact spots, fire-and-forget: a detached task, every failure caught and logged, so
# a slow or broken email provider can never touch a request. One channel for now,
# email via Resend; the channel interface (app.alerts.channels) is where Slack and
# WhatsApp plug in later without the engine changing.
RESEND_API_KEY = os.getenv("RESEND_API_KEY") or None

# WhatsApp via Twilio (phase 13, part 1): a second outbound channel next to email,
# firing off the same cooldown decision. All four are optional; the channel is built
# only when all four are present, otherwise it is silently disabled (a debug line at
# build time), the same "configured means every secret and address is set" rule the
# email channel uses. No twilio SDK: app.alerts.whatsapp posts to the REST endpoint
# over httpx directly.
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID") or None
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN") or None
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM") or None  # e.g. whatsapp:+17372324091
TWILIO_WHATSAPP_TO = os.getenv("TWILIO_WHATSAPP_TO") or None  # e.g. whatsapp:+13128520631

_WHATSAPP_CONFIGURED = all(
    (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO)
)

# The whole switch. Defaults on only when a channel is present: a Resend key or a
# fully-configured Twilio WhatsApp channel. A deployment with neither has no channel
# and stays exactly as phase 10, zero alert code on any path. Set explicitly to
# override either way.
ALERTS_ENABLED = _bool("ALERTS_ENABLED", RESEND_API_KEY is not None or _WHATSAPP_CONFIGURED)

# The sender. Resend's onboarding address works out of the box (to the account
# owner's own email only) with no verified domain; set a verified-domain sender for
# anything real. Recipients are comma-separated; unset means the email channel is not
# configured and, with the switch on, a startup warning says so. Phase 25b: a budget or
# scan alert for a user account goes to that account's saved profile email instead; this
# list gets the operator's own alerts and is the fallback for an account with no email.
ALERT_FROM = os.getenv("ALERT_FROM", "onboarding@resend.dev")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO") or None

# Minimum seconds between two alerts of the same kind for the same account. The latch is
# a Redis key (alert:cooldown:acct:{id}:{kind}; the team label stands in when there is no
# account) with this TTL; a Redis outage fails open to sending, never to crashing.
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "3600"))

# The IANA zone the alert's timestamp is rendered in (e.g. "Aug 18, 2026, 3:20 AM EDT"),
# via the standard-library zoneinfo, no new dependency. An unknown name falls back to
# UTC at format time rather than failing the alert.
ALERT_TIMEZONE = os.getenv("ALERT_TIMEZONE", "America/New_York")

# --- Auth (phase 12): GitHub device-flow login, slice keys, JWT dashboard sessions. ---
# The lock. On (the default), the proxy paths and every /admin and /dashboard path
# require a valid slice key and resolve it to an account, the tenant every counter,
# cache key, log row and read is scoped by. Off is single-tenant LOCAL mode: no key is
# required and everything runs under one local account (the pre-phase-12 behavior, keyed
# by the team header). Off is for local development only, never expose an unlocked
# gateway. Secure by default: unset means on.
AUTH_ENABLED = _bool("AUTH_ENABLED", True)

# The public client id of the GitHub OAuth App slice logs in through. Device flow needs
# no client secret, so this is the only GitHub credential the CLI login needs and it is
# not secret. Unset means login is off: /auth/device/start answers 503 and nobody can
# mint a key.
GITHUB_OAUTH_CLIENT_ID = os.getenv("GITHUB_OAUTH_CLIENT_ID") or None

# Phase 21: the dashboard's browser GitHub sign-in (the authorization-code flow) needs a
# client secret too. Web login is configured only when BOTH GITHUB_OAUTH_CLIENT_ID and
# GITHUB_OAUTH_CLIENT_SECRET are set; the device flow still needs only the id. Secret,
# env only, never logged.
GITHUB_OAUTH_CLIENT_SECRET = os.getenv("GITHUB_OAUTH_CLIENT_SECRET") or None

# The public HTTPS (or http://localhost) origin the browser reaches slice at. The GitHub
# OAuth redirect_uri is PUBLIC_BASE_URL + "/auth/github/callback", so it must match the
# callback URL registered on the OAuth App. Trailing slash trimmed.
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "http://localhost:8080").rstrip("/")

# The name of the httpOnly session cookie the dashboard's web login sets (the JWT rides
# in it). The live SSE stream reads it too, which is why the key never rides in the URL.
SESSION_COOKIE = "slice_session"

# The cookie's Secure flag: on everywhere except a plain http://localhost dev origin,
# where the browser would drop a Secure cookie sent over http.
COOKIE_SECURE = not PUBLIC_BASE_URL.startswith("http://localhost")

# The HMAC secret the dashboard-session JWTs are signed with. Env only, never in code.
# Unset means no JWT is ever minted (login still hands out a slice key; the JWT comes back
# null) and every presented JWT is rejected, closed, not open.
JWT_SECRET = os.getenv("JWT_SECRET") or None

# How long a minted JWT lives. Short-ish on purpose: it is a browser session, not a key.
JWT_TTL_SECONDS = int(os.getenv("JWT_TTL_SECONDS", "28800"))  # 8 hours

# How long a resolved slice key -> account is kept in the in-process cache before the
# next request re-reads Postgres. Same idea as RULES_REFRESH_SECONDS: bounds how long a
# freshly revoked key keeps working, and keeps the proxy off Postgres per request.
AUTH_KEY_CACHE_SECONDS = float(os.getenv("AUTH_KEY_CACHE_SECONDS", "30"))

# Where the `slice` CLI points by default (login, init, and the env lines `slice use`
# prints). Read by the CLI, not the gateway.
SLICE_BASE_URL = os.getenv("SLICE_BASE_URL", "http://localhost:8080")

# --- Dashboard (phase 10): local read endpoints plus a live SSE stream. ---
# Browser origins allowed to call the gateway (CORS), comma-separated. The default is
# the Vite dev server the dashboard runs on; the built dashboard/dist is served by the
# gateway itself and needs no CORS. Since phase 12 every /dashboard/* read needs a slice
# key, but keep this list to the origins you actually serve the dashboard from.


def _csv(name: str, default: str) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        raw = default
    return [item.strip() for item in raw.split(",") if item.strip()]


CORS_ORIGINS = _csv("CORS_ORIGINS", "http://localhost:5173")

# --- AWS security scanner (phase 18a): scan the account slice itself runs in. ---
# All scanner work is fire-and-forget, a detached background task, never on the request
# path, and every boto3 call fails open: a missing credential, a denied permission, or a
# raised check contributes nothing (no finding) rather than crashing the run. boto3 is
# imported lazily (only when a scan actually runs), so the scanner never slows startup.
#
# SCANNER_ENABLED is the whole switch. On (the default) the daily background task runs
# and the /scanner/* endpoints work; off means the daily task never starts and a scan
# only ever runs when explicitly kicked. On the live box the instance role grants the
# read-only permissions the checks need (see infra/ec2); locally, with no credentials,
# every check simply finds nothing.
SCANNER_ENABLED = _bool("SCANNER_ENABLED", True)

# The region the scanner's boto3 session targets. Defaults to us-east-1 so region-needing
# checks work on a box that never set it (an unset value used to fail open silently and
# hide the breakage); the AWS_REGION env var still overrides. Cost Explorer is global and
# is always addressed in us-east-1 regardless.
AWS_REGION = os.getenv("AWS_REGION") or "us-east-1"

# An IAM access key older than this many days is flagged. 90 is the common rotation line.
SCANNER_IAM_KEY_MAX_AGE_DAYS = int(os.getenv("SCANNER_IAM_KEY_MAX_AGE_DAYS", "90"))

# How often the daily background task wakes to check its Redis latch. The latch is keyed
# by calendar day, so a short interval just means "notice the new day sooner"; the scan
# and the (billable) Cost Explorer call still run at most once per day. Also bounds how
# soon after a restart the first scan of the day happens.
SCANNER_DAILY_INTERVAL_SECONDS = int(os.getenv("SCANNER_DAILY_INTERVAL_SECONDS", "3600"))

# How many finding summaries the "new high-risk issues" alert lists in its body.
SCANNER_ALERT_TOP_N = int(os.getenv("SCANNER_ALERT_TOP_N", "5"))

# --- Cross-account scanning (phase 18b): users connect their own AWS account. ---
# A user connects by creating a read-only IAM role that trusts slice's AWS account and
# carries a per-account External ID (confused-deputy protection). The scanner then assumes
# that role to scan THEIR account. Everything stays strictly per-account: every read and
# write is scoped to the authenticated account, and an assumed-role scan that fails is
# marked as an error for that account: it never silently falls back to slice's own data.

# slice's own AWS account id, the Principal a user's trust policy allows to assume their
# role. Baked into the committed CloudFormation template too (users audit that file); this
# is the value the /scanner/connect response echoes for display.
SLICE_AWS_ACCOUNT_ID = os.getenv("SLICE_AWS_ACCOUNT_ID", "194133064379")

# The one slice account that scans slice's *own* infrastructure (part A behavior): no
# connection, no assume-role, findings stored under the NULL/own-account scope. Every other
# account must connect its own AWS account. Default 1 (the first account, the operator). In
# local single-tenant mode (auth off, no account id) the lone tenant is the operator too.
SLICE_OPERATOR_ACCOUNT_ID = int(os.getenv("SLICE_OPERATOR_ACCOUNT_ID", "1"))

# The public HTTPS URL of the committed onboarding template
# (infra/user-onboarding/slice-readonly-role.yaml). The CloudFormation "quick-create"
# console link needs a publicly reachable templateURL, a GitHub raw URL or an S3 object.
# Unset means the quick-create link omits templateURL (the user pastes the template
# manually); set it once the file is hosted. See the phase-18b deploy notes.
SCANNER_TEMPLATE_URL = os.getenv("SCANNER_TEMPLATE_URL") or None

# The region the quick-create console link opens in. IAM is global, so this only picks the
# console's home region; us-east-1 is a safe default.
SCANNER_CONSOLE_REGION = os.getenv("SCANNER_CONSOLE_REGION", "us-east-1")

# The IAM session name and lifetime for an assumed scanner role. 15 minutes is plenty for
# one scan and keeps temporary credentials short-lived.
SCANNER_ASSUME_ROLE_SESSION_NAME = os.getenv("SCANNER_ASSUME_ROLE_SESSION_NAME", "slice-scanner")
SCANNER_ASSUME_ROLE_DURATION_SECONDS = int(os.getenv("SCANNER_ASSUME_ROLE_DURATION_SECONDS", "900"))

# --- Reply-by-email assistant (phase 23b): answer a reply to a slice alert email. ---
# A user replies to an alert email with a question; Resend posts an ``email.received``
# webhook to ``POST /email/inbound``; slice verifies the Svix signature, matches the sender
# to an account by ``accounts.email``, fetches the body, runs the email-channel guardrails,
# answers from that account's own read-only data, and replies in the same thread. Every
# step fails closed: a bad signature is a 401, an unknown sender gets nothing, a blocked
# or failed rail gets the fixed line, and nothing here ever writes to AWS or slice rules.
#
# EMAIL_ASSISTANT_ENABLED is the whole switch. Off (the default) means the route answers
# 200 and does nothing, no engine is built, and no model is ever called.
EMAIL_ASSISTANT_ENABLED = _bool("EMAIL_ASSISTANT_ENABLED", False)

# The Svix signing secret Resend shows for the webhook endpoint (``whsec_<base64>``).
# Unset with the assistant on means every inbound post is rejected with a 401 (fail
# closed) and a warning says why. Env only, never in code.
RESEND_WEBHOOK_SECRET = os.getenv("RESEND_WEBHOOK_SECRET") or None

# The model that writes the reply, wired through langchain-anthropic (ChatAnthropic, the
# same client and ANTHROPIC_API_KEY the guardrails and the eval judge use). Defaults to the
# router judge model. One call per answered email, capped at 300 output tokens; its cost
# is slice's own, out-of-band from the account budget.
EMAIL_ASSISTANT_MODEL = os.getenv("EMAIL_ASSISTANT_MODEL", JUDGE_MODEL)

# Phase 26: the topic rail sorts a question into one of three buckets. A question about
# the sender's own data is answered from that data by EMAIL_ASSISTANT_MODEL, as before. A
# general question about AWS setup, cloud cost, AI models or AI cost is answered by this
# model instead, from its own knowledge, with a disclaimer line first and no account data
# in the prompt at all. Defaults to the Sonnet model already in the pricing table. Same
# 300-token cap, same output rail, same out-of-band cost.
EMAIL_ASSISTANT_GENERAL_MODEL = os.getenv("EMAIL_ASSISTANT_GENERAL_MODEL", "claude-sonnet-5")

# At most this many replies per account per UTC day, counted in Redis (the key carries the
# day, so it resets on its own). The first mail over the line gets one fixed sentence
# saying so; every later one that day gets nothing. A Redis outage fails open to replying,
# the same rule the alert cooldown latch uses.
EMAIL_ASSISTANT_DAILY_LIMIT = int(os.getenv("EMAIL_ASSISTANT_DAILY_LIMIT", "20"))
