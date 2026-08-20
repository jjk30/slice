# ---------------------------------------------------------------------------
# ElastiCache Redis (managed, private, HA via one replica)
# ---------------------------------------------------------------------------

# Subnet group across the two private subnets.
resource "aws_elasticache_subnet_group" "main" {
  name       = "${var.project_name}-cache"
  subnet_ids = aws_subnet.private[*].id

  tags = {
    Name = "${var.project_name}-cache"
  }
}

# Replication group: 1 primary + 1 replica across AZs, automatic failover.
# At-rest encryption on.
#
# NEXT HARDENING STEP (not done now): enable transit_encryption_enabled = true
# and set an auth_token so Redis requires TLS + a password. That changes
# REDIS_URL to rediss:// and adds credentials, so it's left for later.
resource "aws_elasticache_replication_group" "main" {
  replication_group_id = "${var.project_name}-redis"
  description          = "slice Redis (primary + replica)"

  engine         = "redis"
  engine_version = "7.1"
  node_type      = var.cache_node_type
  port           = 6379

  num_cache_clusters         = 2
  automatic_failover_enabled = true
  multi_az_enabled           = true

  at_rest_encryption_enabled = true

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.cache.id]

  tags = {
    Name = "${var.project_name}-redis"
  }
}
