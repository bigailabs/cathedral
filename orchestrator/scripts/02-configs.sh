#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Checking configuration files..."

if [ ! -f "inventories/production.ini" ]; then
    echo "Creating production inventory from example..."
    cp inventories/example.ini inventories/production.ini
    echo "Please edit inventories/production.ini with your hosts."
fi

echo "Configuration check complete."
