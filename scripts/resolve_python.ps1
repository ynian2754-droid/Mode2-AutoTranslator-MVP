# Shared Python resolution for Mode2 AutoTranslator launchers.
#
# Both run.ps1 (start) and setup_dependencies.ps1 (install) dot-source this
# file so they can never disagree about which interpreter the project uses.
#
# Selection rule:
#   1. $env:MODE2_PYTHON wins when it is usable (it is an explicit override).
#   2. Otherwise scan the candidate list and prefer the first interpreter that
#      already has every project dependency available.
#   3. If none is complete, fall back to the first interpreter that meets the
#      base requirement (Python >= 3.10 with pip) so the app can still start.
#   4. If none meets the base requirement, return $null and let the caller
#      bootstrap a runtime.
#
# The probe uses importlib.util.find_spec, so it never executes project code.

$script:Mode2RepoRoot = Split-Path -Parent $PSScriptRoot
$script:Mode2RequiredModules = @('fastapi', 'uvicorn', 'pypdf', 'multipart', 'reportlab', 'docx')

$script:Mode2ProbeCode = @'
import importlib.util
import sys

if sys.version_info < (3, 10):
    raise SystemExit(3)
try:
    import pip  # noqa: F401
except Exception:
    raise SystemExit(3)

names = ['fastapi', 'uvicorn', 'pypdf', 'multipart', 'reportlab', 'docx']
missing = [name for name in names if importlib.util.find_spec(name) is None]
print(','.join(missing))
'@

function Get-Mode2RequiredModules {
    return $script:Mode2RequiredModules
}

function Add-Mode2PythonCandidate {
    param(
        [System.Collections.Generic.List[string]]$List,
        [System.Collections.Generic.HashSet[string]]$Seen,
        [string]$Candidate
    )
    if ([string]::IsNullOrWhiteSpace($Candidate)) {
        return
    }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return
    }
    $resolved = (Resolve-Path -LiteralPath $Candidate).Path
    if ($Seen.Add($resolved)) {
        $List.Add($resolved)
    }
}

# Returns the ordered candidate interpreters.  Project-local environments come
# before machine-wide ones; PATH `python` is consulted last.
function Get-Mode2PythonCandidates {
    $list = New-Object System.Collections.Generic.List[string]
    # Windows paths are case-insensitive: `miniconda3` and `Miniconda3` are the
    # same interpreter and must not be probed twice.
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $repo = $script:Mode2RepoRoot

    Add-Mode2PythonCandidate $list $seen $env:MODE2_PYTHON
    Add-Mode2PythonCandidate $list $seen (Join-Path $repo ".venv\Scripts\python.exe")
    Add-Mode2PythonCandidate $list $seen (Join-Path $repo "miniconda3\python.exe")
    Add-Mode2PythonCandidate $list $seen (Join-Path $repo "runtime\python.exe")
    if ($env:CONDA_PREFIX) {
        Add-Mode2PythonCandidate $list $seen (Join-Path $env:CONDA_PREFIX "python.exe")
    }
    if ($env:USERPROFILE) {
        Add-Mode2PythonCandidate $list $seen (Join-Path $env:USERPROFILE "miniconda3\python.exe")
        Add-Mode2PythonCandidate $list $seen (Join-Path $env:USERPROFILE "Miniconda3\python.exe")
    }
    if ($env:LOCALAPPDATA) {
        Add-Mode2PythonCandidate $list $seen (Join-Path $env:LOCALAPPDATA "miniconda3\python.exe")
        Add-Mode2PythonCandidate $list $seen (Join-Path $env:LOCALAPPDATA "Programs\miniconda3\python.exe")
        Add-Mode2PythonCandidate $list $seen (Join-Path $env:LOCALAPPDATA "Mode2AutoTranslator\Python313\python.exe")
    }
    if ($env:ProgramData) {
        Add-Mode2PythonCandidate $list $seen (Join-Path $env:ProgramData "miniconda3\python.exe")
    }
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand -and $pythonCommand.Source) {
        Add-Mode2PythonCandidate $list $seen $pythonCommand.Source
    }
    $pyCommand = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyCommand) {
        $pyPath = & $pyCommand.Source -3 -c "import sys; print(sys.executable)" 2>$null
        Add-Mode2PythonCandidate $list $seen ($pyPath | Select-Object -First 1)
    }
    return $list
}

# Returns $null when the interpreter is unusable (too old or without pip),
# an empty string when every required module is importable, otherwise a
# comma-joined list of the missing import names.
function Get-Mode2PythonMissing {
    param([string]$Python)
    if ([string]::IsNullOrWhiteSpace($Python)) {
        return $null
    }
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        return $null
    }
    $output = & $Python -X utf8 -B -c $script:Mode2ProbeCode 2>$null
    if ($LASTEXITCODE -ne 0) {
        return $null
    }
    $missing = (@($output) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) -join ','
    return $missing
}

function Test-Mode2PythonUsable {
    param([string]$Python)
    return ($null -ne (Get-Mode2PythonMissing $Python))
}

# Returns a PSCustomObject with Path / Missing / Complete / Explicit, or $null
# when no candidate can run the app at all.
function Resolve-Mode2Python {
    if ($env:MODE2_PYTHON) {
        $overrideMissing = Get-Mode2PythonMissing $env:MODE2_PYTHON
        if ($null -ne $overrideMissing) {
            $overridePath = (Resolve-Path -LiteralPath $env:MODE2_PYTHON).Path
            return [pscustomobject]@{
                Path = $overridePath
                Missing = @($overrideMissing -split ',' | Where-Object { $_ })
                Complete = [string]::IsNullOrEmpty($overrideMissing)
                Explicit = $true
            }
        }
    }

    $fallback = $null
    foreach ($candidate in Get-Mode2PythonCandidates) {
        $missing = Get-Mode2PythonMissing $candidate
        if ($null -eq $missing) {
            continue
        }
        if ([string]::IsNullOrEmpty($missing)) {
            return [pscustomobject]@{ Path = $candidate; Missing = @(); Complete = $true; Explicit = $false }
        }
        if ($null -eq $fallback) {
            $fallback = [pscustomobject]@{
                Path = $candidate
                Missing = @($missing -split ',' | Where-Object { $_ })
                Complete = $false
                Explicit = $false
            }
        }
    }
    return $fallback
}
