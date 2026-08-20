output "repository_url" {
  description = "URL of the slice-gateway ECR repository (use this to tag and push images)."
  value       = aws_ecr_repository.gateway.repository_url
}
