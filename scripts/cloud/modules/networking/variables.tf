variable "name_prefix" {
  description = "Name prefix for resources"
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR block"
  type        = string
}

variable "availability_zones" {
  description = "List of availability zones"
  type        = list(string)
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}

variable "k3s_vpc_peering_connection_id" {
  description = "VPC peering connection ID for K3s cluster (created by orchestrator/cloud)"
  type        = string
  default     = ""
}

variable "k3s_vpc_cidr" {
  description = "CIDR block of the K3s VPC for routing via peering connection"
  type        = string
  default     = ""
}