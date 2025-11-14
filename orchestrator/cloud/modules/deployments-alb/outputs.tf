output "alb_dns_name" {
  description = "DNS name of the ALB"
  value       = aws_lb.deployments.dns_name
}

output "alb_arn" {
  description = "ARN of the ALB"
  value       = aws_lb.deployments.arn
}

output "alb_zone_id" {
  description = "Zone ID of the ALB for Route53"
  value       = aws_lb.deployments.zone_id
}

output "target_group_arn" {
  description = "ARN of the target group"
  value       = aws_lb_target_group.envoy.arn
}

output "alb_security_group_id" {
  description = "Security group ID of the ALB"
  value       = aws_security_group.alb.id
}
