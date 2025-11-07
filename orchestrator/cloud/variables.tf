variable "aws_region" {
  type        = string
  description = "AWS region for K3s cluster infrastructure"
  default     = "us-east-2"
}

variable "project_name" {
  type        = string
  description = "Project name for resource naming"
  default     = "basilica-k3s"
}

variable "ssh_key_name" {
  type        = string
  description = "Name of existing AWS SSH key pair (leave empty to create new)"
  default     = ""
}

variable "ssh_public_key_path" {
  type        = string
  description = "Path to SSH public key file (used if ssh_key_name is empty)"
  default     = "~/.ssh/id_rsa.pub"
}

variable "allowed_ssh_cidr_blocks" {
  type        = list(string)
  description = "CIDR blocks allowed to SSH into K3s nodes"
  default     = ["0.0.0.0/0"]
}

variable "k3s_server_count" {
  type        = number
  description = "Number of K3s server (control plane) nodes - must be odd for etcd quorum (1, 3, 5, etc.)"
  default     = 3
  validation {
    condition     = var.k3s_server_count % 2 == 1 && var.k3s_server_count >= 1
    error_message = "Server count must be an odd number (1, 3, 5, etc.) for etcd quorum."
  }
}

variable "k3s_server_instance_type" {
  type        = string
  description = "EC2 instance type for K3s server (control plane)"
  default     = "t3.xlarge"
}

variable "k3s_agent_instance_type" {
  type        = string
  description = "EC2 instance type for K3s agent (worker) nodes"
  default     = "t3.medium"
}

variable "k3s_agent_count" {
  type        = number
  description = "Number of K3s agent (worker) nodes to provision"
  default     = 2
}

variable "k3s_server_root_volume_size" {
  type        = number
  description = "Root volume size in GB for K3s server"
  default     = 100
}

variable "k3s_agent_root_volume_size" {
  type        = number
  description = "Root volume size in GB for K3s agent nodes"
  default     = 100
}

variable "ubuntu_ami_owner" {
  type        = string
  description = "AMI owner for Ubuntu images (Canonical)"
  default     = "099720109477"
}

variable "ubuntu_version" {
  type        = string
  description = "Ubuntu version to use for K3s nodes"
  default     = "24.04"
}

variable "peer_vpc_id" {
  type        = string
  description = "ECS VPC ID for VPC peering (leave empty to skip peering)"
  default     = ""
}

variable "peer_vpc_cidr" {
  type        = string
  description = "ECS VPC CIDR block for VPC peering"
  default     = ""
}

variable "peer_route_table_id" {
  type        = string
  description = "ECS private route table ID for VPC peering (leave empty to skip route)"
  default     = ""
}

variable "deployment_public_ip" {
  type        = string
  description = "Public IP/DNS for user deployments (defaults to primary K3s server IP, update after Envoy LB is provisioned)"
  default     = ""
}
