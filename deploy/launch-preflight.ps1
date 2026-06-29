# Cathedral launch preflight: read-only operator checks before merge/deploy.
#
# This script does not mutate GitHub, Railway, production, or chain state. It
# proves the local operator environment is ready to perform the launch sequence:
# PR clean, branch current, Railway CLI authenticated/linked, and the final smoke
# gate command valid.

[CmdletBinding()]
param(
    [int]$PrNumber = 317,
    [string]$RailwayExe = $env:RAILWAY_CLI,
    [string]$Python = "python",
    [string]$Node = "node",
    [switch]$SkipRailway,
    [switch]$SkipRailwayEnvAudit
)

$ErrorActionPreference = "Stop"
$Failures = New-Object System.Collections.Generic.List[string]

function Add-Failure {
    param([string]$Message)
    $Failures.Add($Message) | Out-Null
    Write-Host ("FAIL: {0}" -f $Message) -ForegroundColor Red
}

function Add-Pass {
    param([string]$Message)
    Write-Host ("PASS: {0}" -f $Message) -ForegroundColor Green
}

function Invoke-Capture {
    param(
        [string]$Exe,
        [string[]]$CommandArgs
    )
    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $Exe @CommandArgs 2>&1
        [pscustomobject]@{
            Code = $LASTEXITCODE
            Text = ($output | Out-String).Trim()
        }
    } finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }
}

function Require-Command {
    param([string]$Name)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        Add-Failure "Required command '$Name' not found on PATH."
        return $null
    }
    Add-Pass "Found $Name at $($cmd.Source)"
    return $cmd.Source
}

function Resolve-RailwayCli {
    param([string]$Requested)

    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($Requested)) {
        $candidates += $Requested
    }
    $cmd = Get-Command railway -ErrorAction SilentlyContinue
    if ($null -ne $cmd) {
        $candidates += $cmd.Source
    }
    if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
        $candidates += (Join-Path $env:USERPROFILE "bin\railway.exe")
        $candidates += (Join-Path $env:USERPROFILE ".railway\staged-update\railway.exe")
    }

    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            continue
        }
        $resolved = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($null -ne $resolved) {
            return $resolved.Source
        }
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

Write-Host "Cathedral launch preflight (read-only)" -ForegroundColor Cyan

$git = Require-Command "git"
$gh = Require-Command "gh"
$nodeCmd = Require-Command $Node
$pythonCmd = Require-Command $Python
if ($pythonCmd -and $pythonCmd -match "\\WindowsApps\\python(\.exe)?$") {
    Add-Failure "Python resolves to the Microsoft Store stub at '$pythonCmd'. Pass -Python <real-python.exe> or run from WSL."
}

if ($git) {
    $fetch = Invoke-Capture "git" @("fetch", "origin", "main")
    if ($fetch.Code -ne 0) {
        Add-Failure "git fetch origin main failed: $($fetch.Text)"
    } else {
        Add-Pass "Fetched origin/main"
    }

    $status = Invoke-Capture "git" @("status", "--porcelain")
    if ($status.Code -ne 0) {
        Add-Failure "git status failed: $($status.Text)"
    } elseif (-not [string]::IsNullOrWhiteSpace($status.Text)) {
        Add-Failure "Working tree is dirty; commit/stash before launch."
    } else {
        Add-Pass "Working tree clean"
    }

    $relation = Invoke-Capture "git" @("rev-list", "--left-right", "--count", "origin/main...HEAD")
    if ($relation.Code -ne 0) {
        Add-Failure "Could not compare branch with origin/main: $($relation.Text)"
    } else {
        $parts = $relation.Text -split "\s+"
        $behind = [int]$parts[0]
        $ahead = [int]$parts[1]
        if ($behind -ne 0) {
            Add-Failure "Branch is $behind commit(s) behind origin/main."
        } else {
            Add-Pass "Branch is current with origin/main ($ahead commit(s) ahead)"
        }
    }
}

if ($gh) {
    $prRaw = Invoke-Capture "gh" @(
        "pr", "view", [string]$PrNumber,
        "--json", "number,state,isDraft,mergeable,mergeStateStatus,headRefOid,statusCheckRollup,url"
    )
    if ($prRaw.Code -ne 0) {
        Add-Failure "gh pr view failed: $($prRaw.Text)"
    } else {
        $pr = $prRaw.Text | ConvertFrom-Json
        if ($pr.state -ne "OPEN") {
            Add-Failure "PR #$PrNumber is not open (state=$($pr.state))."
        } elseif ($pr.isDraft) {
            Add-Failure "PR #$PrNumber is still draft."
        } elseif ($pr.mergeable -ne "MERGEABLE" -or $pr.mergeStateStatus -ne "CLEAN") {
            Add-Failure "PR #$PrNumber is not cleanly mergeable (mergeable=$($pr.mergeable), state=$($pr.mergeStateStatus))."
        } else {
            Add-Pass "PR #$PrNumber is cleanly mergeable: $($pr.url)"
        }

        $badChecks = @()
        foreach ($check in @($pr.statusCheckRollup)) {
            if ($check.status -ne "COMPLETED") {
                $badChecks += "$($check.name):$($check.status)"
            } elseif ($check.conclusion -and $check.conclusion -notin @("SUCCESS", "NEUTRAL", "SKIPPED")) {
                $badChecks += "$($check.name):$($check.conclusion)"
            }
        }
        if ($badChecks.Count -gt 0) {
            Add-Failure "PR checks not green: $($badChecks -join ', ')"
        } else {
            Add-Pass "PR checks are green"
        }
    }
}

if ($SkipRailway) {
    Write-Host "WARN: Skipping Railway auth/link check; this is not final launch evidence." -ForegroundColor Yellow
} else {
    $railway = Resolve-RailwayCli -Requested $RailwayExe
    if ([string]::IsNullOrWhiteSpace($railway)) {
        Add-Failure "Railway CLI not found. Pass -RailwayExe <path> or set RAILWAY_CLI."
    } else {
        Add-Pass "Found Railway CLI at $railway"
        $railwayStatus = Invoke-Capture $railway @("status")
        if ($railwayStatus.Code -ne 0) {
            Add-Failure "Railway CLI is not authenticated or this checkout is not linked. Run 'railway login' and 'railway link'."
        } else {
            Add-Pass "Railway CLI is authenticated and project-linked"
            if ($SkipRailwayEnvAudit) {
                Write-Host "WARN: Skipping Railway env audit; this is not final launch evidence." -ForegroundColor Yellow
            } else {
                $envAudit = Invoke-Capture "powershell" @(
                    "-NoProfile",
                    "-ExecutionPolicy", "Bypass",
                    "-File", "deploy/railway-env-audit.ps1",
                    "-RailwayExe", $railway
                )
                if ($envAudit.Code -ne 0) {
                    Add-Failure "Railway env audit failed. Run deploy/railway-env-audit.ps1 directly for service-level details."
                } else {
                    Add-Pass "Railway read/submit/worker env audit passed"
                }
            }
        }
    }
}

if ($nodeCmd -and $pythonCmd) {
    $plan = Invoke-Capture "powershell" @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "deploy/post-deploy-smoke.ps1",
        "-PlanOnly",
        "-RequireFinalGate",
        "-Python", $Python,
        "-Node", $Node
    )
    if ($plan.Code -ne 0) {
        Add-Failure "Final smoke plan failed: $($plan.Text)"
    } elseif ($plan.Text -notmatch "full final controlled-v0 gate") {
        Add-Failure "Final smoke plan did not identify the full final gate."
    } else {
        Add-Pass "Final smoke gate command is valid"
    }
}

if ($Failures.Count -gt 0) {
    Write-Host ""
    Write-Host "Launch preflight failed:" -ForegroundColor Red
    foreach ($failure in $Failures) {
        Write-Host ("- {0}" -f $failure) -ForegroundColor Red
    }
    exit 1
}

Write-Host ""
Write-Host "Launch preflight passed. You can merge/deploy, then run deploy/post-deploy-smoke.ps1 -RequireFinalGate -Python deploy\python-wsl.cmd." -ForegroundColor Green
