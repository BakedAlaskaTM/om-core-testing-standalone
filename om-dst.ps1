#!/usr/bin/env pwsh
# om-dst: deterministic simulation testing CLI for OM Core.
# Thin wrapper around `python -m lib_dst`; works from any working directory and
# forwards the CLI exit code.
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = Join-Path $Root ".venv/bin/python" }
if (-not (Test-Path $Python)) { $Python = "python" }
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$Root$([IO.Path]::PathSeparator)$($env:PYTHONPATH)"
} else {
    $env:PYTHONPATH = $Root
}
& $Python -m lib_dst @args
exit $LASTEXITCODE
