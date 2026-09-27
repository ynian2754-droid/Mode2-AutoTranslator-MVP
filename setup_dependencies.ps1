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

if (-not $python) {
    Write-Error "No usable Python 3.10+ installation with pip was found. Install Python 3.10+ with pip, then rerun 安装依赖.bat."
    exit 2
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
Write-Host "All project dependencies are ready."
exit 0
