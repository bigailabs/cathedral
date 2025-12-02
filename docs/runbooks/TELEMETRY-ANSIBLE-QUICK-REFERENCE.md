# Telemetry/Ansible Quick Reference Guide

## Directory Structure at a Glance

```
telemetry/ansible/
├── ansible.cfg              ← Performance & connection settings
├── playbook.yml            ← Single entry point (130 lines)
├── README.md               ← Comprehensive docs (394 lines)
├── inventory.example       ← Template inventory
├── .gitignore              ← Security: hide secrets, keep examples
│
├── group_vars/
│   ├── all.yml             ← Global defaults (GIT-IGNORED)
│   ├── all.yml.example     ← Template to commit
│   ├── vault.yml           ← Encrypted secrets (GIT-IGNORED)
│   └── vault.yml.example   ← Secrets template
│
├── host_vars/
│   ├── basilica_prod.yml     ← Prod overrides (GIT-IGNORED)
│   └── basilica_prod.yml.example
│
└── roles/
    ├── docker/              ← Docker installation (116 lines)
    ├── telemetry/          ← Services deployment (136 lines)
    └── nginx/              ← Reverse proxy (218 lines)
```

**Key Files to Know**:
- `/ansible.cfg` - Settings: pipelining, fact cache, parallel execution
- `/playbook.yml` - Main entry: pre-tasks → roles → post-tasks
- `/group_vars/all.yml.example` - Copy this to all.yml and edit
- `/host_vars/basilica_prod.yml` - Production-specific overrides

---

## Deployment Quick Start

### 1. Initial Setup
```bash
cd basilica/telemetry/ansible

# Copy example files
cp group_vars/all.yml.example group_vars/all.yml
cp group_vars/vault.yml.example group_vars/vault.yml
cp host_vars/basilica_prod.yml.example host_vars/basilica_prod.yml
cp inventory.example inventory

# Edit configuration files
nano group_vars/all.yml
nano host_vars/basilica_prod.yml
ansible-vault create group_vars/vault.yml  # Create encrypted secrets
```

### 2. Edit Inventory
```ini
[all]
basilica_prod ansible_host=YOUR_IP ansible_user=ubuntu ansible_ssh_private_key_file=~/.ssh/id_rsa

[basilica_servers]
basilica_prod
```

### 3. Deploy
```bash
# Standard deployment
ansible-playbook -i inventory playbook.yml

# Dry run first
ansible-playbook -i inventory playbook.yml --check

# With vault password
ansible-playbook -i inventory playbook.yml --ask-vault-pass

# Force full recreation
ansible-playbook -i inventory playbook.yml -e "basilica_force_recreate=true"

# Deploy specific components only
ansible-playbook -i inventory playbook.yml --tags docker
ansible-playbook -i inventory playbook.yml --tags telemetry
ansible-playbook -i inventory playbook.yml --tags nginx
```

---

## Variable Configuration

### Configuration Hierarchy (Lowest to Highest Priority)

```
1. Role defaults
2. group_vars/all.yml      ← Global defaults
3. host_vars/basilica_prod.yml  ← Host overrides
4. group_vars/vault.yml    ← Encrypted secrets
5. Command-line (-e var=val)
```

### Key Variables to Configure

**In `group_vars/all.yml`**:
```yaml
ansible_user: ubuntu
basilica_telemetry_dir: "/opt/basilica/telemetry"
prometheus_version: "v2.47.0"
prometheus_port: 9090
loki_port: 3100
grafana_port: 3000
prometheus_retention_time: "30d"
grafana_admin_password: "basilica_admin"
nginx_enabled: true
nginx_ssl_enabled: false
```

**In `host_vars/basilica_prod.yml`**:
```yaml
prometheus_domain: "basilica-telemetry.example.com"
nginx_ssl_enabled: true
prometheus_retention_time: "90d"
prometheus_retention_size: "50GB"
grafana_admin_password: "{{ vault_grafana_admin_password }}"
nginx_cert_country: "US"
```

**In `group_vars/vault.yml.example`**:
```yaml
vault_grafana_admin_password: "secure_password_here"
```

---

## Playbook Execution Flow

```
PRE-TASKS
  - Update apt cache
  - Install system packages (curl, wget, unzip, git, htop, net-tools, ufw)

ROLES (with tags for selective execution)
  - docker    [infrastructure]  → Install Docker, configure daemon
  - telemetry [services]        → Deploy Prometheus, Loki, Grafana, Alertmanager
  - nginx     [proxy]           → Reverse proxy, SSL, firewall

POST-TASKS (Comprehensive validation - 9 checks)
  1. Wait for service ports (Prometheus, Loki, Grafana, Node Exporter, Alertmanager)
  2. Health check each service API
  3. Test NGINX reverse proxy routing
  4. Verify Grafana datasources
  5. List running Docker containers
  6. Verify Docker network exists
```

---

## Services Deployed

| Service | Port | Role | Purpose |
|---------|------|------|---------|
| Prometheus | 9090 | telemetry | Metrics collection & storage (30-day retention default) |
| Loki | 3100 | telemetry | Log aggregation |
| Grafana | 3000 | telemetry | Dashboards & visualization |
| Node Exporter | 9100 | telemetry | System metrics |
| Alertmanager | 9093 | telemetry | Alert routing & management |
| NGINX | 80/443 | nginx | Reverse proxy & SSL termination |

---

## Post-Deployment Access

After successful deployment:

```bash
# Grafana (main interface)
http://your-server/
Username: admin
Password: basilica_admin (or configured value)

# Direct service access (bypass NGINX)
Prometheus: http://your-server:9090/
Loki: http://your-server:3100/
Grafana: http://your-server:3000/

# Health endpoints
Prometheus: http://your-server:9090/-/healthy
Loki: http://your-server:3100/ready
Grafana: http://your-server:3000/api/health
```

---

## Common Operations

### Redeploy Services (keeping data)
```bash
ansible-playbook -i inventory playbook.yml
```

### Force full recreation (clears data)
```bash
ansible-playbook -i inventory playbook.yml -e "basilica_force_recreate=true"
```

### Deploy only Docker
```bash
ansible-playbook -i inventory playbook.yml --tags docker
```

### Deploy only telemetry services
```bash
ansible-playbook -i inventory playbook.yml --tags telemetry
```

### Deploy only NGINX
```bash
ansible-playbook -i inventory playbook.yml --tags nginx
```

### Check without making changes
```bash
ansible-playbook -i inventory playbook.yml --check
```

### Increase verbosity
```bash
ansible-playbook -i inventory playbook.yml -vvv
```

---

## Troubleshooting

### Port conflicts
```bash
netstat -tulpn | grep -E "(3000|9090|3100|80|443)"
```

### Docker network issues
```bash
docker network ls
docker network inspect basilica_network
```

### Permission issues
```bash
ls -la /opt/basilica/telemetry/
ls -la /var/log/basilica/
```

### NGINX configuration
```bash
nginx -t                          # Test config
systemctl status nginx            # Check status
tail -f /var/log/nginx/error.log  # View errors
```

### Service logs
```bash
docker logs basilica-prometheus
docker logs basilica-loki
docker logs basilica-grafana
docker logs basilica-alertmanager
```

### Restart services
```bash
cd /opt/basilica/telemetry
docker compose -f docker-compose.prod.yml restart
```

### Reset volumes (WARNING: deletes data)
```bash
cd /opt/basilica/telemetry
docker compose -f docker-compose.prod.yml down -v
ansible-playbook -i inventory playbook.yml
```

---

## Role Structure Details

### Docker Role (`roles/docker/`)
- **Purpose**: Install Docker and configure daemon
- **Key Tasks**:
  1. Install prerequisites (apt-transport-https, ca-certificates, curl, etc.)
  2. Install Docker from official script
  3. Configure Docker daemon JSON (storage driver, log rotation, etc.)
  4. Create docker group and add user
  5. Enable and start Docker service
  6. Verify Docker and Docker Compose installation

### Telemetry Role (`roles/telemetry/`)
- **Purpose**: Deploy monitoring services
- **Key Tasks**:
  1. Create directory structure (/opt/basilica/telemetry/grafana, /prometheus, /rules, etc.)
  2. Copy config files (prometheus.yml, loki.yml, alertmanager.yml, grafana/)
  3. Generate docker-compose.prod.yml from template
  4. Create/verify Docker network
  5. Start Docker Compose stack
  6. Wait for services to be ready
  7. Verify service health

### NGINX Role (`roles/nginx/`)
- **Purpose**: Reverse proxy, SSL/TLS, firewall
- **Key Tasks**:
  1. Install NGINX
  2. Create configuration directories
  3. Configure NGINX main settings
  4. Configure upstream proxies (Grafana, Prometheus, Loki)
  5. Enable/symlink site configs
  6. Remove default site
  7. Generate self-signed SSL certificates (if enabled)
  8. Configure UFW firewall rules
  9. Create certificate rotation script (yearly)

---

## Handlers & Notifications

### NGINX Handlers
```yaml
check nginx configuration  ← Runs nginx -t
  ↓ notifies
restart nginx             ← systemctl restart nginx
  ↓ notifies
check nginx configuration ← Validation chain
```

### Telemetry Handlers
```yaml
restart basilica telemetry  ← docker compose down + up -d
```

---

## Ansible Configuration Performance

**`ansible.cfg` Optimizations**:

```ini
pipelining = True              # Reduce SSH calls by 40%
fact_caching = jsonfile        # Cache facts for 24h
fact_caching_timeout = 86400   # 24 hours
gathering = smart              # Only gather changed facts
forks = 10                     # Run on 10 hosts in parallel
hash_behaviour = merge         # Merge dicts intelligently
retry_files_enabled = False    # Don't create .retry files
```

**Impact**:
- First run: Slower (gathers facts)
- Subsequent runs: Much faster (cached facts)
- SSH overhead reduced by 40%
- Safe for re-running playbooks

---

## File Paths (Absolute)

**Core Location**: `/root/workspace/spacejar/basilica/basilica/telemetry/ansible/`

**Configuration**:
- `/ansible.cfg` → Ansible settings
- `/playbook.yml` → Main playbook
- `/group_vars/all.yml` → Global vars (git-ignored)
- `/group_vars/vault.yml` → Secrets (git-ignored)
- `/host_vars/basilica_prod.yml` → Host vars (git-ignored)

**Roles**:
- `/roles/docker/tasks/main.yml` → Docker installation
- `/roles/telemetry/tasks/main.yml` → Services deployment
- `/roles/telemetry/templates/docker-compose.prod.yml.j2` → Docker Compose template
- `/roles/nginx/tasks/main.yml` → NGINX configuration
- `/roles/nginx/templates/nginx.conf.j2` → Main NGINX config
- `/roles/nginx/templates/basilica-*.conf.j2` → Service proxies

**Deployment Target**:
- `/opt/basilica/telemetry/` → Services directory
- `/opt/basilica/telemetry/docker-compose.prod.yml` → Generated compose file
- `/var/log/basilica/` → Application logs

---

## Best Practices

1. **Always use `--check` first**
   ```bash
   ansible-playbook -i inventory playbook.yml --check
   ```

2. **Backup before production**
   ```bash
   docker run --rm -v prometheus_data:/data -v $(pwd):/backup alpine tar czf /backup/prometheus-backup.tar.gz /data
   ```

3. **Use vault for secrets**
   ```bash
   ansible-vault create group_vars/vault.yml
   ```

4. **Keep examples in git**
   ```bash
   group_vars/*.example  # Always commit
   group_vars/*.yml      # Never commit (add to .gitignore)
   ```

5. **Document custom changes**
   - Edit group_vars/all.yml with comments
   - Update README.md if adding new features

6. **Test selective tags**
   ```bash
   ansible-playbook -i inventory playbook.yml --tags docker --check
   ```

7. **Monitor during deployment**
   ```bash
   # In another terminal
   ssh user@host
   docker logs -f basilica-prometheus
   ```

---

## Environment Variables Cheat Sheet

```bash
# Ansible verbosity
ANSIBLE_VERBOSITY=3 ansible-playbook -i inventory playbook.yml

# Skip host key checking (not recommended for production)
ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook -i inventory playbook.yml

# Use specific SSH key
ANSIBLE_PRIVATE_KEY_FILE=~/.ssh/custom_key ansible-playbook -i inventory playbook.yml

# Disable fact caching
ANSIBLE_GATHER_SUBSET=!all ansible-playbook -i inventory playbook.yml
```

---

## One-Line Deployments (Copy & Paste)

```bash
# Full deployment with vault
cd /root/workspace/spacejar/basilica/basilica/telemetry/ansible && \
cp group_vars/all.yml.example group_vars/all.yml && \
ansible-vault create group_vars/vault.yml && \
ansible-playbook -i inventory playbook.yml --ask-vault-pass

# Dry run only
ansible-playbook -i inventory playbook.yml --check

# Redeploy with higher verbosity
ansible-playbook -i inventory playbook.yml -vvv

# Just NGINX
ansible-playbook -i inventory playbook.yml --tags nginx --check
```

