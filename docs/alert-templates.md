# slice alert templates

Plain-text email templates the alerts engine (`app/alerts/channels.py`) fills at send time.
`{spend}`, `{cap}`, `{left}`, and `{total}` are money-formatted (`$25.00`, `$0.0105`; a
positive amount never shows as `$0.00`). `{left}` is cap minus spend, floored at `$0.00`.
`{month}` is the budget month (`August 2026`). `{time}` is the send time in
`ALERT_TIMEZONE` (default `America/New_York`), e.g. `Aug 18, 2026, 3:20 AM EDT`.

---

## Budget warn — live

Fires once per team per month, the first time spend crosses `BUDGET_WARN_RATIO` (default
80%) of `BUDGET_MONTHLY_USD`. `{percent}` is that ratio.

**Subject:** `slice: {team} has used {percent}% of its monthly AI budget`

```
Team {team} has spent {spend} of its {cap} monthly AI budget. About {left} is left for {month}.

Nothing is blocked yet. This is an early warning. At this pace the team hits its cap before the month ends, and slice will then block its AI requests until the new month or a higher cap.

Ways to still continue your work in a more cost effective way:

1. Keep auto-routing on. slice already sends easy work to cheaper models.

2. If most of this team's work is interactive coding, for example through Claude Code (https://claude.com/product/claude-code), a flat-fee coding tool can beat paying per token. Use these tools as a substitute if you have their subscription:
   - GitHub Copilot: https://github.com/features/copilot
   - Codex: https://openai.com/codex

3. Bulk and repeat jobs, like nightly summaries and changelogs, can run free on open models:
   - Ollama: https://ollama.com. One download, then Llama or Mistral runs on your own machine for free.
   - NVIDIA model catalog: https://build.nvidia.com. Hosted Llama, Mistral and Nemotron, with free credits.

This is advice, slice can make mistakes. Verify before acting.

Sent {time}

Best regards,
— slice gateway
```

## Budget block — live

Fires when a request is blocked at the cap (one per team per `ALERT_COOLDOWN_SECONDS`).

**Subject:** `slice: {team} hit its budget cap. AI requests are blocked`

```
Team {team} hit its monthly AI budget cap. Spend: {spend} of {cap}.

What this means: slice is now blocking this team's AI requests. Blocked requests return a clear error and cost nothing. Other teams are not affected.

To unblock:
- Raise this team's cap and restart the gateway, or
- Wait for the new month. The counter resets on its own.

Sent {time}

Best regards,
— slice gateway
```

---

## Public S3 bucket — planned, phase 18, AWS scanner

**Subject:** `slice: your storage bucket {bucket} is open to the internet`

```
slice scanned your AWS account and found a storage bucket anyone on the internet can read.

bucket: {bucket}
region: {region}

Why this matters: public buckets are one of the most common ways companies leak private files, customer data, and IDs. Anyone with the link can read what is inside.

How to fix, about 2 minutes:
1. Open S3 in your AWS console
2. Click the bucket {bucket}
3. Open the Permissions tab
4. Turn on "Block all public access" and save

If this bucket is meant to be public, like a website, you can ignore this.

slice only reads your AWS setup. It never changes anything itself.

Sent {time}

Best regards,
— slice gateway
```

## Open port — planned, phase 18, AWS scanner

**Subject:** `slice: a server port is open to the whole internet`

```
slice found a firewall rule that lets anyone on the internet try to connect to your server.

security group: {group}
port: {port}
region: {region}

Why this matters: open ports get found by scanning bots within minutes. An open SSH port is a standing invitation for break-in attempts.

How to fix:
1. Open EC2 in your AWS console, then Security Groups
2. Click {group}, then Edit inbound rules
3. Change the source from 0.0.0.0/0 to your own IP or VPN range

Sent {time}

Best regards,
— slice gateway
```

## Idle resources — planned, phase 18, AWS scanner

**Subject:** `slice: you are paying for AWS things you are not using`

```
slice found resources in your AWS account that cost money but show no real use:

{list}

Estimated waste: about {total}/mo.

How to fix: stop or delete what you no longer need. Snapshot disks first if unsure.

This is advice, slice can make mistakes. Verify before acting.

Sent {time}

Best regards,
— slice gateway
```
