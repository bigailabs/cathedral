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

variable "hyperstack_api_key" {
  type        = string
  description = "API key for Hyperstack GPU provider"
  sensitive   = true
  default     = ""
}

variable "hyperstack_webhook_secret" {
  type        = string
  description = "Webhook secret token for Hyperstack callbacks (must be URL-safe: A-Z a-z 0-9 - _ . ~)"
  sensitive   = true
  default     = ""
}

variable "hyperstack_callback_base_url" {
  type        = string
  description = "Base URL for Hyperstack webhooks (e.g., https://api.basilica.ai)"
  default     = ""
}

variable "hyperstack_rate_limit_rps" {
  type        = number
  description = "Rate limit for Hyperstack API requests per second (default: 10)"
  default     = 10
}

variable "hyperstack_token_timeout_secs" {
  type        = number
  description = "Timeout in seconds waiting for a rate limit token (default: 300)"
  default     = 300
}

# WireGuard VPN Configuration
variable "wireguard_enabled" {
  type        = bool
  description = "Enable WireGuard VPN for remote GPU nodes"
  default     = false
}

variable "wireguard_servers" {
  type = list(object({
    endpoint     = string # public_ip:port
    public_key   = string # WireGuard public key (base64)
    wireguard_ip = string # e.g., 10.200.0.1
    vpc_subnet   = string # e.g., 10.101.0.0/24
  }))
  description = "List of K3s servers with their WireGuard configurations"
  default     = []
}

# VPC Peering Configuration for K3s cluster connectivity
variable "k3s_vpc_peering_connection_id" {
  type        = string
  description = "VPC peering connection ID for K3s cluster (created by orchestrator/cloud). Required for SSH connectivity from ECS API to K3s servers."
  default     = ""
}

variable "k3s_vpc_cidr" {
  type        = string
  description = "CIDR block of the K3s VPC (e.g., 10.101.0.0/16). Required for routing traffic via VPC peering."
  default     = ""
}

# =============================================================================
# VIP (Managed Machines) Configuration
# =============================================================================

variable "vip_s3_bucket" {
  type        = string
  description = "S3 bucket name containing VIP machines CSV file"
  default     = ""
}

variable "vip_s3_key" {
  type        = string
  description = "S3 object key for VIP machines CSV file (e.g., 'vip/machines.csv')"
  default     = ""
}

variable "vip_s3_region" {
  type        = string
  description = "AWS region for VIP S3 bucket (defaults to aws_region if not specified)"
  default     = ""
}

variable "vip_poll_interval_secs" {
  type        = number
  description = "Polling interval in seconds for VIP machine sync (default: 60)"
  default     = 60
}
