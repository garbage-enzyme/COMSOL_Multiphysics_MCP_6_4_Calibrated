param(
    [Parameter(Mandatory = $true)][string]$TestRoot
)

$ErrorActionPreference = 'Stop'
$Package = Split-Path -Parent $PSScriptRoot
$Module = Join-Path $Package 'powershell\DurableLauncher.psm1'
Import-Module $Module -Force
New-Item -ItemType Directory -Path $TestRoot -Force | Out-Null
$StatusPath = Join-Path $TestRoot 'status.json'
[IO.File]::WriteAllText($StatusPath, '{malformed', [Text.UTF8Encoding]::new($false))
$Config = @{
    JobName = 'Durable launcher refresh failure test'
    JobId = 'refresh-failure-test-v1-3'
    Driver = Join-Path $Package 'tests\fake_durable_driver.py'
    Output = $TestRoot
    TotalPoints = 'not-an-integer'
    StatusPath = $StatusPath
    ResultsPath = Join-Path $TestRoot 'results.jsonl'
    ControlDirectory = Join-Path $TestRoot 'control'
    StdoutPath = Join-Path $TestRoot 'driver.stdout.log'
    StderrPath = Join-Path $TestRoot 'driver.stderr.log'
    MonitorIntervalSeconds = 0.2
    PointEstimateSeconds = 1.0
}
[void](Show-DurableJobMonitor -Config $Config -NoTopMost)
