# PyPI Publishing Setup Guide

This guide explains how to set up PyPI Trusted Publishing for the Basilica Python SDK.

## What is Trusted Publishing?

Trusted Publishing (OpenID Connect) is PyPI's recommended way to publish packages. It's more secure than API tokens because:
- No long-lived credentials to manage
- Automatic authentication through GitHub Actions
- No secrets to rotate or leak
- Scoped per-repository and workflow

## Setup Steps

### 1. Create PyPI Account

If you don't already have one:
1. Go to https://pypi.org/account/register/
2. Verify your email address
3. Enable 2FA (required for publishing)

### 2. Configure Trusted Publisher

**IMPORTANT**: You must configure this BEFORE the first release.

1. Go to https://pypi.org/manage/account/publishing/
2. Click "Add a new pending publisher"
3. Fill in the form:
   - **PyPI Project Name**: `basilica-sdk`
   - **Owner**: `one-covenant` (your GitHub org/username)
   - **Repository name**: `basilica` (your repo name)
   - **Workflow name**: `release-python-sdk.yml`
   - **Environment name**: `pypi`
4. Click "Add"

This creates a "pending publisher" - it will be automatically activated when you first publish.

### 3. Configure GitHub Environment

Create a protected environment for releases:

1. Go to your GitHub repository
2. Navigate to Settings → Environments
3. Click "New environment"
4. Name it: `pypi`
5. Add protection rules (optional but recommended):
   - **Required reviewers**: Add 1-2 maintainers who must approve releases
   - **Deployment branches**: Select "Protected branches" or "Selected branches"
6. Click "Create environment"

### 4. Test the Setup (Optional)

Before publishing to production PyPI, test with TestPyPI:

1. Go to https://test.pypi.org/account/register/
2. Create an account (separate from production PyPI)
3. Configure trusted publisher at https://test.pypi.org/manage/account/publishing/:
   - Same settings as production
   - PyPI Project Name: `basilica-sdk`
4. Modify `.github/workflows/release-python-sdk.yml` temporarily:
   ```yaml
   - name: Publish to TestPyPI
     uses: pypa/gh-action-pypi-publish@release/v1
     with:
       repository-url: https://test.pypi.org/legacy/
   ```
5. Create a test tag: `git tag basilica-sdk-python-v0.1.0-test`
6. Push and verify: `pip install -i https://test.pypi.org/simple/ basilica-sdk`

## First Release Process

### Pre-Release Checklist

- [ ] Trusted publisher configured on PyPI
- [ ] GitHub `pypi` environment created
- [ ] Version updated in `pyproject.toml` and `Cargo.toml`
- [ ] `CHANGELOG.md` updated with release notes
- [ ] All tests passing locally
- [ ] README reflects correct package name (`basilica-sdk`)

### Release Steps

1. **Update version**:
   ```bash
   cd crates/basilica-sdk-python
   ./bump-version.sh 0.1.0
   ```

2. **Update CHANGELOG.md** with release notes

3. **Commit and push**:
   ```bash
   git add .
   git commit -m "Prepare Python SDK v0.1.0 release"
   git push origin main
   ```

4. **Create and push tag**:
   ```bash
   git tag basilica-sdk-python-v0.1.0
   git push origin basilica-sdk-python-v0.1.0
   ```

5. **Monitor workflow**:
   - Go to Actions tab in GitHub
   - Watch "Release Python SDK" workflow
   - Workflow will:
     - Build wheels for all platforms (Linux, macOS, Windows)
     - Build source distribution
     - Publish to PyPI automatically
     - Create GitHub release

6. **Verify publication**:
   ```bash
   # Wait 2-3 minutes for PyPI to propagate
   pip install basilica-sdk==0.1.0
   python -c "import basilica; print('Success!')"
   ```

7. **Check PyPI page**: https://pypi.org/project/basilica-sdk/

## Subsequent Releases

For releases after the first:

1. Bump version: `./bump-version.sh 0.2.0`
2. Update CHANGELOG.md
3. Commit changes
4. Create tag: `git tag basilica-sdk-python-v0.2.0`
5. Push: `git push origin main && git push origin basilica-sdk-python-v0.2.0`

The workflow handles everything else automatically.

## Troubleshooting

### Issue: "Forbidden" error when publishing

**Cause**: Trusted publisher not configured or misconfigured

**Solution**:
1. Verify configuration at https://pypi.org/manage/account/publishing/
2. Check that all fields match exactly:
   - Repository owner
   - Repository name
   - Workflow filename
   - Environment name

### Issue: Workflow can't access environment

**Cause**: Environment doesn't exist or has restricted access

**Solution**:
1. Verify environment exists in Settings → Environments
2. Check that the workflow has permission to access it
3. Ensure no branch restrictions are blocking the tag

### Issue: Package name already taken

**Cause**: `basilica-sdk` already exists on PyPI

**Solution**:
1. Check if the project exists: https://pypi.org/project/basilica-sdk/
2. If owned by your org: Add yourself as maintainer
3. If taken by someone else: Choose a different name in `pyproject.toml`

### Issue: Builds fail on specific platform

**Cause**: Platform-specific dependency or build issue

**Solution**:
1. Check workflow logs for the failing platform
2. Common issues:
   - protoc not found: Verify protobuf installation step
   - Rust toolchain issue: Check rust-toolchain version
   - OpenSSL linking: Verify system dependencies

### Issue: TestPyPI works but production fails

**Cause**: Different trust configuration between test and production

**Solution**:
1. Verify trusted publisher configured on production PyPI
2. Ensure workflow uses correct repository URL
3. Check that `pypi` environment exists (not `testpypi`)

## Manual Publishing (Emergency Only)

If trusted publishing fails, you can publish manually using API tokens:

### Generate API Token

1. Go to https://pypi.org/manage/account/token/
2. Create token with scope: "Entire account" (first release) or "Project: basilica-sdk" (subsequent)
3. Copy token (starts with `pypi-`)

### Add to GitHub Secrets

1. Go to repository Settings → Secrets → Actions
2. New repository secret: `PYPI_API_TOKEN`
3. Paste token value

### Modify Workflow

Update `.github/workflows/release-python-sdk.yml`:

```yaml
- name: Publish to PyPI
  uses: PyO3/maturin-action@v1
  env:
    MATURIN_PYPI_TOKEN: ${{ secrets.PYPI_API_TOKEN }}
  with:
    command: upload
    args: --skip-existing dist/*
```

**Note**: This is less secure than trusted publishing. Switch back when possible.

## Security Best Practices

1. **Enable 2FA** on PyPI account
2. **Use trusted publishing** instead of API tokens when possible
3. **Rotate API tokens** regularly if using manual method
4. **Protect main branch** to prevent unauthorized releases
5. **Require PR reviews** for version bump commits
6. **Monitor downloads** for unusual activity
7. **Enable security advisories** on GitHub

## Support

For issues with:
- **PyPI publishing**: Contact PyPI support at https://github.com/pypi/support
- **GitHub Actions**: Check GitHub Actions documentation
- **Basilica SDK**: Open issue at https://github.com/one-covenant/basilica/issues

## References

- [PyPI Trusted Publishers](https://docs.pypi.org/trusted-publishers/)
- [GitHub OIDC with PyPI](https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-pypi)
- [Maturin Documentation](https://www.maturin.rs/)
- [PyO3 Guide](https://pyo3.rs/)
