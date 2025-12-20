resource "aws_vpc_peering_connection" "k3s_to_ecs" {
  count = var.peer_vpc_id != "" ? 1 : 0

  vpc_id      = var.vpc_id
  peer_vpc_id = var.peer_vpc_id
  auto_accept = true

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-k3s-to-ecs-peering"
    Side = "Requester"
  })
}

# Route from K3s VPC to ECS VPC (managed here)
resource "aws_route" "k3s_to_ecs" {
  count = var.peer_vpc_id != "" ? 1 : 0

  route_table_id            = var.k3s_route_table_id
  destination_cidr_block    = var.peer_vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.k3s_to_ecs[0].id
}

# DISABLED: Route from ECS to K3s is now managed by scripts/cloud/modules/networking
# via aws_route.private_to_k3s to ensure it survives ECS Terraform applies.
# To revert: uncomment the resource below and remove from scripts/cloud state
#
# resource "aws_route" "ecs_to_k3s" {
#   count = var.peer_vpc_id != "" && var.peer_route_table_id != "" ? 1 : 0
#
#   route_table_id            = var.peer_route_table_id
#   destination_cidr_block    = var.vpc_cidr
#   vpc_peering_connection_id = aws_vpc_peering_connection.k3s_to_ecs[0].id
# }
