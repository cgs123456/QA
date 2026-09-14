"""PRD §3.13 打包后四条校验（脚本化）。

用法（仓库根）：
    python scripts/verify-packaged.py [--exe PATH] [--cwd DIR]

1. bundle 内 vendor 文件存在（逐个点名；防 PyInstaller datas 静默空目录）。
2. 从任意 CWD 启动打包产物 → 60s 内读到合法握手 JSON
   （证明 _MEIPASS 绝对锚定 + jieba_dict 成功 + uvicorn 已绑定后再打印）。
3. 本进程 chdir(cwd) 后：load 打包产物内 libsimple（绝对路径）+
   jieba_dict（绝对路径）→ 建表 → 插入中文 → jieba_query/simple_query
   命中 → rebuild（证明打包件与 CWD 无关）。
4. handshake 端口 GET /health → 200；计时 spawn→握手 / spawn→health。

R8：握手 auth_token 仅内存比对，打印时打码。
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LIB_NAMES = {"win32": "libsimple.dll", "darwin": "libsimple.dylib"}
DICT_FILES = [
    "jieba.dict.utf8", "hmm_model.utf8", "user.dict.utf8",
    "idf.utf8", "stop_words.utf8",
]
SAMPLE_ZH = "你的发货周期是多久"


def default_exe() -> Path:
    d = ROOT / "dist" / "interviewcopilot-sidecar"
    name = "interviewcopilot-sidecar" + (".exe" if sys.platform == "win32" else "")
    return d / name


def bundle_vendor_dir(exe: Path) -> Path:
    internal = exe.parent / "_internal" / "vendor"
    if internal.is_dir():
        return internal
    fallback = exe.parent / "vendor"
    return fallback


def fail(msg: str, proc=None) -> None:
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            _, err = proc.communicate(timeout=5)
            if err:
                print("--- exe stderr tail ---")
                print(err.decode("utf-8", "replace")[-2000:])
        except Exception:
            pass
    print(f"PACKAGED_CHECK_FAIL: {msg}", flush=True)
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=str(default_exe()))
    ap.add_argument(
        "--cwd",
        default=r"C:\Windows\System32" if sys.platform == "win32" else "/tmp",
    )
    args = ap.parse_args()
    exe = Path(args.exe)
    cwd = args.cwd

    if not exe.is_file():
        fail(f"打包产物不存在：{exe}（先跑 sidecar/build.py）")

    # —— 第 1 条：bundle 内 vendor 文件存在 ———————————————
    vdir = bundle_vendor_dir(exe)
    lib = vdir / LIB_NAMES.get(sys.platform, "libsimple.so")
    missing = []
    if not lib.is_file():
        missing.append(str(lib))
    for name in DICT_FILES:
        if not (vdir / "dict" / name).is_file():
            missing.append(f"dict/{name}")
    if missing:
        fail("bundle vendor 缺失（疑似 datas 路径写错导致空目录）：" + ", ".join(missing))
    print(f"CHECK1_VENDOR_FILES_OK vendor={vdir} lib_bytes={lib.stat().st_size}")

    # —— 第 2 条：任意 CWD 启动 → 握手 ————————————————————
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [str(exe)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, cwd=cwd,
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(proc.stdout.readline)
        try:
            line = future.result(timeout=60)
        except concurrent.futures.TimeoutError:
            fail(f"60s 内未读到握手行（cwd={cwd}）", proc)
    if not line.strip():
        fail("握手行为空（绑定失败必须非零退出，见 stderr）", proc)
    try:
        hs = json.loads(line)
        assert hs["protocol_version"] == "1.0"
        port = int(hs["port"])
        assert isinstance(hs.get("auth_token"), str) and len(hs["auth_token"]) >= 32
    except Exception as e:
        fail(f"握手 JSON 非法：{e}", proc)
    t_handshake = time.perf_counter() - t0
    redacted = {k: ("<redacted>" if k == "auth_token" else v) for k, v in hs.items()}
    print(f"CHECK2_HANDSHAKE_OK cwd={cwd} handshake={json.dumps(redacted)}")

    try:
        # —— 第 4 条：/health → 200（计时 spawn→可用） —————————
        t_health = None
        last = None
        while time.perf_counter() - t0 < 90:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=2
                ) as r:
                    body = r.read().decode()
                    if r.status == 200:
                        t_health = time.perf_counter() - t0
                        print(f"CHECK4_HEALTH_OK status=200 body={body}")
                        break
                    last = f"status={r.status}"
            except Exception as e:
                last = repr(e)
                time.sleep(0.2)
        if t_health is None:
            fail(f"/health 不可用：{last}", proc)

        # —— 第 3 条：打包件内扩展 CWD 无关（本进程 chdir 后绝对路径加载）——
        prev_cwd = os.getcwd()
        os.chdir(cwd)
        try:
            c = sqlite3.connect(":memory:")
            c.enable_load_extension(True)
            c.load_extension(str(lib))
            c.execute("SELECT jieba_dict(?)", (str(vdir / "dict"),))
            c.execute("CREATE VIRTUAL TABLE t USING fts5(content, tokenize='simple')")
            c.execute("INSERT INTO t(content) VALUES (?)", (SAMPLE_ZH,))
            c.commit()
            r1 = c.execute(
                "SELECT content FROM t WHERE t MATCH jieba_query(?)", ("发货周期",)
            ).fetchall()
            r2 = c.execute(
                "SELECT content FROM t WHERE t MATCH simple_query(?)", ("fahuo",)
            ).fetchall()
            assert any(SAMPLE_ZH in r[0] for r in r1), f"jieba_query 未命中：{r1}"
            assert any(SAMPLE_ZH in r[0] for r in r2), f"simple_query 未命中：{r2}"
            c.execute("INSERT INTO t(t) VALUES('rebuild')")
            r3 = c.execute(
                "SELECT content FROM t WHERE t MATCH simple_query(?)", ("fahuo",)
            ).fetchall()
            assert r3, "rebuild 后无结果"
            c.close()
        finally:
            os.chdir(prev_cwd)
        print(f"CHECK3_BUNDLED_MATCH_REBUILD_OK cwd={cwd}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    # —— 体积与耗时（供 benchmark.md） —————————————————————
    total = 0
    biggest = []
    for p in (exe.parent).rglob("*"):
        if p.is_file():
            s = p.stat().st_size
            total += s
            biggest.append((s, str(p.relative_to(exe.parent))))
    biggest.sort(reverse=True)
    print(f"BUNDLE_BYTES={total} ({total / 1048576:.1f} MiB)")
    for s, rel in biggest[:5]:
        print(f"  TOP {s / 1048576:.2f} MiB  {rel}")
    print(f"T_SPAWN_TO_HANDSHAKE={t_handshake:.2f}s")
    print(f"T_SPAWN_TO_HEALTH={t_health:.2f}s")
    print("PACKAGED_CHECKS_ALL_PASS")


if __name__ == "__main__":
    main()
