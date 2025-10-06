Ansible playbooks to provision a central K3s cluster and apply Basilica E2E readiness manifests.

Quick start

- Prepare inventory (single-node example): see `inventories/example.ini`.
- Adjust variables in `group_vars/all.yml` (images, tenant namespace, options).
- Provision K3s server (and optional agents):
  - `ansible-playbook -i inventories/example.ini playbooks/k3s-setup.yml` \
    `-e k3s_channel=stable -e k3s_disable_traefik=true`
- Apply E2E readiness (RBAC, CRDs, Postgres, Operator, API, optional Envoy/Gateway):
  - `ansible-playbook -i inventories/example.ini playbooks/e2e-apply.yml`

Use CI-built k3_test images

- Build and push images from your branch (tags all services with k3_test):
  - `just ci-build-images TAG=k3_test`
- Deploy using the k3_test images (override defaults):
  - `ansible-playbook -i inventories/example.ini playbooks/e2e-apply.yml \
     -e operator_image=ghcr.io/one-covenant/basilica-operator:k3_test \
     -e api_image=ghcr.io/one-covenant/basilica-api:k3_test`
- Optional toggles in `group_vars/all.yml` you may want to review before running:
  - `tenant_namespace` (default: `u-test`)
  - `use_templates: true` (injects image refs/env via templates)
  - `generate_crds: true` (requires Rust locally to run `cargo run -p basilica-operator --bin crdgen`)
    - If Rust isn’t available on your control machine, set `generate_crds: false` and provide a pre-generated `basilica-crds.yaml` at repo root.

Tie-in with docs/e2e-readiness-checklist.md

- The `playbooks/e2e-apply.yml` automates the checklist steps: namespaces/RBAC → CRDs → Postgres → Operator/API → optional Envoy/Gateway → smoke probe.
- After the run completes, verify:
  - Operator: `kubectl -n basilica-system logs deploy/basilica-operator | head`
  - API health (ephemeral probe runs during the play): `curl http://127.0.0.1:8000/health` (use the long-lived port-forward options if desired).

Contents

- `playbooks/k3s-setup.yml` — installs a central K3s server and joins agents.
- `playbooks/e2e-apply.yml` — runs the steps from `docs/e2e-readiness-checklist.md` on the server.
- `roles/k3s_server` — K3s server install role (idempotent, disables Traefik when requested).
- `roles/k3s_agent` — K3s agent join role.
- `group_vars/all.yml` — defaults for images, namespace, and toggles.
- `inventories/example.ini` — sample inventory for local VM or single remote host.

Notes

- CRD generation: by default the playbook expects you to have Rust locally to run `cargo run -p basilica-operator --bin crdgen`. Set `generate_crds=false` if you provide a pre-generated `basilica-crds.yaml` at repo root, or adjust `crdgen_cmd`.
- The playbook copies the repo’s `config/` directory to the server under `/opt/basilica/config` and applies manifests from there.
- K3s installs `kubectl` on the server (`/usr/local/bin/kubectl`), so manifests are applied on the server host.
- To template image refs/env instead of inline `replace`, set `use_templates: true` (default). Templates live under `scripts/ansible/templates/`.
- To run an ephemeral port-forward and probe `/health`, set `run_smoke_probe: true` (default) — port-forward runs only long enough to probe.
- To keep a long-lived port-forward to the API, set in `group_vars/all.yml`:
  - `port_forward.enabled: true`
  - Adjust `namespace`, `resource_kind`, `resource_name`, `local_port`, `remote_port`, `bind_address` as needed. Defaults keep it bound to `127.0.0.1` for safety. If you need remote access, set `bind_address: 0.0.0.0` and ensure your firewall allows it.
  - Access locally on the server via `curl http://127.0.0.1:8000/health`. From your machine, use SSH tunneling: `ssh -N -L 8000:127.0.0.1:8000 user@server` then `curl http://localhost:8000/health`.

- Postgres and Envoy forwards:
  - Enable `postgres_forward.enabled: true` to keep `kubectl port-forward svc/basilica-postgres 5432:5432` running as a systemd service.
  - Enable `envoy_forward.enabled: true` to keep `kubectl port-forward svc/basilica-envoy 8080:8080` running. For the admin port, enable `envoy_admin_forward.enabled: true` (9901).
  - As with the API, forwards bind to `127.0.0.1` by default; use SSH tunnels or change `bind_address` if necessary.
