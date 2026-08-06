param(
    [Parameter(Mandatory = $true)][string]$PowerShellPath,
    [string]$TestRoot,
    [string]$PythonPath,
    [ValidateRange(1, 120)][int]$TimeoutSeconds = 30
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

function Get-ExactDriverProcessIds {
    $ExactDriver = [System.IO.Path]::GetFullPath($Driver)
    return @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $_.Name -ieq 'python.exe' -and
        -not [string]::IsNullOrWhiteSpace($_.CommandLine) -and
        $_.CommandLine.IndexOf($ExactDriver, [StringComparison]::OrdinalIgnoreCase) -ge 0
    } | ForEach-Object { [int]$_.ProcessId })
}

function Invoke-FailureCase {
    param(
        [string]$ScriptPath,
        [string]$ExpectedMessage,
        [string[]]$AdditionalArguments,
        [string]$CaseName
    )
    $Stdout = Join-Path $TestRoot ((Split-Path -Leaf $ScriptPath) + '.stdout.log')
    $Stderr = Join-Path $TestRoot ((Split-Path -Leaf $ScriptPath) + '.stderr.log')
    $PreviousTestMode = [Environment]::GetEnvironmentVariable('DURABLE_LAUNCHER_TEST_MODE', 'Process')
    try {
        [Environment]::SetEnvironmentVariable('DURABLE_LAUNCHER_TEST_MODE', '1', 'Process')
        $Arguments = @(
            '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            $ScriptPath, '-Run', '-NoTopMost', '-NoErrorHold'
        ) + $AdditionalArguments
        $BeforeDriverIds = @(Get-ExactDriverProcessIds)
        $StartInfo = [Diagnostics.ProcessStartInfo]::new()
        $StartInfo.FileName = $PowerShellPath
        $StartInfo.Arguments = (@($Arguments | ForEach-Object {
            '"' + ([string]$_).Replace('"', '\"') + '"'
        }) -join ' ')
        $StartInfo.UseShellExecute = $false
        $StartInfo.CreateNoWindow = $true
        $StartInfo.RedirectStandardOutput = $true
        $StartInfo.RedirectStandardError = $true
        $Process = [Diagnostics.Process]::new()
        $Process.StartInfo = $StartInfo
        [void]$Process.Start()
        $StdoutTask = $Process.StandardOutput.ReadToEndAsync()
        $StderrTask = $Process.StandardError.ReadToEndAsync()
        if (-not $Process.WaitForExit($TimeoutSeconds * 1000)) {
            try { $Process.Kill() } catch { }
            try { $Process.WaitForExit() } catch { }
            throw "Preflight failure case timed out after $TimeoutSeconds seconds: $CaseName"
        }
        $Process.WaitForExit()
        $ExitCode = [int]$Process.ExitCode
        $StdoutText = $StdoutTask.GetAwaiter().GetResult()
        $StderrText = $StderrTask.GetAwaiter().GetResult()
        [IO.File]::WriteAllText($Stdout, $StdoutText, [Text.UTF8Encoding]::new($false))
        [IO.File]::WriteAllText($Stderr, $StderrText, [Text.UTF8Encoding]::new($false))
        $Output = @($StdoutText, $StderrText) -join [Environment]::NewLine
        $AfterDriverIds = @(Get-ExactDriverProcessIds)
        $UnexpectedDriverIds = @($AfterDriverIds | Where-Object { $_ -notin $BeforeDriverIds })
        foreach ($ProcessId in $UnexpectedDriverIds) {
            Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    finally {
        [Environment]::SetEnvironmentVariable('DURABLE_LAUNCHER_TEST_MODE', $PreviousTestMode, 'Process')
    }
    if ($ExitCode -ne 1) { throw "Expected exit code 1, got ${ExitCode}: $ScriptPath" }
    if ($Output -notmatch 'LAUNCHER STARTUP FAILED' -or $Output.IndexOf($ExpectedMessage, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw "Expected visible failure message '$ExpectedMessage': $Output"
    }
    if ($Output -notmatch 'No active exact driver was detected') { throw "Missing exact-driver state assurance: $Output" }
    if ($UnexpectedDriverIds.Count -ne 0) { throw "Preflight failure started an exact driver: $($UnexpectedDriverIds -join ',')" }
    return [pscustomobject]@{ Script = $ScriptPath; ExitCode = $ExitCode; Output = $Output; StartedDriverCount = $UnexpectedDriverIds.Count }
}

$MissingDriverPath = Join-Path $TestRoot 'missing_driver.py'
$MissingDriverOutput = Join-Path $TestRoot 'missing_driver_output'
$MissingDriver = Invoke-FailureCase -ScriptPath $Template -ExpectedMessage 'Required file is missing' -AdditionalArguments (
    @('-Python', $Python, '-Output', $MissingDriverOutput, '-Driver', $MissingDriverPath)
) -CaseName 'missing_driver'
$MissingModuleScript = Join-Path $TestRoot 'missing_module_launcher.ps1'
[System.IO.File]::Copy($Template, $MissingModuleScript)
$MissingModuleOutput = Join-Path $TestRoot 'missing_module_output'
$MissingModule = Invoke-FailureCase -ScriptPath $MissingModuleScript -ExpectedMessage 'Launcher module is missing' -AdditionalArguments (
    @('-Python', $Python, '-Output', $MissingModuleOutput, '-Driver', $Driver)
) -CaseName 'missing_module'

$Receipt = [ordered]@{
    schema_name = 'durable_launcher.preflight_failure_test_receipt.v1'
    status = 'pass'
    powershell_path = $PowerShellPath
    powershell_version = (& $PowerShellPath -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion.ToString()')
    missing_driver_visible = $true
    missing_module_visible = $true
    failed_attempt_starts_no_driver = ($MissingDriver.StartedDriverCount -eq 0 -and $MissingModule.StartedDriverCount -eq 0)
    test_root = $TestRoot
}
$ReceiptPath = Join-Path $TestRoot 'preflight_failure_receipt.json'
$Receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReceiptPath -Encoding UTF8
Write-Host "DURABLE_PREFLIGHT_FAILURE_TEST_PASS receipt=$ReceiptPath"
