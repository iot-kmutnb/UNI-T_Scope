<#
.SYNOPSIS
  Create the Python virtual environment (.venv) and install the packages of this project, including PyQt6 for scope_gui.

.DESCRIPTION
  Safe to run again: an existing .venv is reused and only missing or outdated packages are installed.
  Installs everything in requirements.txt (pyvisa, Pillow, PyQt6, ...), then checks that PyQt6 and pyvisa import, and
  looks for the NI-VISA runtime that the scope connection needs (it comes with UNI-T Device Manager v2.9).

.PARAMETER Python
  Python 3.9+ interpreter to build the environment with. Default: the Python launcher (py -3), then python on PATH.

.PARAMETER Recreate
  Delete .venv first (scripts\cleanup.ps1) and build it again from scratch.

.PARAMETER Upgrade
  Upgrade the packages to the newest versions that requirements.txt allows.

.EXAMPLE
  scripts\install.ps1
  scripts\install.ps1 -Recreate
  scripts\install.ps1 -Python "C:\Python312\python.exe"
#>
[CmdletBinding()]
param(
    [string]$Python,
    [switch]$Recreate,
    [switch]$Upgrade
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv'
$venvPy = Join-Path $venv 'Scripts\python.exe'
$requirements = Join-Path $root 'requirements.txt'

function Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "ERROR: $text" -ForegroundColor Red; exit 1 }
function Run-Checked {
    # Run a native command and stop with a clear message if it fails.
    param([string]$What, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) { Fail "$What failed (exit code $LASTEXITCODE)." }
}

function Find-Python {
    if ($Python) { return $Python }
    foreach ($candidate in @(@('py', '-3'), @('python'))) {
        $exe = $candidate[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $extra = @($candidate | Select-Object -Skip 1)
        $path = & $exe @extra -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $path) { return ($path | Select-Object -First 1).Trim() }
    }
    return $null
}

if (-not (Test-Path $requirements)) { Fail "requirements.txt not found in $root" }

if ($Recreate -and (Test-Path $venv)) {
    Step 'Removing the existing virtual environment'
    & (Join-Path $PSScriptRoot 'cleanup.ps1') -Force
}

$reused = Test-Path $venvPy
if (-not $reused) {
    $py = Find-Python
    if (-not $py) { Fail 'Python 3.9+ was not found. Install it from python.org (tick "Add python.exe to PATH") or pass -Python <path>.' }
    $version = (& $py -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
    $major, $minor = $version.Split('.') | ForEach-Object { [int]$_ }
    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 9)) { Fail "Python $version found at $py; 3.9 or newer is required." }
    Step "Creating the virtual environment in $venv (Python $version, $py)"
    Run-Checked 'Creating the virtual environment' { & $py -m venv $venv }
} else {
    Step "Using the existing virtual environment in $venv"
}

Step 'Updating pip'
Run-Checked 'Updating pip' { & $venvPy -m pip install --quiet --upgrade pip }

Step 'Installing the packages from requirements.txt (this includes PyQt6, about 80 MB the first time)'
$pipArgs = @('-m', 'pip', 'install', '-r', $requirements)
if ($Upgrade) { $pipArgs += '--upgrade' }
if ($reused) { $pipArgs += '--quiet' }                   # the long 'already satisfied' list is just noise
Run-Checked 'Installing the packages' { & $venvPy @pipArgs }

Step 'Checking the installation'
$check = @'
import sys
import PyQt6.QtCore as qc
import PyQt6.QtWidgets
import pyvisa, PIL
print("  Python  ", sys.version.split()[0])
print("  PyQt6   ", qc.PYQT_VERSION_STR, "(Qt", qc.QT_VERSION_STR + ")")
print("  pyvisa  ", pyvisa.__version__)
print("  Pillow  ", PIL.__version__)
'@
Run-Checked 'The import check' { $check | & $venvPy - }       # the script goes in on stdin: no quoting problems

$visaKey = 'HKLM:\SOFTWARE\National Instruments\NI-VISA'
$visa = (Get-ItemProperty $visaKey -ErrorAction SilentlyContinue).CurrentVersion
if ($visa) {
    Write-Host "  NI-VISA  $visa (found)"
} else {
    Write-Host '  NI-VISA runtime not found. The scope connection needs it: install UNI-T Device Manager v2.9 (it bundles' -ForegroundColor Yellow
    Write-Host '  the NI-VISA 15.5 Runtime) or the NI-VISA runtime on its own, then run this script again to re-check.' -ForegroundColor Yellow
}

Write-Host ''
Write-Host 'Done. Start the panel with:   .venv\Scripts\python -m scope_gui' -ForegroundColor Green
Write-Host 'Run the tests with:           .venv\Scripts\python -m unittest discover -s tests'
Write-Host 'Remove the environment with:  scripts\cleanup.ps1'
