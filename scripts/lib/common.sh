#!/bin/bash
# Common functions and utilities for Cathedral scripts

# Colors for output
export RED='\033[0;31m'
export GREEN='\033[0;32m'
export YELLOW='\033[1;33m'
export BLUE='\033[0;34m'
export PURPLE='\033[0;35m'
export NC='\033[0m' # No Color

# Project paths
export CATHEDRAL_ROOT="${CATHEDRAL_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export SCRIPTS_DIR="$CATHEDRAL_ROOT/scripts"
export CRATES_DIR="$CATHEDRAL_ROOT/crates"

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_header() {
    echo -e "${PURPLE}=== $1 ===${NC}"
}

# Check if a command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Check if we're in the correct directory
ensure_cathedral_root() {
    if [ ! -f "$CATHEDRAL_ROOT/Cargo.toml" ]; then
        log_error "Not in Cathedral root directory"
        log_info "Expected root: $CATHEDRAL_ROOT"
        return 1
    fi
    cd "$CATHEDRAL_ROOT" || return 1
}

# Get list of crates
get_crates() {
    find "$CRATES_DIR" -name "Cargo.toml" -type f | while read -r cargo_file; do
        dirname "$cargo_file" | xargs basename
    done | sort | uniq
}

# Check if a crate exists
crate_exists() {
    local crate=$1
    [ -d "$CRATES_DIR/$crate" ] && [ -f "$CRATES_DIR/$crate/Cargo.toml" ]
}