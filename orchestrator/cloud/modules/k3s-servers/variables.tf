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
  description = "List of private subnet IDs for K3s server nodes (distributed across AZs)"
}

variable "ssh_key_name" {
  type        = string
  description = "Name of SSH key pair"
}

variable "instance_type" {
  type        = string
  description = "EC2 instance type for K3s servers"
}

variable "server_count" {
  type        = number
  description = "Number of K3s server nodes (must be odd for quorum, recommended: 3)"
  validation {
    condition     = var.server_count % 2 == 1 && var.server_count >= 1
    error_message = "Server count must be an odd number (1, 3, 5, etc.) for etcd quorum."
  }
}

variable "root_volume_size" {
  type        = number
  description = "Root volume size in GB"
}

variable "ubuntu_ami_id" {
  type        = string
  description = "AMI ID for Ubuntu"
}

variable "nlb_dns_name" {
  type        = string
  description = "DNS name of the Network Load Balancer for K3s API"
}

variable "peer_vpc_cidr" {
  type        = string
  description = "ECS VPC CIDR block for VPC peering connectivity"
  default     = ""
}

variable "allowed_ssh_cidr_blocks" {
  type        = list(string)
  description = "CIDR blocks allowed to SSH"
}

variable "allowed_k8s_api_cidr_blocks" {
  type        = list(string)
  description = "CIDR blocks allowed to access K3s API server (port 6443)"
}

variable "vpc_cidr" {
  type        = string
  description = "VPC CIDR block for internal communication"
}

variable "alb_security_group_id" {
  type        = string
  description = "Security group ID of the deployments ALB for Envoy access"
  default     = ""
}

variable "wireguard_cidr" {
  type        = string
  description = "WireGuard VPN network CIDR for remote GPU nodes"
  default     = "10.200.0.0/16"
}

variable "tags" {
  type        = map(string)
  description = "Tags to apply to resources"
  default     = {}
}
