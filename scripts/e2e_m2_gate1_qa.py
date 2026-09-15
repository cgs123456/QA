"""task20 门槛1 补充：**固定答案路径的真实 QA 端到端时延**（本机 vendor 就位）。

为什么需要这个脚本：远端那台机器缺 `sidecar/vendor/libsimple.dll`（GitHub 不通），
QA 链跑不起来，于是 `e2e_m2_gate1.py` 的「问答直接路径」只能**引用** acceptance-m1 #9
的 2.4ms 常量。本机 vendor 就位，可以把这笔账从「引用」升级为「实测」。

口径（与门槛1 一致，不新造口径）：
- 链路：`POST /qa/ask`（问题入）→ `GET /qa/stream`（SSE）→ 收到 `done`（答案出）。
  这就是门槛1 分解式里的「问答」那一项；ASR 那一项由 `e2e_m2_gate1.py` 单独实测。
- 问题用**门槛原话**「发货周期是多久」（`sidecar/tests/eval/seed_demo.json` 里有
  对应官方答案 → 判定为 `direct` 档、**零 LLM 调用**，正是「固定答案」路径）。
- 真实 sidecar 子进程（`sidecar/src/main.py`）+ 临时 DB（不污染开发库）+ 真实 HTTP/SSE。
- 代理规避：本机 `http_proxy` 会把 127.0.0.1 也拦成 502，故显式禁用代理，
  不依赖调用者设 `NO_PROXY`（比环境变量可靠）。

输出：stdout 表格 + `.workbuddy-ai/gate1_qa_result.json`。
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / ".workbuddy-ai" / "gate1_qa_result.json"
SEED = ROOT / "sidecar" / "tests" / "eval" / "seed_demo.json"
GATE_QUESTION = "发货周期是多久"

# 显式禁用代理：本机 http_proxy 会把 127.0.0.1 也拦成 502。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(method: str, url: str, token: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with _OPENER.open(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else None


def _read_handshake(proc: subprocess.Popen) -> dict:
    deadline = time.time() + 60
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(f"sidecar 提前退出，rc={proc.returncode}")
            continue
        text = line.decode("utf-8", "replace").strip()
        if text.startswith("{"):
            return json.loads(text)
    raise RuntimeError("60s 内未读到握手行")


def ask_once(base: str, token: str, store_id: str, question: str) -> float:
    """一次「问题入 → done 出」的毫秒数（真实 HTTP + SSE）。"""
    t0 = time.perf_counter()
    task = _http("POST", f"{base}/qa/ask", token,
                 {"question": question, "store_id": store_id})
    task_id = task["task_id"]
    req = urllib.request.Request(f"{base}/qa/stream?task_id={task_id}")
    req.add_header("Authorization", f"Bearer {token}")
    with _OPENER.open(req, timeout=30) as resp:
        buf = ""
        for chunk in resp:
            buf += chunk.decode("utf-8", "replace")
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                for line in frame.split("\n"):
                    if not line.startswith("data:"):
                        continue
                    obj = json.loads(line[5:].strip())
                    if obj.get("type") == "done":
                        return (time.perf_counter() - t0) * 1000.0
    raise RuntimeError("SSE 流结束仍未收到 done 事件")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=10)
    ap.add_argument("--question", default=GATE_QUESTION)
    args = ap.parse_args()

    python = sys.executable
    tmpdir = tempfile.mkdtemp(prefix="gate1-qa-")
    db_path = os.path.join(tmpdir, "gate1qa.db")
    env = {**os.environ, "INTERVIEWCOPILOT_DB": db_path}
    env.pop("http_proxy", None)
    env.pop("HTTP_PROXY", None)
    env.pop("https_proxy", None)
    env.pop("HTTPS_PROXY", None)

    proc = subprocess.Popen(
        [python, str(ROOT / "sidecar" / "src" / "main.py")],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    try:
        hs = _read_handshake(proc)
        base = f"http://127.0.0.1:{hs['port']}"
        token = hs["auth_token"]

        store = _http("POST", f"{base}/store", token, {"name": "gate1-qa"})
        store_id = store["id"]
        seed_text = SEED.read_text(encoding="utf-8")
        compiled = _http("POST", f"{base}/knowledge/compile", token,
                         {"store_id": store_id, "format": "json",
                          "content": seed_text})

        # 预热一次（首次请求含 lazy 初始化，不计入样本）。
        warm = ask_once(base, token, store_id, args.question)

        samples = [ask_once(base, token, store_id, args.question)
                   for _ in range(args.repeat)]
    finally:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass

    result = {
        "question": args.question,
        "qa_inserted": compiled["stats"]["qa_inserted"],
        "n": len(samples),
        "warmup_ms": round(warm, 2),
        "median_ms": round(statistics.median(samples), 2),
        "min_ms": round(min(samples), 2),
        "max_ms": round(max(samples), 2),
        "all_ms": [round(s, 2) for s in samples],
        "path": "real sidecar subprocess + real HTTP/SSE + temp DB (vendor present)",
        "machine": f"{sys.platform} Python {sys.version.split()[0]}",
    }
    result["fixed_answer_pass"] = result["median_ms"] <= 4000.0
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    for k, v in result.items():
        print(f"{k}={v}")
    print(f"GATE1_QA_FIXED_{'PASS' if result['fixed_answer_pass'] else 'FAIL'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
