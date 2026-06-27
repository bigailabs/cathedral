# Cathedral publisher role-split deploy config (Railway).
#
# This script is the single executable source of the per-service environment for
# the read / submit / worker split. Re-running it must NOT be able to quietly
# reintroduce the cap-1 submit choke or the missing read statement timeout that
# took the read origin down (see deploy/RELIABILITY_UPGRADE_PLAN.md, Phase 1).
#
# It is intentionally a DRY-RUN by default: it prints the exact
# `railway variables --set` commands it would run, per service, so the values can
# be reviewed before anything is applied live. Pass -Apply to actually set them.
#
# Why these defaults (do not regress them):
#   CATHEDRAL_SUBMIT_HARD_CAP=8        -> hard cap of 1 serialized the whole submit
#                                         lane; two overlapping submits made one
#                                         miner get 429 submit_busy_retry. 8 is the
#                                         safe restart point (raise to 16 only after
#                                         the service is proven stable at 8).
#   CATHEDRAL_SUBMIT_MAX_CONCURRENCY=24 -> effective cap is min(this, hard_cap).
#   WEB_CONCURRENCY=2                  -> 1 single-threaded the worker process; 2
#                                         restores parallel request handling while
#                                         staying inside DB connection headroom.
#   CATHEDRAL_PM_READ_HARD_CAP=128     -> per-miner read gate; 1 throttled PM CNF
#                                         fetches to one at a time.
#   CATHEDRAL_PG_STATEMENT_TIMEOUT_MS=4000 (read-serving services) -> the MISSING
#                                         value. With no statement timeout,
#                                         /v1/leaderboard/recent ran 30-46s and
#                                         exhausted the connection pool, taking the
#                                         whole read origin down. 4s bounds any
#                                         single query so one slow board scan cannot
#                                         pin pool connections.
#   CATHEDRAL_MATERIALIZED_SNAPSHOT_ENABLED=1 (read service) -> read-heavy
#                                         board/leaderboard surfaces are built on
#                                         a timer and can serve the last good
#                                         snapshot when the live query path is
#                                         degraded. This is load-shedding, not a
#                                         scoring change.
#   CATHEDRAL_CNF_TOKEN_SECRET         -> MUST be the same value on every service so
#                                         active-cnf (issued on one replica/service)
#                                         and CNF fetch (served on another) agree on
#                                         the HMAC token. Never let services drift.

[CmdletBinding()]
param(
    # Apply the variables live via the Railway CLI. Default is dry-run (print only).
    [switch]$Apply,

    # Railway service names. Override if your project names differ.
    [string]$ReadService = "cathedral-read",
    [string]$SubmitService = "cathedral-submit",
    [string]$WorkerService = "cathedral-worker",

    # Shared HMAC secret for CNF tokens. MUST be identical across all services.
    # Provide via -CnfTokenSecret or the CATHEDRAL_CNF_TOKEN_SECRET env var.
    [string]$CnfTokenSecret = $env:CATHEDRAL_CNF_TOKEN_SECRET
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($CnfTokenSecret)) {
    Write-Warning "CATHEDRAL_CNF_TOKEN_SECRET is not set. It MUST be the same value on every service or CNF fetch will fail across replicas. Pass -CnfTokenSecret or set the env var before -Apply."
}

# Safe defaults, keyed by service role. These are the values that must not regress.
$ReadVars = [ordered]@{
    "CATHEDRAL_SERVICE_ROLE"           = "read"
    "CATHEDRAL_REFILL_ENABLED"         = "false"
    "CATHEDRAL_SEED_ON_BOOT"           = "false"
    "WEB_CONCURRENCY"                  = "2"
    "CATHEDRAL_PM_READ_HARD_CAP"       = "128"
    "CATHEDRAL_PG_STATEMENT_TIMEOUT_MS" = "4000"
    "CATHEDRAL_MATERIALIZED_SNAPSHOT_ENABLED" = "1"
    "CATHEDRAL_MATERIALIZED_SNAPSHOT_REFRESH_SECS" = "60"
    "CATHEDRAL_MATERIALIZED_SNAPSHOT_MAX_STALE_SECS" = "900"
    "CATHEDRAL_RECENT_SNAPSHOT_LIMIT"  = "50"
    "CATHEDRAL_RECENT_NO_CURSOR_MAX_LIMIT" = "50"
}

$SubmitVars = [ordered]@{
    "CATHEDRAL_SERVICE_ROLE"           = "submit"
    "CATHEDRAL_REFILL_ENABLED"         = "false"
    "CATHEDRAL_SEED_ON_BOOT"           = "false"
    "CATHEDRAL_SUBMIT_HARD_CAP"        = "8"
    "CATHEDRAL_SUBMIT_MAX_CONCURRENCY" = "24"
    "WEB_CONCURRENCY"                  = "2"
    "CATHEDRAL_PM_READ_HARD_CAP"       = "128"
    "CATHEDRAL_THREADPOOL_TOKENS"      = "32"
    "CATHEDRAL_PG_POOL_MAX"            = "32"
    # Submit also serves active-cnf reads; bound its queries too.
    "CATHEDRAL_PG_STATEMENT_TIMEOUT_MS" = "4000"
}

$WorkerVars = [ordered]@{
    "CATHEDRAL_SERVICE_ROLE"        = "worker"
    "CATHEDRAL_REFILL_ENABLED"      = "true"
    "CATHEDRAL_SINGLETON_RETRY_SECS" = "15"
    "CATHEDRAL_THREADPOOL_TOKENS"   = "8"
    "CATHEDRAL_PG_POOL_MAX"         = "8"
}

function Set-ServiceVars {
    param(
        [string]$ServiceName,
        [System.Collections.Specialized.OrderedDictionary]$Vars
    )

    $all = [ordered]@{}
    foreach ($k in $Vars.Keys) { $all[$k] = $Vars[$k] }
    # Shared secret goes on every service, identical value.
    if (-not [string]::IsNullOrWhiteSpace($CnfTokenSecret)) {
        $all["CATHEDRAL_CNF_TOKEN_SECRET"] = $CnfTokenSecret
    }

    $setArgs = @()
    foreach ($k in $all.Keys) {
        $setArgs += "--set"
        $setArgs += ("{0}={1}" -f $k, $all[$k])
    }

    Write-Host ""
    Write-Host "==> Service: $ServiceName" -ForegroundColor Cyan
    foreach ($k in $all.Keys) {
        $shown = if ($k -eq "CATHEDRAL_CNF_TOKEN_SECRET") { "<shared-secret>" } else { $all[$k] }
        Write-Host ("    {0}={1}" -f $k, $shown)
    }

    if ($Apply) {
        Write-Host "    applying via railway CLI..." -ForegroundColor Yellow
        & railway variables --service $ServiceName @setArgs
        if ($LASTEXITCODE -ne 0) {
            throw "railway variables failed for service '$ServiceName' (exit $LASTEXITCODE)"
        }
    } else {
        $joined = ($setArgs | ForEach-Object {
            if ($_ -like "CATHEDRAL_CNF_TOKEN_SECRET=*") { "CATHEDRAL_CNF_TOKEN_SECRET=<shared-secret>" } else { $_ }
        }) -join " "
        Write-Host "    (dry-run) railway variables --service $ServiceName $joined" -ForegroundColor DarkGray
    }
}

Set-ServiceVars -ServiceName $ReadService   -Vars $ReadVars
Set-ServiceVars -ServiceName $SubmitService -Vars $SubmitVars
Set-ServiceVars -ServiceName $WorkerService -Vars $WorkerVars

Write-Host ""
if ($Apply) {
    Write-Host "Applied. Redeploy each service so the new variables take effect, then verify:" -ForegroundColor Green
} else {
    Write-Host "Dry-run only. Re-run with -Apply to set these live, then redeploy each service and verify:" -ForegroundColor Green
}
Write-Host "  GET /v1/admin/synthetic-boolean/submit-metrics  -> hard_cap: 8"
Write-Host "  GET /v1/leaderboard/recent?limit=2              -> fast, materialized when warm, structured 503 on live-query failure"
