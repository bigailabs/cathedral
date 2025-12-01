# FUSE Security Deployment - Quick Reference

**Full Checklist**: See [FUSE-SECURITY-DEPLOYMENT.md](./FUSE-SECURITY-DEPLOYMENT.md)

---

## ⚠️ CRITICAL: DO NOT DEPLOY TO PRODUCTION WITHOUT DEV TESTING

This change is **HIGH RISK** and **UNTESTED** in production. You MUST test in dev/staging first.

---

## What Changed

**Before**: FUSE containers ran with `privileged: true`
- All capabilities granted
- Seccomp disabled (runs as "Unconfined")
- False security claims in code

**After**: FUSE containers run with `CAP_SYS_ADMIN` only
- Only SYS_ADMIN capability
- RuntimeDefault seccomp **actually enforced**
- Honest security model

---

## Pre-Deployment MANDATORY Steps

### 1. Test in Dev/Staging FIRST

```bash
# Deploy to dev cluster
ansible-playbook -i inventories/development.ini playbooks/deploy.yml

# Create test UserDeployment with storage
kubectl apply -f - <<EOF
apiVersion: basilica.ai/v1
kind: UserDeployment
metadata:
  name: test-fuse
  namespace: u-test
spec:
  userId: "test"
  name: "fuse-test"
  image: "alpine:latest"
  replicas: 1
  port: 8080
  basePath: "/test"
  storage:
    persistent:
      enabled: true
      backend: r2
      bucket: "test-bucket"
      credentialsSecret: "basilica-r2-credentials"
      cacheSize: 2048
      mountPath: "/data"
EOF

# Verify it works
kubectl wait --for=condition=Ready pod -l app=fuse-test -n u-test --timeout=180s
kubectl logs -n u-test -l app=fuse-test -c fuse-storage
# Look for: "Filesystem mounted", NO errors

kubectl exec -n u-test -l app=fuse-test -c fuse-test -- \
  sh -c 'echo test > /data/test.txt && cat /data/test.txt'
# Expected: test

# Run integration tests
cd /root/workspace/spacejar/basilica/basilica
./scripts/test-storage-k8s.sh
# Expected: All pass
```

**If ANY test fails, STOP. Debug before production.**

### 2. Backup Current State

```bash
export KUBECONFIG=~/.kube/k3s-basilica-config

# Backup operator deployment
kubectl get deployment basilica-operator -n basilica-system -o yaml > \
  /tmp/operator-backup-$(date +%Y%m%d-%H%M%S).yaml

# Record current image
kubectl get deployment basilica-operator -n basilica-system \
  -o jsonpath='{.spec.template.spec.containers[0].image}' | \
  tee /tmp/current-operator-image.txt

# Count storage workloads
echo "UserDeployments with storage:"
kubectl get userdeployments -A -o json | \
  jq -r '.items[] | select(.spec.storage.persistent.enabled == true)' | wc -l

echo "Jobs with storage:"
kubectl get basilicajobs -A -o json | \
  jq -r '.items[] | select(.spec.storage.persistent.enabled == true)' | wc -l
```

---

## Deployment Steps

### 1. Build New Operator Image

```bash
cd /root/workspace/spacejar/basilica/basilica

./scripts/operator/build.sh \
  --image-name basilica-operator \
  --image-tag fuse-security-$(date +%Y%m%d-%H%M%S)

# Record image tag
echo "basilica-operator:fuse-security-$(date +%Y%m%d-%H%M%S)" | \
  tee /tmp/new-operator-image.txt
```

### 2. Deploy FUSE Module Loader (if needed)

```bash
kubectl apply -f orchestrator/k8s/core/fuse-module-loader.yaml
kubectl rollout status daemonset/fuse-module-loader -n kube-system --timeout=120s
```

### 3. Update Operator

```bash
NEW_IMAGE=$(cat /tmp/new-operator-image.txt)

# Via Ansible (recommended)
cd orchestrator/ansible
ansible-playbook -i inventories/production.ini playbooks/deploy.yml \
  --tags deploy_operator \
  --extra-vars "operator_image=$NEW_IMAGE"

# OR via kubectl
kubectl set image deployment/basilica-operator \
  operator=$NEW_IMAGE \
  -n basilica-system

# Watch rollout
kubectl rollout status deployment/basilica-operator -n basilica-system --timeout=600s
```

### 4. Verify Deployment

```bash
# Check operator health
kubectl get pods -n basilica-system -l app=basilica-operator
# Expected: 2/2 Running

# Check logs
kubectl logs -n basilica-system -l app=basilica-operator --tail=50
# Look for: Normal startup, no errors

# Create test UserDeployment
kubectl apply -f - <<EOF
apiVersion: basilica.ai/v1
kind: UserDeployment
metadata:
  name: prod-validation
  namespace: basilica-validators
spec:
  userId: "validation"
  name: "fuse-validation"
  image: "alpine:latest"
  replicas: 1
  port: 8080
  basePath: "/validation"
  storage:
    persistent:
      enabled: true
      backend: r2
      bucket: "validation-bucket"
      credentialsSecret: "basilica-r2-credentials"
      cacheSize: 2048
      mountPath: "/data"
EOF

# Wait and verify
kubectl wait --for=condition=Ready pod -l app=fuse-validation \
  -n basilica-validators --timeout=180s

kubectl logs -n basilica-validators -l app=fuse-validation -c fuse-storage
# Expected: Mount successful, no errors

# Verify security context
kubectl get pod -n basilica-validators -l app=fuse-validation -o yaml | \
  grep -A10 securityContext | grep -E "privileged|SYS_ADMIN|seccompProfile"

# Expected:
# privileged: false
# add: [SYS_ADMIN]
# type: RuntimeDefault

# Test file ops
kubectl exec -n basilica-validators -l app=fuse-validation -c fuse-validation -- \
  sh -c 'echo test > /data/test.txt && cat /data/test.txt'
# Expected: test

# Cleanup
kubectl delete userdeployment prod-validation -n basilica-validators
```

---

## Rollback (If Needed)

**When to rollback**:
- FUSE mount failures
- "Operation not permitted" errors
- >5% of storage workloads failing

**Rollback command**:

```bash
# Get backup image
CURRENT_IMAGE=$(cat /tmp/current-operator-image.txt)

# Rollback
kubectl set image deployment/basilica-operator \
  operator=$CURRENT_IMAGE \
  -n basilica-system

# OR
kubectl rollout undo deployment/basilica-operator -n basilica-system

# Wait
kubectl rollout status deployment/basilica-operator -n basilica-system --timeout=300s

# Verify
kubectl get pods -n basilica-system -l app=basilica-operator
```

**Rollback time**: < 2 minutes

---

## Monitoring (First 2 Hours)

**Check every 15 minutes**:

```bash
# Operator health
kubectl get pods -n basilica-system -l app=basilica-operator

# Storage workloads
kubectl get pods -A | grep -E "Error|CrashLoop|Pending"

# FUSE errors
kubectl logs -A -l basilica.ai/has-storage=true -c fuse-storage --since=15m | \
  grep -i "error\|failed\|permission"
```

---

## Success Criteria

- [ ] Operator deployed (2/2 Running)
- [ ] New storage workload created successfully
- [ ] Security context verified (privileged: false, CAP_SYS_ADMIN, RuntimeDefault seccomp)
- [ ] FUSE mount successful
- [ ] File operations work
- [ ] No errors in logs
- [ ] Existing workloads unaffected
- [ ] 2 hours stable operation

---

## Key Files

**Deployment Checklist**: `docs/runbooks/FUSE-SECURITY-DEPLOYMENT.md`
**Code Changes**:
- `crates/basilica-operator/src/controllers/storage_utils.rs`
- `crates/basilica-operator/src/controllers/user_deployment_controller.rs`
- `crates/basilica-operator/src/controllers/job_controller.rs`
- `orchestrator/ansible/playbooks/02-deploy/basilica.yml`

**Backup Location**: `/tmp/operator-backup-*.yaml`

---

## Emergency Contacts

**On-Call**: _______________________
**Escalation**: Platform Team Lead → Infrastructure → CTO
**Slack**: #platform-incidents

---

## Decision Tree

```
Start
  ↓
Dev/Staging tested? → NO → STOP, test first
  ↓ YES
Backups taken? → NO → Take backups
  ↓ YES
Deploy operator
  ↓
Rollout successful? → NO → Rollback
  ↓ YES
Create test workload
  ↓
FUSE mount works? → NO → Rollback
  ↓ YES
Security context correct? → NO → Rollback
  ↓ YES
File ops work? → NO → Rollback
  ↓ YES
Monitor 2 hours
  ↓
Any issues? → YES → Rollback
  ↓ NO
SUCCESS
```

---

**Remember**: When in doubt, ROLLBACK. Better safe than broken production.
