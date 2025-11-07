# connect.sh - SSH Connection Helper

Quick SSH access to K3s nodes using Terraform outputs.

## Synopsis

```bash
./connect.sh <node-name>
```

## Description

The `connect.sh` script provides convenient SSH access to K3s server and agent nodes deployed via Terraform. It automatically:

- Retrieves node IP addresses from Terraform state
- Locates the correct SSH private key
- Establishes SSH connection with proper security settings
- Supports multiple node name formats for convenience

## Usage

### Basic Connection

```bash
# Connect to first server node
./connect.sh k3s-server-1

# Connect to second agent node
./connect.sh k3s-agent-2
```

### Alternative Formats

All these formats work:

```bash
# Full format
./connect.sh k3s-server-1
./connect.sh k3s-agent-2

# Short format (without k3s prefix)
./connect.sh server-1
./connect.sh agent-2

# Compact format (no hyphens)
./connect.sh server1
./connect.sh agent2
```

### List Available Nodes

Run without arguments to see all available nodes:

```bash
./connect.sh
```

Output example:
```
Available nodes:

Servers:
  - k3s-server-1 (18.220.123.45)
  - k3s-server-2 (3.140.67.89)
  - k3s-server-3 (18.224.98.123)

Agents:
  - k3s-agent-1 (3.21.45.67)
  - k3s-agent-2 (18.217.89.123)
```

## How It Works

1. **Parses node name** - Supports multiple formats (k3s-server-1, server-1, server1)
2. **Queries Terraform state** - Gets node IP from `terraform output`
3. **Retrieves SSH key** - Automatically locates the private key file
4. **Establishes connection** - Connects as `ubuntu` user with proper SSH options

## Prerequisites

- Terraform must be initialized: `terraform init`
- Infrastructure must be deployed: `terraform apply`
- Must be run from `orchestrator/cloud/` directory (or script handles this automatically)
- `jq` must be installed for JSON parsing

## SSH Options

The script uses these SSH options for security and convenience:

```bash
-i <key-file>                    # Use Terraform-generated SSH key
-o StrictHostKeyChecking=no      # Skip host key verification (ephemeral IPs)
-o UserKnownHostsFile=/dev/null  # Don't store host keys
-o LogLevel=ERROR                # Suppress warnings
ubuntu@<node-ip>                 # Connect as ubuntu user
```

## Examples

### Development Workflow

```bash
# Deploy infrastructure
./run.sh tplr dev apply

# Connect to primary server
./connect.sh server-1

# Check K3s cluster status
kubectl get nodes

# Exit and connect to agent
exit
./connect.sh agent-1
```

### Production Access

```bash
# Connect to production server
./run.sh tplr prod plan
./run.sh tplr prod apply

# SSH to server for debugging
./connect.sh k3s-server-1

# Check etcd health
sudo k3s etcd-snapshot list

# Check logs
journalctl -u k3s -f
```

### Multi-Node Operations

```bash
# Connect to each server sequentially
for i in 1 2 3; do
    echo "Checking k3s-server-$i..."
    ./connect.sh server-$i "systemctl status k3s" || true
done
```

## Error Messages

### No Arguments

```
Usage: ./connect.sh <node-name>
[Shows examples and available nodes]
```

### Invalid Node Name

```
Error: Invalid node name format: xyz

Valid formats:
  - k3s-server-1, server-1, server1
  - k3s-agent-2, agent-2, agent2
```

### Node Not Found

```
Error: Node not found: k3s-server-5

Available nodes:
[Lists actual nodes]
```

### SSH Key Not Found

```
Error: SSH key file not found: /path/to/key.pem
```

### Terraform Not Applied

```
No nodes found. Make sure Terraform has been applied successfully.

Run: terraform output
```

## Troubleshooting

### Connection Refused

**Cause**: Instance not fully initialized or security group blocks SSH

**Solution**:
```bash
# Wait for instance to initialize
sleep 60

# Check security group allows your IP
terraform output nat_gateway_public_ip

# Verify instance is running
aws ec2 describe-instances --instance-ids <id>
```

### Permission Denied

**Cause**: SSH key has wrong permissions

**Solution**:
```bash
# Fix key permissions
chmod 600 /path/to/k3s-ssh-key.pem
```

### Node IP Changes

**Cause**: Instance was stopped/started (new public IP assigned)

**Solution**:
```bash
# Refresh Terraform state
terraform refresh

# Reconnect
./connect.sh server-1
```

## Integration with Other Scripts

### With run.sh

```bash
# Deploy and connect
./run.sh tplr prod apply-f && ./connect.sh server-1
```

### With Ansible

```bash
# Verify manual SSH works before Ansible
./connect.sh server-1 "echo 'SSH OK'"

# Run Ansible playbook
cd ../ansible
ansible-playbook -i inventories/production.ini playbooks/01-setup/k3s-cluster.yml
```

### With kubectl

```bash
# SSH and use kubectl
./connect.sh server-1

# Inside the server
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get nodes
```

## Advanced Usage

### Run Remote Command

```bash
# Execute command without interactive session
./connect.sh server-1 "hostname && uptime"
```

### Port Forwarding

```bash
# Forward K3s API port
ssh -i <key> -L 6443:localhost:6443 ubuntu@<ip>
```

### File Transfer

```bash
# Copy file to server
SSH_KEY=$(terraform output -raw ssh_private_key_file | awk '{print $1}')
NODE_IP=$(terraform output -json k3s_server_public_ips | jq -r '.[0]')
scp -i "$SSH_KEY" local-file.txt ubuntu@$NODE_IP:/tmp/
```

## Script Source

Location: `orchestrator/cloud/connect.sh`

The script is self-contained and has no external dependencies except:
- `bash` (version 4+)
- `jq` (for JSON parsing)
- `terraform` (for outputs)
- `ssh` (standard OpenSSH client)

## Security Considerations

- **SSH key** - Automatically managed by Terraform, stored locally
- **No password authentication** - Key-based only for security
- **Host key checking disabled** - Acceptable for ephemeral cloud instances
- **Ubuntu user** - Default user with sudo access
- **Private subnets** - Nodes are in private subnets (requires bastion or VPN in production)

Note: The current architecture places nodes in private subnets without public IPs. This script assumes you have direct network access (e.g., via VPN or bastion host). For production, consider updating the architecture to remove public IPs entirely.

## Related Scripts

- `run.sh` - Terraform deployment with AWS profile support
- Ansible playbooks - K3s cluster configuration
- `terraform output` - Raw access to all outputs

## Future Enhancements

Potential improvements:
- Tab completion for node names
- SSH config file generation
- Bastion host support for private-only nodes
- Multi-hop SSH for strict security
- Integration with kubectl context switching
