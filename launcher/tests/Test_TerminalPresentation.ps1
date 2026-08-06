param(
    [string]$TestRoot
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($TestRoot)) {
    $TestRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) (
        'comsol_launcher_tests\v1_8_terminal_' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    )
}
$Package = Split-Path -Parent $PSScriptRoot
$Module = Join-Path $Package 'powershell\DurableLauncher.psm1'
$Driver = Join-Path $Package 'tests\fake_durable_driver.py'
Import-Module $Module -Force
New-Item -ItemType Directory -Path $TestRoot -Force | Out-Null

function Get-TestSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return & (Get-Module DurableLauncher) {
        param($Value)
        Get-DurableSha256 -Path $Value
    } $Path
}

function New-TerminalSnapshot {
    param([string]$Name, [hashtable]$Status)
    $Root = Join-Path $TestRoot $Name
    New-Item -ItemType Directory -Path $Root -Force | Out-Null
    $StatusPath = Join-Path $Root 'status.json'
    $ResultsPath = Join-Path $Root 'results.jsonl'
    Write-DurableAtomicJson -Path $StatusPath -Value $Status
    [IO.File]::WriteAllText($ResultsPath, "{`"point_id`":`"p1`"}`n", [Text.UTF8Encoding]::new($false))
    $Config = @{
        JobName = "Terminal presentation $Name"
        JobId = "terminal-presentation-$Name"
        Driver = $Driver
        Output = $Root
        TotalPoints = [int]$Status.planned
        StatusPath = $StatusPath
        ResultsPath = $ResultsPath
        ControlDirectory = Join-Path $Root 'control'
        StdoutPath = Join-Path $Root 'stdout.log'
        StderrPath = Join-Path $Root 'stderr.log'
        MonitorIntervalSeconds = 1
        PointEstimateSeconds = 1
    }
    return Get-DurableJobSnapshot -Config $Config
}

$Scientific = New-TerminalSnapshot -Name 'scientific' -Status ([ordered]@{
    status = 'symmetry_gate_failed'
    completed = 36
    planned = 718
    elapsed_seconds = 720
    symmetry_gate = [ordered]@{
        status = 'fail'
        pair_count = 9
        max_cross_helicity_absorption_absolute_difference = 0.021967938937959675
        max_ecd_antisymmetry_residual = 0.003926853734736918
        evidence = @([ordered]@{ point = 'omitted_from_bounded_summary' })
    }
})
if ((Get-DurableTerminalDisposition -Snapshot $Scientific) -ne 'not_accepted') {
    throw 'Scientific gate failure was not classified as not_accepted.'
}
if ($Scientific.TerminalReason -ne 'symmetry_gate_failed') {
    throw "Scientific terminal reason was not preserved: $($Scientific.TerminalReason)"
}
$DetailValues = @{}
foreach ($Part in @($Scientific.TerminalDetails -split '; ')) {
    $Pair = @($Part -split '=', 2)
    if ($Pair.Count -eq 2) { $DetailValues[$Pair[0]] = $Pair[1] }
}
$ObservedMetric = 0.0
$MetricText = $DetailValues['max_cross_helicity_absorption_absolute_difference']
if (
    [string]::IsNullOrWhiteSpace($MetricText) -or
    -not [double]::TryParse(
        $MetricText,
        [Globalization.NumberStyles]::Float,
        [Globalization.CultureInfo]::InvariantCulture,
        [ref]$ObservedMetric
    ) -or
    -not $ObservedMetric.Equals([double]0.021967938937959675)
) {
    throw "Scientific terminal details omitted the exact failing metric: $($Scientific.TerminalDetails)"
}
if ($Scientific.TerminalDetails -match 'omitted_from_bounded_summary') {
    throw 'Scientific terminal details leaked an evidence array.'
}

$Failure = New-TerminalSnapshot -Name 'failure' -Status ([ordered]@{
    status = 'failed'
    completed = 7
    planned = 20
    elapsed_seconds = 100
    reason = 'COMSOL child exited with code 1 at point p008'
})
if ((Get-DurableTerminalDisposition -Snapshot $Failure) -ne 'failed') {
    throw 'Runtime failure was not classified as failed.'
}
if ($Failure.TerminalReason -ne 'COMSOL child exited with code 1 at point p008') {
    throw "Runtime failure reason was not preserved: $($Failure.TerminalReason)"
}

$Paused = New-TerminalSnapshot -Name 'paused' -Status ([ordered]@{
    status = 'paused_after_point'
    completed = 8
    planned = 20
    elapsed_seconds = 120
    reason = 'Pause request acknowledged after durable point p008'
})
if ((Get-DurableTerminalDisposition -Snapshot $Paused) -ne 'paused') {
    throw 'Explicit pause was not classified as paused.'
}
if ($Paused.TerminalReason -ne 'Pause request acknowledged after durable point p008') {
    throw "Pause reason was not preserved: $($Paused.TerminalReason)"
}

$ModuleText = [IO.File]::ReadAllText($Module)
foreach ($Required in @(
    "'    SCIENTIFIC / QUALITY GATE NOT MET   '",
    "'                 FAILED                 '",
    "'                 PAUSED                 '",
    "`$Highlights[`$LineIndex] = 'Yellow'",
    "`$Highlights[`$LineIndex] = 'Red'",
    "`$Highlights[`$LineIndex] = 'Blue'",
    '("Reason: {0}" -f $Snapshot.TerminalReason)'
    '("Status JSON: {0}" -f $Config.StatusPath)'
    '("Driver log:  {0}" -f $Config.StdoutPath)'
    '("Error log:   {0}" -f $Config.StderrPath)'
)) {
    if ($ModuleText.IndexOf($Required, [StringComparison]::Ordinal) -lt 0) {
        throw "Terminal presentation implementation is missing: $Required"
    }
}

$Receipt = [ordered]@{
    schema_name = 'durable_launcher.terminal_presentation_test_receipt.v1'
    status = 'pass'
    scientific_disposition = 'not_accepted'
    scientific_color = 'Yellow'
    scientific_reason = $Scientific.TerminalReason
    scientific_details = $Scientific.TerminalDetails
    runtime_failure_disposition = 'failed'
    runtime_failure_color = 'Red'
    runtime_failure_reason = $Failure.TerminalReason
    explicit_pause_disposition = 'paused'
    explicit_pause_color = 'Blue'
    explicit_pause_reason = $Paused.TerminalReason
    evidence_arrays_excluded = $true
    module_sha256 = Get-TestSha256 -Path $Module
}
$ReceiptPath = Join-Path $TestRoot 'terminal_presentation_receipt.json'
Write-DurableAtomicJson -Path $ReceiptPath -Value $Receipt
Write-Host "TERMINAL_PRESENTATION_TEST_PASS receipt=$ReceiptPath sha256=$(Get-TestSha256 -Path $ReceiptPath)"
