<#
.SYNOPSIS
  Remove the Python virtual environment (.venv) of this project, and optionally the caches and the saved GUI settings.

.DESCRIPTION
  Deletes only the .venv folder next to this script's parent (after checking that it really is a virtual environment).
  Your source files are never touched. A running scope_gui (or any Python started from that .venv) holds its files open;
  it is listed and has to be stopped first (-Force stops it for you). Rebuild the environment with scripts\install.ps1.

.PARAMETER Force
  Do not ask for confirmation, and stop Python processes that are running from the .venv.

.PARAMETER Caches
  Also remove __pycache__ folders, .pytest_cache, build, dist and *.egg-info under the project (not inside .venv).

.PARAMETER Settings
  Also remove what scope_gui stores outside the project: the saved settings in the registry
  (HKCU\Software\UNI-T-Scope) and the error log folder (%LOCALAPPDATA%\UNI-T-Scope).

.EXAMPLE
  scripts\cleanup.ps1               # asks first
  scripts\cleanup.ps1 -Force -Caches
  scripts\cleanup.ps1 -WhatIf       # only shows what would be removed
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [switch]$Force,
    [switch]$Caches,
    [switch]$Settings
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$venv = Join-Path $root '.venv'
if ($Force) { $ConfirmPreference = 'None' }

# A session that cannot show a prompt (powershell -NonInteractive, a scheduled task) must not get a raw exception.
trap {
    if ($_.Exception.Message -match 'NonInteractive') {
        Write-Host 'Nothing was removed: this session cannot ask for confirmation. Run again with -Force.' -ForegroundColor Yellow
        exit 1
    }
    throw
}

function Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }

function Get-FolderSize($path) {
    $bytes = (Get-ChildItem $path -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    '{0:N0} MB' -f ($bytes / 1MB)
}

# ---- the virtual environment
if (Test-Path $venv) {
    # Safety: only ever delete a real virtual environment that sits directly in the project folder.
    if ((Split-Path -Parent (Resolve-Path $venv).Path) -ne $root -or -not (Test-Path (Join-Path $venv 'pyvenv.cfg'))) {
        Write-Host "ERROR: $venv does not look like a virtual environment (no pyvenv.cfg); nothing was removed." -ForegroundColor Red
        exit 1
    }
    $running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($venv, [StringComparison]::OrdinalIgnoreCase) })
    if ($running) {
        Write-Host 'These programs run from the virtual environment and keep its files open:' -ForegroundColor Yellow
        $running | ForEach-Object { Write-Host ("  {0}  (PID {1})  {2}" -f $_.ProcessName, $_.Id, $_.MainWindowTitle) }
        if ($WhatIfPreference) {
            Write-Host 'WhatIf: they would be stopped.'
        } elseif ($Force -or $PSCmdlet.ShouldContinue('Stop them and continue?', 'Virtual environment in use')) {
            $running | Stop-Process -Force
            Start-Sleep -Milliseconds 700
        } else {
            Write-Host 'Nothing was removed: close them first, or run again with -Force.'
            exit 1
        }
    }
    $size = Get-FolderSize $venv
    Step "Removing the virtual environment $venv ($size)"
    if ($PSCmdlet.ShouldProcess($venv, 'Remove the virtual environment')) {
        try {
            Remove-Item -LiteralPath $venv -Recurse -Force
        } catch {
            Write-Host "ERROR: could not remove everything: $($_.Exception.Message)" -ForegroundColor Red
            Write-Host 'A program may still be using files in it (an editor, a terminal with the environment activated). Close it and try again.'
            exit 1
        }
        Write-Host '  removed.'
    }
} else {
    Write-Host "No virtual environment at $venv - nothing to remove."
}

# ---- caches
if ($Caches) {
    Step 'Removing Python caches and build folders'
    $targets = Get-ChildItem $root -Recurse -Force -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notlike "$venv*" -and ($_.Name -in '__pycache__', '.pytest_cache', 'build', 'dist' -or $_.Name -like '*.egg-info') }
    foreach ($t in $targets) {
        if ($PSCmdlet.ShouldProcess($t.FullName, 'Remove')) { Remove-Item -LiteralPath $t.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    }
    Write-Host ("  {0} folder(s)." -f @($targets).Count)
}

# ---- settings and log kept outside the project
if ($Settings) {
    Step 'Removing the saved scope_gui settings and log'
    $key = 'HKCU:\Software\UNI-T-Scope'
    if ((Test-Path $key) -and $PSCmdlet.ShouldProcess($key, 'Remove the registry key')) { Remove-Item $key -Recurse -Force }
    $logDir = Join-Path $env:LOCALAPPDATA 'UNI-T-Scope'
    if ((Test-Path $logDir) -and $PSCmdlet.ShouldProcess($logDir, 'Remove the folder')) { Remove-Item $logDir -Recurse -Force }
    Write-Host '  done.'
}

Write-Host ''
Write-Host 'Rebuild the environment with:  scripts\install.ps1' -ForegroundColor Green
