$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $RepoRoot

try {
    $VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

    if (Test-Path $VenvPython -PathType Leaf) {
        $PythonCommand = $VenvPython
        $PythonPrefix = @()
    }
    elseif (Get-Command uv -ErrorAction SilentlyContinue) {
        $PythonCommand = "uv"
        $PythonPrefix = @("run", "--no-sync", "python")
    }
    elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $PythonCommand = "py"
        $PythonPrefix = @("-3.12")
    }
    elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $PythonCommand = "python"
        $PythonPrefix = @()
    }
    else {
        Write-Error "Python 3.12 or uv is required. Install the declared dev dependencies first."
        exit 127
    }

    & $PythonCommand @PythonPrefix -c "import sys; raise SystemExit(sys.version_info < (3, 12))"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Python 3.12 or newer is required."
        exit 2
    }

    & $PythonCommand @PythonPrefix -m pytest
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $PythonCommand @PythonPrefix -m ruff check .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $PythonCommand @PythonPrefix -m ruff format --check .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Pop-Location
}
