# slice — infrastructure (Terraform)

AWS infrastructure-as-code for slice. Region defaults to `us-east-1`.
Uses the default AWS CLI profile (IAM user `Slice-deploy`).

## Current contents

- ECR repository `slice-gateway` (scan-on-push, keeps last 10 images).

## Usage

```bash
terraform init
terraform plan
terraform apply
```

Then grab the repository URL to push the gateway image:

```bash
terraform output repository_url
```

## State

State is **local** for now — it lives in `terraform.tfstate` in this directory
and is gitignored. This is fine for a single operator. When we add more
infrastructure (or a second person), we'll move to a remote S3 backend
(see the note in the phase notes / below).
