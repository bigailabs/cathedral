#!/usr/bin/env bash
set -euo pipefail

DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)

# Generate a local CA and a proper server certificate signed by that CA.
# This avoids clients rejecting the cert with CaUsedAsEndEntity.
mkdir -p "$DIR/tls"

maybe_generate_tls() {
  local tls_dir="$1"
  local need_gen=false

  if [[ ! -f "$tls_dir/ca.crt" || ! -f "$tls_dir/ca.key" ]]; then
    need_gen=true
  fi
  if [[ ! -f "$tls_dir/tls.crt" || ! -f "$tls_dir/tls.key" ]]; then
    need_gen=true
  fi
  # If existing server cert is marked as a CA, regenerate (rustls rejects CA as end-entity)
  if [[ -f "$tls_dir/tls.crt" ]] && openssl x509 -in "$tls_dir/tls.crt" -noout -text 2>/dev/null | grep -q "CA:TRUE"; then
    need_gen=true
  fi

  if [[ "$need_gen" == true ]]; then
    echo "Generating local dev CA and server certificate (SAN=localhost,host.docker.internal)" >&2
    # Root CA
    openssl genrsa -out "$tls_dir/ca.key" 4096 >/dev/null 2>&1
    openssl req -x509 -new -key "$tls_dir/ca.key" -sha256 -days 3650 \
      -subj "/CN=Cathedral Local Dev CA" \
      -addext "basicConstraints=critical,CA:true,pathlen:1" \
      -addext "keyUsage=critical,keyCertSign,cRLSign" \
      -addext "subjectKeyIdentifier=hash" \
      -out "$tls_dir/ca.crt" >/dev/null 2>&1

    # Server key + CSR
    openssl genrsa -out "$tls_dir/tls.key" 2048 >/dev/null 2>&1
    openssl req -new -key "$tls_dir/tls.key" -subj "/CN=host.docker.internal" \
      -addext "subjectAltName=DNS:localhost,DNS:host.docker.internal" \
      -out "$tls_dir/server.csr" >/dev/null 2>&1

    # Server certificate signed by our CA with proper EKU
    openssl x509 -req -in "$tls_dir/server.csr" -CA "$tls_dir/ca.crt" -CAkey "$tls_dir/ca.key" -CAcreateserial \
      -out "$tls_dir/tls.crt" -days 825 -sha256 \
      -extfile <(printf "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,DNS:host.docker.internal\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n") >/dev/null 2>&1

    rm -f "$tls_dir/server.csr" "$tls_dir/ca.crt.srl" 2>/dev/null || true
  fi

  chmod 644 "$tls_dir/ca.crt" "$tls_dir/tls.crt" "$tls_dir/tls.key" 2>/dev/null || true
}

maybe_generate_tls "$DIR/tls"

echo "Starting local Subtensor (Alice/Bob) and Envoy WSS..."
(
  cd "$DIR"
  docker compose up -d
)
# Give the node a moment to finish initializing RPC after the port opens
sleep "${SUBTENSOR_INIT_DELAY:-20}"
echo "Running local Subtensor init (wallets/subnet/registration) against WSS..."
chmod +x "$DIR/init.py"
CHAIN_ENDPOINT="wss://localhost:9944" NETUID=2 WALLET_PATH="$HOME/.bittensor/wallets" python3 "$DIR/init.py"
echo "Done. Endpoints:"
echo "  - WSS (clients): wss://localhost:9944"
echo "  - WS  (tools) : ws://127.0.0.1:19944"
echo "If your client requires CA trust, trust scripts/subtensor-local/tls/ca.crt (or set SSL_CERT_FILE to it)"
