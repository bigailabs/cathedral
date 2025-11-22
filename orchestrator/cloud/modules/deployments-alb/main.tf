# Application Load Balancer for public deployments
resource "aws_lb" "deployments" {
  name               = "${var.name_prefix}-deploy-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.subnet_ids

  enable_deletion_protection       = false
  enable_http2                     = true
  enable_cross_zone_load_balancing = true

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-deploy-alb"
  })
}

# Target group for Envoy Gateway on K3s nodes
resource "aws_lb_target_group" "envoy" {
  name     = "${var.name_prefix}-envoygw-tg"
  port     = 30322
  protocol = "HTTP"
  vpc_id   = var.vpc_id

  health_check {
    enabled             = true
    path                = "/"
    port                = "traffic-port"
    protocol            = "HTTP"
    healthy_threshold   = 2
    unhealthy_threshold = 2
    timeout             = 5
    interval            = 30
    matcher             = "200-499"
  }

  deregistration_delay = 30

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-envoy-tg"
  })
}

# HTTP listener - forward to target group (for Cloudflare Flexible mode)
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.deployments.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.envoy.arn
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-http-listener"
  })
}

# HTTPS listener removed - not needed with Cloudflare Flexible mode
# Cloudflare connects to ALB via HTTP (port 80) only
# If direct HTTPS access to ALB is needed, add an HTTPS listener with a certificate

# Security group for ALB
resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-deploy-alb-sg"
  description = "Security group for deployments ALB"
  vpc_id      = var.vpc_id

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-deploy-alb-sg"
  })
}

# Allow HTTP from internet
resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from internet"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  cidr_ipv4         = "0.0.0.0/0"

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-alb-http-ingress"
  })
}

# Allow HTTPS from internet
resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from internet"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  cidr_ipv4         = "0.0.0.0/0"

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-alb-https-ingress"
  })
}

# Allow all outbound traffic
resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.alb.id
  description       = "Allow all outbound"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-alb-egress"
  })
}
