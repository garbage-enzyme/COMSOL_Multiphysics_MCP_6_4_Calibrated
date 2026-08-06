param(
    [Parameter(Mandatory = $true)][string]$PowerShellPath,
    [string]$TestRoot
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($TestRoot)) {
    $TestRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) (
        'comsol_launcher_tests\v1_8_banner_' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    )
}
$Child = Join-Path $PSScriptRoot 'Test_TerminalBannerChild.ps1'
New-Item -ItemType Directory -Path $TestRoot -Force | Out-Null

function Get-TestSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $Stream = [IO.File]::OpenRead([IO.Path]::GetFullPath($Path))
    $Hash = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($Hash.ComputeHash($Stream))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $Hash.Dispose()
        $Stream.Dispose()
    }
}
$Expected = @{
    scientific = @('SCIENTIFIC / QUALITY GATE NOT MET', 'Reason: symmetry_gate_failed', 'Details:', 'Status JSON:', 'Driver log:', 'Error log:')
    failure = @('FAILED', 'Reason: COMSOL child exited with code 1 at point p008', 'Status JSON:', 'Driver log:', 'Error log:')
    paused = @('PAUSED', 'Reason: Pause request acknowledged after durable point p008', 'Status JSON:', 'Driver log:', 'Error log:')
}
$Cases = @()
foreach ($State in @('scientific', 'failure', 'paused')) {
    $Root = Join-Path $TestRoot $State
    New-Item -ItemType Directory -Path $Root -Force | Out-Null
    $Stdout = Join-Path $Root 'host.stdout.log'
    $Stderr = Join-Path $Root 'host.stderr.log'
    $Process = $null
    try {
        $Process = Start-Process -FilePath $PowerShellPath -ArgumentList @(
            '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            ('"' + $Child + '"'), '-TestRoot', ('"' + $Root + '"'), '-State', $State
        ) -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr -WindowStyle Hidden -PassThru
        $Deadline = [DateTime]::UtcNow.AddSeconds(20)
        $Complete = $false
        while ([DateTime]::UtcNow -lt $Deadline) {
            $Process.Refresh()
            if ($Process.HasExited) { throw "$State terminal monitor exited before rendering." }
            if (Test-Path -LiteralPath $Stdout -PathType Leaf) {
                $Rendered = [string](Get-Content -LiteralPath $Stdout -Raw)
                if ($null -eq $Rendered) { $Rendered = '' }
                $Complete = $true
                foreach ($Text in $Expected[$State]) {
                    if ($Rendered.IndexOf($Text, [StringComparison]::OrdinalIgnoreCase) -lt 0) { $Complete = $false; break }
                }
                if ($Complete) { break }
            }
            Start-Sleep -Milliseconds 250
        }
        if (-not $Complete) {
            throw "$State terminal monitor did not render every expected field within 20 seconds."
        }
        $Process.Refresh()
        if ($Process.HasExited) { throw "$State terminal monitor did not remain latched." }
    }
    finally {
        if ($null -ne $Process) {
            try { $Process.Refresh() } catch { }
            if (-not $Process.HasExited) {
                Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
                try { $Process.WaitForExit() } catch { }
            }
        }
    }
    $Output = if (Test-Path -LiteralPath $Stdout) { Get-Content -LiteralPath $Stdout -Raw } else { '' }
    $ErrorOutput = if (Test-Path -LiteralPath $Stderr) { Get-Content -LiteralPath $Stderr -Raw } else { '' }
    if ($null -eq $Output) { $Output = '' }
    if ($null -eq $ErrorOutput) { $ErrorOutput = '' }
    if (-not [string]::IsNullOrWhiteSpace($ErrorOutput)) { throw "$State terminal monitor wrote stderr: $ErrorOutput" }
    foreach ($Text in $Expected[$State]) {
        if ($Output.IndexOf($Text, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
            throw "$State terminal output omitted '$Text'."
        }
    }
    $Cases += [ordered]@{ state = $State; latched = $true; stdout_sha256 = Get-TestSha256 -Path $Stdout; stderr_empty = $true }
}
$Receipt = [ordered]@{
    schema_name = 'durable_launcher.terminal_banner_liveness_receipt.v1'
    status = 'pass'
    powershell_path = $PowerShellPath
    cases = $Cases
    all_show_reason_and_log_paths = $true
}
$ReceiptPath = Join-Path $TestRoot 'terminal_banner_receipt.json'
$Receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReceiptPath -Encoding UTF8
Write-Host "TERMINAL_BANNER_TEST_PASS receipt=$ReceiptPath sha256=$(Get-TestSha256 -Path $ReceiptPath)"
