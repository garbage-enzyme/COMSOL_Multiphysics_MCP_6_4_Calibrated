param(
    [Parameter(Mandatory = $true)][string]$TestRoot
)

$ErrorActionPreference = 'Stop'
$Package = Split-Path -Parent $PSScriptRoot
$Module = Join-Path $Package 'powershell\DurableLauncher.psm1'
Import-Module $Module -Force
New-Item -ItemType Directory -Path $TestRoot -Force | Out-Null
$StatusPath = Join-Path $TestRoot 'status.json'
[IO.File]::WriteAllText($StatusPath, '{"status":"running","completed":0,"planned":1}', [Text.UTF8Encoding]::new($false))
$Config = @{
    JobName = 'Durable launcher refresh failure test'
    JobId = 'refresh-failure-test-v1-3'
    Driver = Join-Path $Package 'tests\fake_durable_driver.py'
    Output = $TestRoot
    TotalPoints = 1
    StatusPath = $StatusPath
    ResultsPath = Join-Path $TestRoot 'results.jsonl'
    ControlDirectory = Join-Path $TestRoot 'control'
    StdoutPath = Join-Path $TestRoot 'driver.stdout.log'
    StderrPath = Join-Path $TestRoot 'driver.stderr.log'
    MonitorIntervalSeconds = 0.2
    PointEstimateSeconds = 1.0
}
$Initial = Get-DurableJobSnapshot -Config $Config
if ($Initial.Status -ne 'running' -or $Initial.Planned -ne 1) {
    throw "Initial valid status was not read before corruption."
}
[IO.File]::WriteAllText($StatusPath, '{malformed', [Text.UTF8Encoding]::new($false))
$Config.TotalPoints = 'not-an-integer'
try {
    [void](Get-DurableJobSnapshot -Config $Config)
    throw 'Malformed status unexpectedly produced a snapshot with an invalid fallback total.'
}
catch {
    if ($_.Exception.Message -notmatch 'not-an-integer|cannot convert') {
        throw "Unexpected refresh failure trigger: $($_.Exception.Message)"
    }
}
Write-Host 'EXPECTED_REFRESH_FAILURE_TRIGGER status_json_corrupted'
[void](Show-DurableJobMonitor -Config $Config -NoTopMost)
