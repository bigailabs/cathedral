# Cathedral controlled-v0 post-deploy smoke.
#
# Run this after merging/deploying the publisher + edge-router changes. It is
# intentionally read-heavy and non-destructive except for live_smoke.py, which
# submits one deliberately wrong signed SAT assignment to prove submit reaches
# the deployed referee and persists the replay guard.

[CmdletBinding()]
param(
    [string]$BaseUrl = "https://api.cathedral.computer",
    [string]$EdgeBaseUrl = "",
    [string]$ReadBaseUrl = "https://read.cathedral.computer",
    [string]$SubmitBaseUrl = "https://submit.cathedral.computer",
    [string]$WorkerBaseUrl = "",
    [string]$Node = "node",
    [string]$Python = "python",
    [int]$SoakIterations = 3,
    [switch]$SkipValidatorReleaseGate,
    [switch]$ValidatorReleaseNoChain,
    [switch]$SkipSplitOriginSmoke,
    [switch]$SkipEdgeSmoke,
    [switch]$SkipRouteMap,
    [switch]$SkipSoak,
    [switch]$SkipLiveSmoke,
    [switch]$PlanOnly
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($EdgeBaseUrl)) {
    $EdgeBaseUrl = $BaseUrl
}

function Invoke-SmokeStep {
    param(
        [string]$Name,
        [string]$Exe,
        [string[]]$CommandArgs,
        [hashtable]$Env = @{}
    )

    Write-Host ""
    Write-Host "==> $Name" -ForegroundColor Cyan
    foreach ($key in $Env.Keys) {
        Write-Host ("    {0}={1}" -f $key, $Env[$key])
    }
    Write-Host ("    {0} {1}" -f $Exe, ($CommandArgs -join " ")) -ForegroundColor DarkGray

    if ($PlanOnly) {
        return
    }

    $old = @{}
    foreach ($key in $Env.Keys) {
        $old[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
        [Environment]::SetEnvironmentVariable($key, [string]$Env[$key], "Process")
    }
    try {
        & $Exe @CommandArgs
        if ($LASTEXITCODE -ne 0) {
            throw "$Name failed with exit $LASTEXITCODE"
        }
    } finally {
        foreach ($key in $Env.Keys) {
            [Environment]::SetEnvironmentVariable($key, $old[$key], "Process")
        }
    }
}

if (-not $SkipValidatorReleaseGate) {
    $args = @(
        "scripts/validator_release_gate.py",
        "--feed-url",
        $BaseUrl,
        "--read-url",
        $ReadBaseUrl
    )
    if ($ValidatorReleaseNoChain) {
        $args += "--no-chain"
    }
    Invoke-SmokeStep `
        -Name "validator release gate" `
        -Exe $Python `
        -CommandArgs $args
}

if (-not $SkipSplitOriginSmoke) {
    $envVars = @{
        CATHEDRAL_READ_BASE_URL = $ReadBaseUrl
        CATHEDRAL_SUBMIT_BASE_URL = $SubmitBaseUrl
    }
    if (-not [string]::IsNullOrWhiteSpace($WorkerBaseUrl)) {
        $envVars["CATHEDRAL_WORKER_BASE_URL"] = $WorkerBaseUrl
    }
    Invoke-SmokeStep `
        -Name "split origin smoke" `
        -Exe $Node `
        -CommandArgs @("deploy/split-origin-smoke.mjs") `
        -Env $envVars
}

if (-not $SkipEdgeSmoke) {
    Invoke-SmokeStep `
        -Name "edge smoke" `
        -Exe $Node `
        -CommandArgs @("deploy/edge-router/smoke.mjs") `
        -Env @{ CATHEDRAL_EDGE_BASE_URL = $EdgeBaseUrl }
}

if (-not $SkipRouteMap) {
    Invoke-SmokeStep `
        -Name "edge route map" `
        -Exe $Node `
        -CommandArgs @("deploy/edge-router/route-map.mjs") `
        -Env @{ CATHEDRAL_EDGE_BASE_URL = $EdgeBaseUrl }
}

if (-not $SkipSoak) {
    Invoke-SmokeStep `
        -Name "edge short soak" `
        -Exe $Node `
        -CommandArgs @("deploy/edge-router/soak.mjs") `
        -Env @{
            CATHEDRAL_EDGE_BASE_URL = $EdgeBaseUrl
            CATHEDRAL_EDGE_SOAK_ITERATIONS = [string]$SoakIterations
        }
}

if (-not $SkipLiveSmoke) {
    Invoke-SmokeStep `
        -Name "publisher live smoke" `
        -Exe $Python `
        -CommandArgs @("live_smoke.py") `
        -Env @{ BASE_URL = $BaseUrl }
}

Write-Host ""
if ($PlanOnly) {
    Write-Host "Plan only. Re-run without -PlanOnly after deploy." -ForegroundColor Green
} else {
    Write-Host "Post-deploy smoke passed." -ForegroundColor Green
}
