#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for the Cathedral SN39 repository.
#
# Sets up an isolated virtualenv with the thin-subnet mechanism/test toolchain
# (matches .github/workflows/required-checks.yml) plus the two extra runtime
# deps the SQLite-backed publisher server needs (multipart form parsing and
# canonical blake3 hashing). Safe to run repeatedly.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# Cursor's default image ships Python 3.12 but omits the venv seed package, so
# `python3 -m venv` fails with an ensurepip error until it is installed.
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3.12-venv
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
. .venv/bin/activate

python -m pip install --upgrade pip

# Core mechanism + reviewed test/lint toolchain (`.[test]`) matches CI. The two
# trailing deps are the publisher runtime essentials for the default SQLite
# backend; Postgres (psycopg2) and S3 (boto3) stay optional and load lazily.
python -m pip install -e '.[test]' 'python-multipart>=0.0.9' 'blake3>=1.0'

echo "Cathedral dev environment ready: $(.venv/bin/python --version)"
