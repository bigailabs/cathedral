#!/bin/bash
set -e

hostnamectl set-hostname ${hostname}

apt-get update
apt-get install -y curl wget python3 python3-pip

curl -sfL https://get.k3s.io | sh -s - server \
  --cluster-init \
  --token "${k3s_token}" \
  --tls-san "${nlb_dns}" \
  --write-kubeconfig-mode 644 \
  --disable traefik \
  --disable servicelb

echo "Primary K3s server initialized with cluster-init"
