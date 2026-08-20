# ---------------------------------------------------------------------------
# Application Load Balancer (internet-facing, public subnets)
# ---------------------------------------------------------------------------
resource "aws_lb" "main" {
  name               = "${var.project_name}-alb"
  load_balancer_type = "application"
  internal           = false
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]
}

# Target group for the Fargate task. target_type = "ip" is required for awsvpc.
# Health check hits /docs: the app has no dedicated /health route, / only works
# when the dashboard build is present, but /docs (FastAPI) always returns 200.
# Lenient thresholds so the slow first boot doesn't flap the target unhealthy.
resource "aws_lb_target_group" "gateway" {
  name        = "${var.project_name}-gateway"
  port        = var.app_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/docs"
    matcher             = "200"
    healthy_threshold   = 2
    unhealthy_threshold = 5
    interval            = 30
    timeout             = 10
  }
}

# HTTP listener on 80 → target group. HTTPS/443 is added in sub-step 5.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
  }
}
