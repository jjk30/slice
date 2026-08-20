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
