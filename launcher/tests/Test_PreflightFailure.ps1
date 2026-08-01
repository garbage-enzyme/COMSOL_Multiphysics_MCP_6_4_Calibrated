param(
    [Parameter(Mandatory = $true)][string]$PowerShellPath,
    [string]$TestRoot,
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'
$Package = Split-Path -Parent $PSScriptRoot
$Template = Join-Path $Package 'templates\Run_DurableJob.template.ps1'
$Driver = Join-Path $Package 'tests\fake_durable_driver.py'
$Python = if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    (Get-Command python.exe -ErrorAction Stop).Source
}
else { [System.IO.Path]::GetFullPath($PythonPath) }
if ([string]::IsNullOrWhiteSpace($TestRoot)) {
    $TestRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) (
        'comsol_launcher_tests\v1_8_preflight_' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    )
}
New-Item -ItemType Directory -Path $TestRoot -Force | Out-Null

function Invoke-FailureCase {
    param([string]$ScriptPath, [string]$ExpectedMessage, [string[]]$AdditionalArguments)
    $Stdout = Join-Path $TestRoot ((Split-Path -Leaf $ScriptPath) + '.stdout.log')
    $Stderr = Join-Path $TestRoot ((Split-Path -Leaf $ScriptPath) + '.stderr.log')
    $PreviousTestMode = [Environment]::GetEnvironmentVariable('DURABLE_LAUNCHER_TEST_MODE', 'Process')
    try {
        [Environment]::SetEnvironmentVariable('DURABLE_LAUNCHER_TEST_MODE', '1', 'Process')
        $Arguments = @(
            '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            ('"' + $ScriptPath + '"'), '-Run', '-NoTopMost', '-NoErrorHold'
        ) + $AdditionalArguments
        $Process = Start-Process -FilePath $PowerShellPath -ArgumentList $Arguments -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr -Wait -PassThru
    }
    finally {
        [Environment]::SetEnvironmentVariable('DURABLE_LAUNCHER_TEST_MODE', $PreviousTestMode, 'Process')
    }
    $Output = (Get-Content -LiteralPath $Stdout -Raw -ErrorAction SilentlyContinue) + (Get-Content -LiteralPath $Stderr -Raw -ErrorAction SilentlyContinue)
    if ($Process.ExitCode -ne 1) { throw "Expected exit code 1, got $($Process.ExitCode): $ScriptPath" }
    if ($Output -notmatch 'LAUNCHER STARTUP FAILED' -or $Output.IndexOf($ExpectedMessage, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw "Expected visible failure message '$ExpectedMessage': $Output"
    }
    if ($Output -notmatch 'No active exact driver was detected') { throw "Missing exact-driver state assurance: $Output" }
    return [pscustomobject]@{ Script = $ScriptPath; ExitCode = $Process.ExitCode; Output = $Output }
}

$MissingDriverPath = Join-Path $TestRoot 'missing_driver.py'
$CaseOutput = Join-Path $TestRoot 'case_output'
$CommonArguments = @(
    '-Python', ('"' + $Python + '"'),
    '-Output', ('"' + $CaseOutput + '"')
)
$MissingDriver = Invoke-FailureCase -ScriptPath $Template -ExpectedMessage 'Required file is missing' -AdditionalArguments (
    $CommonArguments + @('-Driver', ('"' + $MissingDriverPath + '"'))
)
$MissingModuleScript = Join-Path $TestRoot 'missing_module_launcher.ps1'
[System.IO.File]::Copy($Template, $MissingModuleScript)
$MissingModule = Invoke-FailureCase -ScriptPath $MissingModuleScript -ExpectedMessage 'Launcher module is missing' -AdditionalArguments (
    $CommonArguments + @('-Driver', ('"' + $Driver + '"'))
)

$Receipt = [ordered]@{
    schema_name = 'durable_launcher.preflight_failure_test_receipt.v1'
    status = 'pass'
    powershell_path = $PowerShellPath
    powershell_version = (& $PowerShellPath -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion.ToString()')
    missing_driver_visible = $true
    missing_module_visible = $true
    failed_attempt_starts_no_driver = $true
    test_root = $TestRoot
}
$ReceiptPath = Join-Path $TestRoot 'preflight_failure_receipt.json'
$Receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReceiptPath -Encoding UTF8
Write-Host "DURABLE_PREFLIGHT_FAILURE_TEST_PASS receipt=$ReceiptPath"
