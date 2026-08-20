# ---------------------------------------------------------------------------
# RDS PostgreSQL (managed, private, multi-AZ)
# ---------------------------------------------------------------------------

# Subnet group across the two private subnets — RDS lives only in private space.
resource "aws_db_subnet_group" "main" {
  name       = "${var.project_name}-db"
  subnet_ids = aws_subnet.private[*].id

  tags = {
    Name = "${var.project_name}-db"
  }
}

# Master password. Generated here, never written as a literal or an output.
# Lives only in Terraform state and in Secrets Manager. Special chars are
# restricted to a URL-safe set (no / @ " space : etc.) so DATABASE_URL is clean.
resource "random_password" "db" {
  length           = 32
  special          = true
  override_special = "!#%*_-=+"
}

resource "aws_db_instance" "main" {
  identifier     = "${var.project_name}-db"
  engine         = "postgres"
  engine_version = "16.4"
  instance_class = var.db_instance_class

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.database.id]
  publicly_accessible    = false
  multi_az               = true

  backup_retention_period = 1
  skip_final_snapshot     = true # disposable demo DB
  deletion_protection     = false

  tags = {
    Name = "${var.project_name}-db"
  }
}

# ---------------------------------------------------------------------------
# Secrets Manager: full connection details as a single JSON secret (slice/db).
# host/port reference the RDS instance so the secret holds the real endpoint
# after apply.
# ---------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "db" {
  name        = "${var.project_name}/db"
  description = "slice PostgreSQL connection details"
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id

  secret_string = jsonencode({
    username = var.db_username
    # Raw, un-encoded password so direct readers of this field get the true value.
    password = random_password.db.result
    host     = aws_db_instance.main.address
    port     = aws_db_instance.main.port
    dbname   = var.db_name
    # Convenience: ready-to-use DATABASE_URL. The password may contain URL-special
    # chars (from override_special "!#%*_-=+"), so it is urlencode()'d here; the
    # username, host, port and dbname are safe as-is.
    url = "postgresql://${var.db_username}:${urlencode(random_password.db.result)}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/${var.db_name}"
  })
}
