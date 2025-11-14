output "security_group_id" {
  description = "Security group ID for K3s agent nodes"
  value       = aws_security_group.k3s_agent.id
}

output "instance_ids" {
  description = "List of K3s agent instance IDs"
  value       = aws_instance.k3s_agent[*].id
}

output "private_ips" {
  description = "List of K3s agent private IP addresses"
  value       = aws_instance.k3s_agent[*].private_ip
}

output "public_ips" {
  description = "List of K3s agent public IP addresses"
  value       = aws_instance.k3s_agent[*].public_ip
}
