output "peering_connection_id" {
  description = "ID of the VPC peering connection"
  value       = var.peer_vpc_id != "" ? aws_vpc_peering_connection.k3s_to_ecs[0].id : ""
}

output "peering_status" {
  description = "Status of the VPC peering connection"
  value       = var.peer_vpc_id != "" ? aws_vpc_peering_connection.k3s_to_ecs[0].accept_status : "not_created"
}
