# Python SDK Release Checklist

Quick reference for releasing new versions of the Basilica Python SDK to PyPI.

## Pre-Release Setup (First Time Only)

- [ ] PyPI account created with 2FA enabled
- [ ] Trusted publisher configured on PyPI (see PYPI_SETUP_GUIDE.md)
- [ ] GitHub `pypi` environment created
- [ ] TestPyPI tested (optional but recommended)

## Release Process

### 1. Prepare Release

```bash
cd crates/basilica-sdk-python

# Bump version
./bump-version.sh X.Y.Z

# Update CHANGELOG.md with release notes
# Add new version section with date and changes
```

### 2. Test Locally

```bash
# Build package
maturin build --release

# Install locally
pip install target/wheels/basilica_sdk-*.whl

# Test import
python -c "import basilica; print(basilica.DEFAULT_API_URL)"

# Run examples (if applicable)
cd examples
python quickstart.py  # with test credentials
cd ..

# Uninstall
pip uninstall basilica-sdk -y
```

### 3. Commit and Review

```bash
# Commit version bump
git add .
git commit -m "Bump Python SDK to vX.Y.Z"

# Push to feature branch
git checkout -b release/python-sdk-vX.Y.Z
git push origin release/python-sdk-vX.Y.Z

# Create PR and get approval
# Merge to main
```

### 4. Create Release

```bash
# Pull latest main
git checkout main
git pull origin main

# Create annotated tag
git tag -a basilica-sdk-python-vX.Y.Z -m "Release Python SDK vX.Y.Z"

# Push tag to trigger workflow
git push origin basilica-sdk-python-vX.Y.Z
```

### 5. Monitor Release

- [ ] Go to GitHub Actions tab
- [ ] Watch "Release Python SDK" workflow
- [ ] All jobs pass (build-wheels-*, build-sdist, publish-to-pypi)
- [ ] GitHub release created automatically

### 6. Verify Publication

```bash
# Wait 2-3 minutes for PyPI propagation

# Install from PyPI
pip install basilica-sdk==X.Y.Z

# Verify import and basic functionality
python -c "import basilica; client = basilica.BasilicaClient('https://api.basilica.ai')"

# Check all platforms available
# Visit: https://pypi.org/project/basilica-sdk/X.Y.Z/
```

### 7. Post-Release

- [ ] Verify PyPI page displays correctly
- [ ] Test installation on different platforms (optional)
- [ ] Announce release (Discord, Twitter, etc.)
- [ ] Update documentation if needed
- [ ] Close any related GitHub issues

## Hotfix Release

For urgent bug fixes:

```bash
# Create hotfix branch from tag
git checkout -b hotfix/python-sdk-vX.Y.Z basilica-sdk-python-vX.Y.Z

# Fix bug
# ...

# Bump patch version
./bump-version.sh X.Y.Z+1

# Commit and tag
git commit -am "Hotfix: [description]"
git tag basilica-sdk-python-vX.Y.Z+1
git push origin basilica-sdk-python-vX.Y.Z+1

# Merge back to main
git checkout main
git merge hotfix/python-sdk-vX.Y.Z
git push origin main
```

## Rollback Procedure

If a release has critical issues:

### 1. Yank Bad Version on PyPI

1. Go to https://pypi.org/project/basilica-sdk/
2. Navigate to the bad version
3. Click "Options" → "Yank release"
4. Provide reason for yanking

### 2. Release Hotfix

Follow hotfix release process above to release fixed version.

### 3. Communicate

- Post issue on GitHub explaining the problem
- Announce in community channels
- Update documentation with known issues

## Common Issues

### Build Fails on Specific Platform

1. Check Actions logs for error details
2. Common fixes:
   - Update Rust toolchain
   - Verify protoc installation
   - Check system dependencies
3. Test locally with Docker:
   ```bash
   docker run --rm -v $(pwd):/work -w /work rust:latest bash -c "
     apt-get update && apt-get install -y protobuf-compiler &&
     pip install maturin &&
     maturin build --release
   "
   ```

### PyPI Publishing Fails

1. Check trusted publisher configuration
2. Verify `pypi` environment exists
3. Check workflow permissions
4. See PYPI_SETUP_GUIDE.md for detailed troubleshooting

### Tag Already Exists

If you need to re-release:

```bash
# Delete local tag
git tag -d basilica-sdk-python-vX.Y.Z

# Delete remote tag
git push --delete origin basilica-sdk-python-vX.Y.Z

# Recreate and push
git tag -a basilica-sdk-python-vX.Y.Z -m "Release Python SDK vX.Y.Z"
git push origin basilica-sdk-python-vX.Y.Z
```

### Version Mismatch

If pyproject.toml and Cargo.toml versions don't match:

```bash
# Use bump-version.sh to sync both
./bump-version.sh X.Y.Z
```

## Version Numbering Guide

Follow Semantic Versioning (semver):

- **Major (X.0.0)**: Breaking API changes
  - Removed functions/classes
  - Changed function signatures
  - Renamed modules

- **Minor (0.X.0)**: New features, backward compatible
  - New functions/methods
  - New optional parameters
  - Performance improvements

- **Patch (0.0.X)**: Bug fixes, backward compatible
  - Bug fixes
  - Documentation updates
  - Dependency updates

## Release Schedule

Recommended release cadence:

- **Major releases**: Quarterly or as needed
- **Minor releases**: Every 2-4 weeks
- **Patch releases**: As needed for bugs
- **Hotfixes**: Immediately for critical issues

## Contact

For questions about the release process:
- GitHub Issues: https://github.com/one-covenant/basilica/issues
- Team: team@basilica.ai
