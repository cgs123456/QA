param(
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
& $Python (Join-Path $root "sidecar/build.py")
