terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State is intentionally LOCAL for this cheap always-on stack. There is no
  # remote backend here: the box is single-instance and disposable, so the
  # convenience of local state outweighs remote locking. terraform.tfstate is
  # gitignored (it holds the generated Postgres password) — this is accepted.
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "slice"
      ManagedBy = "terraform"
      Stack     = "ec2-cheap"
    }
  }
}
