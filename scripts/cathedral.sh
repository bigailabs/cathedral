#!/bin/bash
# Main Cathedral CLI tool

set -e

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

# Version
VERSION="0.1.0"

# Commands
print_usage() {
    cat << EOF
cathedral - Cathedral project management tool

USAGE:
    cathedral <COMMAND> [OPTIONS]

COMMANDS:
    test        Run or manage tests
    build       Build the project
    check       Check code quality
    provision   Provision and manage Cathedral infrastructure
    deploy      Deploy services to production
    manage      Manage running services
    help        Show help for a command

GLOBAL OPTIONS:
    -h, --help      Show this help message
    -v, --version   Show version information

EXAMPLES:
    cathedral test verify              # Verify test implementation
    cathedral test run                 # Run all tests
    cathedral test run -p              # Run tests in parallel
    cathedral test stats               # Show test statistics
    cathedral build --release          # Build release version
    cathedral provision all            # Complete end-to-end provisioning
    cathedral deploy production        # Deploy to production servers
    cathedral manage status            # Check service status

For more help on a command, use: cathedral help <COMMAND>
EOF
}

# Command: test
cmd_test() {
    local subcommand="${1:-help}"
    shift || true
    
    case "$subcommand" in
        run)
            exec "$SCRIPT_DIR/test/run.sh" "$@"
            ;;
        verify)
            exec "$SCRIPT_DIR/test/verify.sh" "$@"
            ;;
        stats)
            exec "$SCRIPT_DIR/test/stats.sh" "$@"
            ;;
        help|--help|-h)
            cat << EOF
cathedral test - Test management commands

USAGE:
    cathedral test <SUBCOMMAND> [OPTIONS]

SUBCOMMANDS:
    run         Run tests
    verify      Verify test implementation
    stats       Show test statistics

OPTIONS:
    -h, --help  Show this help message

EXAMPLES:
    cathedral test run                # Run all tests
    cathedral test run -p             # Run tests in parallel
    cathedral test run miner          # Run only miner tests
    cathedral test verify             # Verify test implementation
    cathedral test stats              # Show summary statistics
    cathedral test stats detailed     # Show detailed report
EOF
            ;;
        *)
            log_error "Unknown test subcommand: $subcommand"
            echo "Use 'cathedral test help' for available commands"
            exit 1
            ;;
    esac
}

# Command: build
cmd_build() {
    ensure_cathedral_root || exit 1
    
    local args=""
    while [[ $# -gt 0 ]]; do
        case $1 in
            --release|-r)
                args="$args --release"
                shift
                ;;
            --verbose|-v)
                args="$args -v"
                shift
                ;;
            *)
                args="$args $1"
                shift
                ;;
        esac
    done
    
    log_header "Building Cathedral"
    cargo build $args
}

# Command: check
cmd_check() {
    ensure_cathedral_root || exit 1
    
    log_header "Running checks"
    
    log_info "Format check..."
    cargo fmt -- --check
    
    log_info "Clippy check..."
    cargo clippy -- -D warnings
    
    log_info "Test compilation..."
    cargo test --no-run
    
    log_success "All checks passed!"
}

# Command: provision
cmd_provision() {
    local subcommand="${1:-help}"
    shift || true
    
    case "$subcommand" in
        all)
            exec "$SCRIPT_DIR/provision/provision-enhanced.sh" all "$@"
            ;;
        servers)
            exec "$SCRIPT_DIR/provision/provision-enhanced.sh" servers "$@"
            ;;
        build)
            exec "$SCRIPT_DIR/provision/deploy.sh" build "$@"
            ;;
        config|configure)
            exec "$SCRIPT_DIR/provision/config-generator.sh" all "$@"
            ;;
        wallets)
            exec "$SCRIPT_DIR/provision/wallet-manager.sh" create-all "$@"
            ;;
        deploy)
            exec "$SCRIPT_DIR/provision/deploy.sh" all "$@"
            ;;
        validate|test)
            exec "$SCRIPT_DIR/provision/preflight-check.sh" "$@"
            ;;
        help|--help|-h)
            cat << EOF
cathedral provision - Provision and manage Cathedral infrastructure

USAGE:
    cathedral provision <SUBCOMMAND> [OPTIONS]

SUBCOMMANDS:
    all         Complete end-to-end provisioning with checks
    servers     Setup servers with dependencies
    build       Build all binaries locally
    config      Generate service configurations
    wallets     Create development wallets
    deploy      Deploy binaries and configs
    validate    Run pre-flight checks

OPTIONS:
    -h, --help  Show this help message

EXAMPLES:
    cathedral provision all production    # Complete provisioning
    cathedral provision build             # Build binaries only
    cathedral provision config production # Generate configs
    cathedral provision validate          # Run pre-flight checks

FEATURES:
    - Pre-flight checks before deployment
    - Automatic configuration generation
    - Development wallet creation
    - Binary deployment with rollback
    - Systemd service management
EOF
            ;;
        *)
            log_error "Unknown provision subcommand: $subcommand"
            echo "Use 'cathedral provision help' for available commands"
            exit 1
            ;;
    esac
}

# Command: deploy
cmd_deploy() {
    local subcommand="${1:-help}"
    shift || true
    
    case "$subcommand" in
        all)
            exec "$SCRIPT_DIR/provision/deploy.sh" all "$@"
            ;;
        binaries)
            exec "$SCRIPT_DIR/provision/deploy.sh" binaries "$@"
            ;;
        configs)
            # Generate and deploy configs
            "$SCRIPT_DIR/provision/config-generator.sh" all "$@" && \
            exec "$SCRIPT_DIR/provision/deploy.sh" binaries "$@"
            ;;
        production|prod)
            exec "$SCRIPT_DIR/provision/deploy.sh" all production
            ;;
        staging|stage)
            exec "$SCRIPT_DIR/provision/deploy.sh" all staging
            ;;
        help|--help|-h)
            cat << EOF
cathedral deploy - Deploy services to environments

USAGE:
    cathedral deploy <SUBCOMMAND> [environment]

SUBCOMMANDS:
    all         Deploy everything (binaries + configs)
    binaries    Deploy binaries only
    configs     Generate and deploy configurations
    production  Shortcut for 'all production'
    staging     Shortcut for 'all staging'

ENVIRONMENT:
    production  Production servers (default)
    staging     Staging servers

OPTIONS:
    -h, --help   Show this help message

EXAMPLES:
    cathedral deploy all production      # Full deployment
    cathedral deploy binaries            # Deploy binaries only
    cathedral deploy configs staging     # Deploy configs to staging
EOF
            ;;
        *)
            log_error "Unknown deploy command: $subcommand"
            echo "Use 'cathedral deploy help' for available commands"
            exit 1
            ;;
    esac
}

# Command: manage
cmd_manage() {
    local operation="${1:-help}"
    shift || true
    
    case "$operation" in
        status|health)
            exec "$SCRIPT_DIR/provision/service-manager.sh" status "$@"
            ;;
        start)
            exec "$SCRIPT_DIR/provision/service-manager.sh" start "$@"
            ;;
        stop)
            exec "$SCRIPT_DIR/provision/service-manager.sh" stop "$@"
            ;;
        restart)
            exec "$SCRIPT_DIR/provision/service-manager.sh" start "$@"
            ;;
        logs)
            exec "$SCRIPT_DIR/provision/service-manager.sh" logs "$@"
            ;;
        enable)
            exec "$SCRIPT_DIR/provision/service-manager.sh" enable "$@"
            ;;
        deploy-services)
            exec "$SCRIPT_DIR/provision/service-manager.sh" deploy "$@"
            ;;
        help|--help|-h)
            cat << EOF
cathedral manage - Manage running services

USAGE:
    cathedral manage <OPERATION> [service] [environment]

OPERATIONS:
    status              Check service status
    start <service|all> Start service(s)
    stop <service|all>  Stop service(s)  
    restart <service>   Restart service
    logs <service>      View service logs
    enable              Enable auto-start
    deploy-services     Deploy systemd files

SERVICES:
    executor, miner, validator, all

OPTIONS:
    -h, --help  Show this help message

EXAMPLES:
    cathedral manage status                    # Check all services
    cathedral manage start all production      # Start all services
    cathedral manage logs executor             # View executor logs
    cathedral manage enable production         # Enable auto-start
EOF
            ;;
        *)
            log_error "Unknown management operation: $operation"
            echo "Use 'cathedral manage help' for available operations"
            exit 1
            ;;
    esac
}

# Command: help
cmd_help() {
    local command="${1:-}"
    
    case "$command" in
        test)
            cmd_test help
            ;;
        build)
            cat << EOF
cathedral build - Build the project

USAGE:
    cathedral build [OPTIONS]

OPTIONS:
    -r, --release   Build in release mode
    -v, --verbose   Verbose output
    -h, --help      Show this help message

EXAMPLES:
    cathedral build              # Debug build
    cathedral build --release    # Release build
EOF
            ;;
        check)
            cat << EOF
cathedral check - Check code quality

USAGE:
    cathedral check

Runs format check, clippy, and test compilation.
EOF
            ;;
        provision)
            cmd_provision help
            ;;
        deploy)
            cmd_deploy help
            ;;
        manage)
            cmd_manage help
            ;;
        *)
            print_usage
            ;;
    esac
}

# Main
main() {
    case "${1:-help}" in
        test)
            shift
            cmd_test "$@"
            ;;
        build)
            shift
            cmd_build "$@"
            ;;
        check)
            shift
            cmd_check "$@"
            ;;
        provision)
            shift
            cmd_provision "$@"
            ;;
        deploy)
            shift
            cmd_deploy "$@"
            ;;
        manage)
            shift
            cmd_manage "$@"
            ;;
        help|--help|-h)
            shift
            cmd_help "$@"
            ;;
        --version|-v)
            echo "cathedral $VERSION"
            ;;
        *)
            log_error "Unknown command: $1"
            print_usage
            exit 1
            ;;
    esac
}

main "$@"