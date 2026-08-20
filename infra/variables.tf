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
