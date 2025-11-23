#!/bin/bash

set -euo pipefail

PROFILE="${1:-}"
WORKSPACE="${2:-}"
ACTION="${3:-plan}"

if [[ -z "$PROFILE" ]]; then
    echo "Usage: $0 <profile> <workspace> [action]"
    echo ""
    echo "Arguments:"
    echo "  profile:   AWS profile name (e.g., 'tplr', 'production', 'dev')"
    echo "  workspace: Terraform workspace - 'dev' or 'prod'"
    echo "  action:    Terraform action (default: plan)"
    echo ""
    echo "Actions:"
    echo "  plan      - Preview infrastructure changes (default)"
    echo "  apply     - Apply changes with confirmation"
    echo "  apply-f   - Force apply without confirmation"
    echo "  destroy   - Destroy infrastructure with confirmation"
    echo "  validate  - Validate Terraform configuration"
    echo "  output    - Show Terraform outputs"
    echo ""
    echo "Example:"
    echo "  $0 tplr prod apply-f"
    exit 1
fi

if [[ -z "$WORKSPACE" ]]; then
    echo "Error: workspace argument is required"
    echo "Usage: $0 <profile> <workspace> [action]"
    exit 1
fi

if [[ ! "$WORKSPACE" =~ ^(dev|prod)$ ]]; then
    echo "Error: Workspace must be 'dev' or 'prod', got: '$WORKSPACE'"
    exit 1
fi

if [[ ! "$ACTION" =~ ^(plan|apply|apply-f|destroy|validate|output)$ ]]; then
    echo "Error: Action must be 'plan', 'apply', 'apply-f', 'destroy', 'validate', or 'output'"
    exit 1
fi

if ! command -v terraform &> /dev/null; then
    echo "Error: Terraform not found. Please install Terraform."
    exit 1
fi

export AWS_PROFILE="$PROFILE"

if ! aws sts get-caller-identity --profile "$PROFILE" &> /dev/null; then
    echo "Error: AWS credentials not configured for profile '$PROFILE'"
    echo ""
    echo "To configure AWS credentials for this profile, run:"
    echo "  aws configure --profile $PROFILE"
    echo ""
    echo "Or set environment variables:"
    echo "  export AWS_ACCESS_KEY_ID=..."
    echo "  export AWS_SECRET_ACCESS_KEY=..."
    echo "  export AWS_DEFAULT_REGION=us-east-2"
    exit 1
fi

echo "✓ Using AWS profile: $PROFILE"
aws sts get-caller-identity --profile "$PROFILE" --query 'Account' --output text | xargs -I {} echo "✓ AWS Account ID: {}"

cd "$(dirname "$0")"

if [[ ! -f "terraform.tfvars" ]]; then
    echo "Error: terraform.tfvars not found"
    echo ""
    echo "Please create it from terraform.tfvars.example:"
    echo "  cp terraform.tfvars.example terraform.tfvars"
    echo "  vim terraform.tfvars"
    exit 1
fi

echo "✓ Found terraform.tfvars"

terraform init
terraform workspace select "$WORKSPACE" 2>/dev/null || terraform workspace new "$WORKSPACE"

echo "✓ Using Terraform workspace: $WORKSPACE"

case "$ACTION" in
    plan)
        terraform plan
        ;;
    apply)
        if [[ "$WORKSPACE" == "prod" ]]; then
            echo ""
            echo "========================================="
            echo "WARNING: Applying to PRODUCTION"
            echo "========================================="
            echo ""
            read -p "Continue? (yes/no): " confirm
            if [[ "$confirm" != "yes" ]]; then
                echo "Aborted."
                exit 0
            fi
        fi
        terraform apply
        ;;
    apply-f)
        terraform apply -auto-approve
        ;;
    destroy)
        echo ""
        echo "========================================="
        echo "WARNING: This will DESTROY infrastructure in '$WORKSPACE'"
        echo "========================================="
        echo ""
        read -p "Type 'destroy-$WORKSPACE' to confirm: " confirm
        if [[ "$confirm" != "destroy-$WORKSPACE" ]]; then
            echo "Aborted."
            exit 0
        fi
        terraform destroy
        ;;
    validate)
        terraform validate
        if [[ $? -eq 0 ]]; then
            echo "✓ Terraform configuration is valid"
        else
            echo "✗ Terraform configuration has errors"
            exit 1
        fi
        ;;
    output)
        terraform output
        ;;
esac
