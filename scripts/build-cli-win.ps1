<#Requires -Version 5.1
<#
.SYNOPSIS
  PyInstaller 单文件打包运维 CLI（P9 F11.6，与主包分离分发）。

  用法（仓库根）：
    powershell -ExecutionPolicy Bypass -File scripts/build-cli-win.ps1 [--distpath DIR] [--workpath DIR]

  体积纪律（DoD）：CLI 单独分发，主包（dist-sidecar/）体积不受影响——
  本脚本只读 sidecar/src 与 vendor，不碰 dist-sidecar/、不改 sidecar/build.py。
  CLI 不需要 onnxruntime/transformers/faster-whisper/sherpa（导出恢复不做向量推理），
  故只显式收集 sqlite_vec（vec0 动态库，静态分析看不到，见 sidecar/build.py 同款理由）；
  其余重型依赖即使被 PyInstaller 静态分析扫到，也应按需加 --exclude（当前不需要）。
#>
param(
    [string]$distpath = "",
    [string]$workpath = ""
)

$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $PSScriptRoot
if ($distpath -eq "") { $distpath = Join-Path $ROOT "dist-cli" }
if ($workpath -eq "") { $workpath = Join-Path $ROOT "build-cli" }
$SIDECAR_SRC = Join-Path $ROOT "sidecar/src"
$VENDOR = Join-Path $ROOT "sidecar/vendor"

$lib = Join-Path $VENDOR "libsimple.dll"
if (-not (Test-Path -LiteralPath $lib)) { Write-Error "vendor 缺失：$lib"; exit 1 }

$py = Join-Path $ROOT ".venv/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $py)) { $py = "python" }

& $py -m PyInstaller `
    --noconfirm --clean `
    --onefile --console `
    --name "interview-copilot-cli" `
    --paths $SIDECAR_SRC `
    --add-data "$VENDOR;vendor" `
    --collect-all "sqlite_vec" `
    --exclude-module "onnxruntime" `
    --exclude-module "transformers" `
    --exclude-module "faster_whisper" `
    --exclude-module "sherpa_onnx" `
    --exclude-module "torch" `
    --exclude-module "uvicorn" `
    --exclude-module "fastapi" `
    --exclude-module "httpx" `
    --exclude-module "websockets" `
    --exclude-module "openpyxl" `
    --exclude-module "pypdf" `
    --distpath $distpath `
    --workpath $workpath `
    --specpath $workpath `
    (Join-Path $ROOT "cli/__main__.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "CLI 单文件已落盘：$distpath"
