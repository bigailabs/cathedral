resource "random_password" "k3s_token" {
  length  = 64
  special = false
}

resource "aws_security_group" "k3s_server" {
  name_prefix = "${var.name_prefix}-k3s-server"
  vpc_id      = var.vpc_id
  description = "Security group for K3s server (control plane) nodes"

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-k3s-server-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "k3s_server_ssh" {
  count             = length(var.allowed_ssh_cidr_blocks)
  security_group_id = aws_security_group.k3s_server.id
  description       = "SSH access from ${var.allowed_ssh_cidr_blocks[count.index]}"
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
  cidr_ipv4         = var.allowed_ssh_cidr_blocks[count.index]

  tags = {
    Name = "ssh-access-${count.index}"
  }
}

resource "aws_vpc_security_group_ingress_rule" "k3s_server_api" {
  count             = length(var.allowed_k8s_api_cidr_blocks)
  security_group_id = aws_security_group.k3s_server.id
  description       = "K3s API server from allowed CIDR ${var.allowed_k8s_api_cidr_blocks[count.index]}"
  ip_protocol       = "tcp"
  from_port         = 6443
  to_port           = 6443
  cidr_ipv4         = var.allowed_k8s_api_cidr_blocks[count.index]

  tags = {
    Name = "k3s-api-server-${count.index}"
  }
}

resource "aws_vpc_security_group_ingress_rule" "k3s_server_api_from_ecs" {
  count             = var.peer_vpc_cidr != "" ? 1 : 0
  security_group_id = aws_security_group.k3s_server.id
  description       = "K3s API server from ECS VPC (VPC peering)"
  ip_protocol       = "tcp"
  from_port         = 6443
  to_port           = 6443
  cidr_ipv4         = var.peer_vpc_cidr

  tags = {
    Name = "k3s-api-from-ecs"
  }
}

resource "aws_vpc_security_group_ingress_rule" "k3s_server_kubelet" {
  security_group_id = aws_security_group.k3s_server.id
  description       = "Kubelet metrics"
  ip_protocol       = "tcp"
  from_port         = 10250
  to_port           = 10250
  cidr_ipv4         = var.vpc_cidr

  tags = {
    Name = "kubelet-metrics"
  }
}

resource "aws_vpc_security_group_ingress_rule" "k3s_server_etcd" {
  security_group_id = aws_security_group.k3s_server.id
  description       = "etcd server-to-server communication"
  ip_protocol       = "tcp"
  from_port         = 2379
  to_port           = 2380
  cidr_ipv4         = var.vpc_cidr

  tags = {
    Name = "etcd-communication"
  }
}

resource "aws_vpc_security_group_ingress_rule" "k3s_server_vxlan" {
  security_group_id = aws_security_group.k3s_server.id
  description       = "Flannel VXLAN"
  ip_protocol       = "udp"
  from_port         = 8472
  to_port           = 8472
  cidr_ipv4         = var.vpc_cidr

  tags = {
    Name = "flannel-vxlan"
  }
}

resource "aws_vpc_security_group_ingress_rule" "k3s_server_wireguard" {
  security_group_id = aws_security_group.k3s_server.id
  description       = "Flannel WireGuard"
  ip_protocol       = "udp"
  from_port         = 51820
  to_port           = 51821
  cidr_ipv4         = var.vpc_cidr

  tags = {
    Name = "flannel-wireguard"
  }
}

resource "aws_vpc_security_group_ingress_rule" "k3s_server_internal" {
  security_group_id            = aws_security_group.k3s_server.id
  description                  = "All traffic between server nodes"
  ip_protocol                  = "-1"
  referenced_security_group_id = aws_security_group.k3s_server.id

  tags = {
    Name = "server-to-server"
  }
}

resource "aws_vpc_security_group_ingress_rule" "k3s_server_envoy_from_alb" {
  security_group_id            = aws_security_group.k3s_server.id
  description                  = "Envoy Gateway from deployments ALB"
  ip_protocol                  = "tcp"
  from_port                    = 30322
  to_port                      = 30322
  referenced_security_group_id = var.alb_security_group_id

  tags = {
    Name = "gateway-from-alb"
  }
}

resource "aws_vpc_security_group_egress_rule" "k3s_server_all" {
  security_group_id = aws_security_group.k3s_server.id
  description       = "Allow all outbound traffic"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"

  tags = {
    Name = "all-outbound"
  }
}

resource "aws_instance" "k3s_server" {
  count = var.server_count

  ami                    = var.ubuntu_ami_id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_ids[count.index % length(var.subnet_ids)]
  vpc_security_group_ids = [aws_security_group.k3s_server.id]
  key_name               = var.ssh_key_name

  root_block_device {
    volume_size           = var.root_volume_size
    volume_type           = "gp3"
    delete_on_termination = true
    encrypted             = true
  }

  user_data = count.index == 0 ? templatefile("${path.module}/user_data_primary.sh.tpl", {
    hostname  = "k3s-server-${count.index + 1}"
    k3s_token = random_password.k3s_token.result
    nlb_dns   = var.nlb_dns_name
    }) : templatefile("${path.module}/user_data_secondary.sh.tpl", {
    hostname   = "k3s-server-${count.index + 1}"
    k3s_token  = random_password.k3s_token.result
    nlb_dns    = var.nlb_dns_name
    server_url = "https://${var.nlb_dns_name}:6443"
  })

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-k3s-server-${count.index + 1}"
    Role = "k3s-server"
  })

  lifecycle {
    ignore_changes = [ami]
  }

  depends_on = [random_password.k3s_token]
}
