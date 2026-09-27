Set-Location $PSScriptRoot

$requirements = Join-Path $PSScriptRoot "requirements.txt"
if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
    Write-Error ("requirements.txt was not found at {0}." -f $requirements)
    exit 2
}

# Resolve one usable Python first.  A Python distribution is the bootstrap
# prerequisite; the project packages are installed below from requirements.txt.
# The same shared resolver is used by run.ps1, so the interpreter this script
# installs into is exactly the one the launcher starts.
. (Join-Path $PSScriptRoot "scripts\resolve_python.ps1")

if ($env:MODE2_PYTHON -and -not (Test-Path -LiteralPath $env:MODE2_PYTHON -PathType Leaf)) {
    Write-Error ("MODE2_PYTHON does not point to a file: {0}" -f $env:MODE2_PYTHON)
    exit 2
}

$resolved = Resolve-Mode2Python
$python = $null
if ($resolved) {
    $python = $resolved.Path
    if ($resolved.Complete) {
        Write-Host ("Selected Python (all project modules present): {0}" -f $python)
    } else {
        Write-Host ("Selected Python (missing: {0}): {1}" -f ($resolved.Missing -join ", "), $python)
    }
}

$bootstrappedRuntime = $false
if (-not $python) {
    # The project carries a CPython installer so a clean Windows machine does
    # not need Miniconda specifically.  Install it per-user, without changing
    # the system PATH, then make a project-local venv for the existing launcher.
    $pythonInstaller = Join-Path $PSScriptRoot "python-3.13.15-amd64.exe"
    if (-not (Test-Path -LiteralPath $pythonInstaller -PathType Leaf)) {
        Write-Error "No usable Python 3.10+ installation with pip was found, and the bundled CPython installer is missing."
        exit 2
    }

    if ($env:LOCALAPPDATA) {
        $runtimeRoot = Join-Path $env:LOCALAPPDATA "Mode2AutoTranslator\Python313"
    } else {
        $runtimeRoot = Join-Path $PSScriptRoot "python-runtime"
    }
    $runtimeParent = Split-Path -Parent $runtimeRoot
    New-Item -ItemType Directory -Force -Path $runtimeParent | Out-Null

    Write-Host ("No usable Python found. Installing the bundled CPython runtime to: {0}" -f $runtimeRoot)
    $installerArguments = @(
        "/quiet",
        "InstallAllUsers=0",
        ("TargetDir={0}" -f $runtimeRoot),
        "Include_pip=1",
        "Include_test=0",
        "Include_launcher=0",
        "PrependPath=0"
    )
    & $pythonInstaller @installerArguments
    $installerExitCode = $LASTEXITCODE
    if ($installerExitCode -ne 0) {
        Write-Error ("CPython installation failed with exit code {0}." -f $installerExitCode)
        exit $installerExitCode
    }

    $python = Join-Path $runtimeRoot "python.exe"
    if (-not (Test-Mode2PythonUsable $python)) {
        Write-Error ("The CPython installer completed, but the new interpreter is not usable: {0}" -f $python)
        exit 2
    }
    $bootstrappedRuntime = $true
}

# The original run.ps1 already prefers .venv.  When this script had to install
# Python from scratch, create that venv so a later double-click uses the same
# interpreter without changing PATH or run.ps1.
if ($bootstrappedRuntime) {
    $venvRoot = Join-Path $PSScriptRoot ".venv"
    $venvPython = Join-Path $venvRoot "Scripts\python.exe"
    if ((Test-Path -LiteralPath $venvRoot) -and (-not (Test-Path -LiteralPath $venvPython))) {
        Write-Error ("The project .venv directory exists but is incomplete: {0}" -f $venvRoot)
        exit 2
    }
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Host ("Creating project virtual environment: {0}" -f $venvRoot)
        & $python -X utf8 -B -m venv $venvRoot
        if ($LASTEXITCODE -ne 0) {
            Write-Error ("Creating the project virtual environment failed with exit code {0}." -f $LASTEXITCODE)
            exit $LASTEXITCODE
        }
    }
    if (-not (Test-Mode2PythonUsable $venvPython)) {
        Write-Error ("The project virtual environment is not usable: {0}" -f $venvPython)
        exit 2
    }
    $python = (Resolve-Path -LiteralPath $venvPython).Path
}

$pythonDirectory = Split-Path -Parent $python
$env:Path = "$pythonDirectory;$env:Path"
Write-Host ("Using Python: {0}" -f $python)

# The required import names live in scripts/resolve_python.ps1 so this script,
# run.ps1 and the launcher all agree on what "dependency present" means.  A
# package folder in the project would also be visible to find_spec and would
# therefore not be installed again; this project currently vendors none of
# these modules.
$missingModules = @()
$missingRaw = Get-Mode2PythonMissing $python
if ($null -eq $missingRaw) {
    $missingModules = @('__probe_failed__')
} elseif (-not [string]::IsNullOrEmpty($missingRaw)) {
    $missingModules = @($missingRaw -split ',')
}

if ($missingModules.Count -eq 0) {
    Write-Host "All project dependencies are already available; no installation is needed."
} else {
    Write-Host ("Missing dependencies: {0}" -f ($missingModules -join ", "))
    $isVirtualEnv = & $python -X utf8 -B -c "import sys; print(int(sys.prefix != sys.base_prefix))" 2>$null
    $pipArguments = @("-m", "pip", "install", "--disable-pip-version-check")
    if ("$isVirtualEnv".Trim() -ne "1") {
        $pipArguments += "--user"
    }
    $pipArguments += @("-r", $requirements)
    Write-Host ("Installing all dependencies from: {0}" -f $requirements)
    & $python @pipArguments
    if ($LASTEXITCODE -ne 0) {
        Write-Error ("Dependency installation failed with exit code {0}." -f $LASTEXITCODE)
        exit $LASTEXITCODE
    }
}

$verifyMissing = Get-Mode2PythonMissing $python
if ($null -eq $verifyMissing) {
    Write-Error "Dependency verification failed: the selected Python is not usable."
    exit 1
}
if (-not [string]::IsNullOrEmpty($verifyMissing)) {
    Write-Error ("Dependency verification failed; still missing: {0}" -f $verifyMissing)
    exit 1
}
Write-Host "Dependency verification passed."

$reportLabVersion = & $python -X utf8 -B -c "import reportlab; print(reportlab.Version)"
Write-Host ("ReportLab: {0}" -f $reportLabVersion)
if ($bootstrappedRuntime) {
    Write-Host ("Project runtime ready. Future launches will use: {0}" -f $python)
}
Write-Host "All project dependencies are ready."
exit 0
