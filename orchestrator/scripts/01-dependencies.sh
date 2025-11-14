#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Installing Ansible Galaxy collections..."
ansible-galaxy collection install -r requirements.yml --force

echo "Dependencies installed successfully."
