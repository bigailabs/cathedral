#!/bin/bash
set -euo pipefail

NPROC_PER_NODE="${NPROC_PER_NODE:-1}"
NNODES="${NNODES:-1}"
NODE_RANK="${NODE_RANK:-0}"
MASTER_ADDR="${MASTER_ADDR:-localhost}"
MASTER_PORT="${MASTER_PORT:-29400}"
RDZV_ID="${RDZV_ID:-default}"

if [ "$NNODES" -eq 1 ]; then
    exec torchrun \
        --standalone \
        --nproc_per_node="$NPROC_PER_NODE" \
        train.py "$@"
else
    exec torchrun \
        --nnodes="$NNODES" \
        --nproc_per_node="$NPROC_PER_NODE" \
        --node_rank="$NODE_RANK" \
        --rdzv_id="$RDZV_ID" \
        --rdzv_backend=c10d \
        --rdzv_endpoint="$MASTER_ADDR:$MASTER_PORT" \
        train.py "$@"
fi
