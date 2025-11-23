#!/usr/bin/env bash
set -euo pipefail

# Install local Subtensor CA into the system trust store so rustls platform verifier accepts it.
CA_SRC="/etc/ssl/certs/subtensor-ca.crt"
CA_DST="/usr/local/share/ca-certificates/subtensor-local.crt"

if [[ -f "$CA_SRC" ]]; then
  cp -f "$CA_SRC" "$CA_DST"
  update-ca-certificates >/dev/null 2>&1 || true
fi

exec "$@"

