#!/bin/bash
set -e

echo "Starting environment post-processing..."

# Get environment name from ENV_NAME
ENV_NAME="${ENV_NAME:-}"

if [ -z "$ENV_NAME" ]; then
    echo "No ENV_NAME specified, skipping environment post-processing"
    exit 0
fi

echo "Post-processing AgentGym environment: $ENV_NAME"
pip install loguru || true

# Environment-specific post-processing
case "$ENV_NAME" in
    "webshop")
        echo "Installing webshop-specific packages..."
        pip install --force-reinstall typing-extensions==4.5.0 || true
        ;;

    "sqlgym")
        echo "Installing sqlgym-specific packages..."
        pip install sqlgym requests || true
        ;;

    "babyai")
        echo "Installing babyai-specific packages..."
        pip install requests || true
        ;;

    "lmrlgym")
        echo "Installing lmrlgym-specific packages..."
        pip install pycparser cffi
        pip install -r /app/AgentGym/agentenv-lmrlgym/lmrlgym/requirements.txt
        ;;

    "tool")
        echo "Installing babyai-specific packages..."
        pip install geopy || true
        ;;

    "sciworld")
        echo "Installing sciworld-specific packages..."
        pip install numpy requests || true
        
        echo "Installing Java for sciworld..."
        apt-get update && \
        apt-get install -y --no-install-recommends openjdk-17-jdk-headless && \
        rm -rf /var/lib/apt/lists/*
        ;;
    
    *)
        echo "No specific post-processing needed for $ENV_NAME"
        ;;
esac

echo "Post-processing completed for $ENV_NAME"