# Read-only Railway environment audit for Cathedral launch.
#
# This script calls `railway variable list --json`, which returns raw values, but
# it never prints those values. It reports only pass/fail for required reliability
# variables and whether the shared CNF token is present/equal across services.

[CmdletBinding()]
param(
    [string]$ReadService = "cathedral-read",
    [string]$SubmitService = "cathedral-submit",
    [string]$WorkerService = "cathedral-worker",
    [string]$Environment = "",
    [string]$Project = "",
    [string]$RailwayExe = $env:RAILWAY_CLI
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

function Add-RailwayStatusFailures {
    param([string]$Text)

    $matched = $false
    if ($Text -match "invalid_grant|Unauthorized|railway login") {
        Add-Failure "Railway CLI auth is expired or unauthorized. Run 'railway login'."
        $matched = $true
    }
    if ($Text -match "No linked project|railway link") {
        Add-Failure "This checkout is not linked to a Railway project. Run 'railway link'."
        $matched = $true
    }
    if (-not $matched) {
        Add-Failure "Railway status failed. Run 'railway status' for details."
    }
}

function Convert-RailwayVars {
    param($Json)

    $vars = @{}
    if ($null -eq $Json) {
        return $vars
    }

    if ($Json -is [System.Array]) {
        foreach ($item in $Json) {
            $name = $null
            foreach ($candidate in @("name", "key", "variableName")) {
                $prop = $item.PSObject.Properties[$candidate]
                if ($null -ne $prop -and -not [string]::IsNullOrWhiteSpace([string]$prop.Value)) {
                    $name = [string]$prop.Value
                    break
                }
            }
            if ([string]::IsNullOrWhiteSpace($name)) {
                continue
            }
            $value = ""
            foreach ($candidate in @("value", "val")) {
                $prop = $item.PSObject.Properties[$candidate]
                if ($null -ne $prop) {
                    $value = [string]$prop.Value
                    break
                }
            }
            $vars[[string]$name] = $value
        }
        return $vars
    }

    foreach ($prop in $Json.PSObject.Properties) {
        $value = $prop.Value
        if ($value -is [pscustomobject] -and $null -ne $value.PSObject.Properties["value"]) {
            $vars[[string]$prop.Name] = [string]$value.value
        } else {
            $vars[[string]$prop.Name] = [string]$value
        }
    }
    return $vars
}

function Get-ServiceVars {
    param(
        [string]$Railway,
        [string]$Service
    )

    $args = @("variable", "list", "--service", $Service, "--json")
    if (-not [string]::IsNullOrWhiteSpace($Environment)) {
        $args += @("--environment", $Environment)
    }
    if (-not [string]::IsNullOrWhiteSpace($Project)) {
        $args += @("--project", $Project)
    }

    $result = Invoke-Capture $Railway $args
    if ($result.Code -ne 0) {
        Add-Failure "Could not read Railway variables for service '$Service'. Check railway login/link and service name."
        return $null
    }
    try {
        return Convert-RailwayVars ($result.Text | ConvertFrom-Json)
    } catch {
        Add-Failure "Could not parse Railway variable JSON for service '$Service'."
        return $null
    }
}

function Test-ExpectedVars {
    param(
        [string]$Label,
        [hashtable]$Actual,
        [hashtable]$Expected
    )

    if ($null -eq $Actual) {
        return
    }
    foreach ($key in $Expected.Keys) {
        if (-not $Actual.ContainsKey($key)) {
            Add-Failure "$Label missing $key"
            continue
        }
        if ([string]$Actual[$key] -ne [string]$Expected[$key]) {
            Add-Failure "$Label has wrong $key"
        } else {
            Add-Pass "$Label $key"
        }
    }
}

$ReadExpected = @{
    CATHEDRAL_SERVICE_ROLE = "read"
    CATHEDRAL_REFILL_ENABLED = "false"
    CATHEDRAL_SEED_ON_BOOT = "false"
    WEB_CONCURRENCY = "2"
    CATHEDRAL_PM_READ_HARD_CAP = "128"
    CATHEDRAL_PG_STATEMENT_TIMEOUT_MS = "4000"
    CATHEDRAL_MATERIALIZED_SNAPSHOT_ENABLED = "1"
    CATHEDRAL_MATERIALIZED_SNAPSHOT_REFRESH_SECS = "60"
    CATHEDRAL_MATERIALIZED_SNAPSHOT_MAX_STALE_SECS = "900"
    CATHEDRAL_RECENT_SNAPSHOT_LIMIT = "50"
    CATHEDRAL_RECENT_NO_CURSOR_MAX_LIMIT = "50"
}

$SubmitExpected = @{
    CATHEDRAL_SERVICE_ROLE = "submit"
    CATHEDRAL_REFILL_ENABLED = "false"
    CATHEDRAL_SEED_ON_BOOT = "false"
    CATHEDRAL_SUBMIT_HARD_CAP = "8"
    CATHEDRAL_SUBMIT_MAX_CONCURRENCY = "24"
    WEB_CONCURRENCY = "2"
    CATHEDRAL_PM_READ_HARD_CAP = "128"
    CATHEDRAL_THREADPOOL_TOKENS = "32"
    CATHEDRAL_PG_POOL_MAX = "32"
    CATHEDRAL_PG_STATEMENT_TIMEOUT_MS = "4000"
}

$WorkerExpected = @{
    CATHEDRAL_SERVICE_ROLE = "worker"
    CATHEDRAL_REFILL_ENABLED = "true"
    CATHEDRAL_SINGLETON_RETRY_SECS = "15"
    CATHEDRAL_THREADPOOL_TOKENS = "8"
    CATHEDRAL_PG_POOL_MAX = "8"
}

Write-Host "Cathedral Railway env audit (read-only; no secret values printed)" -ForegroundColor Cyan

$railway = Resolve-RailwayCli -Requested $RailwayExe
if ([string]::IsNullOrWhiteSpace($railway)) {
    Add-Failure "Railway CLI not found. Pass -RailwayExe <path> or set RAILWAY_CLI."
} else {
    Add-Pass "Found Railway CLI"
    $status = Invoke-Capture $railway @("status")
    if ($status.Code -ne 0) {
        Add-RailwayStatusFailures $status.Text
    } else {
        Add-Pass "Railway CLI authenticated and project-linked"

        $readVars = Get-ServiceVars -Railway $railway -Service $ReadService
        $submitVars = Get-ServiceVars -Railway $railway -Service $SubmitService
        $workerVars = Get-ServiceVars -Railway $railway -Service $WorkerService

        Test-ExpectedVars -Label $ReadService -Actual $readVars -Expected $ReadExpected
        Test-ExpectedVars -Label $SubmitService -Actual $submitVars -Expected $SubmitExpected
        Test-ExpectedVars -Label $WorkerService -Actual $workerVars -Expected $WorkerExpected

        $tokenValues = @()
        foreach ($entry in @(
            @{Label=$ReadService; Vars=$readVars},
            @{Label=$SubmitService; Vars=$submitVars},
            @{Label=$WorkerService; Vars=$workerVars}
        )) {
            if ($null -eq $entry.Vars) {
                continue
            }
            if (-not $entry.Vars.ContainsKey("CATHEDRAL_CNF_TOKEN_SECRET") -or
                    [string]::IsNullOrWhiteSpace([string]$entry.Vars["CATHEDRAL_CNF_TOKEN_SECRET"])) {
                Add-Failure "$($entry.Label) missing CATHEDRAL_CNF_TOKEN_SECRET"
            } else {
                Add-Pass "$($entry.Label) CATHEDRAL_CNF_TOKEN_SECRET present"
                $tokenValues += [string]$entry.Vars["CATHEDRAL_CNF_TOKEN_SECRET"]
            }
        }
        if ($tokenValues.Count -eq 3) {
            $distinct = @($tokenValues | Select-Object -Unique)
            if ($distinct.Count -eq 1) {
                Add-Pass "CATHEDRAL_CNF_TOKEN_SECRET equal across read/submit/worker"
            } else {
                Add-Failure "CATHEDRAL_CNF_TOKEN_SECRET differs across services"
            }
        }
    }
}

if ($Failures.Count -gt 0) {
    Write-Host ""
    Write-Host "Railway env audit failed:" -ForegroundColor Red
    foreach ($failure in $Failures) {
        Write-Host ("- {0}" -f $failure) -ForegroundColor Red
    }
    exit 1
}

Write-Host ""
Write-Host "Railway env audit passed." -ForegroundColor Green
