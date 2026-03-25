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
    $repoRoot = (Resolve-Path (Join-Path $scriptDir "..\\..\\..\\..")).Path
} else {
    $repoRoot = ([string]$repoRoot).Trim()
}

Set-Location $repoRoot

$logDir = Join-Path ([System.IO.Path]::GetTempPath()) ("code-change-verification-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $logDir | Out-Null

$steps = New-Object System.Collections.Generic.List[object]
$heartbeatIntervalSeconds = 10
if ($env:CODE_CHANGE_VERIFICATION_HEARTBEAT_SECONDS) {
    $heartbeatIntervalSeconds = [int]$env:CODE_CHANGE_VERIFICATION_HEARTBEAT_SECONDS
}

function Resolve-MakeInvocation {
    $command = Get-Command make -ErrorAction Stop

    while ($command.CommandType -eq [System.Management.Automation.CommandTypes]::Alias) {
        $command = $command.ResolvedCommand
    }

    if ($command.CommandType -in @(
        [System.Management.Automation.CommandTypes]::Application,
        [System.Management.Automation.CommandTypes]::ExternalScript
    )) {
        $commandPath = if ($command.Path) { $command.Path } else { $command.Source }
        return [PSCustomObject]@{
            FilePath = $commandPath
            ArgumentList = @()
        }
    }

    if ($command.CommandType -eq [System.Management.Automation.CommandTypes]::Function) {
        $shellPath = (Get-Process -Id $PID).Path
        if (-not $shellPath) {
            throw "Unable to resolve the current PowerShell executable for make wrapper launches."
        }

        $wrapperPath = Join-Path $logDir "invoke-make.ps1"
        $escapedRepoRoot = $repoRoot -replace "'", "''"
        $wrapperTemplate = @'
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath '{0}'
function global:make {{
{1}
}}
& make @args
exit $LASTEXITCODE
'@
        $wrapperScript = $wrapperTemplate -f $escapedRepoRoot, $command.Definition.TrimEnd()
        Set-Content -Path $wrapperPath -Value $wrapperScript -Encoding UTF8

        return [PSCustomObject]@{
            FilePath = $shellPath
            ArgumentList = @("-NoLogo", "-NoProfile", "-File", $wrapperPath)
        }
    }

    throw "code-change-verification: make must resolve to an application, script, alias, or function."
}

$script:MakeInvocation = Resolve-MakeInvocation

function Invoke-MakeStep {
    param(
        [Parameter(Mandatory = $true)][string]$Step
    )

    Write-Host "Running make $Step..."
    & $script:MakeInvocation.FilePath @($script:MakeInvocation.ArgumentList + $Step)

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

    $stdoutLogPath = Join-Path $logDir "$Step.stdout.log"
    $stderrLogPath = Join-Path $logDir "$Step.stderr.log"
    Write-Host "Running make $Step..."
    $process = Start-Process -FilePath $script:MakeInvocation.FilePath -ArgumentList @($script:MakeInvocation.ArgumentList + $Step) -RedirectStandardOutput $stdoutLogPath -RedirectStandardError $stderrLogPath -PassThru
    $steps.Add([PSCustomObject]@{
        Name = $Step
        Process = $process
        StdoutLogPath = $stdoutLogPath
        StderrLogPath = $stderrLogPath
        StartTime = Get-Date
    })
}

function Stop-RunningSteps {
    foreach ($step in $steps) {
        if ($null -eq $step.Process) {
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
            if (Test-Path $step.StderrLogPath) {
                Write-Host "--- $($step.Name) stderr log (last 80 lines) ---"
                Get-Content $step.StderrLogPath -Tail 80
            }
            if (Test-Path $step.StdoutLogPath) {
                Write-Host "--- $($step.Name) stdout log (last 80 lines) ---"
                Get-Content $step.StdoutLogPath -Tail 80
            }

            Stop-RunningSteps
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
