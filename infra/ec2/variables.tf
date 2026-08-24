variable "aws_region" {
  description = "AWS region to deploy the cheap always-on slice stack into."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Short name for the project, used to name and tag resources."
  type        = string
  default     = "slice"
}

variable "domain_name" {
  description = "Apex domain for slice (Route 53 hosted zone). This stack recreates its own hosted zone, fully separate from the production stack in ../."
  type        = string
  default     = "sliceapp.dev"
}

variable "api_subdomain" {
  description = "Fully-qualified hostname the API is served at (A record -> Elastic IP)."
  type        = string
  default     = "api.sliceapp.dev"
}

variable "instance_type" {
  description = "EC2 instance type. t4g.small is arm64 (Graviton) and cheap."
  type        = string
  default     = "t4g.small"
}

variable "root_volume_size" {
  description = "Root EBS volume size in GB."
  type        = number
  default     = 30
}

variable "app_secret_name" {
  description = "Name of the existing Secrets Manager secret holding ANTHROPIC_API_KEY and JWT_SECRET."
  type        = string
  default     = "slice/app"
}

variable "ecr_registry" {
  description = "ECR registry host the gateway image is pulled from."
  type        = string
  default     = "194133064379.dkr.ecr.us-east-1.amazonaws.com"
}

variable "gateway_image" {
  description = "Fully-qualified gateway image reference to run."
  type        = string
  default     = "194133064379.dkr.ecr.us-east-1.amazonaws.com/slice-gateway:latest"
}

variable "db_name" {
  description = "Local Postgres database name (matches phase 15 compose)."
  type        = string
  default     = "slice"
}

variable "db_username" {
  description = "Local Postgres username (matches phase 15 compose)."
  type        = string
  default     = "slice"
}
