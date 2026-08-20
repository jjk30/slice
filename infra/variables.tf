variable "aws_region" {
  description = "AWS region to deploy slice infrastructure into."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Short name for the project, used to name and tag resources."
  type        = string
  default     = "slice"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "app_port" {
  description = "Port the gateway container listens on."
  type        = number
  default     = 8080
}

variable "db_instance_class" {
  description = "RDS instance class for the Postgres database."
  type        = string
  default     = "db.t3.micro"
}

variable "db_name" {
  description = "Initial Postgres database name."
  type        = string
  default     = "slice"
}

variable "db_username" {
  description = "Postgres master username."
  type        = string
  default     = "slice"
}

variable "cache_node_type" {
  description = "ElastiCache node type for the Redis cluster."
  type        = string
  default     = "cache.t3.micro"
}
