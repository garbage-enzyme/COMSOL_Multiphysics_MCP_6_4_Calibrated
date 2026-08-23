# Links the bridge package into a DSH profile's node_modules so the loader
# can resolve '@local/dsh-comsol-bridge'. Uses a directory junction (no admin
# needed, follows the profile's npm layout like pnpm symlinks).
#
# Usage:
#   .\install.ps1                          # into ~/.dsh/profiles/web (default)
#   .\install.ps1 -Target <custom target>  # CI smoke of the script itself
param(
  [string]$Source = $PSScriptRoot,
  [string]$Target = (Join-Path $env:USERPROFILE '.dsh\profiles\web\node_modules\@local\dsh-comsol-bridge')
)
$ErrorActionPreference = 'Stop'
# Normalize -Source once so the package.json check and the junction target are
# unambiguous regardless of the caller's current working directory.
$Source = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Source)
if (-not (Test-Path (Join-Path $Source 'package.json'))) {
  throw "source is not the bridge package root: $Source"
}
$parent = Split-Path -Parent $Target
New-Item -ItemType Directory -Force -Path $parent | Out-Null
# Get-Item -Force sees broken junctions that Test-Path misses, so a stale or
# dangling link is diagnosed here instead of failing inside New-Item.
$item = Get-Item $Target -Force -ErrorAction SilentlyContinue
if ($null -ne $item) {
  if ($item.LinkType -ne 'Junction') {
    throw "target exists and is not a junction: $Target"
  }
  $existing = @($item.Target)[0]
  if (-not $existing -and ($item.PSObject.Properties.Name -contains 'LinkTarget')) {
    $existing = $item.LinkTarget
  }
  if (-not $existing) {
    throw "junction target could not be read: $Target"
  }
  $resolvedExisting = [System.IO.Path]::GetFullPath($existing)
  if ($resolvedExisting.TrimEnd('\') -ine $Source.TrimEnd('\')) {
    throw "junction points elsewhere: $Target -> $resolvedExisting (expected $Source)"
  }
  Write-Output "already linked: $Target"
  exit 0
}
New-Item -ItemType Junction -Path $Target -Target $Source | Out-Null
Write-Output "linked: $Target -> $Source"
