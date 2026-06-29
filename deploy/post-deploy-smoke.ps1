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

$script:SmokeToolPreflight = @{}

function Get-SmokeToolKind {
    param([string]$Exe)

    $leaf = [System.IO.Path]::GetFileName($Exe).ToLowerInvariant()
    if ($leaf -match "^python(\.exe)?$" -or $leaf -match "^py(\.exe)?$") {
        return "Python"
    }
    if ($leaf -match "^node(\.exe)?$") {
        return "Node"
    }
    return "Tool"
}

function Assert-SmokeExecutable {
    param(
        [string]$Exe,
        [string]$Name
    )

    if ($script:SmokeToolPreflight.ContainsKey($Exe)) {
        return
    }

    $kind = Get-SmokeToolKind -Exe $Exe
    $cmd = Get-Command $Exe -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        throw "$Name requires $kind executable '$Exe', but it was not found on PATH. Pass -$kind <path> or run from an environment where it is installed."
    }

    $resolved = [string]$cmd.Path
    if ([string]::IsNullOrWhiteSpace($resolved)) {
        $resolved = [string]$cmd.Source
    }
    if ([string]::IsNullOrWhiteSpace($resolved)) {
        $resolved = [string]$cmd.Definition
    }
    if ($kind -eq "Python" -and $resolved -match "\\WindowsApps\\python(\.exe)?$") {
        throw "$Name requires a real Python, but '$Exe' resolves to the Microsoft Store stub at '$resolved'. Pass -Python <real-python.exe>, or run the smoke from WSL with the repo virtualenv."
    }

    try {
        $versionOutput = & $Exe "--version" 2>&1
        $exit = $LASTEXITCODE
    } catch {
        throw "$Name could not execute $kind '$Exe --version': $($_.Exception.Message)"
    }
    if ($exit -ne 0) {
        throw "$Name requires usable $kind '$Exe'; '$Exe --version' exited $exit. Output: $versionOutput"
    }

    $script:SmokeToolPreflight[$Exe] = $true
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

    Assert-SmokeExecutable -Exe $Exe -Name $Name

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
    Write-Host ""
    Write-Host "NOTE: publisher live smoke submits one deliberately wrong signed SAT assignment and expects it to be rejected/persisted." -ForegroundColor Yellow
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
