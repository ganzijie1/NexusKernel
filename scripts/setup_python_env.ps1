[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$Recreate,
    [switch]$Help
)

$ErrorActionPreference = "Stop"

function Show-Usage {
    @"
Usage: scripts/setup_python_env.ps1 [-Python PATH] [-Recreate] [-Help]

Create or reuse a Windows Python 3.11-3.13 virtual environment and install .[ai,dev].

Options:
  -Python PATH  Supported Python interpreter used to create the environment.
  -Recreate     Safely replace the existing .venv-win environment.
  -Help         Show this help message.
"@
}

if ($Help) {
    Show-Usage
    exit 0
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "scripts/setup_python_env.ps1 supports Windows only"
}

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = [IO.Path]::GetFullPath((Join-Path $scriptDirectory ".."))
$versionChecker = Join-Path $scriptDirectory "check_python_version.py"
$environmentDirectory = [IO.Path]::GetFullPath(
    (Join-Path $projectRoot ".venv-win")
)

if (-not (Test-Path -LiteralPath $versionChecker -PathType Leaf)) {
    throw "missing Python version checker: $versionChecker"
}

function Invoke-Python {
    param(
        [string]$Interpreter,
        [string[]]$Arguments
    )

    & $Interpreter @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE"
    }
}

function Test-SupportedPython {
    param([string]$Interpreter)

    & $Interpreter $versionChecker "--quiet"
    return $LASTEXITCODE -eq 0
}

function Get-EnvironmentPython {
    param([string]$EnvironmentPath)

    foreach ($candidate in @(
        (Join-Path $EnvironmentPath "Scripts\python.exe"),
        (Join-Path $EnvironmentPath "bin\python")
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}

function Test-EnvironmentOwnership {
    param(
        [string]$Interpreter,
        [string]$ExpectedEnvironment
    )

    & $Interpreter -c @'
import sys
from pathlib import Path

expected = Path(sys.argv[1]).resolve()
prefix = Path(sys.prefix).resolve()
base_prefix = Path(sys.base_prefix).resolve()
raise SystemExit(0 if prefix == expected and base_prefix != prefix else 1)
'@ $ExpectedEnvironment
    return $LASTEXITCODE -eq 0
}

function Remove-ProjectEnvironment {
    param([string]$Target)

    $safeTarget = [IO.Path]::GetFullPath((Join-Path $projectRoot ".venv-win"))
    $actualTarget = [IO.Path]::GetFullPath($Target)
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($safeTarget, $actualTarget)) {
        throw "refusing to remove unsafe environment path: $actualTarget"
    }
    Remove-Item -LiteralPath $actualTarget -Recurse -Force
}

$createEnvironment = $true
$environmentPython = $null

if (Test-Path -LiteralPath $environmentDirectory) {
    if ($Recreate) {
        if (-not (Test-SupportedPython $Python)) {
            throw "Python >=3.11,<3.14 is required"
        }
        Remove-ProjectEnvironment $environmentDirectory
    }
    else {
        $environmentPython = Get-EnvironmentPython $environmentDirectory
        if (-not $environmentPython) {
            throw "existing environment is invalid; rerun with -Recreate"
        }
        if (-not (Test-EnvironmentOwnership $environmentPython $environmentDirectory)) {
            throw "existing environment is invalid; rerun with -Recreate"
        }
        if (-not (Test-SupportedPython $environmentPython)) {
            throw "existing environment uses unsupported Python; rerun with -Recreate"
        }
        $createEnvironment = $false
    }
}

if ($createEnvironment) {
    if (-not (Test-SupportedPython $Python)) {
        throw "Python >=3.11,<3.14 is required"
    }
    Invoke-Python $Python @("-m", "venv", $environmentDirectory)
    $environmentPython = Get-EnvironmentPython $environmentDirectory
    if (-not $environmentPython) {
        throw "created environment does not contain a Python interpreter"
    }
}

Push-Location $projectRoot
try {
    Invoke-Python $environmentPython @("-m", "pip", "--version")
    Invoke-Python $environmentPython @("-m", "pip", "install", "--editable", ".[ai,dev]")
    Invoke-Python $environmentPython @("-c", "import sys; print(sys.executable)")
}
finally {
    Pop-Location
}
