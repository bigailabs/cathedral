variable "name_prefix" {
  type        = string
  description = "Prefix for resource names"
}

variable "vpc_id" {
  type        = string
  description = "VPC ID"
}

variable "subnet_ids" {
  type        = list(string)
  description = "List of subnet IDs for NLB (public subnets)"
}

variable "tags" {
  type        = map(string)
  description = "Tags to apply to resources"
  default     = {}
}
