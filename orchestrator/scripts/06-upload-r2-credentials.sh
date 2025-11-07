#!/usr/bin/env bash
# Upload R2 credentials directly to cluster (for pre-configured credentials)
# Usage: ./upload-r2-credentials.sh

set -euo pipefail

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}  Upload R2 Credentials to Cluster${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "This will upload your R2 credentials directly to the cluster."
echo ""

# Check if credentials are provided via environment variables
if [ -z "${BASILICA_R2_BUCKET:-}" ] || [ -z "${BASILICA_R2_ACCESS_KEY_ID:-}" ] || [ -z "${BASILICA_R2_SECRET_ACCESS_KEY:-}" ]; then
    echo -e "${YELLOW}R2 credentials not found in environment variables.${NC}"
    echo ""
    echo "Please provide your R2 credentials:"
    echo ""

    read -p "R2 Bucket Name: " BASILICA_R2_BUCKET
    read -p "R2 Endpoint (e.g., https://abc123.r2.cloudflarestorage.com): " BASILICA_R2_ENDPOINT
    read -p "R2 Access Key ID: " BASILICA_R2_ACCESS_KEY_ID
    read -s -p "R2 Secret Access Key: " BASILICA_R2_SECRET_ACCESS_KEY
    echo ""
    read -p "R2 Backend (r2/s3/gcs) [r2]: " BASILICA_R2_BACKEND
    BASILICA_R2_BACKEND=${BASILICA_R2_BACKEND:-r2}
    echo ""
else
    echo -e "${GREEN}✅ Found R2 credentials in environment variables${NC}"
    echo ""
    echo "Bucket: ${BASILICA_R2_BUCKET}"
    echo "Endpoint: ${BASILICA_R2_ENDPOINT:-<not set>}"
    echo "Access Key ID: ${BASILICA_R2_ACCESS_KEY_ID:0:8}... (masked)"
    echo "Backend: ${BASILICA_R2_BACKEND:-r2}"
    echo ""

    read -p "Use these credentials? (yes/no): " USE_ENV
    if [ "$USE_ENV" != "yes" ]; then
        echo ""
        echo "Please provide your R2 credentials:"
        echo ""

        read -p "R2 Bucket Name: " BASILICA_R2_BUCKET
        read -p "R2 Endpoint: " BASILICA_R2_ENDPOINT
        read -p "R2 Access Key ID: " BASILICA_R2_ACCESS_KEY_ID
        read -s -p "R2 Secret Access Key: " BASILICA_R2_SECRET_ACCESS_KEY
        echo ""
        read -p "R2 Backend (r2/s3/gcs) [r2]: " BASILICA_R2_BACKEND
        BASILICA_R2_BACKEND=${BASILICA_R2_BACKEND:-r2}
        echo ""
    fi
fi

# Export for Ansible
export BASILICA_R2_BUCKET
export BASILICA_R2_ENDPOINT
export BASILICA_R2_ACCESS_KEY_ID
export BASILICA_R2_SECRET_ACCESS_KEY
export BASILICA_R2_BACKEND=${BASILICA_R2_BACKEND:-r2}
export BASILICA_ENABLE_PERSISTENT_STORAGE=true

# Run Ansible playbook
echo -e "${GREEN}Uploading credentials to cluster...${NC}"
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check inventory file
INVENTORY="inventories/production.ini"
if [ ! -f "$INVENTORY" ]; then
    INVENTORY="inventories/example.ini"
    echo -e "${YELLOW}Using example inventory: $INVENTORY${NC}"
    echo ""
fi

# Run the playbook (only the basilica-storage role)
ansible-playbook -i "$INVENTORY" playbooks/e2e-apply.yml \
    --tags basilica_storage \
    -e "basilica_enable_persistent_storage=true" \
    -e "basilica_r2_bucket=$BASILICA_R2_BUCKET" \
    -e "basilica_r2_endpoint=${BASILICA_R2_ENDPOINT}" \
    -e "basilica_r2_access_key_id=$BASILICA_R2_ACCESS_KEY_ID" \
    -e "basilica_r2_secret_access_key=$BASILICA_R2_SECRET_ACCESS_KEY" \
    -e "basilica_r2_backend=${BASILICA_R2_BACKEND}"

echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}✅ Upload Complete!${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "R2 credentials have been uploaded to the cluster."
echo ""
echo "Verify deployment:"
echo "  kubectl get secret basilica-r2-credentials -n basilica-system"
echo "  kubectl describe secret basilica-r2-credentials -n basilica-system"
echo ""
echo "Test with example job:"
echo "  kubectl apply -f ../../examples/persistent-storage-job.yaml"
echo ""
