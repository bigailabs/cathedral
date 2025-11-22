locals {
  name_prefix = "${var.project_name}-${terraform.workspace}"

  availability_zones = slice(data.aws_availability_zones.available.names, 0, 3)

  common_tags = {
    Project     = var.project_name
    Environment = terraform.workspace
    ManagedBy   = "terraform"
    Component   = "k3s-cluster"
  }

  env_config = {
    dev = {
      vpc_cidr                    = "10.100.0.0/16"
      k3s_server_count            = 1
      k3s_server_instance_type    = "t3.medium"
      k3s_agent_instance_type     = "t3.small"
      k3s_agent_count             = 1
      k3s_server_root_volume_size = 50
      k3s_agent_root_volume_size  = 50
    }
    prod = {
      vpc_cidr                    = "10.101.0.0/16"
      k3s_server_count            = var.k3s_server_count
      k3s_server_instance_type    = var.k3s_server_instance_type
      k3s_agent_instance_type     = var.k3s_agent_instance_type
      k3s_agent_count             = var.k3s_agent_count
      k3s_server_root_volume_size = var.k3s_server_root_volume_size
      k3s_agent_root_volume_size  = var.k3s_agent_root_volume_size
    }
  }

  workspace_config = lookup(local.env_config, terraform.workspace, local.env_config["dev"])

  ssh_key_file = var.ssh_key_name == "" ? abspath("${path.module}/k3s-ssh-key.pem") : "~/.ssh/${var.ssh_key_name}.pem"
}
