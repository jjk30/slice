# github-oidc

Keyless GitHub Actions to AWS authentication over OIDC, so CI can push Docker
images to the `slice-gateway` ECR repo with no long lived AWS access keys.

It creates:

- An IAM OIDC provider for `token.actions.githubusercontent.com`.
- An IAM role `slice-gha-ecr-push` that only `jjk30/slice` on `refs/heads/main`
  can assume, with a least privilege ECR push policy.

In your GitHub Actions workflow, configure AWS credentials using the role ARN
from the `gha_role_arn` output and request an OIDC token with the
`sts.amazonaws.com` audience.

## Apply

```bash
cd infra/github-oidc
terraform init
terraform apply
```

This folder keeps its own separate Terraform state. It does not manage the ECR
repository or any EC2 resources; those live in other states and are only read
here, never modified.
