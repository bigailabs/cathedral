#!/bin/bash
set -e

hostnamectl set-hostname ${hostname}

apt-get update
apt-get install -y curl wget python3 python3-pip

until curl -k --silent --fail "${server_url}/ping" > /dev/null 2>&1; do
  echo "Waiting for K3s API server to be available at ${server_url}..."
  sleep 5
done

curl -sfL https://get.k3s.io | sh -s - agent \
  --server "${server_url}" \
  --token "${k3s_token}"

echo "K3s agent joined cluster via ${server_url}"
