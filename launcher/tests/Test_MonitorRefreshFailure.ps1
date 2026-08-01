param(
    [Parameter(Mandatory = $true)][string]$PowerShellPath,
    [string]$TestRoot
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($TestRoot)) {
    $TestRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) (
        'comsol_launcher_tests\v1_8_refresh_' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    )
}
$Child = Join-Path $PSScriptRoot 'Test_RefreshFailureChild.ps1'
New-Item -ItemType Directory -Path $TestRoot -Force | Out-Null
$Stdout = Join-Path $TestRoot 'host.stdout.log'
$Stderr = Join-Path $TestRoot 'host.stderr.log'
$Process = Start-Process -FilePath $PowerShellPath -ArgumentList @(
    '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
    ('"' + $Child + '"'), '-TestRoot', ('"' + $TestRoot + '"')
) -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr -WindowStyle Hidden -PassThru
$Deadline = [DateTime]::UtcNow.AddSeconds(15)
$Alive = $true
$Output = ''
while ([DateTime]::UtcNow -lt $Deadline) {
    $Process.Refresh()
    $Alive = -not $Process.HasExited
    $Output = (Get-Content -LiteralPath $Stdout -Raw -ErrorAction SilentlyContinue) + (Get-Content -LiteralPath $Stderr -Raw -ErrorAction SilentlyContinue)
    if (-not $Alive -or ($Output -match 'MONITOR REFRESH FAILED' -and $Output -match 'displayed progress is not current')) {
        break
    }
    Start-Sleep -Milliseconds 100
}
if ($Alive) {
    Stop-Process -Id $Process.Id -Force
    $Process.WaitForExit()
}
if (-not $Alive) { throw "Refresh-failure monitor exited instead of latching: $Output" }
if ($Output -notmatch 'MONITOR REFRESH FAILED' -or $Output -notmatch 'displayed progress is not current') {
    throw "Refresh-failure monitor did not visibly replace stale progress: $Output"
}
$Receipt = [ordered]@{
    schema_name = 'durable_launcher.monitor_refresh_failure_test_receipt.v1'
    status = 'pass'
    powershell_path = $PowerShellPath
    powershell_version = (& $PowerShellPath -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion.ToString()')
    monitor_alive_until_failure_banner = $true
    refresh_failure_banner_visible = $true
    stale_progress_explicitly_disclaimed = $true
    test_pid = $Process.Id
    test_root = $TestRoot
}
$ReceiptPath = Join-Path $TestRoot 'monitor_refresh_failure_receipt.json'
$Receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReceiptPath -Encoding UTF8
Write-Host "DURABLE_MONITOR_REFRESH_FAILURE_TEST_PASS receipt=$ReceiptPath"
