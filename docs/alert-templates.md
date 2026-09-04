# slice alert templates

Plain-text email templates the alerts engine (`app/alerts/channels.py`) fills at send time.
`{spend}`, `{cap}`, `{left}` are money-formatted (`$25.00`, `$0.0105`; a positive amount
never shows as `$0.00`). `{left}` is cap minus spend, floored at `$0.00`. `{month}` is the
budget month (`August 2026`). `{time}` is the send time in `ALERT_TIMEZONE` (default
`America/New_York`), e.g. `Aug 18, 2026, 3:20 AM EDT`.

Every email ends with a two-part footer, in place of any sign-off: one AI note, a blank
line, then the send time. The note depends on the kind of email (phase 26):

| Email | Footer |
| --- | --- |
| AWS scan | `slice is an AI. Please double check before you change anything in AWS.` |
| Budget warn, budget block | `slice is an AI. Please double check before you change your AI setup.` |
| Reply-by-email, about the user's own data | `slice is an AI. Please double check before you change your AI setup.` |
| Reply-by-email, general advice | `slice is an AI. This is general advice. Please double check before you act on it.` |

No email uses an em dash anywhere. The words are meant to read like a person wrote them:
short, plain, no jargon. A budget email speaks to the person it lands with ("You have
spent"), never about a team in the third person.

## HTML and text

Every email goes to Resend as both `text` and `html` (phase 26); the text is the fallback
for clients that do not render HTML. The HTML is rendered from the plain body by
`render_html`: the cake logo (`https://sliceapp.dev/logo.png`, 192px wide, shown at 48px, alt `slice`) with
the word slice next to it at the top, then the same words in a readable layout, at most
600px wide, in the system font, inline styles only (no external CSS), URLs as links, one
paragraph per blank-line-separated block. Nothing is added or reworded.

---

## Budget warn (live)

Fires once per account per month, the first time spend crosses `BUDGET_WARN_RATIO` (default
80%) of the account's monthly cap (its own, set in the dashboard's Settings, or the
`BUDGET_MONTHLY_USD` default). `{cap}` is that account's cap. `{percent}` is that ratio.

**Subject:** `slice: you have used {percent}% of your monthly AI budget`

```
You have spent {spend} of your {cap} monthly AI budget. About {left} is left for {month}.

Nothing is blocked yet. This is an early heads up. At this pace you will hit your cap before the month ends, and slice will then block your AI requests until the new month or a higher cap.

Three ways to keep working for less:

1. Leave auto-routing on. slice already sends easy work to cheaper models.

2. If most of your work is coding in a tool like Claude Code (https://claude.com/product/claude-code), a flat monthly fee can beat paying per token. These work as a substitute if you have a plan:
   - GitHub Copilot: https://github.com/features/copilot
   - Codex: https://openai.com/codex

3. Bulk and repeat jobs, like nightly summaries and changelogs, can run for free on open models:
   - Ollama: https://ollama.com. One download, then Llama or Mistral runs on your own machine for free.
   - NVIDIA model catalog: https://build.nvidia.com. Hosted Llama, Mistral and Nemotron, with free credits.

slice is an AI. Please double check before you change your AI setup.

Sent {time}
```

## Budget block (live)

Fires when a request is blocked at the cap (one per account per `ALERT_COOLDOWN_SECONDS`).

**Subject:** `slice: you hit your budget cap. AI requests are blocked`

```
You hit your monthly AI budget cap. Spend: {spend} of {cap}.

slice is now blocking your AI requests. Blocked requests return a clear error and cost nothing.

To unblock:
- Raise your cap in Settings on the dashboard, or
- Wait for the new month. The counter resets on its own.

slice is an AI. Please double check before you change your AI setup.

Sent {time}
```

---

## AWS scan (live)

Fires when a scan finds new HIGH findings for an account, one per account per
`ALERT_COOLDOWN_SECONDS`. `{count}` is how many are new (`thing` / `things` agree with it).
The body renders one short block per finding, up to `SCANNER_ALERT_TOP_N`; if there are more
than that, a final `And {n} more like these.` line stands in for the rest.

Phase 24b: a finding the user marked as expected on the dashboard (`POST
/scanner/expectations`) is still recorded and still listed on the dashboard, but it is left
out of this email. When any new highs were skipped that way, one line follows the list:
`{n} expected finding(s) not shown. Manage them on the dashboard.` If every new high was
expected, no email is sent at all.

**Subject:** `slice found {count} thing(s) to check in your AWS account`

**Opening line:** `slice looked at your AWS account and found {count} thing(s) worth a look.`

Each finding block is three short lines (what it is, why it matters, the first thing to do)
followed by a `Read more:` line with the AWS doc page for that check. The wording lives in
one dict, `SCAN_CHECK_COPY` in `app/alerts/channels.py`, keyed by check id. `{resource}` and
`{region}` are filled from the finding. The same "what" line is the finding's title on the
dashboard (`finding_title`).

### Public S3 bucket (`s3_public`)

```
Your S3 storage bucket {resource} in {region} is open to the internet.
Anyone who finds the link can read what is inside, and that is how private files leak.
In the S3 console, open the bucket, go to Permissions, and turn on Block all public access.
Read more: https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html
```

When the resource is the whole account (the account-level Block Public Access setting is
off or partial), the block reads:

```
Block Public Access is turned off for your whole AWS account.
With it off, any one bucket can be made public by mistake.
In the S3 console, open Block Public Access settings for this account, and turn it on if no bucket needs to be public.
Read more: https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html
```

### Open port (`sg_open`)

```
A firewall rule on {resource} in {region} lets the whole internet reach a server port.
Bots find open ports within minutes and keep trying to get in.
In the EC2 console open Security Groups, edit the inbound rule, and set the source to your own IP.
Read more: https://docs.aws.amazon.com/vpc/latest/userguide/security-group-rules.html
```

### Unencrypted storage (`unencrypted`)

```
The storage {resource} in {region} is not encrypted.
If someone gets the raw storage, they can read the data straight off it.
Turn on default encryption for it in the AWS console.
Read more: https://docs.aws.amazon.com/AmazonS3/latest/userguide/default-bucket-encryption.html
```

### IAM risk (`iam_risk`)

The check raises two shapes of finding. A user with AdministratorAccess attached straight
to their account (resource: the user name, severity high):

```
The user {resource} has full admin access attached straight to their account.
If that one login is stolen, an attacker gets the keys to everything.
In IAM, move the user into a group and give them only the access they need.
Read more: https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html
```

An access key older than `SCANNER_IAM_KEY_MAX_AGE_DAYS` (resource: the key id, `AKIA...`
or `ASIA...`, severity medium; the finding's detail carries the owning user, the age and
the last-used date). Phase 26: a key is about age, not admin rights, so it gets its own
words (`SCAN_KEY_COPY`). `{key_age}` is the configured age, 90 by default:

```
The access key {resource} is more than {key_age} days old.
Old keys end up copied into scripts and laptops nobody remembers, and the longer a key lives the more places it can leak from.
In IAM, open the user the key belongs to, make a new key, switch your tools to it, then make the old one inactive and delete it.
Read more: https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_access-keys.html#Using_RotateAccessKey
```

### Unattached disk (`ebs_waste`)

```
The disk {resource} in {region} is not attached to anything, but you still pay for it.
It sits there unused and adds to your bill every month for nothing.
Make sure you do not need it, snapshot it if unsure, then delete it in the EC2 console.
Read more: https://docs.aws.amazon.com/ebs/latest/userguide/ebs-deleting-volume.html
```

### Idle Elastic IP (`eip_waste`)

```
The Elastic IP {resource} in {region} is not attached to anything, but still costs money.
AWS charges for a reserved IP address that nothing is using.
Release it in the EC2 console once you are sure nothing needs it.
Read more: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/elastic-ip-addresses-eip.html
```

### Old snapshot (`snapshot_waste`)

```
The old backup {resource} in {region} is still on your bill.
Snapshots you no longer need keep costing a little every month.
Delete the ones you no longer need in the EC2 console.
Read more: https://docs.aws.amazon.com/ebs/latest/userguide/ebs-deleting-snapshot.html
```

### Idle server (`idle_instances`)

```
The server {resource} in {region} is running but barely used.
You pay the full price for a machine that is doing almost nothing.
Stop it, or move it to a smaller size, in the EC2 console.
Read more: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-resize.html
```

### A full example

For a scan that found three new things:

```
slice looked at your AWS account and found 3 things worth a look.

Your S3 storage bucket acme-invoices in us-east-1 is open to the internet.
Anyone who finds the link can read what is inside, and that is how private files leak.
In the S3 console, open the bucket, go to Permissions, and turn on Block all public access.
Read more: https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html

A firewall rule on sg-0a1b2c3d in us-east-1 lets the whole internet reach a server port.
Bots find open ports within minutes and keep trying to get in.
In the EC2 console open Security Groups, edit the inbound rule, and set the source to your own IP.
Read more: https://docs.aws.amazon.com/vpc/latest/userguide/security-group-rules.html

The server i-04f9 in us-east-1 is running but barely used.
You pay the full price for a machine that is doing almost nothing.
Stop it, or move it to a smaller size, in the EC2 console.
Read more: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-resize.html

slice is an AI. Please double check before you change anything in AWS.

Sent {time}
```

---

## Reply-by-email (live)

A reply to any of the emails above goes through `app/email_assistant`. The topic rail
(`guardrails/prompts.yml`, mode `email`) sorts the question into one of three buckets, and
the bucket is recorded in `email_replies.verdict`:

| Bucket | What it is | Who answers | Verdict |
| --- | --- | --- | --- |
| own_data | the sender's own spend, budget, routing, cache, alerts, findings, AWS cost | `EMAIL_ASSISTANT_MODEL`, from the account's own read-only data | `answered_own` |
| general | a general question about AWS setup, cloud cost, AI models or AI cost | `EMAIL_ASSISTANT_GENERAL_MODEL`, from its own knowledge, no account data | `answered_general` |
| blocked | everything else, any error, or any answer from the rail that is not a known label | nobody; the fixed line `Sorry, I can't help with that here.` | `blocked_input` |

A general reply always starts with the line `General advice, not from your account.` and
ends with the general footer. An own-data reply ends with the AI setup footer. Each goes
through the output rail written for its bucket: the `email` output prompt for own data,
the `email_general` one for general advice (it allows general advice on AWS setup, cloud
cost, AI models and AI cost, requires the disclaimer line first, and still blocks
commands, scripts, code, IAM policy text, claims about the sender's own account or
numbers, other accounts' data, slice internals, harmful content, and anything off those
subjects). A block either way sends the fixed line (`blocked_output`). Dollar amounts
under one cent in the context read `less than a cent`, never `$0.00`.

Each account gets at most `EMAIL_ASSISTANT_DAILY_LIMIT` replies per UTC day (default 20).
The first mail over the line gets exactly `You have reached today's reply limit. Try again
tomorrow.` (`limit_reached`); every later one that day gets nothing (`limit_silenced`).
