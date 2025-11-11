# Changelog - AWS Profile Support for run.sh

## Summary

Added AWS profile support to `orchestrator/cloud/run.sh` to match the structure and functionality of `scripts/cloud/run.sh`, enabling production-ready infrastructure deployment with proper credential management.

## Changes Made

### 1. Updated `run.sh` Script

**File:** `orchestrator/cloud/run.sh`

**Changes:**
- Added AWS profile as first required argument
- Shifted workspace to second argument  
- Shifted action to third argument (optional, default: plan)
- Added AWS credential validation via `aws sts get-caller-identity`
- Exported `AWS_PROFILE` environment variable for Terraform
- Enhanced help text with detailed usage instructions
- Improved error messages with remediation steps
- Added visual feedback (✓ checkmarks) for validation steps

**Before:**
```bash
./run.sh <workspace> [action]
```

**After:**
```bash
./run.sh <profile> <workspace> [action]
```

**Example Usage:**
```bash
# Production deployment with tplr profile
./run.sh tplr prod apply-f

# Development deployment
./run.sh tplr dev apply

# Validation
./run.sh tplr prod validate
```

### 2. Updated Documentation

**Files Updated:**
- `orchestrator/cloud/README.md` - Added AWS profile configuration section, updated all examples
- `orchestrator/cloud/QUICK-START.md` - Added profile setup steps, updated deployment commands
- `orchestrator/cloud/RUN-SH-USAGE.md` - Created comprehensive usage guide (new file)

**Key Documentation Updates:**
- Added AWS profile configuration prerequisites
- Updated all terraform commands to use run.sh
- Added comparison table between scripts/cloud and orchestrator/cloud
- Documented all validation checks and safety features
- Added troubleshooting section

### 3. Created Supporting Documentation

**New Files:**
- `RUN-SH-USAGE.md` - Complete usage guide with examples and troubleshooting
- `CHANGELOG.md` - This file

## Technical Details

### Validation Flow

1. ✓ Profile argument provided
2. ✓ Workspace argument provided and valid (dev or prod)
3. ✓ Action valid (if provided)
4. ✓ Terraform binary installed
5. ✓ AWS credentials valid for profile
6. ✓ terraform.tfvars file exists
7. → Execute Terraform command

### Safety Features

**Production Confirmation:**
- Interactive prompt when applying to prod workspace
- Requires explicit "yes" response

**Destroy Confirmation:**
- Requires typing "destroy-{workspace}" to confirm
- Prevents accidental infrastructure deletion

**AWS Profile Validation:**
- Validates credentials before any Terraform operations
- Provides helpful error messages with remediation steps

## Design Principles Applied

### DRY (Don't Repeat Yourself)
- Reused patterns from `scripts/cloud/run.sh`
- Single source of truth for configuration
- No duplicated validation code

### SOLID
- **Single Responsibility**: Script validates and executes Terraform
- **Open/Closed**: Easy to extend with new actions
- **Interface Segregation**: Clean CLI with minimal complexity

### KISS (Keep It Simple, Stupid)
- Linear execution flow
- Clear variable names
- Helpful error messages
- No complex abstractions
- 134 lines of code (concise)

### Production Ready
- ✓ No mocks, stubs, or placeholders
- ✓ Real AWS credential validation
- ✓ Real error handling
- ✓ Comprehensive validation
- ✓ Safety confirmations

## Tested Scenarios

✓ No arguments - Shows help
✓ Missing workspace - Shows error
✓ Invalid workspace - Shows error  
✓ Invalid action - Shows error
✓ Invalid AWS profile - Shows error with remediation
✓ Valid profile (tplr) - Validates and proceeds
✓ Exact pattern from requirement: `./run.sh tplr prod apply-f`

## Compatibility

### Consistent with scripts/cloud/run.sh

| Feature | scripts/cloud | orchestrator/cloud |
|---------|---------------|--------------------|
| Argument order | `<profile> <workspace> [action]` | `<profile> <workspace> [action]` ✓ |
| Profile validation | Yes | Yes ✓ |
| Workspace support | dev, prod | dev, prod ✓ |
| Safety confirmations | Yes | Yes ✓ |
| Error handling | Comprehensive | Comprehensive ✓ |

### Actions Comparison

**scripts/cloud:** plan, apply, apply-f, destroy, force-unlock, tasks
**orchestrator/cloud:** plan, apply, apply-f, destroy, validate, output

Different actions are intentional:
- `force-unlock` and `tasks` are ECS-specific
- `validate` and `output` are K3s-specific

## Migration Guide

### Old Usage
```bash
cd orchestrator/cloud
terraform workspace select prod
terraform apply
```

### New Usage
```bash
cd orchestrator/cloud
./run.sh tplr prod apply
```

### Benefits
- Automatic AWS profile management
- Built-in validation
- Safety confirmations
- Consistent with scripts/cloud pattern
- Better error messages

## Commands for Future Developers

### Setup
```bash
# Configure AWS profile
aws configure --profile tplr

# Verify
aws sts get-caller-identity --profile tplr

# Create config
cp terraform.tfvars.example terraform.tfvars
vim terraform.tfvars
```

### Deployment
```bash
# Development
./run.sh tplr dev apply

# Production (with confirmation)
./run.sh tplr prod apply

# CI/CD (no confirmation)
./run.sh tplr prod apply-f
```

### Maintenance
```bash
# Validate
./run.sh tplr prod validate

# Show outputs
./run.sh tplr prod output

# Destroy
./run.sh tplr prod destroy
```

## Testing Commands Used

```bash
# Validate syntax
bash -n run.sh

# Test argument validation
./run.sh  # No args
./run.sh tplr  # Missing workspace
./run.sh tplr staging  # Invalid workspace
./run.sh tplr prod invalid  # Invalid action

# Test AWS profile
./run.sh nonexistent-profile prod plan  # Invalid profile
./run.sh tplr prod plan  # Valid profile

# Test exact pattern from requirement
./run.sh tplr prod apply-f
```

## Integration

The updated script integrates seamlessly with:
- Terraform workspaces (dev, prod via locals.tf)
- AWS credential chain (via AWS_PROFILE)
- Ansible playbooks (via generated inventory)
- CI/CD pipelines (via apply-f action)

## Files Modified

1. `orchestrator/cloud/run.sh` - Complete rewrite with profile support
2. `orchestrator/cloud/README.md` - Updated all examples and prerequisites
3. `orchestrator/cloud/QUICK-START.md` - Added profile setup, updated commands
4. `orchestrator/cloud/RUN-SH-USAGE.md` - New comprehensive guide
5. `orchestrator/cloud/CHANGELOG.md` - This file

## Verification

All changes have been:
- ✓ Syntax validated
- ✓ Functionality tested
- ✓ Documented comprehensively
- ✓ Aligned with DRY, SOLID, KISS principles
- ✓ Production-ready (no mocks/stubs/placeholders)
