param(
    [int]$Port = 4873,
    [string]$BindHost = "127.0.0.1"
)

Set-Location $PSScriptRoot

# Pick the interpreter the same way setup_dependencies.ps1 does.  The shared
# resolver prefers an interpreter that already has every project dependency, so
# a machine with several Python installations (for example Miniconda + a
# python.org install) no longer starts the app on the wrong one.
. (Join-Path $PSScriptRoot "scripts\resolve_python.ps1")

$resolved = Resolve-Mode2Python
if (-not $resolved) {
    Write-Error "No usable Python 3.10+ with pip was found. Run setup_dependencies.ps1 first."
    exit 2
}

$python = $resolved.Path
if ($resolved.Explicit) {
    Write-Host ("Using Python from MODE2_PYTHON: {0}" -f $python)
} else {
    Write-Host ("Using Python: {0}" -f $python)
}
if (-not $resolved.Complete) {
    Write-Warning ("The selected Python is missing project modules: {0}" -f ($resolved.Missing -join ", "))
    Write-Warning ("Some features (for example PDF export) will fail until they are installed into: {0}" -f $python)
    Write-Warning "Run setup_dependencies.ps1 to install them."
}

Write-Host ("Starting Mode2 AutoTranslator at http://{0}:{1}/" -f $BindHost, $Port)
& $python -m uvicorn app:app --host $BindHost --port $Port
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
