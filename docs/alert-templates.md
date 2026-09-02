# slice alert templates

Plain-text email templates the alerts engine (`app/alerts/channels.py`) fills at send time.
`{spend}`, `{cap}`, `{left}` are money-formatted (`$25.00`, `$0.0105`; a positive amount
never shows as `$0.00`). `{left}` is cap minus spend, floored at `$0.00`. `{month}` is the
budget month (`August 2026`). `{time}` is the send time in `ALERT_TIMEZONE` (default
`America/New_York`), e.g. `Aug 18, 2026, 3:20 AM EDT`.

Every email ends with the same two-part footer, in place of any sign-off:

```
slice is an AI. Please double check before you change anything in AWS.

Sent {time}
```

No email uses an em dash anywhere. The words are meant to read like a person wrote them:
short, plain, no jargon.

---

## Budget warn — live

Fires once per team per month, the first time spend crosses `BUDGET_WARN_RATIO` (default
80%) of `BUDGET_MONTHLY_USD`. `{percent}` is that ratio.

**Subject:** `slice: {team} has used {percent}% of its monthly AI budget`

```
Team {team} has spent {spend} of its {cap} monthly AI budget. About {left} is left for {month}.

Nothing is blocked yet. This is an early heads up. At this pace the team will hit its cap before the month ends, and slice will then block its AI requests until the new month or a higher cap.

Three ways to keep working for less:

1. Leave auto-routing on. slice already sends easy work to cheaper models.

2. If most of this team's work is coding in a tool like Claude Code (https://claude.com/product/claude-code), a flat monthly fee can beat paying per token. These work as a substitute if you have a plan:
   - GitHub Copilot: https://github.com/features/copilot
   - Codex: https://openai.com/codex

3. Bulk and repeat jobs, like nightly summaries and changelogs, can run for free on open models:
   - Ollama: https://ollama.com. One download, then Llama or Mistral runs on your own machine for free.
   - NVIDIA model catalog: https://build.nvidia.com. Hosted Llama, Mistral and Nemotron, with free credits.

slice is an AI. Please double check before you change anything in AWS.

Sent {time}
```

## Budget block — live

Fires when a request is blocked at the cap (one per team per `ALERT_COOLDOWN_SECONDS`).

**Subject:** `slice: {team} hit its budget cap. AI requests are blocked`

```
Team {team} hit its monthly AI budget cap. Spend: {spend} of {cap}.

slice is now blocking this team's AI requests. Blocked requests return a clear error and cost nothing. Other teams are not affected.

To unblock:
- Raise this team's cap and restart the gateway, or
- Wait for the new month. The counter resets on its own.

slice is an AI. Please double check before you change anything in AWS.

Sent {time}
```

---

## AWS scan — live

Fires when a scan finds new HIGH findings for an account, one per account per
`ALERT_COOLDOWN_SECONDS`. `{count}` is how many are new (`thing` / `things` agree with it).
The body renders one short block per finding, up to `SCANNER_ALERT_TOP_N`; if there are more
than that, a final `And {n} more like these.` line stands in for the rest.

**Subject:** `slice found {count} thing(s) to check in your AWS account`

**Opening line:** `slice looked at your AWS account and found {count} thing(s) worth a look.`

Each finding block is three short lines (what it is, why it matters, the first thing to do)
followed by a `Read more:` line with the AWS doc page for that check. The wording lives in
one dict, `SCAN_CHECK_COPY` in `app/alerts/channels.py`, keyed by check id. `{resource}` and
`{region}` are filled from the finding.

### Public S3 bucket (`s3_public`)

```
Your S3 storage bucket {resource} in {region} is open to the internet.
Anyone who finds the link can read what is inside, and that is how private files leak.
In the S3 console, open the bucket, go to Permissions, and turn on Block all public access.
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

```
The user {resource} has full admin access attached straight to their account.
If that one login is stolen, an attacker gets the keys to everything.
In IAM, move the user into a group and give them only the access they need.
Read more: https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html
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
