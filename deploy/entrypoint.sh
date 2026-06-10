#!/bin/sh
# Runs as root: take ownership of the Railway volume mount (mounted root-owned
# over /app/data at runtime — build-time chown does not survive the mount),
# then drop privileges to the runtime user and exec the CMD with env intact.
set -e
mkdir -p /app/data
chown -R cathedral:cathedral /app/data
export HOME=/home/cathedral
exec setpriv --reuid=cathedral --regid=cathedral --init-groups "$@"
