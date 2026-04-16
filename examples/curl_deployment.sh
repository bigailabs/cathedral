#!/bin/bash
#
# Cathedral UserDeployment - curl API Example
#
# This script demonstrates how to create a UserDeployment with public access
# and persistent storage using direct API calls with curl.
#
# The deployment runs a FastAPI application that:
# - Exposes HTTP endpoints at a public URL
# - Reads and writes data to persistent FUSE storage backed by object storage
#
# Prerequisites:
#   - BASILICA_API_TOKEN environment variable set
#   - curl and jq installed
#
# Usage:
#   export BASILICA_API_TOKEN="your-token-here"
#   ./userdeployment_curl_example.sh
#

set -e

# Configuration
BASILICA_API_URL="${BASILICA_API_URL:-https://api.basilica.ai}"
INSTANCE_NAME="curl-fastapi-$(date +%s)"

# Check prerequisites
if [ -z "$BASILICA_API_TOKEN" ]; then
    echo "Error: BASILICA_API_TOKEN environment variable not set"
    echo ""
    echo "To get a token:"
    echo "  1. Run: cathedral tokens create my-token"
    echo "  2. Export: export BASILICA_API_TOKEN='cathedral_...'"
    exit 1
fi

if ! command -v jq &> /dev/null; then
    echo "Warning: jq not installed - JSON output will not be formatted"
    JQ_CMD="cat"
else
    JQ_CMD="jq"
fi

echo "========================================================================"
echo "Cathedral UserDeployment - curl API Example"
echo "========================================================================"
echo ""
echo "Configuration:"
echo "  API URL:       $BASILICA_API_URL"
echo "  Instance Name: $INSTANCE_NAME"
echo "  Token:         ${BASILICA_API_TOKEN:0:20}..."
echo ""

# Step 1: Create the deployment
echo "Step 1: Creating deployment with FastAPI app and storage..."
echo "------------------------------------------------------------------------"

# The FastAPI application embedded in the deployment
# It provides endpoints for health, storage read/write, and info
FASTAPI_APP=$(cat <<'PYEOF'
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import os
from pathlib import Path
import socket
from datetime import datetime

app = FastAPI(title="Cathedral Storage Demo")

STORAGE_PATH = Path("/data")

class WriteRequest(BaseModel):
    filename: str
    content: str

@app.get("/")
def root():
    return {
        "service": "Cathedral FastAPI Demo",
        "hostname": socket.gethostname(),
        "storage_mounted": STORAGE_PATH.exists(),
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.get("/storage/list")
def list_files():
    if not STORAGE_PATH.exists():
        raise HTTPException(status_code=503, detail="Storage not mounted")
    files = []
    for f in STORAGE_PATH.rglob("*"):
        if f.is_file() and not f.name.startswith("."):
            files.append(str(f.relative_to(STORAGE_PATH)))
    return {"files": files, "count": len(files)}

@app.post("/storage/write")
def write_file(req: WriteRequest):
    if not STORAGE_PATH.exists():
        raise HTTPException(status_code=503, detail="Storage not mounted")
    file_path = STORAGE_PATH / req.filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(req.content)
    return {"success": True, "path": req.filename, "size": len(req.content)}

@app.get("/storage/read/{filename:path}")
def read_file(filename: str):
    if not STORAGE_PATH.exists():
        raise HTTPException(status_code=503, detail="Storage not mounted")
    file_path = STORAGE_PATH / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return {"path": filename, "content": file_path.read_text()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
PYEOF
)

# Escape the Python app for JSON embedding using base64 to avoid shell escaping issues
FASTAPI_APP_B64=$(echo "$FASTAPI_APP" | base64 -w0)

# Create the deployment request JSON
# Note: The command decodes the base64 Python code and runs it
# Storage requires full StorageSpec structure with persistent config
DEPLOYMENT_JSON=$(cat <<EOF
{
    "instance_name": "$INSTANCE_NAME",
    "image": "python:3.11-slim",
    "replicas": 1,
    "port": 8000,
    "command": ["bash", "-c", "pip install -q fastapi uvicorn pydantic && echo ${FASTAPI_APP_B64} | base64 -d > /tmp/app.py && python /tmp/app.py"],
    "cpu": "500m",
    "memory": "512Mi",
    "ttl_seconds": 1800,
    "public": true,
    "storage": {
        "persistent": {
            "enabled": true,
            "backend": "r2",
            "bucket": "",
            "syncIntervalMs": 1000,
            "cacheSizeMb": 1024,
            "mountPath": "/data"
        }
    }
}
EOF
)

# Make the API call to create the deployment
RESPONSE=$(curl -s -w "\n%{http_code}" \
    -X POST \
    -H "Authorization: Bearer $BASILICA_API_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$DEPLOYMENT_JSON" \
    "$BASILICA_API_URL/deployments")

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n-1)

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
    echo "Deployment created successfully (HTTP $HTTP_CODE)"
    echo ""
    echo "Response:"
    echo "$BODY" | $JQ_CMD

    # Extract the actual instance name and public URL from the response
    # The API may return a different instance name (UUID) than the one we requested
    if [ "$JQ_CMD" = "jq" ]; then
        ACTUAL_INSTANCE_NAME=$(echo "$BODY" | jq -r '.instanceName // empty')
        PUBLIC_URL=$(echo "$BODY" | jq -r '.url // empty')
        STATE=$(echo "$BODY" | jq -r '.state // empty')
    else
        ACTUAL_INSTANCE_NAME=$(echo "$BODY" | grep -o '"instanceName":"[^"]*"' | cut -d'"' -f4)
        PUBLIC_URL=$(echo "$BODY" | grep -o '"url":"[^"]*"' | cut -d'"' -f4)
        STATE=$(echo "$BODY" | grep -o '"state":"[^"]*"' | cut -d'"' -f4)
    fi

    # Use the actual instance name for all subsequent operations
    if [ -n "$ACTUAL_INSTANCE_NAME" ]; then
        INSTANCE_NAME="$ACTUAL_INSTANCE_NAME"
    fi
else
    echo "Failed to create deployment (HTTP $HTTP_CODE)"
    echo "Response: $BODY"
    exit 1
fi

echo ""
echo "Step 2: Waiting for deployment to be ready..."
echo "------------------------------------------------------------------------"

# Poll deployment status until ready
MAX_WAIT=180
ELAPSED=0

while [ $ELAPSED -lt $MAX_WAIT ]; do
    RESPONSE=$(curl -s \
        -H "Authorization: Bearer $BASILICA_API_TOKEN" \
        "$BASILICA_API_URL/deployments/$INSTANCE_NAME")

    if [ "$JQ_CMD" = "jq" ]; then
        STATE=$(echo "$RESPONSE" | jq -r '.state // empty')
        READY=$(echo "$RESPONSE" | jq -r '.replicas.ready // 0')
        DESIRED=$(echo "$RESPONSE" | jq -r '.replicas.desired // 0')
        PUBLIC_URL=$(echo "$RESPONSE" | jq -r '.url // empty')
    else
        STATE=$(echo "$RESPONSE" | grep -o '"state":"[^"]*"' | cut -d'"' -f4)
        READY="0"
        DESIRED="1"
    fi

    echo "  ${ELAPSED}s: State=$STATE, Ready=$READY/$DESIRED"

    if [ "$STATE" = "Active" ] || [ "$STATE" = "Running" ]; then
        if [ "$READY" = "$DESIRED" ] && [ "$READY" != "0" ]; then
            echo ""
            echo "Deployment is ready!"
            break
        fi
    fi

    sleep 5
    ELAPSED=$((ELAPSED + 5))
done

if [ $ELAPSED -ge $MAX_WAIT ]; then
    echo ""
    echo "Warning: Deployment not ready after ${MAX_WAIT}s"
fi

# Wait extra time for the HTTP server to start
echo ""
echo "Waiting 15s for HTTP server to initialize..."
sleep 15

echo ""
echo "Step 3: Testing the deployment..."
echo "------------------------------------------------------------------------"

# Test the health endpoint
echo ""
echo "Testing health endpoint: $PUBLIC_URL/health"
HEALTH_RESPONSE=$(curl -s --max-time 10 "$PUBLIC_URL/health" || echo '{"error": "request failed"}')
echo "Response: $HEALTH_RESPONSE"

# Test the root endpoint
echo ""
echo "Testing root endpoint: $PUBLIC_URL/"
ROOT_RESPONSE=$(curl -s --max-time 10 "$PUBLIC_URL/" || echo '{"error": "request failed"}')
echo "Response:"
echo "$ROOT_RESPONSE" | $JQ_CMD

echo ""
echo "Step 4: Testing storage operations..."
echo "------------------------------------------------------------------------"

# Write a file to storage
echo ""
echo "Writing file to storage..."
WRITE_RESPONSE=$(curl -s --max-time 10 \
    -X POST \
    -H "Content-Type: application/json" \
    -d '{"filename": "hello.txt", "content": "Hello from Cathedral storage!"}' \
    "$PUBLIC_URL/storage/write" || echo '{"error": "request failed"}')
echo "Write response: $WRITE_RESPONSE"

# Wait for sync
sleep 3

# Read the file back
echo ""
echo "Reading file from storage..."
READ_RESPONSE=$(curl -s --max-time 10 "$PUBLIC_URL/storage/read/hello.txt" || echo '{"error": "request failed"}')
echo "Read response:"
echo "$READ_RESPONSE" | $JQ_CMD

# List files
echo ""
echo "Listing files in storage..."
LIST_RESPONSE=$(curl -s --max-time 10 "$PUBLIC_URL/storage/list" || echo '{"error": "request failed"}')
echo "List response:"
echo "$LIST_RESPONSE" | $JQ_CMD

echo ""
echo "Step 5: Cleanup..."
echo "------------------------------------------------------------------------"

# Ask before cleanup
read -p "Delete the deployment? (y/N): " CONFIRM
if [ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ]; then
    DELETE_RESPONSE=$(curl -s -w "\n%{http_code}" \
        -X DELETE \
        -H "Authorization: Bearer $BASILICA_API_TOKEN" \
        "$BASILICA_API_URL/deployments/$INSTANCE_NAME")

    DELETE_HTTP=$(echo "$DELETE_RESPONSE" | tail -n1)
    DELETE_BODY=$(echo "$DELETE_RESPONSE" | head -n-1)

    if [ "$DELETE_HTTP" = "200" ]; then
        echo "Deployment deleted successfully"
    else
        echo "Delete returned HTTP $DELETE_HTTP: $DELETE_BODY"
    fi
else
    echo "Deployment left running at: $PUBLIC_URL"
    echo ""
    echo "To delete later, run:"
    echo "  curl -X DELETE -H \"Authorization: Bearer \$BASILICA_API_TOKEN\" \\"
    echo "    \"$BASILICA_API_URL/deployments/$INSTANCE_NAME\""
fi

echo ""
echo "========================================================================"
echo "Example completed!"
echo "========================================================================"
echo ""
echo "Summary:"
echo "  Instance:    $INSTANCE_NAME"
echo "  Public URL:  $PUBLIC_URL"
echo "  Storage:     /data (backed by object storage)"
echo ""
echo "Key API Endpoints Used:"
echo "  POST   /deployments             - Create deployment"
echo "  GET    /deployments/{name}      - Get deployment status"
echo "  DELETE /deployments/{name}      - Delete deployment"
echo ""
echo "For more information, see:"
echo "  - docs/USERDEPLOYMENT-ARCHITECTURE.md"
echo "  - examples/README-deployments.md"
