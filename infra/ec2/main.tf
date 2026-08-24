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

# ---------------------------------------------------------------------------
# IAM: instance role + profile.
#   - AmazonSSMManagedInstanceCore    -> Session Manager access (replaces SSH)
#   - AmazonEC2ContainerRegistryReadOnly -> pull the gateway image from ECR
#   - inline policy                   -> read only the slice/app secret
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

data "aws_iam_policy_document" "secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.app.arn]
  }
}

resource "aws_iam_role_policy" "secrets" {
  name   = "${var.project_name}-ec2-app-secret"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.secrets.json
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.project_name}-ec2-profile"
  role = aws_iam_role.instance.name
}

# ---------------------------------------------------------------------------
# Local Postgres password. Stored ONLY in local state (which is gitignored) and
# handed to the instance via user data — this is accepted for this cheap stack.
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
    aws_region        = var.aws_region
    ecr_registry      = var.ecr_registry
    gateway_image     = var.gateway_image
    app_secret_name   = var.app_secret_name
    api_domain        = var.api_subdomain
    grafana_domain    = var.grafana_subdomain
    db_name           = var.db_name
    db_username       = var.db_username
    postgres_password = random_password.postgres.result
  })

  # Re-run user data if the template's rendered output changes.
  user_data_replace_on_change = true

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
