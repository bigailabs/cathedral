---
name: cathedral-rentals-ops
description: Use when the user wants direct machine access on Cathedral via rentals, including discovery, SSH keys, secure-cloud/community-cloud selection, volumes, SSH, exec, file copy, and teardown.
---

# Cathedral Rentals Ops

Use this skill when the user wants:

- a GPU or CPU machine with shell access
- direct SSH access instead of an HTTP deployment
- custom training environments
- long-running jobs that are easier to manage on a box
- huge models that may be awkward as serverless deployments

## When To Choose Rentals Instead Of Deploys

Prefer rentals when the task needs:

- SSH
- custom package or system setup
- distributed training
- manual process control
- large multi-GPU inference where health-check timing is likely to be painful

If the user wants a public URL, an HTTP API, or a managed inference endpoint, use the serverless skill instead.

## Canonical Preflight

Authenticate:

```bash
cathedral login
```

Check balance before creating cost-bearing resources:

```bash
cathedral balance
```

Make sure an SSH key exists in Cathedral:

```bash
cathedral ssh-keys list
cathedral ssh-keys add
```

## Discover Available Compute

List everything:

```bash
cathedral ls
```

Filter by GPU type:

```bash
cathedral ls h100
cathedral ls h200
```

Filter by budget, location, and compute pool:

```bash
cathedral ls --price-max 5 --country US
cathedral ls --compute secure-cloud
cathedral ls --compute community-cloud
```

Useful filters supported by the CLI include:

- positional GPU type like `h100`
- `--gpu-min`
- `--gpu-max`
- `--price-max`
- `--memory-min`
- `--country`
- `--compute`

## Start A Rental

Example: single-machine rental

```bash
cathedral up h100 --gpu-count 1
```

Example: large multi-GPU box

```bash
cathedral up h200 --gpu-count 8 --compute secure-cloud
```

The compute selector currently maps to:

- `citadel`, `secure-cloud`, `secure`
- `bourse`, `community-cloud`, `community`

Prefer being explicit with `--compute` when the user cares about the environment.

## Inspect Running Or Previous Rentals

List active rentals:

```bash
cathedral ps
```

Include history:

```bash
cathedral ps --history
```

Inspect one rental:

```bash
cathedral status <rental-id>
```

## Operate A Rental

SSH into the machine:

```bash
cathedral ssh <rental-id>
```

Run a one-off command:

```bash
cathedral exec "nvidia-smi" --target <rental-id>
```

Stream logs:

```bash
cathedral logs <rental-id>
```

Restart:

```bash
cathedral restart <rental-id>
```

Copy files:

```bash
cathedral cp ./local.txt <rental-id>:/workspace/local.txt
cathedral cp <rental-id>:/workspace/output.txt ./output.txt
```

## Volumes

For secure-cloud rentals that need persistent storage:

Create a volume:

```bash
cathedral volumes create --name cache --size 100 --provider hyperstack --region US-1
```

List volumes:

```bash
cathedral volumes list
```

Attach to a rental:

```bash
cathedral volumes attach cache --rental <rental-id>
```

Detach:

```bash
cathedral volumes detach cache --yes
```

Delete:

```bash
cathedral volumes delete cache --yes
```

Current volume constraints exposed in the CLI:

- provider is `hyperstack`
- regions include `US-1`, `CANADA-1`, `NORWAY-1`

## Teardown

Stop one rental:

```bash
cathedral down <rental-id>
```

Stop everything:

```bash
cathedral down --all
```

Agents should tear down rentals at the end of a task unless the user explicitly wants them preserved.

## Operational Notes

- `cathedral up` ensures an SSH key is present before provisioning.
- Secure-cloud log streaming is limited; if logs are thin or missing, use `cathedral ssh <rental-id>` and inspect the machine directly.
- If the user needs CPU-only secure-cloud resources, the Python SDK currently exposes that surface more directly than the CLI.

## Good Agent Defaults

- start with `cathedral balance`
- then `cathedral ls ...`
- then create the rental
- immediately return the rental ID and connection method
- explicitly ask whether to keep or tear down the machine when the work is done

## File Pointers

- `crates/cathedral-cli/src/cli/handlers/gpu_rental.rs`
- `crates/cathedral-cli/src/cli/handlers/ssh_keys.rs`
- `crates/cathedral-cli/src/cli/handlers/volumes.rs`
- `crates/cathedral-sdk-python/examples/start_secure_cloud_gpu_rental.py`
- `crates/cathedral-sdk-python/examples/start_cpu_rental.py`

## TODOs

- add a tested example for secure-cloud CPU rentals once the CLI gets a cleaner CPU-first workflow
- add a repo-local transcript showing `cathedral up` -> `cathedral ssh` -> `cathedral down`
