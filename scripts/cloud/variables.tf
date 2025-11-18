variable "billing_image" {
  type = string
}

variable "payments_image" {
  type = string
}

variable "aws_region" {
  type    = string
  default = "us-east-2"
}

variable "project_name" {
  type    = string
  default = "basilica"
}

variable "certificate_arn" {
  type    = string
  default = null
}

variable "basilica_api_image" {
  type = string
}

variable "basilica_api_validator_hotkey" {
  type = string
}

variable "basilica_api_network" {
  type    = string
  default = "finney"
}

variable "basilica_api_netuid" {
  type    = number
  default = 39
}

variable "basilica_auth0_domain" {
  type    = string
  default = "your-auth0-domain"
}

variable "basilica_auth0_client_id" {
  type    = string
  default = "your-auth0-client-id"
}

variable "basilica_auth0_audience" {
  type    = string
  default = "your-auth0-audience"
}

variable "basilica_auth0_issuer" {
  type    = string
  default = "your-auth0-issuer"
}

variable "validator_allowed_ips" {
  type        = list(string)
  description = "List of validator IP addresses/CIDR blocks allowed to access billing gRPC endpoint"
  default     = []
}

variable "route53_zone_id" {
  type        = string
  description = "Route53 hosted zone ID for DNS records (optional, leave empty to skip DNS management)"
  default     = ""
}

variable "payments_reconciliation_coldwallet_address" {
  type        = string
  description = "SS58 address of the cold wallet for hotwallet reconciliation sweeps"
  default     = ""
}

variable "payments_reconciliation_enabled" {
  type        = bool
  description = "Enable automatic hotwallet-to-coldwallet reconciliation sweeps"
  default     = false
}

variable "payments_reconciliation_dry_run" {
  type        = bool
  description = "Run reconciliation in dry-run mode (no actual transfers)"
  default     = true
}

variable "marketplace_api_key" {
  type        = string
  description = "API key for Shadeform marketplace pricing API"
  sensitive   = true
}

variable "payments_blockchain_websocket_url" {
  type        = string
  description = "WebSocket URL for blockchain connectivity (payments service)"
  default     = "wss://entrypoint-finney.opentensor.ai:443"
}

variable "kubeconfig_path" {
  type        = string
  description = "Path to kubeconfig file for K3s cluster connection (e.g., ~/.kube/k3s-basilica-config). Leave empty to manually upload kubeconfig to AWS Secrets Manager."
  default     = ""
}

variable "k3s_server_url" {
  type        = string
  description = "K3S server URL for interacting with the cluster"
}

variable "k3s_ssh_enabled" {
  type        = string
  description = "Enable SSH-based K3s token generation"
  default     = "true"
}

variable "k3s_ssh_servers" {
  type        = string
  description = "Comma-separated list of K3s server IPs with optional ports (e.g., 10.101.0.10:22,10.101.0.11:22)"
  default     = ""
}

variable "k3s_ssh_username" {
  type        = string
  description = "SSH username for K3s servers"
  default     = ""
}

variable "k3s_ssh_key_path" {
  type        = string
  description = "Path to SSH private key for K3s servers"
  default     = ""
}

variable "cloudflare_api_token" {
  type        = string
  description = "API token for Cloudflare"
  sensitive   = true
}

variable "cloudflare_zone_id" {
  type        = string
  description = "Cloudflare zone ID for deployments.basilica.ai"
}

variable "cloudflare_domain" {
  type        = string
  description = "Cloudflare domain for deployments.basilica.ai"
}

variable "deployments_alb_dns_name" {
  type        = string
  description = "DNS name of the ALB for deployments.basilica.ai"
}
