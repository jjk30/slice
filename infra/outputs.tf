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

output "alb_dns_name" {
  description = "Public DNS name of the ALB: use this to reach slice."
  value       = aws_lb.main.dns_name
}

output "ecs_cluster_name" {
  description = "Name of the ECS cluster."
  value       = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  description = "Name of the ECS service."
  value       = aws_ecs_service.gateway.name
}

output "route53_nameservers" {
  description = "Route 53 nameservers for the hosted zone: paste these into Namecheap."
  value       = aws_route53_zone.main.name_servers
}

output "acm_certificate_arn" {
  description = "ARN of the validated ACM certificate (apex + api subdomain)."
  value       = aws_acm_certificate_validation.main.certificate_arn
}

output "api_url" {
  description = "HTTPS URL the slice API is served at."
  value       = "https://${var.api_subdomain}"
}
