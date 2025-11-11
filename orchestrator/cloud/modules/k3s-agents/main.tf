resource "aws_security_group" "k3s_agent" {
  name_prefix = "${var.name_prefix}-k3s-agent"
  vpc_id      = var.vpc_id
  description = "Security group for K3s agent (worker) nodes"

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-k3s-agent-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "k3s_agent_ssh" {
  security_group_id = aws_security_group.k3s_agent.id
  description       = "SSH access"
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
  cidr_ipv4         = var.allowed_ssh_cidr_blocks[0]

  tags = {
    Name = "ssh-access"
  }
}

resource "aws_vpc_security_group_ingress_rule" "k3s_agent_kubelet" {
  security_group_id = aws_security_group.k3s_agent.id
  description       = "Kubelet metrics"
  ip_protocol       = "tcp"
  from_port         = 10250
  to_port           = 10250
  cidr_ipv4         = var.vpc_cidr

  tags = {
    Name = "kubelet-metrics"
  }
}

resource "aws_vpc_security_group_ingress_rule" "k3s_agent_vxlan" {
  security_group_id = aws_security_group.k3s_agent.id
  description       = "Flannel VXLAN"
  ip_protocol       = "udp"
  from_port         = 8472
  to_port           = 8472
  cidr_ipv4         = var.vpc_cidr

  tags = {
    Name = "flannel-vxlan"
  }
}

resource "aws_vpc_security_group_ingress_rule" "k3s_agent_wireguard" {
  security_group_id = aws_security_group.k3s_agent.id
  description       = "Flannel WireGuard"
  ip_protocol       = "udp"
  from_port         = 51820
  to_port           = 51821
  cidr_ipv4         = var.vpc_cidr

  tags = {
    Name = "flannel-wireguard"
  }
}

resource "aws_vpc_security_group_ingress_rule" "k3s_agent_from_server" {
  security_group_id            = aws_security_group.k3s_agent.id
  description                  = "All traffic from K3s server nodes"
  ip_protocol                  = "-1"
  referenced_security_group_id = var.k3s_server_security_group_id

  tags = {
    Name = "from-k3s-server"
  }
}

resource "aws_vpc_security_group_ingress_rule" "k3s_agent_internal" {
  security_group_id            = aws_security_group.k3s_agent.id
  description                  = "All traffic between agent nodes"
  ip_protocol                  = "-1"
  referenced_security_group_id = aws_security_group.k3s_agent.id

  tags = {
    Name = "agent-to-agent"
  }
}

resource "aws_vpc_security_group_egress_rule" "k3s_agent_all" {
  security_group_id = aws_security_group.k3s_agent.id
  description       = "Allow all outbound traffic"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"

  tags = {
    Name = "all-outbound"
  }
}

resource "aws_instance" "k3s_agent" {
  count = var.agent_count

  ami                    = var.ubuntu_ami_id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_ids[count.index % length(var.subnet_ids)]
  vpc_security_group_ids = [aws_security_group.k3s_agent.id]
  key_name               = var.ssh_key_name

  root_block_device {
    volume_size           = var.root_volume_size
    volume_type           = "gp3"
    delete_on_termination = true
    encrypted             = true
  }

  user_data = templatefile("${path.module}/user_data_agent.sh.tpl", {
    hostname   = "k3s-agent-${count.index + 1}"
    server_url = "https://${var.nlb_dns_name}:6443"
    k3s_token  = var.k3s_token
  })

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-k3s-agent-${count.index + 1}"
    Role = "k3s-agent"
  })

  lifecycle {
    ignore_changes = [ami]
  }
}
