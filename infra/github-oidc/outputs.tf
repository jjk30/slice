output "gha_role_arn" {
  description = "ARN of the role GitHub Actions assumes to push images to ECR."
  value       = aws_iam_role.gha_ecr_push.arn
}

output "oidc_provider_arn" {
  description = "ARN of the GitHub Actions OIDC provider."
  value       = aws_iam_openid_connect_provider.github.arn
}
