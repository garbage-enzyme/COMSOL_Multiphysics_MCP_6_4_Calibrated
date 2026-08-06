param(
    [Parameter(Mandatory = $true)][string]$TestRoot,
    [Parameter(Mandatory = $true)][ValidateSet('scientific', 'failure', 'paused')][string]$State
)

$ErrorActionPreference = 'Stop'
$Package = Split-Path -Parent $PSScriptRoot
$Module = Join-Path $Package 'powershell\DurableLauncher.psm1'
Import-Module $Module -Force
New-Item -ItemType Directory -Path $TestRoot -Force | Out-Null
$StatusPath = Join-Path $TestRoot 'status.json'
$ResultsPath = Join-Path $TestRoot 'results.jsonl'
$Status = switch ($State) {
    'scientific' {
        [ordered]@{
            status = 'symmetry_gate_failed'; completed = 36; planned = 718; elapsed_seconds = 720
            symmetry_gate = [ordered]@{ status = 'fail'; max_metric = 0.02196794; evidence = @('excluded') }
        }
    }
    'failure' {
        [ordered]@{ status = 'failed'; completed = 7; planned = 20; elapsed_seconds = 100; reason = 'COMSOL child exited with code 1 at point p008' }
    }
    'paused' {
        [ordered]@{ status = 'paused_after_point'; completed = 8; planned = 20; elapsed_seconds = 120; reason = 'Pause request acknowledged after durable point p008' }
    }
}
Write-DurableAtomicJson -Path $StatusPath -Value $Status
[IO.File]::WriteAllText($ResultsPath, "{`"point_id`":`"p1`"}`n", [Text.UTF8Encoding]::new($false))
$Config = @{
    JobName = "Terminal banner $State"
    JobId = "terminal-banner-$State"
    Driver = Join-Path $Package 'tests\fake_durable_driver.py'
    Output = $TestRoot
    TotalPoints = [int]$Status.planned
    StatusPath = $StatusPath
    ResultsPath = $ResultsPath
    ControlDirectory = Join-Path $TestRoot 'control'
    StdoutPath = Join-Path $TestRoot 'driver.stdout.log'
    StderrPath = Join-Path $TestRoot 'driver.stderr.log'
    MonitorIntervalSeconds = 0.2
    PointEstimateSeconds = 1
}
$MonitorResult = Show-DurableJobMonitor -Config $Config -NoTopMost
throw "Terminal banner monitor returned unexpectedly: $MonitorResult"
