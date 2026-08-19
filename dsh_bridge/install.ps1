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
if (-not (Test-Path (Join-Path $Source 'package.json'))) {
  throw "source is not the bridge package root: $Source"
}
$parent = Split-Path -Parent $Target
New-Item -ItemType Directory -Force -Path $parent | Out-Null
if (Test-Path $Target) {
  $item = Get-Item $Target -Force
  if ($item.LinkType -eq 'Junction') {
    Write-Output "already linked: $Target"
    exit 0
  }
  throw "target exists and is not a junction: $Target"
}
New-Item -ItemType Junction -Path $Target -Target $Source | Out-Null
Write-Output "linked: $Target -> $Source"
