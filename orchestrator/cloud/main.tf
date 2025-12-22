data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = [var.ubuntu_ami_owner]

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-${var.ubuntu_version}-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

resource "tls_private_key" "ssh" {
  count     = var.ssh_key_name == "" ? 1 : 0
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "aws_key_pair" "k3s" {
  count      = var.ssh_key_name == "" ? 1 : 0
  key_name   = "${local.name_prefix}-k3s-key"
  public_key = var.ssh_key_name == "" ? tls_private_key.ssh[0].public_key_openssh : file(pathexpand(var.ssh_public_key_path))

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-k3s-key"
  })
}

resource "local_file" "private_key" {
  count           = var.ssh_key_name == "" ? 1 : 0
  content         = tls_private_key.ssh[0].private_key_pem
  filename        = "${path.module}/k3s-ssh-key.pem"
  file_permission = "0600"
}

module "networking" {
  source = "./modules/networking"

  name_prefix        = local.name_prefix
  vpc_cidr           = local.workspace_config.vpc_cidr
  availability_zones = local.availability_zones

  tags = local.common_tags
}

module "k3s_nlb" {
  source = "./modules/k3s-nlb"

  name_prefix = local.name_prefix
  vpc_id      = module.networking.vpc_id
  subnet_ids  = module.networking.public_subnet_ids

  tags = local.common_tags

  depends_on = [module.networking]
}

module "deployments_alb" {
  source = "./modules/deployments-alb"

  name_prefix     = local.name_prefix
  vpc_id          = module.networking.vpc_id
  subnet_ids      = module.networking.public_subnet_ids
  enable_https    = var.deployments_alb_enable_https
  certificate_arn = var.deployments_alb_certificate_arn

  tags = local.common_tags

  depends_on = [module.networking]
}

module "k3s_servers" {
  source = "./modules/k3s-servers"

  name_prefix                  = local.name_prefix
  vpc_id                       = module.networking.vpc_id
  vpc_cidr                     = module.networking.vpc_cidr
  subnet_ids                   = module.networking.public_subnet_ids
  ssh_key_name                 = var.ssh_key_name == "" ? aws_key_pair.k3s[0].key_name : var.ssh_key_name
  instance_type                = local.workspace_config.k3s_server_instance_type
  server_count                 = local.workspace_config.k3s_server_count
  root_volume_size             = local.workspace_config.k3s_server_root_volume_size
  ubuntu_ami_id                = data.aws_ami.ubuntu.id
  allowed_ssh_cidr_blocks      = var.allowed_ssh_cidr_blocks
  allowed_k8s_api_cidr_blocks  = var.allowed_k8s_api_cidr_blocks
  nlb_dns_name                 = module.k3s_nlb.nlb_dns_name
  peer_vpc_cidr                = var.peer_vpc_cidr
  alb_security_group_id        = module.deployments_alb.alb_security_group_id

  tags = local.common_tags

  depends_on = [module.networking, module.k3s_nlb, module.deployments_alb]
}

resource "aws_lb_target_group_attachment" "k3s_servers" {
  count = local.workspace_config.k3s_server_count

  target_group_arn = module.k3s_nlb.target_group_arn
  target_id        = module.k3s_servers.instance_ids[count.index]
  port             = 6443

  depends_on = [module.k3s_servers, module.k3s_nlb]
}

resource "aws_lb_target_group_attachment" "envoy_on_k3s_servers" {
  count = local.workspace_config.k3s_server_count

  target_group_arn = module.deployments_alb.target_group_arn
  target_id        = module.k3s_servers.instance_ids[count.index]
  port             = 30322

  depends_on = [module.k3s_servers, module.deployments_alb]
}

module "k3s_agents" {
  source = "./modules/k3s-agents"

  name_prefix                  = local.name_prefix
  vpc_id                       = module.networking.vpc_id
  vpc_cidr                     = module.networking.vpc_cidr
  subnet_ids                   = module.networking.public_subnet_ids
  ssh_key_name                 = var.ssh_key_name == "" ? aws_key_pair.k3s[0].key_name : var.ssh_key_name
  instance_type                = local.workspace_config.k3s_agent_instance_type
  agent_count                  = local.workspace_config.k3s_agent_count
  root_volume_size             = local.workspace_config.k3s_agent_root_volume_size
  ubuntu_ami_id                = data.aws_ami.ubuntu.id
  allowed_ssh_cidr_blocks      = var.allowed_ssh_cidr_blocks
  k3s_server_security_group_id = module.k3s_servers.security_group_id
  nlb_dns_name                 = module.k3s_nlb.nlb_dns_name
  k3s_token                    = module.k3s_servers.k3s_token

  tags = local.common_tags

  depends_on = [module.networking, module.k3s_servers, module.k3s_nlb]
}

module "vpc_peering" {
  source = "./modules/vpc-peering"

  name_prefix         = local.name_prefix
  vpc_id              = module.networking.vpc_id
  vpc_cidr            = module.networking.vpc_cidr
  k3s_route_table_id  = module.networking.public_route_table_id
  peer_vpc_id         = var.peer_vpc_id
  peer_vpc_cidr       = var.peer_vpc_cidr
  peer_route_table_id = var.peer_route_table_id

  tags = local.common_tags

  depends_on = [module.networking]
}

resource "local_file" "ansible_inventory" {
  content = templatefile("${path.module}/templates/inventory.tpl", {
    k3s_server_public_ips  = module.k3s_servers.public_ips
    k3s_server_private_ips = module.k3s_servers.private_ips
    k3s_agent_public_ips   = module.k3s_agents.public_ips
    k3s_agent_private_ips  = module.k3s_agents.private_ips
    ssh_user               = "ubuntu"
    ssh_key_file           = local.ssh_key_file
    deployment_public_ip   = var.deployment_public_ip != "" ? var.deployment_public_ip : module.k3s_servers.primary_server_public_ip
    nlb_dns_name           = module.k3s_nlb.nlb_dns_name
  })
  filename        = "${path.module}/../ansible/inventories/production.ini"
  file_permission = "0644"

  depends_on = [module.k3s_servers, module.k3s_agents]
}
