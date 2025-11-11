# Basilica K3s Infrastructure - Terraform

Production-ready Terraform configuration for provisioning AWS infrastructure for K3s clusters that integrate seamlessly with Ansible deployment.

## Overview

This Terraform configuration creates a complete AWS infrastructure for running K3s clusters with proper networking, security groups, and instance configuration. It automatically generates Ansible inventory files for seamless integration with the Ansible deployment playbooks in `../ansible/`.

### What This Provisions

- **3-Tier VPC Architecture** - Public, Private, and Database subnets across 3 availability zones
- **Network Load Balancer** - Internet-facing NLB for K3s API high availability
- **NAT Gateway** - Stable outbound IP for private subnet traffic
- **EC2 Instances** - K3s server and agent nodes in private subnets
- **Security Groups** - Production-grade firewall rules with VPC peering support
- **VPC Peering** - Optional connectivity to ECS services VPC
- **SSH Key Pair** - Optional SSH key generation or use existing keys
- **Ansible Integration** - Auto-generated inventory files for Ansible deployment

### Architecture

**Production-Ready HA Architecture - 3 Server Nodes Across 3 AZs:**

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ VPC (10.101.0.0/16)                                                         │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ Public Subnets (3 AZs) - 10.101.0.0/24, 10.101.1.0/24, 10.101.2.0/24│  │
│  │                                                                        │  │
│  │  ┌─────────────────────────────────────────────────────────────────┐ │  │
│  │  │ Network Load Balancer (Internet-Facing)                         │ │  │
│  │  │ DNS: xxx.elb.us-east-2.amazonaws.com:6443                      │ │  │
│  │  │ Distributes traffic to K3s API servers across 3 AZs            │ │  │
│  │  └────────────────────────┬────────────────────────────────────────┘ │  │
│  │                            │                                          │  │
│  │  ┌───────────┐             │                                          │  │
│  │  │NAT Gateway│             │                                          │  │
│  │  │ (Elastic  │             │                                          │  │
│  │  │  IP: xxx) │             │                                          │  │
│  │  └─────┬─────┘             │                                          │  │
│  └────────┼───────────────────┼──────────────────────────────────────────┘  │
│           │                   │                                              │
│  ┌────────▼───────────────────▼──────────────────────────────────────────┐  │
│  │ Private Subnets (3 AZs) - 10.101.10.0/24, .11.0/24, .12.0/24        │  │
│  │                                                                        │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │  │
│  │  │ K3s Server 1 │  │ K3s Server 2 │  │ K3s Server 3 │               │  │
│  │  │ AZ1          │  │ AZ2          │  │ AZ3          │               │  │
│  │  │ (t3.xlarge)  │  │ (t3.xlarge)  │  │ (t3.xlarge)  │               │  │
│  │  │ Primary      │◄─┤ etcd quorum  │◄─┤ etcd quorum  │               │  │
│  │  │ --cluster-   │  │ --server     │  │ --server     │               │  │
│  │  │   init       │  │  <nlb-dns>   │  │  <nlb-dns>   │               │  │
│  │  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘               │  │
│  │         │                 │                 │                        │  │
│  │         └─────────────────┴─────────────────┘                        │  │
│  │                           │                                           │  │
│  │         ┌─────────────────┴─────────────────┐                        │  │
│  │         │                                     │                        │  │
│  │  ┌──────▼─────┐                      ┌──────▼─────┐                  │  │
│  │  │ K3s Agent 1│                      │ K3s Agent 2│                  │  │
│  │  │ AZ1        │                      │ AZ2        │                  │  │
│  │  │ (t3.medium)│                      │ (t3.medium)│                  │  │
│  │  └────────────┘                      └────────────┘                  │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │ Database Subnets (3 AZs) - 10.101.20.0/24, .21.0/24, .22.0/24        │  │
│  │ Reserved for future RDS/database deployments                          │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  Internet Gateway ◄────┐                                                     │
│                        │                                                     │
│  VPC Peering ────────► ECS Services VPC (scripts/cloud)                     │
│  (Optional)            - API, Billing, Payments services                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Key Features:**
- **3-Tier Networking**: Public (NLB, NAT, K3s nodes), Private (reserved), Database subnets
- **Multi-AZ Deployment**: Servers and agents distributed across 3 availability zones
- **High Availability**: NLB + 3 server nodes with embedded etcd quorum
- **Direct SSH Access**: All K3s nodes have public IPs for direct SSH access via `connect.sh`
- **Stable Outbound IP**: NAT Gateway with Elastic IP for external service whitelisting
- **Server Discovery**: Automatic via NLB DNS and shared cluster token
- **Agent Discovery**: K3s client-side load balancing via NLB
- **VPC Peering**: Optional connectivity to ECS services for Basilica API integration
- **Cross-Zone Load Balancing**: NLB distributes API traffic evenly across AZs
- **Security**: Security group rules control SSH access, VPC peering, and internal communication

## Prerequisites

### Control Machine Requirements

- Terraform >= 1.5.0
- AWS CLI configured with named profile (`aws configure --profile <profile-name>`)
- SSH access capabilities

### AWS Requirements

- AWS Account with sufficient permissions
- EC2, VPC, and IAM permissions
- Optionally: existing SSH key pair in AWS

## Quick Start

### 1. Configure AWS Profile

```bash
# Configure AWS credentials with a named profile
aws configure --profile tplr

# Verify credentials
aws sts get-caller-identity --profile tplr
```

### 2. Initial Setup

```bash
cd orchestrator/cloud

# Copy example configuration
cp terraform.tfvars.example terraform.tfvars

# Edit configuration
vim terraform.tfvars
```

### 3. Deploy Infrastructure

Using the run.sh helper script (recommended):

```bash
# Plan deployment
./run.sh tplr prod plan

# Deploy to production (with confirmation)
./run.sh tplr prod apply

# Or force apply without confirmation (CI/CD)
./run.sh tplr prod apply-f

# Deploy to dev environment
./run.sh tplr dev apply
```

Manual deployment (alternative):

```bash
# Export AWS profile
export AWS_PROFILE=tplr

# Initialize Terraform
terraform init
terraform workspace select prod || terraform workspace new prod

# Plan deployment
terraform plan

# Deploy
terraform apply
```

### 4. Verify Infrastructure

```bash
# List all nodes
./connect.sh

# SSH to a specific node
./connect.sh k3s-server-1
./connect.sh server-1    # Short format
./connect.sh agent-2     # Agent node
```

### 5. Deploy K3s with Ansible

The Terraform deployment automatically creates an Ansible inventory file. Use it to deploy K3s:

```bash
cd ../ansible

# Deploy K3s cluster
ansible-playbook -i inventories/production.ini playbooks/01-setup/k3s-cluster.yml

# Verify deployment
ansible-playbook -i inventories/production.ini playbooks/03-verify/cluster-health.yml
```

## Configuration

### Required Variables

Edit `terraform.tfvars`:

```hcl
# AWS Configuration
aws_region   = "us-east-2"
project_name = "basilica-k3s"

# SSH Configuration (choose one)
# Option 1: Use existing key
ssh_key_name = "my-existing-key"

# Option 2: Generate new key (recommended)
ssh_key_name = ""
```

### Optional Variables

```hcl
# Security
allowed_ssh_cidr_blocks = ["203.0.113.10/32"]  # Restrict SSH access

# High Availability Configuration
k3s_server_count = 3  # Must be odd (1, 3, 5, etc.) for etcd quorum

# Instance Types
k3s_server_instance_type = "t3.xlarge"  # 4 vCPU, 16GB RAM
k3s_agent_instance_type  = "t3.medium"  # 2 vCPU, 4GB RAM
k3s_agent_count          = 2

# Storage
k3s_server_root_volume_size = 100  # GB
k3s_agent_root_volume_size  = 100  # GB

# VPC Peering (Optional - for ECS connectivity)
peer_vpc_id           = "vpc-xxxxx"         # ECS VPC ID
peer_vpc_cidr         = "10.0.0.0/16"       # ECS VPC CIDR
peer_route_table_id   = "rtb-xxxxx"         # ECS private route table
```

## Security Groups

### K3s Server Security Group

**Inbound Rules:**
- TCP 22 (SSH) - From allowed CIDR blocks
- TCP 6443 (K3s API) - From 0.0.0.0/0
- TCP 10250 (Kubelet) - From VPC CIDR
- TCP 2379-2380 (etcd) - From VPC CIDR (for HA cluster communication)
- UDP 8472 (Flannel VXLAN) - From VPC CIDR
- UDP 51820-51821 (Flannel WireGuard) - From VPC CIDR
- All traffic - From server security group (server-to-server communication)

**Outbound Rules:**
- All traffic - To 0.0.0.0/0

### K3s Agent Security Group

**Inbound Rules:**
- TCP 22 (SSH) - From allowed CIDR blocks
- TCP 10250 (Kubelet) - From VPC CIDR
- UDP 8472 (Flannel VXLAN) - From VPC CIDR
- UDP 51820-51821 (Flannel WireGuard) - From VPC CIDR
- All traffic - From server security group
- All traffic - From agent security group (inter-agent communication)

**Outbound Rules:**
- All traffic - To 0.0.0.0/0

## Instance Configuration

### Default Instance Types

| Node Type | Instance Type | vCPU | RAM | Storage | Cost/Month (est.) |
|-----------|---------------|------|-----|---------|-------------------|
| Server    | t3.xlarge     | 4    | 16GB| 100GB   | ~$120-140 each    |
| Agent     | t3.medium     | 2    | 4GB | 100GB   | ~$30-35 each      |

### Total Monthly Cost Estimate

**Production (HA Mode):**
- 3 Servers (t3.xlarge): ~$390
- 2 Agents (t3.medium): ~$65
- Network Load Balancer: ~$18
- NAT Gateway: ~$32 (plus data transfer)
- **Total: ~$505/month** (plus data transfer costs)

**Development (Single Server):**
- 1 Server (t3.medium): ~$30
- 1 Agent (t3.small): ~$15
- NAT Gateway: ~$32 (plus data transfer)
- **Total: ~$77/month** (plus data transfer costs)

**Note**: NAT Gateway data transfer pricing:
- First 1 GB/month: Free
- 1 GB - 10 TB: $0.045/GB
- Estimate ~$15-30/month for typical workloads

## Outputs

### Terraform Outputs

```bash
# View all outputs
terraform output

# View specific output
terraform output k3s_server_public_ip
terraform output ssh_connection_commands
```

### Key Outputs

- `nlb_dns_name` - Network Load Balancer DNS name for K3s API
- `k3s_api_endpoint` - Full K3s API endpoint URL (https://nlb-dns:6443)
- `nat_gateway_public_ip` - Stable outbound IP address (for whitelisting)
- `vpc_peering_connection_id` - VPC peering connection ID (if configured)
- `k3s_server_private_ips` - List of server private IP addresses
- `k3s_agent_private_ips` - List of agent private IPs
- `cluster_info` - Cluster configuration (server count, HA status, etc.)
- `ansible_inventory_file` - Path to generated Ansible inventory

### Generated Files

- `../ansible/inventories/production.ini` - Ansible inventory (auto-generated with all server and agent nodes)
- `k3s-ssh-key.pem` - SSH private key (if generated by Terraform)

## SSH Access

**Important**: All K3s nodes are deployed in **private subnets** and do **not** have public IP addresses. You have several options for accessing nodes:

### Option 1: AWS Systems Manager (SSM) Session Manager (Recommended)

```bash
# Connect to server node
aws ssm start-session --target <instance-id>

# List instances
aws ec2 describe-instances --filters "Name=tag:Role,Values=k3s-server" --query "Reservations[*].Instances[*].[InstanceId,PrivateIpAddress,Tags[?Key=='Name'].Value|[0]]" --output table
```

### Option 2: Bastion Host

Deploy a bastion host in the public subnet and use it as a jump host:

```bash
# SSH through bastion
ssh -i key.pem -J ubuntu@<bastion-ip> ubuntu@<private-node-ip>
```

### Option 3: VPN Connection

Set up AWS Client VPN or Site-to-Site VPN to access private subnets directly.

## Ansible Integration

### Auto-Generated Inventory

Terraform automatically creates `../ansible/inventories/production.ini`:

```ini
[k3s_server]
server1 ansible_host=<public-ip-1> ansible_user=ubuntu ansible_ssh_private_key_file=<key-path> ansible_become=true server_private_ip=<private-ip-1>
server2 ansible_host=<public-ip-2> ansible_user=ubuntu ansible_ssh_private_key_file=<key-path> ansible_become=true server_private_ip=<private-ip-2>
server3 ansible_host=<public-ip-3> ansible_user=ubuntu ansible_ssh_private_key_file=<key-path> ansible_become=true server_private_ip=<private-ip-3>

[k3s_agents]
agent1 ansible_host=<public-ip> ansible_user=ubuntu ansible_ssh_private_key_file=<key-path> ansible_become=true agent_private_ip=<private-ip>
agent2 ansible_host=<public-ip> ansible_user=ubuntu ansible_ssh_private_key_file=<key-path> ansible_become=true agent_private_ip=<private-ip>

[k3s_cluster:children]
k3s_server
k3s_agents
```

**Note**: The inventory dynamically adjusts based on `k3s_server_count` and `k3s_agent_count` variables.

### Deployment Workflow

```bash
# 1. Provision infrastructure
cd orchestrator/cloud
./run.sh tplr prod apply-f

# 2. Wait for instances to initialize (~2 minutes)
sleep 120

# 3. Deploy K3s
cd ../ansible
ansible-playbook -i inventories/production.ini playbooks/01-setup/k3s-cluster.yml

# 4. Verify cluster
ansible-playbook -i inventories/production.ini playbooks/03-verify/cluster-health.yml
```

## Module Structure

### Networking Module (`modules/networking/`)

Creates production-ready 3-tier VPC architecture with NAT Gateway.

**Resources:**
- VPC with DNS support (10.101.0.0/16)
- Public subnets (3 AZs) - offset 0
- Private subnets (3 AZs) - offset 10
- Database subnets (3 AZs) - offset 20
- Internet gateway
- NAT Gateway with Elastic IP
- Public and private route tables

### K3s NLB Module (`modules/k3s-nlb/`)

Network Load Balancer for K3s API high availability.

**Resources:**
- Internet-facing Network Load Balancer
- Target group for K3s API (port 6443)
- TCP listener on port 6443
- Cross-zone load balancing enabled

### K3s Servers Module (`modules/k3s-servers/`)

Provisions EC2 instances for K3s control plane with HA support in private subnets.

**Resources:**
- K3s server instances (1, 3, 5, or more) distributed across AZs
- Server security group with etcd ports and VPC peering rules
- Random token generation for cluster authentication
- User data templates for primary (--cluster-init) and secondary (--server) servers
- EBS volumes (GP3 encrypted)

### K3s Agents Module (`modules/k3s-agents/`)

Provisions EC2 instances for K3s worker nodes in private subnets.

**Resources:**
- K3s agent instances distributed across AZs
- Agent security group
- User data template for joining cluster via NLB
- EBS volumes (GP3 encrypted)

### VPC Peering Module (`modules/vpc-peering/`)

Optional VPC peering for ECS connectivity.

**Resources:**
- VPC peering connection (auto-accept)
- Bidirectional routes in both VPCs
- Conditional creation based on peer_vpc_id variable

## Workspaces

### Development Environment

```bash
./run.sh tplr dev apply
```

**Dev Configuration:**
- VPC CIDR: 10.100.0.0/16
- Servers: 1x t3.medium (2 vCPU, 4GB RAM) - single server, no HA
- Agents: 1x t3.small (2 vCPU, 2GB RAM)
- Storage: 50GB per node

### Production Environment

```bash
./run.sh tplr prod apply
```

**Prod Configuration:**
- VPC CIDR: 10.101.0.0/16
- Servers: 3x t3.xlarge (4 vCPU, 16GB RAM) - HA with etcd quorum
- Agents: 2x t3.medium (2 vCPU, 4GB RAM)
- Storage: 100GB per node

## Troubleshooting

### Infrastructure Issues

**Problem: SSH connection refused**
```bash
# Check instance status
aws ec2 describe-instance-status --instance-ids <instance-id>

# Wait for instance initialization
watch -n 5 'aws ec2 describe-instance-status --instance-ids <instance-id> --query "InstanceStatuses[0].InstanceStatus.Status" --output text'
```

**Problem: Security group blocking connections**
```bash
# Verify security group rules
aws ec2 describe-security-groups --group-ids <sg-id>

# Check if rules allow required ports
terraform plan  # Should show no changes if everything is correct
```

**Problem: Ansible inventory not found**
```bash
# Regenerate inventory
cd orchestrator/cloud
terraform refresh
terraform output ansible_inventory_file
```

### K3s Deployment Issues

See `../ansible/README.md` for Ansible-specific troubleshooting.

## Cost Optimization

### Reduce Costs

1. **Use Spot Instances** - Edit `modules/k3s-nodes/main.tf`:
   ```hcl
   instance_market_options {
     market_type = "spot"
   }
   ```

2. **Reduce Instance Sizes** - Edit `terraform.tfvars`:
   ```hcl
   k3s_server_instance_type = "t3.medium"
   k3s_agent_instance_type  = "t3.small"
   ```

3. **Reduce Agent Count** - Edit `terraform.tfvars`:
   ```hcl
   k3s_agent_count = 1
   ```

4. **Use Dev Workspace**:
   ```bash
   terraform workspace select dev
   ```

## Cleanup

### Destroy Infrastructure

```bash
# Review what will be destroyed
terraform plan -destroy

# Destroy all resources
terraform destroy

# Or use the default workspace
terraform workspace select default
terraform destroy
```

### Important Notes

- Destroying infrastructure will delete all EC2 instances and data
- SSH keys generated by Terraform will be deleted
- Ansible inventory will be removed
- K3s cluster data will be lost

## Integration with scripts/cloud/

This configuration complements the existing `scripts/cloud/` Terraform setup:

| Component | scripts/cloud/ | orchestrator/cloud/ |
|-----------|----------------|---------------------|
| Purpose   | ECS services (API, billing, payments) | K3s cluster infrastructure |
| Region    | us-east-2 | us-east-2 (same) |
| Networking| VPC with private/public/db subnets | VPC with public subnet |
| Compute   | ECS Fargate | EC2 instances |
| Database  | RDS Aurora Serverless v2 | Deployed in K3s via Ansible |

Both can run in the same AWS account without conflicts (separate VPCs).

## Next Steps

After infrastructure is provisioned:

1. **Deploy K3s** - Use Ansible playbooks in `../ansible/`
2. **Configure kubectl** - Fetch kubeconfig from server
3. **Deploy Basilica** - Run Basilica deployment playbooks
4. **Monitor** - Set up Prometheus/Grafana
5. **Secure** - Restrict SSH access, enable MFA

## Support

For issues or questions:
- Check `../ansible/README.md` for K3s deployment help
- Review Terraform logs: `terraform show`
- Validate configuration: `terraform validate`
- Check AWS Console for resource status

## License

Same as parent Basilica project.
