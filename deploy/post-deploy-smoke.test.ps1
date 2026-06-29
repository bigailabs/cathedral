$ErrorActionPreference = "Stop"

$scriptPath = Join-Path $PSScriptRoot "post-deploy-smoke.ps1"

function Invoke-ChildPowerShell {
    param([string[]]$ChildArgs)

    $exe = if ($PSVersionTable.PSEdition -eq "Core") {
        (Get-Command pwsh -ErrorAction Stop).Source
    } else {
        (Get-Command powershell -ErrorAction Stop).Source
    }
    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $exe @ChildArgs 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }
    return @{
        Code = $code
        Text = ($output | Out-String)
    }
}

$plan = Invoke-ChildPowerShell @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    $scriptPath,
    "-PlanOnly",
    "-SkipSoak",
    "-SkipLiveSmoke"
)
if ($plan.Code -ne 0) {
    throw "PlanOnly should not require Python/Node preflight. Output: $($plan.Text)"
}
if ($plan.Text -notmatch "validator release gate" -or
    $plan.Text -notmatch "split origin smoke" -or
    $plan.Text -notmatch "edge smoke") {
    throw "PlanOnly did not print the expected smoke steps. Output: $($plan.Text)"
}

$fullPlan = Invoke-ChildPowerShell @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    $scriptPath,
    "-PlanOnly"
)
if ($fullPlan.Code -ne 0) {
    throw "Full PlanOnly should not require Python/Node preflight. Output: $($fullPlan.Text)"
}
foreach ($expected in @(
    "validator release gate",
    "split origin smoke",
    "edge smoke",
    "edge route map",
    "edge short soak",
    "publisher live smoke",
    "deliberately wrong signed SAT assignment"
)) {
    if ($fullPlan.Text -notmatch [regex]::Escape($expected)) {
        throw "Full PlanOnly did not print expected final-gate item '$expected'. Output: $($fullPlan.Text)"
    }
}

$missingPython = Invoke-ChildPowerShell @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    $scriptPath,
    "-Python",
    (Join-Path $PSScriptRoot "__missing__\python.exe"),
    "-ValidatorReleaseNoChain",
    "-SkipSplitOriginSmoke",
    "-SkipEdgeSmoke",
    "-SkipRouteMap",
    "-SkipSoak",
    "-SkipLiveSmoke"
)
if ($missingPython.Code -eq 0) {
    throw "Missing Python preflight should fail before running the validator gate."
}
if ($missingPython.Text -notmatch "requires Python executable") {
    throw "Missing Python failure was not actionable. Output: $($missingPython.Text)"
}

$missingNode = Invoke-ChildPowerShell @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    $scriptPath,
    "-Node",
    (Join-Path $PSScriptRoot "__missing__\node.exe"),
    "-SkipValidatorReleaseGate",
    "-SkipLiveSmoke",
    "-SkipSoak"
)
if ($missingNode.Code -eq 0) {
    throw "Missing Node preflight should fail before running split-origin smoke."
}
if ($missingNode.Text -notmatch "requires Node executable") {
    throw "Missing Node failure was not actionable. Output: $($missingNode.Text)"
}

Write-Host "post-deploy-smoke PowerShell tests passed"
