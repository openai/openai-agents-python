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

$logDir = Join-Path ([System.IO.Path]::GetTempPath()) ("code-change-verification-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $logDir | Out-Null

$steps = New-Object System.Collections.Generic.List[object]
$heartbeatIntervalSeconds = 10
if ($env:CODE_CHANGE_VERIFICATION_HEARTBEAT_SECONDS) {
    $heartbeatIntervalSeconds = [int]$env:CODE_CHANGE_VERIFICATION_HEARTBEAT_SECONDS
}

function Invoke-MakeStep {
    param(
        [Parameter(Mandatory = $true)][string]$Step
    )

    Write-Host "Running make $Step..."
    & make $Step

    if ($LASTEXITCODE -ne 0) {
        Write-Host "code-change-verification: make $Step failed with exit code $LASTEXITCODE."
        return $LASTEXITCODE
    }

    return 0
}

function Start-MakeStep {
    param(
        [Parameter(Mandatory = $true)][string]$Step
    )

    $logPath = Join-Path $logDir "$Step.log"
    Write-Host "Running make $Step..."
    $process = Start-Process -FilePath "make" -ArgumentList $Step -RedirectStandardOutput $logPath -RedirectStandardError $logPath -PassThru
    $steps.Add([PSCustomObject]@{
        Name = $Step
        Process = $process
        LogPath = $logPath
        StartTime = Get-Date
    })
}

function Stop-RunningSteps {
    param(
        [int]$ExcludePid = -1
    )

    foreach ($step in $steps) {
        if ($null -eq $step.Process) {
            continue
        }

        $step.Process.Refresh()
        if ($step.Process.HasExited -or $step.Process.Id -eq $ExcludePid) {
            continue
        }

        & taskkill /PID $step.Process.Id /T /F *> $null
    }

    foreach ($step in $steps) {
        if ($null -eq $step.Process) {
            continue
        }

        try {
            $step.Process.WaitForExit()
        } catch {
        }
    }
}

function Wait-ForParallelSteps {
    $pending = New-Object System.Collections.Generic.List[object]
    foreach ($step in $steps) {
        $pending.Add($step)
    }
    $nextHeartbeatAt = (Get-Date).AddSeconds($heartbeatIntervalSeconds)

    while ($pending.Count -gt 0) {
        foreach ($step in @($pending)) {
            $step.Process.Refresh()
            if (-not $step.Process.HasExited) {
                continue
            }

            $duration = [int]((Get-Date) - $step.StartTime).TotalSeconds
            if ($step.Process.ExitCode -eq 0) {
                Write-Host "make $($step.Name) passed in ${duration}s."
                [void]$pending.Remove($step)
                continue
            }

            Write-Host "code-change-verification: make $($step.Name) failed with exit code $($step.Process.ExitCode) after ${duration}s."
            Write-Host "--- $($step.Name) log (last 80 lines) ---"
            if (Test-Path $step.LogPath) {
                Get-Content $step.LogPath -Tail 80
            }

            Stop-RunningSteps -ExcludePid $step.Process.Id
            return $step.Process.ExitCode
        }

        if ($pending.Count -gt 0) {
            if ((Get-Date) -ge $nextHeartbeatAt) {
                $running = @()
                foreach ($step in $pending) {
                    $elapsed = [int]((Get-Date) - $step.StartTime).TotalSeconds
                    $running += "$($step.Name) (${elapsed}s)"
                }
                Write-Host ("code-change-verification: still running: " + ($running -join ", ") + ".")
                $nextHeartbeatAt = (Get-Date).AddSeconds($heartbeatIntervalSeconds)
            }
            Start-Sleep -Seconds 1
        }
    }

    return 0
}

$exitCode = 0

try {
    $exitCode = Invoke-MakeStep -Step "format"
    if ($exitCode -eq 0) {
        Write-Host "Running make lint, make typecheck, and make tests in parallel..."
        Start-MakeStep -Step "lint"
        Start-MakeStep -Step "typecheck"
        Start-MakeStep -Step "tests"

        $exitCode = Wait-ForParallelSteps
    }
} finally {
    Stop-RunningSteps
    Remove-Item $logDir -Recurse -Force -ErrorAction SilentlyContinue
}

if ($exitCode -ne 0) {
    exit $exitCode
}

Write-Host "code-change-verification: all commands passed."
