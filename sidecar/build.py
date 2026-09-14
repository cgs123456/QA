"""PyInstaller onedir 打包 sidecar。

用法（仓库根）：
    python sidecar/build.py [--distpath DIR] [--workpath DIR]

- datas 收 vendor/ 整目录（libsimple + dict）；datas 路径写错时 PyInstaller
  会静默打包空目录 → scripts/verify-packaged.py 第 1 条校验显式点名缺失文件兜底。
- sqlite_vec 用 --collect-all 兜底（vec0 动态库若是包数据而非导入模块，
  常规分析会漏掉；verify 第 3 步证明它真的在包里）。
- websockets 同样用 --collect-all 兜底，理由更硬：uvicorn 在 `ws="auto"` 时
  **动态** import 它（uvicorn/config.py 里的 import_from_string），静态分析看不到。
  漏掉的后果不是"降级"而是 **WS 全挂**：uvicorn 对 /audio/stream 的升级请求直接
  返回 404，Rust 客户端永远连不上（task-14 实测：装 websockets 前三个用例全 404）。
- ctranslate2 / av（faster-whisper 的运行时）：ctranslate2 带原生 DLL（加载期按名字
  找库，静态分析看不到），av 是 `faster_whisper/audio.py` 的**顶层** import
  （`__init__.py` 一进来就要它，不是可选解码路径）。两者都用 --collect-all 兜底；
  漏掉的后果同样是"装得上但跑不了"，且只在真实转写时才暴露。
- 保留控制台（stdout 握手行是 R2 契约）。
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIDECAR_SRC = ROOT / "sidecar" / "src"
VENDOR = ROOT / "sidecar" / "vendor"


def main() -> None:
    ap = argparse.ArgumentParser()
    # 注意：前端 vite 产物占用 dist/（且构建会清空它），sidecar 独占 dist-sidecar/。
    ap.add_argument("--distpath", default=str(ROOT / "dist-sidecar"))
    ap.add_argument("--workpath", default=str(ROOT / "build-sidecar"))
    args = ap.parse_args()

    lib_names = {
        "win32": "libsimple.dll",
        "darwin": "libsimple.dylib",
    }
    lib_file = VENDOR / lib_names.get(sys.platform, "libsimple.so")
    for required in [lib_file, VENDOR / "dict" / "jieba.dict.utf8"]:
        if not required.is_file():
            print(f"vendor 缺失，打包前先放文件：{required}", flush=True)
            sys.exit(1)

    sep = ";" if os.name == "nt" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir", "--console",
        "--name", "interviewcopilot-sidecar",
        # 显式 --paths：实测 PyInstaller 6 不把脚本所在 sidecar/src 自动记入
        # pathex（Analysis-00.toc 实证），缺了它 sibling 模块（app/core/database）
        # 会被静默漏掉，打包产物启动即 ModuleNotFoundError。
        "--paths", str(SIDECAR_SRC),
        "--add-data", f"{VENDOR}{sep}vendor",
        "--collect-all", "sqlite_vec",
        # uvicorn 动态 import WS 实现，静态分析看不到；漏了则 /audio/stream 404。
        "--collect-all", "websockets",
        # faster-whisper 运行时：ctranslate2 的原生 DLL 按名字加载，
        # av 是 faster_whisper.audio 的顶层 import——都收不到就"装得上跑不了"。
        "--collect-all", "ctranslate2",
        "--collect-all", "av",
        # task17: sherpa_onnx ships a native extension loaded dynamically
        # (same "runs-but-fails" class as ctranslate2 above).
        "--collect-all", "sherpa_onnx",
        "--distpath", args.distpath,
        "--workpath", args.workpath,
        "--specpath", args.workpath,
        str(SIDECAR_SRC / "main.py"),
    ]
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(ROOT), check=True)


if __name__ == "__main__":
    main()
