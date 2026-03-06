Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$repoRoot = $null

try {
    $repoRoot = (& git -C $scriptDir rev-parse --show-toplevel 2>$null)
} catch {
    $repoRoot = $null
}

if (-not $repoRoot) {
    $repoRoot = Resolve-Path (Join-Path $scriptDir "..\\..\\..\\..")
}

Set-Location $repoRoot

function Invoke-MakeStep {
    param(
        [Parameter(Mandatory = $true)][string]$Step
    )

    Write-Host "Running make $Step..."
    & make $Step

    if ($LASTEXITCODE -ne 0) {
        Write-Error "code-change-verification: make $Step failed with exit code $LASTEXITCODE."
        exit $LASTEXITCODE
    }
}

Invoke-MakeStep -Step "format"
Invoke-MakeStep -Step "lint"
Invoke-MakeStep -Step "mypy"
Invoke-MakeStep -Step "tests"

Write-Host "code-change-verification: all commands passed."
