#!/bin/bash
set -euo pipefail

echo "Installing Ansible and required dependencies..."

if command -v apt-get &> /dev/null; then
    sudo apt-get update
    sudo apt-get install -y python3 python3-pip python3-venv
else
    echo "Unsupported package manager. Please install Python 3 manually."
    exit 1
fi

python3 -m pip install --user --upgrade pip
python3 -m pip install --user "ansible>=2.11.0"

echo "Ansible installed successfully:"
ansible --version
