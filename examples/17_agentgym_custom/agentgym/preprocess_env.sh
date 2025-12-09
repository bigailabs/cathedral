#!/bin/bash
set -e

echo "Starting environment preprocessing..."

# Get environment name from ENV_NAME
ENV_NAME="${ENV_NAME:-}"

if [ -z "$ENV_NAME" ]; then
    echo "No ENV_NAME specified, skipping environment installation"
    exit 0
fi

echo "Installing AgentGym environment: $ENV_NAME"

# Set environment-specific compiler flags for alfworld
if [ "$ENV_NAME" = "alfworld" ]; then
    export CFLAGS="-O3 -fPIC -Wno-incompatible-pointer-types"
    export CPPFLAGS="-Wno-incompatible-pointer-types"
fi

ENV_PATH="/app/AgentGym/agentenv-$ENV_NAME"

# Install Miniconda if environment.yml exists
ENV_YAML_PATH="$ENV_PATH/environment.yml"
if [ -f "$ENV_YAML_PATH" ]; then
    echo "Found environment.yml for $ENV_NAME, setting up conda environment..."
    
    CONDA_PATH="/opt/miniconda"
    if [ ! -d "$CONDA_PATH" ]; then
        echo "Installing Miniconda..."
        wget -q https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O /tmp/miniconda.sh
        bash /tmp/miniconda.sh -b -p $CONDA_PATH
        rm /tmp/miniconda.sh
        
        # Accept conda terms of service
        echo "Accepting conda terms of service..."
        $CONDA_PATH/bin/conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main || true
        $CONDA_PATH/bin/conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r || true
    else
        echo "Miniconda already installed"
    fi
    
    echo "Creating conda environment from $ENV_YAML_PATH"
    conda env create --yes -f "$ENV_YAML_PATH" -n agentenv || true
fi

# Install required Python packages
echo "Installing required Python packages..."
python -m pip install --upgrade pip setuptools wheel || true
python -m pip install --no-cache-dir fastapi uvicorn httpx pydantic || true

# Execute setup.sh if exists
SETUP_SCRIPT="$ENV_PATH/setup.sh"
if [ -f "$SETUP_SCRIPT" ]; then
    echo "Found setup.sh for $ENV_NAME, executing..."
    chmod +x "$SETUP_SCRIPT"
    export PIP_NO_INPUT=1
    cd "$ENV_PATH"
    bash "$SETUP_SCRIPT" || true
    cd /app
else
    # Install the environment package if directory exists
    if [ -d "$ENV_PATH" ]; then
        pip install -e "$ENV_PATH" --no-build-isolation || true
    fi
fi

echo "Environment preprocessing completed for $ENV_NAME"