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
    Push-Location -LiteralPath $PSScriptRoot
    try {
        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $probeOutput = & $Candidate -c $script:PythonProbe 2>$null
        $probeExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousErrorAction
        if ($probeExitCode -ne 0) {
            Write-Verbose ("Python probe failed: {0}" -f ($probeOutput -join "`n"))
            return $false
        }
        return (($probeOutput -join "`n").Trim() -eq 'COMSOL_MCP_SETTINGS_GUI_PYTHON_READY')
    }
    catch {
        return $false
    }
    finally {
        Pop-Location
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
            $pyExecutable = & $pyCommand.Source -3.14 -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($pyExecutable)) {
                [void]$candidates.Add(($pyExecutable -join "`n").Trim())
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
        if (-not [System.IO.Path]::IsPathRooted($SettingsPath)) {
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
    $jsonLines = @($launchResult | Where-Object { $_ -is [string] -and $_.Trim() -match '^\{.*\}$' })
    if ($jsonLines.Count -ne 1) {
        throw 'The Settings GUI launcher did not emit one bounded JSON result.'
    }
    $null = $jsonLines[0] | ConvertFrom-Json
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
