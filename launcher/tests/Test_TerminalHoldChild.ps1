param(
    [Parameter(Mandatory = $true)][string]$TestRoot
)

$ErrorActionPreference = 'Stop'
$Package = Split-Path -Parent $PSScriptRoot
$Module = Join-Path $Package 'powershell\DurableLauncher.psm1'
Import-Module $Module -Force
New-Item -ItemType Directory -Path $TestRoot -Force | Out-Null
$StatusPath = Join-Path $TestRoot 'status.json'
$ResultsPath = Join-Path $TestRoot 'results.jsonl'
Write-DurableAtomicJson -Path $StatusPath -Value ([ordered]@{
    status = 'solver_and_postprocess_complete_unclassified'
    completed = 1
    planned = 1
    elapsed_seconds = 1.0
    spec_id = 'terminal-hold-test-v1-3'
})
[IO.File]::WriteAllText($ResultsPath, "{`"point_id`":`"p1`"}`n", [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText((Join-Path $TestRoot 'monitor.ready'), 'ready', [Text.UTF8Encoding]::new($false))
$Config = @{
    JobName = 'Durable launcher terminal hold test'
    JobId = 'terminal-hold-test-v1-3'
    Driver = Join-Path $Package 'tests\fake_durable_driver.py'
    Output = $TestRoot
    TotalPoints = 1
    StatusPath = $StatusPath
    ResultsPath = $ResultsPath
    ControlDirectory = Join-Path $TestRoot 'control'
    StdoutPath = Join-Path $TestRoot 'stdout.log'
    StderrPath = Join-Path $TestRoot 'stderr.log'
    MonitorIntervalSeconds = 0.2
    PointEstimateSeconds = 1.0
}
[void](Show-DurableJobMonitor -Config $Config -NoTopMost)
