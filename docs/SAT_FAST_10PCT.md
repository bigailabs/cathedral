# SAT fast-path 10% reward wire (Release 1)

Cathedral's own verified fast-path SAT scoreboard (`GET /v2/validator/weights/next`
on v2-beta) is a real, verified scoreboard, but it isn't part of the real weight
vector yet. Release 1 wires it in by treating it as **just another external
mechanism** — the same intake Violet uses — rather than building a second blend.

```txt
v2 scoreboard -> scripts/sat_fast_score_poster.py -> POST /v1/external-scores/violet
              -> (existing hardened blend in weights.py) -> Cathedral-signed vector
```

This reuses `scaffold/publisher/external_scores.py` (intake: bearer + optional
HMAC, validation, storage) and `scaffold/publisher/weights.py::_apply_external_scores`
(blend: registration-gated, fraction-capped, fail-closed) exactly as hardened for
Violet. See `docs/VIOLET_EXTERNAL_SCORES.md` for the full safety writeup — it all
applies here unchanged.

The one endpoint-level change made to enable this: `POST /v1/external-scores/violet`
previously hard-rejected any `source` other than `violet_audio`. It now checks
against `external_scores.ALLOWED_ENDPOINT_SOURCES = {"violet_audio",
"cathedral_sat_fast"}`. This is a source-label allowlist, not a safety gate —
auth, HMAC, registration-gate, and fraction-cap are all unchanged.

## Run the poster

```bash
# Inspect what would be posted — never hits the network for POST, safe to run anytime.
python scripts/sat_fast_score_poster.py --dry-run

# One-shot post (needs CATHEDRAL_EXTERNAL_SCORES_TOKEN set — see below).
python scripts/sat_fast_score_poster.py --once

# Or run it as a long-lived poller (e.g. supervised process / systemd unit),
# polling every 5 minutes:
python scripts/sat_fast_score_poster.py --loop --interval 300
```

Flags / env:

- `--challenge-base` / `CATHEDRAL_V2_SCOREBOARD_URL` — the v2 scoreboard URL to
  read (default `https://v2-beta.cathedral.computer/v2/validator/weights/next`).
- `--submit-base` / `CATHEDRAL_PUBLISHER_URL` — publisher base URL;
  `/v1/external-scores/violet` is appended (default `https://api.cathedral.computer`).
- `CATHEDRAL_EXTERNAL_SCORES_TOKEN` — bearer token for the intake (required to
  actually POST; `--dry-run` works without it).
- `CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET` — optional raw-body HMAC, same
  `sha256=<hex>` scheme `external_scores.verify_hmac` checks.

The poster maps each scoreboard entry to `{miner_hotkey, score}` with
`score = raw_score (or weight) / max(...)`, clamped to `[0, 1]`. Zero/empty
entries are skipped. An empty or degraded scoreboard (no positive scores) logs
and exits 0 — it posts nothing rather than erroring. A POST failure or rejection
exits non-zero so a scheduler/cron sees the failure. Nothing here auto-enables
anything on the publisher side; it is a manual, `--dry-run`-friendly tool by
default.

## Enable the 10% blend on the publisher

The poster only stores reports. To actually make Cathedral's fast-path SAT
scores count for 10% of the real signed vector, set these on the **publisher**:

```bash
CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_TOKEN=<shared bearer token>       # same value the poster uses
CATHEDRAL_EXTERNAL_SCORES_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_SOURCE=cathedral_sat_fast
CATHEDRAL_EXTERNAL_SCORES_FRACTION=0.10
CATHEDRAL_EXTERNAL_SCORES_REQUIRE_REGISTERED=1              # default; keep it on
```

With these set: the blend pays **only registered hotkeys** (fail-closed if the
metagraph snapshot is stale/unavailable), the external share is fixed at 10%
and hard-capped by `CATHEDRAL_EXTERNAL_SCORES_MAX_FRACTION` (default 0.5) even
if misconfigured, and ingest refuses unauthenticated posts whenever the blend
is live. None of that logic is new — it's the same blend Violet already uses,
just pointed at `source=cathedral_sat_fast` instead of `violet_audio`.

Rollout order matters: turn on `INGEST_ENABLED` + `TOKEN` first and run the
poster (or `--dry-run` against a real publisher) to confirm reports are
accepted, *before* flipping `EXTERNAL_SCORES_ENABLED=1` to start blending into
the real vector.
