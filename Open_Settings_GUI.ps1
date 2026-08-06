[CmdletBinding()]
param(
    [string]$PythonPath,
    [string]$SettingsPath,
    [switch]$ValidateOnly
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:PythonProbe = 'import sys; from comsol_mcp.settings_gui_launcher import launch_settings_gui; ready = sys.version_info[:2] == (3, 14) and callable(launch_settings_gui); print(''COMSOL_MCP_SETTINGS_GUI_PYTHON_READY'') if ready else sys.exit(2)'
$script:LaunchCode = 'import json; from comsol_mcp.settings_gui_launcher import launch_settings_gui; result = launch_settings_gui(); print(json.dumps(result, sort_keys=True, separators=('','', '':''))); raise SystemExit(0 if result.get(''success'') is True else 2)'
$script:PythonProbeTimeoutMilliseconds = 5000

function Invoke-BoundedPython {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$PrefixArguments = @(),
        [Parameter(Mandatory = $true)][string]$Code
    )

    $quotedCode = '"' + $Code.Replace('"', '\"') + '"'
    $arguments = (@($PrefixArguments) + @('-c', $quotedCode)) -join ' '
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    if ([System.IO.Path]::GetExtension($Executable) -in @('.cmd', '.bat')) {
        $startInfo.FileName = $env:ComSpec
        $escapedExecutable = '"' + $Executable.Replace('"', '""') + '"'
        $startInfo.Arguments = '/D /S /C "' + $escapedExecutable + ' ' + $arguments + '"'
    }
    else {
        $startInfo.FileName = $Executable
        $startInfo.Arguments = $arguments
    }
    $startInfo.WorkingDirectory = $PSScriptRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { return $null }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($script:PythonProbeTimeoutMilliseconds)) {
            try { $process.Kill() }
            catch { }
            try { $process.WaitForExit() }
            catch { }
            return $null
        }
        $process.WaitForExit()
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            Stdout = $stdoutTask.GetAwaiter().GetResult()
            Stderr = $stderrTask.GetAwaiter().GetResult()
        }
    }
    finally {
        $process.Dispose()
    }
}

function ConvertTo-ConsolePython {
    param([Parameter(Mandatory = $true)][string]$Candidate)

    $fullPath = [System.IO.Path]::GetFullPath($Candidate)
    if ([System.IO.Path]::GetFileName($fullPath) -ieq 'pythonw.exe') {
        $consolePath = Join-Path ([System.IO.Path]::GetDirectoryName($fullPath)) 'python.exe'
        if (Test-Path -LiteralPath $consolePath -PathType Leaf) {
            return [System.IO.Path]::GetFullPath($consolePath)
        }
        throw 'pythonw.exe requires a python.exe companion for bounded console probing.'
    }
    return $fullPath
}

function Test-ComsolMcpPython {
    param([Parameter(Mandatory = $true)][string]$Candidate)

    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $false
    }
    try {
        $probe = Invoke-BoundedPython -Executable $Candidate -Code $script:PythonProbe
        if ($null -eq $probe -or $probe.ExitCode -ne 0) {
            if ($null -ne $probe) {
                Write-Verbose ("Python probe failed: {0}" -f $probe.Stdout)
            }
            return $false
        }
        return ($probe.Stdout.Trim() -eq 'COMSOL_MCP_SETTINGS_GUI_PYTHON_READY')
    }
    catch {
        return $false
    }
}

function Get-ComsolMcpPython {
    param([string]$RequestedPath)

    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        $requested = ConvertTo-ConsolePython -Candidate $RequestedPath
        if (-not (Test-ComsolMcpPython -Candidate $requested)) {
            throw 'The selected Python must be CPython 3.14 and import comsol_mcp.settings_gui_launcher.'
        }
        return $requested
    }

    $candidates = New-Object 'System.Collections.Generic.List[string]'
    if (-not [string]::IsNullOrWhiteSpace($env:VIRTUAL_ENV)) {
        [void]$candidates.Add((Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe'))
        [void]$candidates.Add((Join-Path $env:VIRTUAL_ENV 'python.exe'))
    }

    $settingsCommand = Get-Command 'comsol-mcp-settings.exe' -ErrorAction SilentlyContinue
    if ($null -ne $settingsCommand) {
        $commandDirectory = Split-Path -Parent $settingsCommand.Source
        [void]$candidates.Add((Join-Path $commandDirectory 'python.exe'))
        [void]$candidates.Add((Join-Path (Split-Path -Parent $commandDirectory) 'python.exe'))
    }

    $pythonCommand = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand) {
        [void]$candidates.Add($pythonCommand.Source)
    }

    $pyCommand = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if ($null -ne $pyCommand) {
        try {
            $pyProbe = Invoke-BoundedPython -Executable $pyCommand.Source -PrefixArguments @('-3.14') -Code 'import sys; print(sys.executable)'
            if ($null -ne $pyProbe -and $pyProbe.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($pyProbe.Stdout)) {
                [void]$candidates.Add($pyProbe.Stdout.Trim())
            }
        }
        catch {
            # Continue through the remaining deterministic candidates.
        }
    }

    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($candidate in $candidates) {
        try {
            $normalized = ConvertTo-ConsolePython -Candidate $candidate
        }
        catch {
            continue
        }
        if ($seen.Add($normalized) -and (Test-ComsolMcpPython -Candidate $normalized)) {
            return $normalized
        }
    }
    throw 'No supported Python was found. Pass -PythonPath with a CPython 3.14 python.exe.'
}

$settingsPathWasPresent = Test-Path Env:COMSOL_MCP_SETTINGS_PATH
$previousSettingsPath = $env:COMSOL_MCP_SETTINGS_PATH
try {
    $settingsPathOverride = $settingsPathWasPresent
    if (-not [string]::IsNullOrWhiteSpace($SettingsPath)) {
        $settingsRoot = [System.IO.Path]::GetPathRoot($SettingsPath)
        $hasDriveRoot = $settingsRoot -match '^[A-Za-z]:[\\/]$'
        $hasUncRoot = $settingsRoot -match '^\\\\[^\\]+\\[^\\]+'
        if (-not ($hasDriveRoot -or $hasUncRoot)) {
            throw '-SettingsPath must be an absolute path.'
        }
        $resolvedSettingsPath = [System.IO.Path]::GetFullPath($SettingsPath)
        $settingsParent = Split-Path -Parent $resolvedSettingsPath
        if (-not (Test-Path -LiteralPath $settingsParent -PathType Container)) {
            throw 'The parent directory for -SettingsPath must already exist.'
        }
        if (Test-Path -LiteralPath $resolvedSettingsPath -PathType Container) {
            throw '-SettingsPath must identify a file, not a directory.'
        }
        $env:COMSOL_MCP_SETTINGS_PATH = $resolvedSettingsPath
        $settingsPathOverride = $true
    }

    $python = Get-ComsolMcpPython -RequestedPath $PythonPath
    if ($ValidateOnly) {
        [ordered]@{
            schema_name = 'comsol_mcp.settings_gui_root_launcher'
            schema_version = '1.0.0'
            ready = $true
            settings_path_override = $settingsPathOverride
            solver_started = $false
        } | ConvertTo-Json -Compress
        exit 0
    }

    Push-Location -LiteralPath $PSScriptRoot
    try {
        $launchResult = & $python -c $script:LaunchCode
        $launchExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    $jsonLines = @(
        foreach ($line in $launchResult) {
            if ($line -isnot [string] -or $line.Length -gt 65536) { continue }
            try { $candidateResult = $line | ConvertFrom-Json -ErrorAction Stop }
            catch { continue }
            if ($null -ne $candidateResult.PSObject.Properties['success'] -and $candidateResult.success -is [bool]) {
                $line
            }
        }
    )
    if ($jsonLines.Count -ne 1) {
        throw 'The Settings GUI launcher did not emit one bounded JSON result.'
    }
    $jsonLines[0] | Write-Output
    exit $launchExitCode
}
catch {
    Write-Error $_.Exception.Message -ErrorAction Continue
    exit 1
}
finally {
    if ($settingsPathWasPresent) {
        $env:COMSOL_MCP_SETTINGS_PATH = $previousSettingsPath
    }
    else {
        Remove-Item Env:COMSOL_MCP_SETTINGS_PATH -ErrorAction SilentlyContinue
    }
}
