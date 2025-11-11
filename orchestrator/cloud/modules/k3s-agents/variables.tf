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
  description = "List of private subnet IDs for K3s agent nodes (distributed across AZs)"
}

variable "ssh_key_name" {
  type        = string
  description = "Name of SSH key pair"
}

variable "instance_type" {
  type        = string
  description = "EC2 instance type for K3s agents"
}

variable "agent_count" {
  type        = number
  description = "Number of K3s agent nodes"
  validation {
    condition     = var.agent_count >= 1
    error_message = "Agent count must be at least 1."
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

variable "k3s_token" {
  type        = string
  description = "K3s cluster token for agent joining"
  sensitive   = true
}

variable "allowed_ssh_cidr_blocks" {
  type        = list(string)
  description = "CIDR blocks allowed to SSH"
}

variable "vpc_cidr" {
  type        = string
  description = "VPC CIDR block for internal communication"
}

variable "k3s_server_security_group_id" {
  type        = string
  description = "Security group ID of K3s server nodes"
}

variable "tags" {
  type        = map(string)
  description = "Tags to apply to resources"
  default     = {}
}
