#!/bin/bash
set -e

hostnamectl set-hostname ${hostname}

apt-get update
apt-get install -y curl wget python3 python3-pip

until curl -k --silent --fail "${server_url}/ping" > /dev/null 2>&1; do
  echo "Waiting for primary K3s server to be available at ${server_url}..."
  sleep 5
done

curl -sfL https://get.k3s.io | sh -s - server \
  --server "${server_url}" \
  --token "${k3s_token}" \
  --tls-san "${nlb_dns}" \
  --write-kubeconfig-mode 644 \
  --disable traefik \
  --disable servicelb

echo "Secondary K3s server joined cluster via ${server_url}"
