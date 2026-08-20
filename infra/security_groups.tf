# ---------------------------------------------------------------------------
# ALB security group: public-facing. Allow HTTP (80) and HTTPS (443) from
# anywhere. 443 is unused until sub-step 5 (TLS) but opened now.
# ---------------------------------------------------------------------------
resource "aws_security_group" "alb" {
  name        = "${var.project_name}-alb"
  description = "Public ALB: allow inbound HTTP/HTTPS from the internet"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP from anywhere"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS from anywhere"
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
    Name = "${var.project_name}-alb"
  }
}

# ---------------------------------------------------------------------------
# Gateway (app) security group: inbound app port ONLY from the ALB SG.
# No CIDR ingress — the container is unreachable except via the load balancer,
# which is what keeps it private.
# ---------------------------------------------------------------------------
resource "aws_security_group" "gateway" {
  name        = "${var.project_name}-gateway"
  description = "Gateway app: allow inbound app port only from the ALB"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "App port from ALB only"
    from_port       = var.app_port
    to_port         = var.app_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-gateway"
  }
}
