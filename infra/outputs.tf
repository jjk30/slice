output "repository_url" {
  description = "URL of the slice-gateway ECR repository (use this to tag and push images)."
  value       = aws_ecr_repository.gateway.repository_url
}

output "vpc_id" {
  description = "ID of the slice VPC."
  value       = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "IDs of the public subnets (one per AZ)."
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "IDs of the private subnets (one per AZ)."
  value       = aws_subnet.private[*].id
}

output "alb_security_group_id" {
  description = "ID of the ALB security group."
  value       = aws_security_group.alb.id
}

output "gateway_security_group_id" {
  description = "ID of the gateway (app) security group."
  value       = aws_security_group.gateway.id
}

output "rds_endpoint" {
  description = "Address (hostname) of the RDS Postgres instance."
  value       = aws_db_instance.main.address
}

output "rds_port" {
  description = "Port of the RDS Postgres instance."
  value       = aws_db_instance.main.port
}

output "redis_primary_endpoint" {
  description = "Primary endpoint address of the Redis replication group."
  value       = aws_elasticache_replication_group.main.primary_endpoint_address
}

output "db_secret_arn" {
  description = "ARN of the Secrets Manager secret holding DB connection details."
  value       = aws_secretsmanager_secret.db.arn
}
