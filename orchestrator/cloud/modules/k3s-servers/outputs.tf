output "security_group_id" {
  description = "Security group ID for K3s server nodes"
  value       = aws_security_group.k3s_server.id
}

output "instance_ids" {
  description = "List of K3s server instance IDs"
  value       = aws_instance.k3s_server[*].id
}

output "private_ips" {
  description = "List of K3s server private IP addresses"
  value       = aws_instance.k3s_server[*].private_ip
}

output "public_ips" {
  description = "List of K3s server public IP addresses"
  value       = aws_instance.k3s_server[*].public_ip
}

output "primary_server_private_ip" {
  description = "Primary K3s server private IP (for initial cluster setup)"
  value       = aws_instance.k3s_server[0].private_ip
}

output "primary_server_public_ip" {
  description = "Primary K3s server public IP (for initial cluster setup)"
  value       = aws_instance.k3s_server[0].public_ip
}

output "primary_server_id" {
  description = "Primary K3s server instance ID"
  value       = aws_instance.k3s_server[0].id
}

output "k3s_token" {
  description = "K3s cluster token for server and agent joining"
  value       = random_password.k3s_token.result
  sensitive   = true
}
