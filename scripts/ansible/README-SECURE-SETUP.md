# Secure R2 Credentials Setup

This guide explains how to securely upload and manage R2 credentials for Basilica using Ansible Vault.

## Overview

The `secure-r2-setup.sh` script provides a **secure, interactive way** to:
- ✅ Collect R2 credentials (bucket, access key, secret key)
- ✅ Encrypt credentials using Ansible Vault
- ✅ Deploy encrypted credentials to your Kubernetes cluster
- ✅ Never store plaintext credentials in files

## Quick Start

### 1. Run the Secure Setup Script

```bash
cd scripts/ansible
./secure-r2-setup.sh
```

The script will:
1. **Prompt for vault password** - Used to encrypt/decrypt credentials (min 12 chars)
2. **Collect R2 credentials** - Bucket name, endpoint, access key ID, secret key
3. **Encrypt credentials** - Creates `group_vars/all/vault.yml` (encrypted)
4. **Save vault password** (optional) - Creates `.vault_password` for convenience

### 2. Deploy Credentials to Cluster

With saved vault password:
```bash
ansible-playbook -i inventories/production.ini playbooks/e2e-apply.yml --vault-password-file=.vault_password
```

Without saved vault password:
```bash
ansible-playbook -i inventories/production.ini playbooks/e2e-apply.yml --ask-vault-pass
```

### 3. Verify Deployment

```bash
kubectl get secret basilica-r2-credentials -n basilica-system
kubectl describe secret basilica-r2-credentials -n basilica-system
```

## Example Session

```bash
$ ./secure-r2-setup.sh
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Basilica R2 Credentials - Secure Setup
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This script will securely collect and encrypt your R2 credentials
using Ansible Vault for deployment to your Basilica cluster.

Step 1: Create Vault Password
This password will encrypt your R2 credentials.
You'll need this password to deploy or update credentials.

Enter vault password: ****************
Confirm vault password: ****************
✅ Vault password set

Save vault password to .vault_password file? (yes/no): yes
✅ Vault password saved to .vault_password
   IMPORTANT: Add .vault_password to .gitignore!
✅ Added .vault_password to .gitignore

Step 2: Enter R2 Credentials
Obtain these from Cloudflare R2 dashboard:
  https://dash.cloudflare.com/ → R2 → Manage R2 API Tokens

R2 Bucket Name: basilica-storage-production
R2 Endpoint (e.g., https://abc123.r2.cloudflarestorage.com): https://abc123.r2.cloudflarestorage.com
R2 Access Key ID: 1234567890abcdef
R2 Secret Access Key: ****************************
R2 Backend (r2/s3/gcs) [r2]: r2

✅ Credentials collected

Step 3: Encrypting credentials...
✅ Credentials encrypted and saved

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Setup Complete!
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Encrypted credentials saved to:
  /path/to/scripts/ansible/group_vars/all/vault.yml

Configuration:
  Backend:  r2
  Bucket:   basilica-storage-production
  Endpoint: https://abc123.r2.cloudflarestorage.com

Next Steps:

1. Review encrypted credentials (optional):
   ansible-vault view group_vars/all/vault.yml --vault-password-file=.vault_password

2. Deploy credentials to your cluster:
   cd /path/to/scripts/ansible
   ansible-playbook -i inventories/production.ini playbooks/e2e-apply.yml --vault-password-file=.vault_password

3. Verify deployment:
   kubectl get secret basilica-r2-credentials -n basilica-system
   kubectl describe secret basilica-r2-credentials -n basilica-system

Security Reminders:
  ⚠️  Keep your vault password secure
  ⚠️  Never commit .vault_password to git
  ⚠️  The vault.yml file is encrypted and safe to commit
  ⚠️  Rotate R2 API tokens regularly

✅ .vault_password is in .gitignore

Done! You can now deploy credentials to your cluster.
```

## Managing Credentials

### View Encrypted Credentials

```bash
# With saved password file
ansible-vault view group_vars/all/vault.yml --vault-password-file=.vault_password

# Or enter password interactively
ansible-vault view group_vars/all/vault.yml --ask-vault-pass
```

### Edit Credentials

```bash
# With saved password file
ansible-vault edit group_vars/all/vault.yml --vault-password-file=.vault_password

# Or enter password interactively
ansible-vault edit group_vars/all/vault.yml --ask-vault-pass
```

### Re-run Setup (Update Credentials)

```bash
./secure-r2-setup.sh
# Choose "yes" to overwrite existing vault file
```

### Rotate R2 API Token

1. Create new R2 API token in Cloudflare dashboard
2. Run `./secure-r2-setup.sh` with new credentials
3. Deploy updated credentials:
   ```bash
   ansible-playbook -i inventories/production.ini playbooks/e2e-apply.yml --vault-password-file=.vault_password
   ```
4. Delete old R2 API token in Cloudflare dashboard

## Credential Priority

The Ansible role supports multiple ways to provide credentials (in order of priority):

1. **Ansible Vault variables** (RECOMMENDED for production)
   - Variables prefixed with `vault_basilica_*`
   - Encrypted in `group_vars/all/vault.yml`
   - Created by `secure-r2-setup.sh`

2. **Environment variables** (Good for development)
   - Variables prefixed with `BASILICA_*`
   - Useful for local testing

3. **Playbook vars** (For programmatic configuration)
   - Variables prefixed with `basilica_*`
   - Can be passed via `-e` flag

Example:
```yaml
# Vault variables (highest priority)
vault_basilica_r2_bucket: "production-bucket"

# Environment variable (fallback)
export BASILICA_R2_BUCKET="dev-bucket"

# Playbook var (lowest priority)
basilica_r2_bucket: "default-bucket"
```

Result: Uses `production-bucket` from vault.

## Security Best Practices

### ✅ DO

- ✅ Use `secure-r2-setup.sh` to create encrypted credentials
- ✅ Keep vault password secure (use a password manager)
- ✅ Add `.vault_password` to `.gitignore`
- ✅ Commit encrypted `vault.yml` file to git (it's safe when encrypted)
- ✅ Use strong vault password (min 12 characters, mix of chars)
- ✅ Rotate R2 API tokens quarterly
- ✅ Use `--vault-password-file` for CI/CD pipelines
- ✅ Store vault password in your organization's secret manager (1Password, Vault, etc.)

### ❌ DON'T

- ❌ Never commit `.vault_password` to git
- ❌ Never commit plaintext credentials
- ❌ Don't share vault password via email/slack
- ❌ Don't use weak vault passwords
- ❌ Don't store credentials in environment variables on shared systems
- ❌ Don't commit unencrypted credential files

## Troubleshooting

### Vault password file not found

```bash
Error: ERROR! A vault password must be specified to decrypt group_vars/all/vault.yml
```

**Solution**: Either provide password file or enter interactively:
```bash
# Option 1: Use password file
ansible-playbook ... --vault-password-file=.vault_password

# Option 2: Enter password
ansible-playbook ... --ask-vault-pass
```

### Wrong vault password

```bash
Error: ERROR! Decryption failed (no vault secrets were found that could decrypt)
```

**Solution**: You entered the wrong vault password. Try again or re-run `secure-r2-setup.sh`.

### Credentials not decrypting

```bash
Error: basilica_r2_access_key_id is undefined
```

**Solution**: Make sure you're using `--vault-password-file` or `--ask-vault-pass`:
```bash
ansible-playbook -i inventories/production.ini playbooks/e2e-apply.yml --vault-password-file=.vault_password
```

### Overwriting existing credentials

```bash
Warning: Vault file already exists
```

**Solution**: Type `yes` to overwrite, or `no` to keep existing credentials.

## Advanced Usage

### Using with CI/CD

Store vault password in your CI/CD secret manager:

**GitHub Actions**:
```yaml
- name: Deploy R2 credentials
  env:
    VAULT_PASSWORD: ${{ secrets.ANSIBLE_VAULT_PASSWORD }}
  run: |
    echo "$VAULT_PASSWORD" > .vault_password
    chmod 600 .vault_password
    ansible-playbook -i inventories/production.ini playbooks/e2e-apply.yml --vault-password-file=.vault_password
    rm -f .vault_password
```

**GitLab CI**:
```yaml
deploy_credentials:
  script:
    - echo "$VAULT_PASSWORD" > .vault_password
    - chmod 600 .vault_password
    - ansible-playbook -i inventories/production.ini playbooks/e2e-apply.yml --vault-password-file=.vault_password
    - rm -f .vault_password
  variables:
    VAULT_PASSWORD: $ANSIBLE_VAULT_PASSWORD
```

### Multiple Environments

Create separate vault files for each environment:

```bash
# Production
./secure-r2-setup.sh
# Save to: group_vars/production/vault.yml

# Staging
./secure-r2-setup.sh
# Save to: group_vars/staging/vault.yml

# Deploy to specific environment
ansible-playbook -i inventories/production.ini playbooks/e2e-apply.yml --vault-password-file=.vault_password
```

### Re-keying Vault (Change Password)

If you need to change the vault password:

```bash
# Decrypt and re-encrypt with new password
ansible-vault rekey group_vars/all/vault.yml --ask-vault-pass

# Update .vault_password file
echo "new-password" > .vault_password
chmod 600 .vault_password
```

## Files Created

After running `secure-r2-setup.sh`:

```
scripts/ansible/
├── .gitignore                          # Updated with .vault_password
├── .vault_password                     # Vault password (NEVER COMMIT!)
├── group_vars/
│   └── all/
│       └── vault.yml                   # Encrypted credentials (safe to commit)
└── secure-r2-setup.sh                  # This script
```

## Vault File Format

The encrypted `vault.yml` contains:

```yaml
---
# Basilica R2 Credentials (Encrypted with Ansible Vault)
# Created: 2025-01-15 10:30:00 UTC

# R2 Storage Backend
vault_basilica_r2_backend: "r2"

# R2 Bucket Configuration
vault_basilica_r2_bucket: "basilica-storage-production"
vault_basilica_r2_endpoint: "https://abc123.r2.cloudflarestorage.com"

# R2 API Credentials (SENSITIVE)
vault_basilica_r2_access_key_id: "1234567890abcdef"
vault_basilica_r2_secret_access_key: "supersecretkey123"

# Enable persistent storage deployment
vault_basilica_enable_persistent_storage: true
```

**Note**: This content is encrypted. To view it, use:
```bash
ansible-vault view group_vars/all/vault.yml --vault-password-file=.vault_password
```

## Alternative: Using Environment Variables

For local development, you can still use environment variables:

```bash
# Set credentials in environment
export BASILICA_R2_ACCESS_KEY_ID="your-key"
export BASILICA_R2_SECRET_ACCESS_KEY="your-secret"
export BASILICA_R2_BUCKET="your-bucket"
export BASILICA_R2_ENDPOINT="https://your-account.r2.cloudflarestorage.com"
export BASILICA_ENABLE_PERSISTENT_STORAGE=true

# Deploy (no vault password needed)
ansible-playbook -i inventories/production.ini playbooks/e2e-apply.yml
```

**Warning**: Environment variables are less secure than Ansible Vault. Use for development only.

## Summary

The secure setup workflow:

1. **Run script**: `./secure-r2-setup.sh`
2. **Create vault password** (min 12 chars)
3. **Enter R2 credentials** (bucket, endpoint, keys)
4. **Credentials encrypted** → `group_vars/all/vault.yml`
5. **Deploy**: `ansible-playbook ... --vault-password-file=.vault_password`
6. **Verify**: `kubectl get secret basilica-r2-credentials -n basilica-system`

**Security**: Credentials encrypted with AES-256, vault password never stored in git, `.vault_password` in `.gitignore`.

## Support

For issues or questions:
- Review [Ansible Vault Documentation](https://docs.ansible.com/ansible/latest/user_guide/vault.html)
- Check [Basilica Storage Role README](roles/basilica-storage/README.md)
- Open issue on GitHub

## Next Steps

After deploying credentials:
1. ✅ Verify secret exists: `kubectl get secret basilica-r2-credentials -n basilica-system`
2. ✅ Test with example job: `kubectl apply -f ../../examples/persistent-storage-job.yaml`
3. ✅ Monitor costs in Cloudflare R2 dashboard
4. ✅ Set up R2 lifecycle policies for automatic cleanup
5. ✅ Configure External Secrets Operator for production (optional)

Happy deploying! 🚀
