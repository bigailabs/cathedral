# Cathedral SN39 sandbox relaunch env template.
# Secrets use <secret:...> placeholders and must be supplied only on the host.
# Keep this file aligned with /home/polaris/cathedral/.env.sh via:
#   python3 deploy/check_env_template.py --template deploy/sandbox/env.template.sh --env-file /home/polaris/cathedral/.env.sh

export DATABASE_URL='<secret:postgres-dsn>'
export CATHEDRAL_CNF_TOKEN_SECRET='<secret:v1-cnf-token-hmac>'
export CATHEDRAL_EVAL_SIGNING_KEY='<secret:ed25519-signing-key>'
export CATHEDRAL_PERMINER_SEED_SECRET='<secret:per-miner-seed>'
export CATHEDRAL_PUBLISHER_ADMIN_TOKEN='<secret:publisher-admin-token>'
export CATHEDRAL_V2_SUBMIT_TOKEN_SECRET='<secret:v2-submit-token-hmac>'

export CATHEDRAL_LAUNCH_PROFILE=v2-converged
export CATHEDRAL_SERVICE_ROLE=all
export CATHEDRAL_V2_VERIFY_WORKER_ENABLED=1
export PORT=8000
export WEB_CONCURRENCY=1

export CATHEDRAL_PG_POOL_MIN=1
export CATHEDRAL_PG_POOL_MAX=8
export CATHEDRAL_PG_STATEMENT_TIMEOUT_MS=4000
export CATHEDRAL_THREADPOOL_TOKENS=16

export CATHEDRAL_PM_READ_HARD_CAP=8
export CATHEDRAL_V2_READ_THREADS=4
export CATHEDRAL_V2_SUBMIT_BITSET_THREADS=4
export CATHEDRAL_V2_SUBMIT_BACKPRESSURE_ENABLED=true
# Backpressure must engage BEFORE the box is in trouble. Both 2026-07-09 open
# windows (10% and 5%) wedged at the exact moment pending hit the 5000 queue
# cap with bp sheds still at zero: shedding AT the cap is shedding too late.
# 1500 sheds while the event loop is healthy; miner scripts see clean 429
# v2_submit_backpressure + Retry-After and settle into the verify drain rate.
export CATHEDRAL_V2_SUBMIT_BACKPRESSURE_MAX_PENDING=1500
export CATHEDRAL_V2_SUBMIT_BACKPRESSURE_MAX_OLDEST_AGE_SECS=120
export CATHEDRAL_V2_SUBMIT_BACKPRESSURE_RETRY_AFTER_SECS=5
export CATHEDRAL_SUBMIT_BUSY_WAIT_SECS=0.10
export CATHEDRAL_SUBMIT_HARD_CAP=32
export CATHEDRAL_SUBMIT_MAX_CONCURRENCY=32
export CATHEDRAL_RATELIMIT_RPM=90
export CATHEDRAL_PER_HOTKEY_LIMIT_ENABLED=true
export CATHEDRAL_PER_HOTKEY_BURST=30
export CATHEDRAL_PER_HOTKEY_REFILL_PER_SEC=5.0
export CATHEDRAL_PER_HOTKEY_RETRY_AFTER_SECS=1

export CATHEDRAL_V2_VERIFY_BATCH_SIZE=8
export CATHEDRAL_V2_VERIFY_INTERVAL_SECS=1
export CATHEDRAL_V2_VERIFY_LOCK_SECS=120
# 2 threads: verify drain was the hard ceiling (~6.5/s single-threaded; every
# admitted percentage outruns it and fills the queue in minutes). The private
# publisher shares 4 cores with the public origin + PG, so 2, not more.
export CATHEDRAL_V2_BITSET_VERIFY_THREADS=2
export CATHEDRAL_V2_SUBMIT_TOKEN_TTL_SECS=1800

export CATHEDRAL_PERMINER_NVARS_T1=600
# Tier2 difficulty gate (2026-07-08): 800/3408 keeps ratio 4.26 but makes
# tier2 the throughput limiter - miner solve time gates submit volume, so
# tight per-miner rate limits are unnecessary. Tier1 stays the easy 'biased'
# participation floor, bounded by ALLOTMENT_T1 so it is not a spam lane.
export CATHEDRAL_PERMINER_NVARS_T2=800
export CATHEDRAL_PERMINER_NCLAUSES_T1=2556
export CATHEDRAL_PERMINER_NCLAUSES_T2=3408
# 2026-07-08 wave-1 finding: at 500, active miners page hundreds of easy T1
# challenges past prebake depth, saturating the public origin's read path with
# on-demand generation at every epoch rollover. 50 keeps T1 a real
# participation floor and pushes competition to T2 (3x pay), which is
# miner-CPU-bound, not origin-bound.
export CATHEDRAL_PERMINER_ALLOTMENT_T1=50
export CATHEDRAL_PERMINER_SCORING_MODE=pm_primary
export CATHEDRAL_V2_REAL_FRACTION=0

export CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE=1
export CATHEDRAL_WEIGHTS_MODE=proportional
export CATHEDRAL_WEIGHTS_TIER2_MULT=3.0
# 72h (was 48): 2026-07-10 all-miner reopen. With the gate closed since the
# July 9 windows, the +18h coverage horizon collapsed to 44/256 live UIDs on
# a 48h window (preflight FAIL at min ratio 0.50). 72h keeps the same
# verified July 8-9 solve history paying through the horizon (154/256 =
# 0.60) instead of concentrating weight on the few brief-window solvers.
# Fairness-preserving: no history fabricated, guard untouched.
export CATHEDRAL_WEIGHTS_WINDOW_HOURS=72

export CATHEDRAL_BOARD_TTL_SECS=60
export CATHEDRAL_RECENT_CACHE_TTL_SECS=30
export CATHEDRAL_SLOW_REQUEST_LOG_SECS=30
export CATHEDRAL_DB_PATH=/home/polaris/cathedral/data/publisher.db
export CATHEDRAL_MATERIALIZED_SNAPSHOT_ENABLED=true
export CATHEDRAL_MATERIALIZED_SNAPSHOT_REFRESH_SECS=30
export CATHEDRAL_MATERIALIZED_SNAPSHOT_MAX_AGE=15
export CATHEDRAL_MATERIALIZED_SNAPSHOT_MAX_STALE_SECS=900
export CATHEDRAL_MATERIALIZED_SNAPSHOT_SWR_SECS=1200
export CATHEDRAL_DASHBOARD_SNAPSHOT_ENABLED=true
export CATHEDRAL_DASHBOARD_SNAPSHOT_REFRESH_SECS=10
export CATHEDRAL_DASHBOARD_SNAPSHOT_MAX_STALE_SECS=120
