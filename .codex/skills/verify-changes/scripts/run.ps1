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

Write-Host "Running make format..."
& make format

Write-Host "Running make lint..."
& make lint

Write-Host "Running make mypy..."
& make mypy

Write-Host "Running make tests..."
& make tests

Write-Host "verify-changes: all commands passed."
