# Quick Start Guide - Basilica K3s Infrastructure

Deploy a production-ready K3s cluster on AWS in 5 minutes.

## Prerequisites

- AWS CLI configured with named profile (`aws configure --profile <profile-name>`)
- Terraform >= 1.5.0 installed
- SSH access capabilities

## Step-by-Step Deployment

### 1. Configure AWS Profile

```bash
# Configure AWS credentials for your profile
aws configure --profile tplr

# Verify credentials
aws sts get-caller-identity --profile tplr
```

### 2. Configure Infrastructure

```bash
cd orchestrator/cloud

# Copy example configuration
cp terraform.tfvars.example terraform.tfvars

# Edit configuration (minimal required)
vim terraform.tfvars
```

**Minimal `terraform.tfvars` configuration:**
```hcl
aws_region        = "us-east-2"
project_name      = "basilica-k3s"
ssh_key_name      = ""  # Generate new SSH key
k3s_server_count  = 3   # 3 for HA, 1 for dev/test
```

### 3. Deploy Infrastructure

Using the convenient run.sh script:

```bash
# Review deployment plan
./run.sh tplr prod plan

# Deploy to production
./run.sh tplr prod apply

# Or force deploy without confirmation (CI/CD)
./run.sh tplr prod apply-f

# Deploy to dev environment
./run.sh tplr dev apply
```

**Manual deployment (alternative):**
```bash
# Export AWS profile
export AWS_PROFILE=tplr

# Initialize Terraform
terraform init
terraform workspace select prod || terraform workspace new prod

# Review deployment plan
terraform plan

# Deploy
terraform apply
```

**Deployment takes ~3-5 minutes**

### 4. Verify Infrastructure and SSH Access

```bash
# List all deployed nodes
./connect.sh

# Connect to primary server
./connect.sh k3s-server-1

# Or use short format
./connect.sh server-1
```

### 5. Verify Terraform Outputs

```bash
# View outputs
terraform output

# Test SSH connectivity
SSH_KEY=$(terraform output -raw ssh_private_key_file | awk '{print $1}')
SERVER_IP=$(terraform output -raw k3s_server_public_ip)
ssh -i "$SSH_KEY" ubuntu@"$SERVER_IP" 'echo "SSH successful"'
```

### 4. Deploy K3s with Ansible

```bash
# Wait for instances to finish initializing
sleep 120

# Navigate to Ansible directory
cd ../ansible

# Deploy K3s cluster
ansible-playbook -i inventories/production.ini playbooks/01-setup/k3s-cluster.yml
```

**K3s deployment takes ~5-10 minutes**

### 5. Verify K3s Cluster

```bash
# Verify cluster health
ansible-playbook -i inventories/production.ini playbooks/03-verify/cluster-health.yml

# Or SSH to server and check directly
ssh -F ../cloud/ssh_config k3s-server
kubectl get nodes
kubectl get pods -A
```

### 6. Deploy Basilica Services

```bash
cd ../ansible

# Deploy Basilica application stack
ansible-playbook -i inventories/production.ini playbooks/02-deploy/basilica.yml
```

## Common Commands

### Infrastructure Management

```bash
# View infrastructure status
terraform show

# Update infrastructure
terraform apply

# Destroy infrastructure
terraform destroy
```

### SSH Access

```bash
# Connect to K3s server
ssh -F orchestrator/cloud/ssh_config k3s-server

# Connect to K3s agent
ssh -F orchestrator/cloud/ssh_config k3s-agent-1
```

### Ansible Operations

```bash
cd orchestrator/ansible

# Check cluster health
ansible-playbook -i inventories/production.ini playbooks/03-verify/cluster-health.yml

# Fetch kubeconfig
ansible-playbook -i inventories/production.ini playbooks/04-maintain/kubeconfig.yml

# View API status
ansible-playbook -i inventories/production.ini playbooks/03-verify/api-status.yml
```

## Troubleshooting

### Issue: SSH Connection Refused

**Cause:** Instances still initializing

**Solution:**
```bash
# Check instance status
aws ec2 describe-instance-status \
  --instance-ids $(terraform output -json k3s_server_id | jq -r) \
  --query "InstanceStatuses[0].InstanceStatus.Status" \
  --output text

# Wait until returns "ok"
```

### Issue: Ansible Cannot Connect

**Cause:** Inventory file not found or SSH key permissions wrong

**Solution:**
```bash
# Verify inventory exists
ls -la ../ansible/inventories/production.ini

# Fix SSH key permissions
chmod 600 orchestrator/cloud/k3s-ssh-key.pem

# Test Ansible connectivity
cd ../ansible
ansible -i inventories/production.ini all -m ping
```

### Issue: K3s Agent Not Connecting

**Cause:** This is the problem we're fixing! Should not occur with this infrastructure.

**Verification:**
```bash
# Check security group rules
terraform state show module.networking.aws_security_group.k3s_server
terraform state show module.networking.aws_security_group.k3s_agent

# Verify connectivity from agent to server
ssh -F orchestrator/cloud/ssh_config k3s-agent-1
ping -c 3 <server-private-ip>
nc -zv <server-private-ip> 6443
```

## Cost Estimate

**Production Configuration (HA mode, default):**
- 3x t3.xlarge (K3s servers): ~$390/month
- 2x t3.medium (K3s agents): ~$65/month
- **Total: ~$455/month**

**Development Configuration:**
```bash
terraform workspace new dev
terraform workspace select dev
terraform apply
```
- 1x t3.medium (K3s server): ~$30/month
- 1x t3.small (K3s agent): ~$15/month
- **Total: ~$45/month**

**Single Server Production (not HA):**
Edit `terraform.tfvars`:
```hcl
k3s_server_count = 1
```
- 1x t3.xlarge (K3s server): ~$130/month
- 2x t3.medium (K3s agents): ~$65/month
- **Total: ~$195/month**

## Security Notes

### Production Hardening

1. **Restrict SSH Access** - Edit `terraform.tfvars`:
```hcl
allowed_ssh_cidr_blocks = ["YOUR.IP.ADDRESS/32"]
```

2. **Use Existing SSH Key**:
```hcl
ssh_key_name = "my-existing-aws-key"
```

3. **Enable Instance Termination Protection**:
```hcl
# Add to modules/k3s-nodes/main.tf
disable_api_termination = true
```

### Security Groups

The infrastructure creates properly configured security groups:
- **K3s Server SG**: Allows 6443, 10250, 2379-2380 (etcd), 8472, 51820-51821
- **K3s Agent SG**: Allows 10250, 8472, 51820-51821
- **Critical**:
  - Servers can communicate with each other (for etcd quorum)
  - Servers and agents can communicate via all ports (fixes the connection issue)

## Next Steps

After successful deployment:

1. **Configure kubectl locally**:
```bash
cd orchestrator/ansible
ansible-playbook -i inventories/production.ini playbooks/04-maintain/kubeconfig.yml
export KUBECONFIG=~/.kube/k3s-basilica-config
kubectl get nodes
```

2. **Deploy Monitoring** (optional):
```bash
# Edit group_vars/all/application.yml to enable monitoring
# Then run:
ansible-playbook -i inventories/production.ini playbooks/02-deploy/basilica.yml --tags monitoring
```

3. **Set up DNS** (optional):
- Point your domain to the K3s server public IP
- Configure SSL certificates
- Update ingress configurations

## Complete Example

Full workflow from scratch:

```bash
# 1. Configure
cd orchestrator/cloud
cp terraform.tfvars.example terraform.tfvars
vim terraform.tfvars  # Set aws_region, project_name

# 2. Deploy infrastructure
terraform init
terraform apply

# 3. Wait for initialization
sleep 120

# 4. Deploy K3s
cd ../ansible
ansible-playbook -i inventories/production.ini playbooks/01-setup/k3s-cluster.yml

# 5. Verify
ansible-playbook -i inventories/production.ini playbooks/03-verify/cluster-health.yml

# 6. Deploy Basilica
ansible-playbook -i inventories/production.ini playbooks/02-deploy/basilica.yml

# 7. Check status
ssh -F ../cloud/ssh_config k3s-server
kubectl get nodes
kubectl get pods -A
```

**Total time: ~15-20 minutes**

## Support

For detailed documentation, see [README.md](README.md)

For Ansible-specific help, see [../ansible/README.md](../ansible/README.md)
