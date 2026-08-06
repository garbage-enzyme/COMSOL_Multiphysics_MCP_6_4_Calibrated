param(
    [switch]$Run,
    [switch]$ValidateOnly,
    [switch]$Monitor,
    [switch]$NoTopMost,
    [string]$Python = $env:DURABLE_LAUNCHER_PYTHON,
    [string]$Driver = $env:DURABLE_LAUNCHER_DRIVER,
    [string]$Output = $env:DURABLE_LAUNCHER_OUTPUT,
    [string]$JobName = 'COMSOL durable job',
    [string]$JobId = 'comsol-durable-job',
    [ValidateRange(1, 1000000)][int]$TotalPoints = 1,
    [ValidateRange(0.1, 3600.0)][double]$MonitorIntervalSeconds = 2.0,
    [ValidateRange(0.0, 1000000000.0)][double]$PointEstimateSeconds = 60.0,
    [ValidateRange(0.0, 1048576.0)][double]$MinimumFreeRamGiB = 1.0,
    [ValidateRange(0.0, 1048576.0)][double]$MinimumFreeSystemDriveGiB = 5.0,
    [ValidateRange(0.0, 1048576.0)][double]$MinimumFreeOutputDriveGiB = 20.0,
    [string]$ModulePath,
    [string[]]$RequiredFiles = @(),
    [Parameter(DontShow = $true)][switch]$NoErrorHold
)

$ErrorActionPreference = 'Stop'
$EffectiveNoErrorHold = $NoErrorHold -and $env:DURABLE_LAUNCHER_TEST_MODE -eq '1'

function Show-LauncherBootstrapFailure {
    param([System.Management.Automation.ErrorRecord]$Record, [switch]$NoHold)
    Write-Host '========================================' -ForegroundColor Red
    Write-Host '        LAUNCHER STARTUP FAILED         ' -ForegroundColor Red
    Write-Host '========================================' -ForegroundColor Red
    Write-Host ("Type:     {0}" -f $Record.Exception.GetType().FullName) -ForegroundColor Red
    Write-Host ("Message:  {0}" -f $Record.Exception.Message) -ForegroundColor Red
    Write-Host ("Launcher: {0}" -f $PSCommandPath)
    Write-Host 'No active exact driver was detected after this bootstrap failure.' -ForegroundColor Yellow
    if ($NoHold) { return }
    if (-not [Environment]::UserInteractive) { return }
    try { $CanRead = -not [Console]::IsInputRedirected }
    catch { $CanRead = $true }
    while ($CanRead) {
        try { $Raw = Read-Host 'Enter quit to close' }
        catch { return }
        if ($null -eq $Raw) { return }
        $Command = ([string]$Raw).Trim().ToLowerInvariant()
        if ($Command -in @('quit', 'q', 'close')) { return }
        Write-Host ("Invalid input '{0}'. Enter quit." -f $Command) -ForegroundColor Yellow
    }
}

function Resolve-LauncherFile {
    param([string]$Value, [string]$Label, [switch]$AllowCommand)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw [System.ArgumentException]::new("$Label is required.")
    }
    if (Test-Path -LiteralPath $Value -PathType Leaf) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    if ($AllowCommand) {
        $Command = Get-Command -Name $Value -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $Command) {
            return [System.IO.Path]::GetFullPath($Command.Source)
        }
    }
    throw [System.IO.FileNotFoundException]::new("$Label is missing: $Value")
}

try {
    $PackageRoot = Split-Path -Parent $PSScriptRoot
    if ([string]::IsNullOrWhiteSpace($ModulePath)) {
        $ModulePath = Join-Path $PackageRoot 'powershell\DurableLauncher.psm1'
    }
    if (-not (Test-Path -LiteralPath $ModulePath -PathType Leaf)) {
        throw [System.IO.FileNotFoundException]::new("Launcher module is missing: $ModulePath")
    }
    Import-Module $ModulePath -Force

    if ([string]::IsNullOrWhiteSpace($Python)) {
        throw [System.ArgumentException]::new(
            'Python is required. Pass -Python or set DURABLE_LAUNCHER_PYTHON.'
        )
    }
    if ([string]::IsNullOrWhiteSpace($Driver)) {
        throw [System.ArgumentException]::new(
            'Driver is required. Pass -Driver or set DURABLE_LAUNCHER_DRIVER.'
        )
    }
    if ([string]::IsNullOrWhiteSpace($Output)) {
        throw [System.ArgumentException]::new(
            'Output is required. Pass -Output or set DURABLE_LAUNCHER_OUTPUT.'
        )
    }
    $ModulePath = Resolve-LauncherFile -Value $ModulePath -Label 'Launcher module'
    $Python = Resolve-LauncherFile -Value $Python -Label 'Python executable' -AllowCommand
    $Driver = Resolve-LauncherFile -Value $Driver -Label 'Required file'
    $Output = [System.IO.Path]::GetFullPath($Output)
    if (Test-Path -LiteralPath $Output) {
        if (-not (Test-Path -LiteralPath $Output -PathType Container)) {
            throw [System.IO.IOException]::new("Output is not a directory: $Output")
        }
    }
    else {
        [void](New-Item -ItemType Directory -Path $Output -ErrorAction Stop)
    }
    $ProbePath = Join-Path $Output ('.launcher-write-probe-' + [Guid]::NewGuid().ToString('N'))
    $Probe = [System.IO.File]::Open(
        $ProbePath,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None,
        4096,
        [System.IO.FileOptions]::DeleteOnClose
    )
    $Probe.Dispose()
    $ResolvedRequired = @(
        foreach ($RequiredFile in $RequiredFiles) {
            Resolve-LauncherFile -Value $RequiredFile -Label 'Required file'
        }
    )
    $Required = @($ModulePath, $Driver) + $ResolvedRequired

    $Config = @{
        JobName = $JobName
        JobId = $JobId
        LauncherPath = $PSCommandPath
        ModulePath = $ModulePath
        Python = $Python
        Driver = $Driver
        Output = $Output
        TotalPoints = $TotalPoints
        StatusPath = Join-Path $Output 'status.json'
        ResultsPath = Join-Path $Output 'results.jsonl'
        ControlDirectory = Join-Path $Output 'control'
        StdoutPath = Join-Path $Output 'launcher.stdout.log'
        StderrPath = Join-Path $Output 'launcher.stderr.log'
        MonitorIntervalSeconds = $MonitorIntervalSeconds
        PointEstimateSeconds = $PointEstimateSeconds
        MinimumFreeRamGiB = $MinimumFreeRamGiB
        MinimumFreeSystemDriveGiB = $MinimumFreeSystemDriveGiB
        MinimumFreeOutputDriveGiB = $MinimumFreeOutputDriveGiB
        RequiredFiles = $Required
        ValidateEnvironment = @{
            DURABLE_JOB_MODE = 'validate'
            DURABLE_JOB_OUTPUT = $Output
            DURABLE_JOB_CONTROL_DIR = Join-Path $Output 'control'
        }
        RunEnvironment = @{
            DURABLE_JOB_MODE = 'solve'
            DURABLE_JOB_OUTPUT = $Output
            DURABLE_JOB_CONTROL_DIR = Join-Path $Output 'control'
        }
    }

    Invoke-DurableJobLauncher -Config $Config -Run:$Run -ValidateOnly:$ValidateOnly -Monitor:$Monitor -NoTopMost:$NoTopMost
}
catch {
    $OriginalError = $_
    try {
        if ($null -ne (Get-Command Show-DurableLauncherFailure -ErrorAction SilentlyContinue)) {
            $DriverState = 'unknown'
            $DriverCandidate = $null
            if ($null -ne $Config -and $Config.ContainsKey('Driver')) {
                $DriverCandidate = $Config.Driver
            }
            elseif (-not [string]::IsNullOrWhiteSpace($Driver)) {
                try { $DriverCandidate = [System.IO.Path]::GetFullPath($Driver) }
                catch { $DriverCandidate = $null }
            }
            if ($null -ne $DriverCandidate) {
                try {
                    $DriverState = if (@(Get-DurableDriverProcesses -DriverPath $DriverCandidate).Count -gt 0) { 'active' } else { 'absent' }
                }
                catch { $DriverState = 'unknown' }
            }
            [void](Show-DurableLauncherFailure -ErrorRecord $OriginalError -LauncherPath $PSCommandPath -DriverState $DriverState -NoHold:$EffectiveNoErrorHold -NoTopMost:$NoTopMost)
        }
        else {
            Show-LauncherBootstrapFailure -Record $OriginalError -NoHold:$EffectiveNoErrorHold
        }
    }
    catch {
        try { Show-LauncherBootstrapFailure -Record $OriginalError -NoHold:$EffectiveNoErrorHold }
        catch { }
    }
    exit 1
}
