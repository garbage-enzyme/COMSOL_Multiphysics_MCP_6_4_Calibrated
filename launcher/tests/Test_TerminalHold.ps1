param(
    [Parameter(Mandatory = $true)][string]$PowerShellPath,
    [string]$TestRoot
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($TestRoot)) {
    $TestRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) (
        'comsol_launcher_tests\v1_8_hold_' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    )
}
$Child = Join-Path $PSScriptRoot 'Test_TerminalHoldChild.ps1'
New-Item -ItemType Directory -Path $TestRoot -Force | Out-Null
$Stdout = Join-Path $TestRoot 'host.stdout.log'
$Stderr = Join-Path $TestRoot 'host.stderr.log'
$Process = Start-Process -FilePath $PowerShellPath -ArgumentList @(
    '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
    ('"' + $Child + '"'), '-TestRoot', ('"' + $TestRoot + '"')
) -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr -WindowStyle Hidden -PassThru
$Deadline = [DateTime]::UtcNow.AddSeconds(20)
while ([DateTime]::UtcNow -lt $Deadline) {
    $Process.Refresh()
    if ($Process.HasExited) { throw 'Successful terminal monitor exited before rendering.' }
    $Output = if (Test-Path -LiteralPath $Stdout) {
        Get-Content -LiteralPath $Stdout -Raw -ErrorAction SilentlyContinue
    }
    else { '' }
    if ($Output -match 'COMPLETED SUCCESSFULLY') { break }
    Start-Sleep -Milliseconds 250
}
$Process.Refresh()
$Output = if (Test-Path -LiteralPath $Stdout) {
    Get-Content -LiteralPath $Stdout -Raw -ErrorAction SilentlyContinue
}
else { '' }
$ErrorOutput = if (Test-Path -LiteralPath $Stderr) {
    Get-Content -LiteralPath $Stderr -Raw -ErrorAction SilentlyContinue
}
else { '' }
if ($Process.HasExited) { throw 'Successful terminal monitor did not remain latched.' }
if ($Output -notmatch 'COMPLETED SUCCESSFULLY') {
    throw "Successful terminal banner did not render: $Output"
}
if (-not [string]::IsNullOrWhiteSpace($ErrorOutput)) {
    throw "Successful terminal monitor wrote stderr: $ErrorOutput"
}
Stop-Process -Id $Process.Id -Force
$Process.WaitForExit()
$Receipt = [ordered]@{
    schema_name = 'durable_launcher.terminal_hold_test_receipt.v1'
    status = 'pass'
    powershell_path = $PowerShellPath
    powershell_version = (& $PowerShellPath -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion.ToString()')
    success_banner_visible = $true
    terminal_alive_until_operator_quit = $true
    stderr_empty = $true
    test_root = $TestRoot
}
$ReceiptPath = Join-Path $TestRoot 'terminal_hold_receipt.json'
$Receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReceiptPath -Encoding UTF8
Write-Host "DURABLE_TERMINAL_HOLD_TEST_PASS receipt=$ReceiptPath"
