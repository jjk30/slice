# ===========================================================================
# Cheap always-on slice stack: a single t4g.small (arm64) EC2 instance running
# the whole stack via docker compose — gateway (from ECR) + Postgres 16 +
# Redis 7 + Caddy 2 (HTTPS via Let's Encrypt). This is deliberately separate
# from the production Terraform in ../ (Fargate/RDS/ElastiCache/ALB) and keeps
# its own local state.
# ===========================================================================

# ---------------------------------------------------------------------------
# Network: run in the region's default VPC / a default subnet. No dedicated
# networking to keep this stack minimal and cheap.
# ---------------------------------------------------------------------------
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

# ---------------------------------------------------------------------------
# Latest Amazon Linux 2023 arm64 AMI.
# ---------------------------------------------------------------------------
data "aws_ami" "al2023_arm64" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-arm64"]
  }
  filter {
    name   = "architecture"
    values = ["arm64"]
  }
  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# ---------------------------------------------------------------------------
# Route 53 hosted zone for the apex domain, recreated in THIS stack (separate
# from the production stack's zone). Creating it allocates a fresh set of
# nameservers — see the route53_nameservers output.
# ---------------------------------------------------------------------------
resource "aws_route53_zone" "main" {
  name = var.domain_name
}

# ---------------------------------------------------------------------------
# Static public IP for the box, and an A record pointing the API hostname at it.
# ---------------------------------------------------------------------------
resource "aws_eip" "api" {
  domain = "vpc"

  tags = {
    Name = "${var.project_name}-ec2-eip"
  }
}

resource "aws_route53_record" "api" {
  zone_id = aws_route53_zone.main.zone_id
  name    = var.api_subdomain
  type    = "A"
  ttl     = 300
  records = [aws_eip.api.public_ip]
}

# Phase 17: Grafana on the same box, same Elastic IP. Caddy terminates TLS for this
# host and reverse-proxies it to the grafana container; Prometheus and node_exporter
# stay internal (no DNS, no public port).
resource "aws_route53_record" "grafana" {
  zone_id = aws_route53_zone.main.zone_id
  name    = var.grafana_subdomain
  type    = "A"
  ttl     = 300
  records = [aws_eip.api.public_ip]
}

# Marketing site on the same box, same Elastic IP. Caddy serves the static website/
# folder on the apex, and redirects www -> apex. Both are A records to the EIP so
# Caddy's automatic HTTPS (Let's Encrypt) can complete the ACME challenge on each host.
resource "aws_route53_record" "root" {
  zone_id = aws_route53_zone.main.zone_id
  name    = var.domain_name
  type    = "A"
  ttl     = 300
  records = [aws_eip.api.public_ip]
}

resource "aws_route53_record" "www" {
  zone_id = aws_route53_zone.main.zone_id
  name    = "www.${var.domain_name}"
  type    = "A"
  ttl     = 300
  records = [aws_eip.api.public_ip]
}

# ---------------------------------------------------------------------------
# Security group: HTTP/HTTPS in from anywhere (Caddy needs 80 for ACME and 443
# for traffic), everything out. No port 22 — access is via SSM Session Manager.
# ---------------------------------------------------------------------------
resource "aws_security_group" "instance" {
  name        = "${var.project_name}-ec2-sg"
  description = "slice cheap EC2: allow 80/443 in, all out; no SSH (use SSM)."
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "HTTP (Caddy / ACME challenge)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-ec2-sg"
  }
}

data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# Private S3 bucket for the box's static stack config (docker-compose.yml,
# Caddyfile, prometheus.yml, the grafana provisioning tree). These used to be
# inlined in user_data, which pushed it toward AWS's 16KB limit; the box now
# `aws s3 sync`s them at boot. Everything here is non-secret config — secrets are
# still fetched from Secrets Manager and written into the env files at boot.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "config" {
  bucket = "${var.project_name}-ec2-config-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "config" {
  bucket                  = aws_s3_bucket.config.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Upload every file under files/ preserving its relative path as the object key, so
# `aws s3 sync s3://<bucket>/ /opt/slice/` reproduces the tree the compose file expects
# (docker-compose.yml, Caddyfile, prometheus.yml, grafana/provisioning/...). The
# filemd5 etag makes Terraform re-upload only files whose content actually changed.
resource "aws_s3_object" "config" {
  for_each = fileset("${path.module}/files", "**")

  bucket = aws_s3_bucket.config.id
  key    = each.value
  source = "${path.module}/files/${each.value}"
  etag   = filemd5("${path.module}/files/${each.value}")
}

# ---------------------------------------------------------------------------
# IAM: instance role + profile.
#   - AmazonSSMManagedInstanceCore    -> Session Manager access (replaces SSH)
#   - AmazonEC2ContainerRegistryReadOnly -> pull the gateway image from ECR
#   - inline policies                 -> read the app + db secrets, and the config bucket
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${var.project_name}-ec2-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "ecr_readonly" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# Look up the pre-existing app secret by name so we don't hardcode the ARN suffix.
data "aws_secretsmanager_secret" "app" {
  name = var.app_secret_name
}

# The Postgres password lives in its OWN secret (not inlined into user_data or the
# compose file, and not written into the shared app secret — which Terraform doesn't
# own and would clobber). Terraform generates it (random_password.postgres) and stores
# it here; the box reads it at boot and hands it to compose via the environment.
resource "aws_secretsmanager_secret" "ec2_db" {
  name        = var.db_secret_name
  description = "slice cheap EC2 box: local Postgres password (managed by Terraform)."
}

resource "aws_secretsmanager_secret_version" "ec2_db" {
  secret_id     = aws_secretsmanager_secret.ec2_db.id
  secret_string = jsonencode({ POSTGRES_PASSWORD = random_password.postgres.result })
}

data "aws_iam_policy_document" "secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      data.aws_secretsmanager_secret.app.arn,
      aws_secretsmanager_secret.ec2_db.arn,
    ]
  }
}

resource "aws_iam_role_policy" "secrets" {
  name   = "${var.project_name}-ec2-app-secret"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.secrets.json
}

# Read-only access to the config bucket only (GetObject + ListBucket), so the box can
# `aws s3 sync` its stack config at boot. Scoped to this one bucket — nothing else.
data "aws_iam_policy_document" "config_bucket" {
  statement {
    sid       = "ListConfigBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.config.arn]
  }
  statement {
    sid       = "GetConfigObjects"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.config.arn}/*"]
  }
}

resource "aws_iam_role_policy" "config_bucket" {
  name   = "${var.project_name}-ec2-config-bucket"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.config_bucket.json
}

# ---------------------------------------------------------------------------
# Nightly Postgres backup bucket. The box runs infra/ec2/scripts/backup.sh from
# cron, dumps the compose Postgres, and uploads slice-YYYY-MM-DD.dump here. All
# public access is blocked, and objects expire after 30 days. The bucket name
# carries no secret (project name plus account id, same scheme as the config
# bucket).
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "backups" {
  bucket = "${var.project_name}-ec2-backups-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Expire backups after 30 days. The empty filter applies the rule to every object.
resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    id     = "expire-after-30-days"
    status = "Enabled"

    filter {}

    expiration {
      days = 30
    }
  }
}

# Let the box write backup objects to this one bucket, nothing else. Attached to
# the instance's existing role (the one with SSM), so no new role is created.
data "aws_iam_policy_document" "backups" {
  statement {
    sid       = "PutBackupObjects"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.backups.arn}/*"]
  }
}

resource "aws_iam_role_policy" "backups" {
  name   = "${var.project_name}-ec2-backups-put"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.backups.json
}

# ---------------------------------------------------------------------------
# Phase 18a: read-only permissions for the AWS security scanner. slice scans the
# account it runs in — public S3, world-open security groups, unencrypted storage,
# old IAM keys / direct AdministratorAccess — and pulls Cost Explorer spend.
#
# Least privilege: every action is a read, scoped to exactly what the four checks and
# the cost pull call. No writes, no wildcards. Resources are "*" only because these
# describe/list/get calls are account-wide by nature (you cannot enumerate buckets,
# security groups, volumes, or users without a list over the whole account); the actions
# themselves are strictly read-only, so "*" here grants visibility, never mutation.
#
# The two beyond the base list — sts:GetCallerIdentity and s3control:GetPublicAccessBlock
# — back the *account-level* S3 Block Public Access check (it needs the account id, then
# the account-level BPA setting). That check fails open if they are denied, so they are a
# clean addition rather than a hard requirement.
data "aws_iam_policy_document" "scanner" {
  statement {
    sid    = "S3ReadForScanner"
    effect = "Allow"
    actions = [
      "s3:ListAllMyBuckets",
      "s3:GetBucketAcl",
      "s3:GetBucketPolicy",
      "s3:GetBucketPolicyStatus",
      "s3:GetEncryptionConfiguration",
      "s3:GetBucketPublicAccessBlock",
      "s3:GetAccountPublicAccessBlock",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "STSIdentityForScanner"
    effect    = "Allow"
    actions   = ["sts:GetCallerIdentity"]
    resources = ["*"]
  }

  statement {
    sid    = "EC2ReadForScanner"
    effect = "Allow"
    actions = [
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeVolumes",
      # Phase 18c cost-waste checks: unassociated EIPs, stale snapshots, idle instances.
      "ec2:DescribeAddresses",
      "ec2:DescribeSnapshots",
      "ec2:DescribeInstances",
    ]
    resources = ["*"]
  }

  statement {
    # Phase 18c: idle-instance detection reads CPUUtilization from CloudWatch.
    sid       = "CloudWatchReadForScanner"
    effect    = "Allow"
    actions   = ["cloudwatch:GetMetricStatistics"]
    resources = ["*"]
  }

  statement {
    sid    = "IAMReadForScanner"
    effect = "Allow"
    actions = [
      "iam:ListUsers",
      "iam:ListAccessKeys",
      "iam:ListAttachedUserPolicies",
      "iam:GetAccessKeyLastUsed",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "CostExplorerReadForScanner"
    effect    = "Allow"
    actions   = ["ce:GetCostAndUsage"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "scanner" {
  name   = "${var.project_name}-ec2-scanner-readonly"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.scanner.json
}

# ---------------------------------------------------------------------------
# Phase 18b: the box assumes a read-only role in each connected user's account.
#
# Narrowest workable scope: sts:AssumeRole limited to roles under the fixed path
# "/slice-scanner/" — the exact path the committed onboarding CloudFormation template
# (infra/user-onboarding/slice-readonly-role.yaml) creates. slice cannot assume any other
# role, in any account. Cross-account role names are not knowable ahead of time (each user
# creates their own in their own account), so a per-ARN allowlist is impractical; the path
# constraint bounds the blast radius to exactly slice-created roles.
#
# The real security gate is on the *target* side: each user's role trust policy allows only
# slice's account (194133064379) AND requires the per-account External ID on every call, so
# even this permission can only assume roles that explicitly opted in. (A plain
# sts:AssumeRole on "*" would be the fallback if a path scheme were not used — avoided here.)
data "aws_iam_policy_document" "scanner_assume" {
  statement {
    sid       = "AssumeUserScannerRoles"
    effect    = "Allow"
    actions   = ["sts:AssumeRole"]
    resources = ["arn:aws:iam::*:role/slice-scanner/*"]
  }
}

resource "aws_iam_role_policy" "scanner_assume" {
  name   = "${var.project_name}-ec2-scanner-assume"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.scanner_assume.json
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.project_name}-ec2-profile"
  role = aws_iam_role.instance.name
}

# ---------------------------------------------------------------------------
# Local Postgres password. Generated by Terraform and stored in its own Secrets
# Manager secret (aws_secretsmanager_secret.ec2_db above); the box reads it at boot.
# It is no longer inlined into user_data or the compose file.
# ---------------------------------------------------------------------------
resource "random_password" "postgres" {
  length  = 32
  special = false # keep it URL-safe: it goes straight into DATABASE_URL
}

# ---------------------------------------------------------------------------
# The instance. User data is idempotent and safe across reboots/replacements:
# it (re)writes /opt/slice config and runs `docker compose up -d`; the named
# Postgres volume preserves data across restarts.
# ---------------------------------------------------------------------------
resource "aws_instance" "app" {
  ami                    = data.aws_ami.al2023_arm64.id
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.instance.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    aws_region      = var.aws_region
    ecr_registry    = var.ecr_registry
    gateway_image   = var.gateway_image
    app_secret_name = var.app_secret_name
    db_secret_name  = var.db_secret_name
    config_bucket   = aws_s3_bucket.config.bucket
    api_domain      = var.api_subdomain
    grafana_domain  = var.grafana_subdomain
    db_name         = var.db_name
    db_username     = var.db_username
  })

  # Replacement guard: a change to the rendered user_data updates the instance in
  # place and never replaces it. This protects the live box, its Postgres volume,
  # and its Elastic IP association from being destroyed by an edit to user_data.
  user_data_replace_on_change = false

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_size
    encrypted   = true
  }

  metadata_options {
    http_tokens   = "required" # IMDSv2 only
    http_endpoint = "enabled"
  }

  tags = {
    Name = "${var.project_name}-ec2"
  }
}

# Bind the Elastic IP to the instance.
resource "aws_eip_association" "api" {
  instance_id   = aws_instance.app.id
  allocation_id = aws_eip.api.id
}
