# ---------------------------------------------------------------------------
# CloudWatch logs
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "gateway" {
  name              = "/ecs/${var.project_name}"
  retention_in_days = 7
}

# ---------------------------------------------------------------------------
# ECS cluster
# ---------------------------------------------------------------------------
resource "aws_ecs_cluster" "main" {
  name = var.project_name
}

# ---------------------------------------------------------------------------
# Task definition
# ---------------------------------------------------------------------------
# Secrets are injected via `valueFrom` (Secrets Manager ARN + JSON key), never
# as plaintext env. `<arn>:<json_key>::` selects a single key from the JSON secret.
resource "aws_ecs_task_definition" "gateway" {
  family                   = "${var.project_name}-gateway"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  # Image is built on Apple Silicon (arm64). Run the task on Fargate ARM64 to
  # match, natively supported and cheaper than amd64.
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([
    {
      name      = "gateway"
      image     = "${aws_ecr_repository.gateway.repository_url}:latest"
      essential = true

      portMappings = [
        {
          containerPort = var.app_port
          protocol      = "tcp"
        }
      ]

      # Only what's needed to boot. Optional feature flags are omitted on
      # purpose: the app fails open to its own defaults when they're unset.
      environment = [
        {
          name  = "REDIS_URL"
          value = "redis://${aws_elasticache_replication_group.main.primary_endpoint_address}:6379"
        }
      ]

      secrets = [
        {
          name      = "DATABASE_URL"
          valueFrom = "${aws_secretsmanager_secret.db.arn}:url::"
        },
        {
          name      = "ANTHROPIC_API_KEY"
          valueFrom = "${data.aws_secretsmanager_secret.app.arn}:ANTHROPIC_API_KEY::"
        },
        {
          name      = "JWT_SECRET"
          valueFrom = "${data.aws_secretsmanager_secret.app.arn}:JWT_SECRET::"
        }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.gateway.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "gateway"
        }
      }
    }
  ])
}

# ---------------------------------------------------------------------------
# ECS service
# ---------------------------------------------------------------------------
resource "aws_ecs_service" "gateway" {
  name            = "${var.project_name}-gateway"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.gateway.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  # Runs in the private subnets; reaches ECR/Secrets/Anthropic outbound via NAT.
  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.gateway.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.gateway.arn
    container_name   = "gateway"
    container_port   = var.app_port
  }

  # Heavy libs (torch, ragas) make first boot slow: give it room before the
  # ALB health check can kill the task.
  health_check_grace_period_seconds = 120

  # Target group must be attached to the listener before the service registers.
  depends_on = [aws_lb_listener.http]
}
