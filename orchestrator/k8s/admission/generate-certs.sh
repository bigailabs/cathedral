#!/bin/bash
# Generate self-signed TLS certificates for the admission webhook.
# The certificates are stored in a Kubernetes secret.
#
# Usage: ./generate-certs.sh [--namespace basilica-system]
#
# For production, consider using cert-manager instead.

set -euo pipefail

NAMESPACE="${1:-basilica-system}"
SERVICE_NAME="basilica-operator-webhook"
SECRET_NAME="basilica-webhook-certs"
CERT_DIR=$(mktemp -d)

trap "rm -rf ${CERT_DIR}" EXIT

echo "Generating certificates for ${SERVICE_NAME}.${NAMESPACE}.svc"

# Generate CA
openssl genrsa -out "${CERT_DIR}/ca.key" 2048
openssl req -x509 -new -nodes -key "${CERT_DIR}/ca.key" \
    -subj "/CN=basilica-webhook-ca" \
    -days 3650 -out "${CERT_DIR}/ca.crt"

# Generate server key and CSR
openssl genrsa -out "${CERT_DIR}/tls.key" 2048

cat > "${CERT_DIR}/csr.conf" <<EOF
[req]
req_extensions = v3_req
distinguished_name = req_distinguished_name
[req_distinguished_name]
[v3_req]
basicConstraints = CA:FALSE
keyUsage = nonRepudiation, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names
[alt_names]
DNS.1 = ${SERVICE_NAME}
DNS.2 = ${SERVICE_NAME}.${NAMESPACE}
DNS.3 = ${SERVICE_NAME}.${NAMESPACE}.svc
DNS.4 = ${SERVICE_NAME}.${NAMESPACE}.svc.cluster.local
EOF

openssl req -new -key "${CERT_DIR}/tls.key" \
    -subj "/CN=${SERVICE_NAME}.${NAMESPACE}.svc" \
    -out "${CERT_DIR}/server.csr" \
    -config "${CERT_DIR}/csr.conf"

# Sign the certificate
openssl x509 -req -in "${CERT_DIR}/server.csr" \
    -CA "${CERT_DIR}/ca.crt" -CAkey "${CERT_DIR}/ca.key" \
    -CAcreateserial -out "${CERT_DIR}/tls.crt" \
    -days 365 -extensions v3_req \
    -extfile "${CERT_DIR}/csr.conf"

# Create or update the secret
echo "Creating/updating secret ${SECRET_NAME} in namespace ${NAMESPACE}"
kubectl create secret tls "${SECRET_NAME}" \
    --namespace="${NAMESPACE}" \
    --cert="${CERT_DIR}/tls.crt" \
    --key="${CERT_DIR}/tls.key" \
    --dry-run=client -o yaml | kubectl apply -f -

# Get CA bundle for webhook configuration
CA_BUNDLE=$(base64 -w0 < "${CERT_DIR}/ca.crt")
echo ""
echo "CA Bundle (for ValidatingWebhookConfiguration):"
echo "${CA_BUNDLE}"
echo ""
echo "To patch the webhook configuration:"
echo "kubectl patch validatingwebhookconfiguration basilica-storage-mount-validator \\"
echo "  --type='json' -p='[{\"op\": \"replace\", \"path\": \"/webhooks/0/clientConfig/caBundle\", \"value\": \"'${CA_BUNDLE}'\"}]'"
