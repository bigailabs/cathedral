# Morning report — 2026-04-19

Status at handoff: 23:40 UTC 2026-04-18.

## Mission recap

1. **Miner and validator working and live on the site** — shipped, but last status has both miners in `offline` mid-re-verification after the final validator restart. Next scheduler cycle lands inside 10 min of wakeup. See `docs/overnight-miner-watch.log` (updated every 15 min by launchd `com.cathedral.miner-watch`).
2. **Periodically check our friend's miner** — `com.cathedral.miner-watch` launchd agent is loaded and running. Any state change on McDee (miner_155) gets appended to `overnight-miner-watch.log`. He DID reconnect at 23:15 UTC (confirmed). Waiting on verification.
3. **Everything tested e2e** — CLI walks list → show → rent (dry-run with impossible min-memory). Cloudflare 403 bug found and fixed (PR #36).
4. **Surgically enable CPU listing** — done. Validator has a CPU probe branch, pricing API has three CPU tiers. Behaviour is opt-in by category prefix; legacy GPU nodes unaffected.
5. **Asset retrieval via API + CLI** — done. `/api/v1/cathedral/machines` + `/rentals` live on api.polaris.computer. `scripts/cathedral-cli.py` stdlib-only, works without any setup.

## What I shipped (19 PRs merged this session)

| PR | Repo | Summary |
|---|---|---|
| [#29](https://github.com/bigailabs/cathedral/pull/29) | cathedral | SSH scheduler deadlock timeout |
| [#31](https://github.com/bigailabs/cathedral/pull/31) | cathedral | admit home nodes when binary attestation disabled |
| [#32](https://github.com/bigailabs/cathedral/pull/32) | cathedral | miner accepts any gpu_category |
| [#33](https://github.com/bigailabs/cathedral/pull/33) | cathedral | validator accepts any gpu_category |
| [#34](https://github.com/bigailabs/cathedral/pull/34) | cathedral | CPU-only verification path |
| [#35](https://github.com/bigailabs/cathedral/pull/35) | cathedral | cathedral-cli |
| [#36](https://github.com/bigailabs/cathedral/pull/36) | cathedral | CLI User-Agent fix |
| [#693](https://github.com/bigailabs/polariscomputer/pull/693) | polariscomputer | 23-category home-ownable pricing |
| [#694](https://github.com/bigailabs/polariscomputer/pull/694) | polariscomputer | `/api/substrate/validator/registry` |
| [#699](https://github.com/bigailabs/polariscomputer/pull/699) | polariscomputer | per-miner status in registry |
| [#703](https://github.com/bigailabs/polariscomputer/pull/703) | polariscomputer | revert accidental 10k-file commit |
| [#704](https://github.com/bigailabs/polariscomputer/pull/704) | polariscomputer | CPU pricing tiers (clean replay) |
| [#705](https://github.com/bigailabs/polariscomputer/pull/705) | polariscomputer | `/api/v1/cathedral/*` proxy |
| [cathedral-site#20](https://github.com/bigailabs/cathedral-site/pull/20) | site | three-row miner funnel |
| [cathedral-site#21](https://github.com/bigailabs/cathedral-site/pull/21) | site | sticky verified at top of register |

Also filed: cathedral#28, #30 (diagnostic only).

## Architecture changes

**Validator (`cathedral`):**
- `basilica-common/ssh/connection.rs`: `tokio::process::Command` + `timeout` wrap
- `basilica-validator/miner_prover/scheduler.rs`: 10-min workflow-wide timeout
- `basilica-validator/miner_prover/validation_strategy.rs`: NAT skipped when binary disabled; CPU probe branch on `gpu_category.starts_with("CPU_")`
- `basilica-validator/miner_prover/verification.rs`: GPU declaration enforcement skipped when binary disabled
- `basilica-validator/grpc/registration.rs`: accepts any non-empty gpu_category

**Miner (`cathedral`):**
- `basilica-miner/node_manager.rs`: accepts any non-empty gpu_category

**Backend (`polariscomputer`):**
- `polaris/api/routers/incentive.py`: 23+3 categories, CPU pricing floors
- `polaris/api/routers/substrate.py`: `/validator/registry` with per-miner status, SSH-proxied from validator.db
- `polaris/api/routers/cathedral_rentals.py`: thin SSH proxy over validator's 9090 rental API

**Site (`cathedral-site`):**
- Three-tier funnel rendered (declared / registered / verified)
- Verified and registered miners stick to top of Miners Register
- New `--ok` (sage) palette token

## Known gotchas for morning triage

1. **McDee's declared category is still "A100"** — our fork admits it but the site's `category` column will look odd. Reach out when he's around and have him flip to the actual GPU string (his box has 3090 + 2x 3060, so probably `RTX_3090` for the first slot).
2. **GPU declaration enforcement is off** — any miner can claim any category. Necessary tradeoff today (per #24) but file a real attestation task before we invite many more miners.
3. **Rental API has no auth** — `POLARIS_CATHEDRAL_API_KEY` env defaults to empty. Before the Africa comp, set this on Railway and hand out the key to contestants. Alternatively keep it open for the comp and accept the blast radius.
4. **Python-urllib UA** — fixed in CLI PR #36; anyone writing their own client needs to know about the Cloudflare challenge.
5. **CPU attestation is weak** — SSH probe runs `lscpu`/`free`, trivially spoofable. Same tradeoff as GPU side; acceptable for comp.

## To resume where I left off

```bash
# Check overnight activity
tail -40 ~/Documents/PROJECTS/cathedral/docs/overnight-miner-watch.log

# Current DB
ssh root@135.181.8.214 'sqlite3 -separator "|" /root/cathedral-data/validator.db \
  "SELECT miner_id, status, gpu_category, datetime(last_node_check) FROM miner_nodes;"'

# Test the CLI
python3 ~/Documents/PROJECTS/cathedral/scripts/cathedral-cli.py list

# Our miner on Lium
ssh -i ~/.ssh/cathedral_miner_lium -p 40039 root@91.224.44.207 \
  'tail -20 /root/cathedral-miner/logs/miner.log'
```

## What I didn't do (worth your call)

- Did NOT rent a test container via CLI (invasive — would run on McDee's actual box). The CLI passes input validation; the real rent needs you to approve it first.
- Did NOT add rate limiting to `/api/v1/cathedral/*`. Proxy is open today.
- Did NOT change McDee's declared GPU category (not our wallet).
- Did NOT write docs for the CLI yet. The `--help` output is sufficient for now.
