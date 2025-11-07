variable "name_prefix" {
  type        = string
  description = "Prefix for resource names"
}

variable "vpc_id" {
  type        = string
  description = "K3s VPC ID (requester)"
}

variable "vpc_cidr" {
  type        = string
  description = "K3s VPC CIDR block"
}

variable "k3s_route_table_id" {
  type        = string
  description = "K3s private route table ID"
}

variable "peer_vpc_id" {
  type        = string
  description = "ECS VPC ID (accepter) - leave empty to skip peering"
  default     = ""
}

variable "peer_vpc_cidr" {
  type        = string
  description = "ECS VPC CIDR block"
  default     = ""
}

variable "peer_route_table_id" {
  type        = string
  description = "ECS private route table ID - leave empty to skip route"
  default     = ""
}

variable "tags" {
  type        = map(string)
  description = "Tags to apply to resources"
  default     = {}
}
