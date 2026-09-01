locals {
  account_id  = "194133064379"
  region      = "us-east-1"
  github_repo = "jjk30/slice"
  ecr_repo    = "slice-gateway"
}

# The ECR repo already exists and is managed elsewhere. We only read it here.
data "aws_ecr_repository" "gateway" {
  name = local.ecr_repo
}

# OIDC provider that GitHub Actions uses to hand AWS a short lived token.
# AWS now validates the certificate chain for token.actions.githubusercontent.com,
# so the thumbprint below is legacy and no longer the trust anchor, but the
# resource still requires a value, so we keep the well known GitHub thumbprint.
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

# Trust policy: only GitHub Actions from jjk30/slice on refs/heads/main may
# assume this role, and only with the sts.amazonaws.com audience.
data "aws_iam_policy_document" "assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:jjk30/slice:ref:refs/heads/main"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "gha_ecr_push" {
  name               = "slice-gha-ecr-push"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

# Least privilege ECR push policy. GetAuthorizationToken cannot be scoped to a
# single repository, so it stays on Resource "*". Every other action is scoped
# to the slice-gateway repository in this account.
data "aws_iam_policy_document" "ecr_push" {
  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPush"
    effect = "Allow"
    actions = [
      "ecr:BatchGetImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [data.aws_ecr_repository.gateway.arn]
  }
}

resource "aws_iam_role_policy" "ecr_push" {
  name   = "slice-gha-ecr-push"
  role   = aws_iam_role.gha_ecr_push.id
  policy = data.aws_iam_policy_document.ecr_push.json
}
