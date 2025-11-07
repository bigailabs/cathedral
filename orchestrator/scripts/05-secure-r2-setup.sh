#!/usr/bin/env bash
# Secure R2 Credentials Setup for Basilica
# This script securely collects and encrypts R2 credentials using Ansible Vault

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT_FILE="${SCRIPT_DIR}/group_vars/all/vault.yml"
VAULT_PASSWORD_FILE="${SCRIPT_DIR}/.vault_password"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}  Basilica R2 Credentials - Secure Setup${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "This script will securely collect and encrypt your R2 credentials"
echo "using Ansible Vault for deployment to your Basilica cluster."
echo ""

# Check if ansible-vault is available
if ! command -v ansible-vault &> /dev/null; then
    echo -e "${RED}Error: ansible-vault not found. Please install Ansible.${NC}"
    echo "  Ubuntu/Debian: sudo apt-get install ansible"
    echo "  macOS: brew install ansible"
    exit 1
fi

# Create group_vars directory if it doesn't exist
mkdir -p "${SCRIPT_DIR}/group_vars/all"

# Check if vault file already exists
if [ -f "$VAULT_FILE" ]; then
    echo -e "${YELLOW}Warning: Vault file already exists at:${NC}"
    echo "  $VAULT_FILE"
    echo ""
    read -p "Do you want to overwrite it? (yes/no): " OVERWRITE
    if [ "$OVERWRITE" != "yes" ]; then
        echo -e "${YELLOW}Aborting. To view existing credentials:${NC}"
        echo "  ansible-vault view $VAULT_FILE"
        exit 0
    fi
    echo ""
fi

# Prompt for vault password
echo -e "${GREEN}Step 1: Create Vault Password${NC}"
echo "This password will encrypt your R2 credentials."
echo "You'll need this password to deploy or update credentials."
echo ""

while true; do
    read -s -p "Enter vault password: " VAULT_PASSWORD
    echo ""
    read -s -p "Confirm vault password: " VAULT_PASSWORD_CONFIRM
    echo ""

    if [ "$VAULT_PASSWORD" = "$VAULT_PASSWORD_CONFIRM" ]; then
        if [ ${#VAULT_PASSWORD} -lt 12 ]; then
            echo -e "${RED}Password must be at least 12 characters long.${NC}"
            echo ""
        else
            break
        fi
    else
        echo -e "${RED}Passwords do not match. Please try again.${NC}"
        echo ""
    fi
done

echo -e "${GREEN}✅ Vault password set${NC}"
echo ""

# Save vault password (optional)
read -p "Save vault password to .vault_password file? (yes/no): " SAVE_PASSWORD
if [ "$SAVE_PASSWORD" = "yes" ]; then
    echo "$VAULT_PASSWORD" > "$VAULT_PASSWORD_FILE"
    chmod 600 "$VAULT_PASSWORD_FILE"
    echo -e "${GREEN}✅ Vault password saved to $VAULT_PASSWORD_FILE${NC}"
    echo -e "${YELLOW}   IMPORTANT: Add .vault_password to .gitignore!${NC}"

    # Add to .gitignore if not already present
    GITIGNORE="${SCRIPT_DIR}/.gitignore"
    if [ -f "$GITIGNORE" ]; then
        if ! grep -q ".vault_password" "$GITIGNORE"; then
            echo ".vault_password" >> "$GITIGNORE"
            echo -e "${GREEN}✅ Added .vault_password to .gitignore${NC}"
        fi
    else
        echo ".vault_password" > "$GITIGNORE"
        echo -e "${GREEN}✅ Created .gitignore with .vault_password${NC}"
    fi
else
    echo -e "${YELLOW}⚠️  You'll need to enter the vault password each time you deploy.${NC}"
fi
echo ""

# Prompt for R2 credentials
echo -e "${GREEN}Step 2: Enter R2 Credentials${NC}"
echo "Obtain these from Cloudflare R2 dashboard:"
echo "  https://dash.cloudflare.com/ → R2 → Manage R2 API Tokens"
echo ""

read -p "R2 Bucket Name: " R2_BUCKET
while [ -z "$R2_BUCKET" ]; do
    echo -e "${RED}Bucket name cannot be empty${NC}"
    read -p "R2 Bucket Name: " R2_BUCKET
done

read -p "R2 Endpoint (e.g., https://abc123.r2.cloudflarestorage.com): " R2_ENDPOINT
while [ -z "$R2_ENDPOINT" ]; do
    echo -e "${RED}Endpoint cannot be empty${NC}"
    read -p "R2 Endpoint: " R2_ENDPOINT
done

read -p "R2 Access Key ID: " R2_ACCESS_KEY_ID
while [ -z "$R2_ACCESS_KEY_ID" ]; do
    echo -e "${RED}Access Key ID cannot be empty${NC}"
    read -p "R2 Access Key ID: " R2_ACCESS_KEY_ID
done

read -s -p "R2 Secret Access Key: " R2_SECRET_ACCESS_KEY
echo ""
while [ -z "$R2_SECRET_ACCESS_KEY" ]; do
    echo -e "${RED}Secret Access Key cannot be empty${NC}"
    read -s -p "R2 Secret Access Key: " R2_SECRET_ACCESS_KEY
    echo ""
done

read -p "R2 Backend (r2/s3/gcs) [r2]: " R2_BACKEND
R2_BACKEND=${R2_BACKEND:-r2}

echo ""
echo -e "${GREEN}✅ Credentials collected${NC}"
echo ""

# Create unencrypted vault content
VAULT_CONTENT="---
# Basilica R2 Credentials (Encrypted with Ansible Vault)
# Created: $(date -u +"%Y-%m-%d %H:%M:%S UTC")
#
# To view:   ansible-vault view $(basename $VAULT_FILE)
# To edit:   ansible-vault edit $(basename $VAULT_FILE)
# To deploy: ansible-playbook -i inventories/production.ini playbooks/e2e-apply.yml --ask-vault-pass

# R2 Storage Backend
vault_basilica_r2_backend: \"$R2_BACKEND\"

# R2 Bucket Configuration
vault_basilica_r2_bucket: \"$R2_BUCKET\"
vault_basilica_r2_endpoint: \"$R2_ENDPOINT\"

# R2 API Credentials (SENSITIVE)
vault_basilica_r2_access_key_id: \"$R2_ACCESS_KEY_ID\"
vault_basilica_r2_secret_access_key: \"$R2_SECRET_ACCESS_KEY\"

# Enable persistent storage deployment
vault_basilica_enable_persistent_storage: true
"

# Create temporary file
TEMP_FILE=$(mktemp)
echo "$VAULT_CONTENT" > "$TEMP_FILE"

# Encrypt with ansible-vault
echo -e "${GREEN}Step 3: Encrypting credentials...${NC}"
echo "$VAULT_PASSWORD" | ansible-vault encrypt "$TEMP_FILE" --output "$VAULT_FILE" --vault-password-file /dev/stdin

# Clean up
rm -f "$TEMP_FILE"
unset VAULT_PASSWORD
unset VAULT_PASSWORD_CONFIRM
unset R2_ACCESS_KEY_ID
unset R2_SECRET_ACCESS_KEY

echo -e "${GREEN}✅ Credentials encrypted and saved${NC}"
echo ""

# Display summary
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}✅ Setup Complete!${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "Encrypted credentials saved to:"
echo "  $VAULT_FILE"
echo ""
echo "Configuration:"
echo "  Backend:  $R2_BACKEND"
echo "  Bucket:   $R2_BUCKET"
echo "  Endpoint: $R2_ENDPOINT"
echo ""

# Show next steps
echo -e "${YELLOW}Next Steps:${NC}"
echo ""
echo "1. Review encrypted credentials (optional):"
if [ "$SAVE_PASSWORD" = "yes" ]; then
    echo "   ansible-vault view $VAULT_FILE --vault-password-file=$VAULT_PASSWORD_FILE"
else
    echo "   ansible-vault view $VAULT_FILE --ask-vault-pass"
fi
echo ""

echo "2. Deploy credentials to your cluster:"
if [ "$SAVE_PASSWORD" = "yes" ]; then
    echo "   cd ${SCRIPT_DIR}"
    echo "   ansible-playbook -i inventories/production.ini playbooks/e2e-apply.yml --vault-password-file=$VAULT_PASSWORD_FILE"
else
    echo "   cd ${SCRIPT_DIR}"
    echo "   ansible-playbook -i inventories/production.ini playbooks/e2e-apply.yml --ask-vault-pass"
fi
echo ""

echo "3. Verify deployment:"
echo "   kubectl get secret basilica-r2-credentials -n basilica-system"
echo "   kubectl describe secret basilica-r2-credentials -n basilica-system"
echo ""

echo -e "${YELLOW}Security Reminders:${NC}"
echo "  ⚠️  Keep your vault password secure"
echo "  ⚠️  Never commit .vault_password to git"
echo "  ⚠️  The vault.yml file is encrypted and safe to commit"
echo "  ⚠️  Rotate R2 API tokens regularly"
echo ""

# Check if .vault_password is in .gitignore
if [ -f "${SCRIPT_DIR}/.gitignore" ] && grep -q ".vault_password" "${SCRIPT_DIR}/.gitignore"; then
    echo -e "${GREEN}✅ .vault_password is in .gitignore${NC}"
else
    echo -e "${RED}⚠️  WARNING: Add .vault_password to .gitignore!${NC}"
fi
echo ""

echo -e "${GREEN}Done! You can now deploy credentials to your cluster.${NC}"
