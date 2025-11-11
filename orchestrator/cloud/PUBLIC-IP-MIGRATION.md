# Public IP Migration Guide

## Summary

K3s nodes have been moved from **private subnets** to **public subnets** to enable direct SSH access via the `connect.sh` script.

## Changes Made

### 1. Terraform Configuration (`main.tf`)

**Before:**
```terraform
# Servers and agents in private subnets
subnet_ids = module.networking.private_subnet_ids
```

**After:**
```terraform
# Servers and agents in public subnets with auto-assigned public IPs
subnet_ids = module.networking.public_subnet_ids
```

**Lines Changed:**
- Line 80: `k3s_servers` module - Changed to `public_subnet_ids`
- Line 111: `k3s_agents` module - Changed to `public_subnet_ids`

### 2. Documentation Updates

- `outputs.tf` - Updated cluster configuration notes
- Added SSH access section with `connect.sh` examples
- Removed bastion/SSM references

## Impact

### Before (Private Subnets)

```
✗ No public IPs assigned
✗ SSH requires bastion host or SSM Session Manager
✗ connect.sh script doesn't work
✗ More complex deployment workflow
```

### After (Public Subnets)

```
✓ Public IPs auto-assigned via subnet configuration
✓ Direct SSH access from allowed CIDR blocks
✓ connect.sh script works out of the box
✓ Simpler deployment workflow
✓ Still protected by security groups
```

## Security Considerations

### Still Secure

1. **Security Groups** - SSH access limited to `allowed_ssh_cidr_blocks` (default: 0.0.0.0/0, should be restricted)
2. **SSH Key Authentication** - No password authentication
3. **K3s API** - Protected behind Network Load Balancer
4. **Internal Communication** - Security group rules for inter-node traffic

### Recommendations

Update `terraform.tfvars` to restrict SSH access:

```hcl
# Restrict SSH to your organization's IPs
allowed_ssh_cidr_blocks = ["203.0.113.0/24"]  # Replace with your IP range

# Or restrict to specific IP
allowed_ssh_cidr_blocks = ["203.0.113.10/32"]
```

## Migration Path for Existing Deployments

### Option 1: Destroy and Recreate (Recommended)

**⚠️ WARNING**: This will destroy all K3s nodes and workloads.

```bash
# Save any important data from cluster
kubectl get all --all-namespaces -o yaml > backup.yaml

# Destroy existing infrastructure
./run.sh tplr prod destroy

# Deploy with new configuration
./run.sh tplr prod apply

# Verify nodes have public IPs
terraform output k3s_server_public_ips
terraform output k3s_agent_public_ips

# Test SSH access
./connect.sh server-1
```

### Option 2: Manual Migration (Complex)

If you need zero-downtime migration:

1. Create new nodes in public subnets
2. Drain old nodes: `kubectl drain <node> --ignore-daemonsets`
3. Remove old nodes from cluster
4. Terminate old EC2 instances
5. Update Terraform state

**Not recommended** - Destroy/recreate is simpler and faster.

## Verification

After applying changes:

```bash
# Check public IPs are assigned
terraform output k3s_server_public_ips
# Expected: ["18.x.x.x", "3.x.x.x", "18.x.x.x"]

terraform output k3s_agent_public_ips
# Expected: ["3.x.x.x", "18.x.x.x"]

# Test SSH connection
./connect.sh k3s-server-1
# Expected: SSH connection successful

# Inside the node, verify public IP
curl -s ifconfig.me
# Expected: Shows the node's public IP
```

## Troubleshooting

### Public IPs Still Empty

**Cause**: Existing instances not recreated

**Solution**:
```bash
# Force recreation
terraform taint 'module.k3s_servers.aws_instance.k3s_server[0]'
terraform taint 'module.k3s_servers.aws_instance.k3s_server[1]'
terraform taint 'module.k3s_servers.aws_instance.k3s_server[2]'
terraform taint 'module.k3s_agents.aws_instance.k3s_agent[0]'
terraform taint 'module.k3s_agents.aws_instance.k3s_agent[1]'
terraform apply
```

### SSH Connection Refused

**Cause**: Security group doesn't allow your IP

**Solution**:
```bash
# Check your public IP
curl -s ifconfig.me

# Update terraform.tfvars
allowed_ssh_cidr_blocks = ["YOUR_IP/32"]

# Apply changes
terraform apply
```

### Nodes Can't Communicate

**Cause**: Security group rules need updating

**Solution**: Security groups are automatically configured for inter-node communication. If issues persist, verify:

```bash
# Check security group rules
aws ec2 describe-security-groups \
  --group-ids $(terraform output -json k3s_server_ids | jq -r '.[0]' | xargs aws ec2 describe-instances --instance-ids --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text)
```

## Architecture Comparison

### Before: Private Subnet Architecture

```
┌─────────────────────────────────────┐
│ Public Subnets                      │
│ - Network Load Balancer             │
│ - NAT Gateway                       │
└─────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────┐
│ Private Subnets                     │
│ - K3s Servers (no public IP)        │
│ - K3s Agents (no public IP)         │
│ - Outbound via NAT only             │
└─────────────────────────────────────┘
```

**Access**: Bastion host or AWS SSM required

### After: Public Subnet Architecture

```
┌─────────────────────────────────────┐
│ Public Subnets                      │
│ - Network Load Balancer             │
│ - NAT Gateway                       │
│ - K3s Servers (public IPs)          │
│ - K3s Agents (public IPs)           │
│ - Direct SSH access (via SG rules)  │
└─────────────────────────────────────┘
```

**Access**: Direct SSH via public IPs (controlled by security groups)

## Best Practices

1. **Restrict SSH Access**: Always set `allowed_ssh_cidr_blocks` to your organization's IP range
2. **Use VPN**: For production, consider VPN + private subnets for maximum security
3. **Monitor Access**: Enable VPC Flow Logs to monitor SSH access attempts
4. **Rotate Keys**: Regularly rotate SSH keys used for node access
5. **Use kubectl**: For most operations, use kubectl instead of SSH

## Related Documentation

- `connect.sh` - SSH connection helper
- `CONNECT-USAGE.md` - Complete SSH connection guide
- `README.md` - Main infrastructure documentation
- `QUICK-START.md` - Deployment quickstart

## Rollback

To revert to private subnets (not recommended):

```bash
# Edit main.tf
# Change both subnet_ids back to:
subnet_ids = module.networking.private_subnet_ids

# Apply
terraform apply

# Note: Nodes will NOT have public IPs
# You'll need bastion host or SSM for access
```
