# InterviewCopilot（QA）— 项目长期约定

## 跑测试的环境配方（每次都要，漏了就假失败）

```bash
export PATH="/c/Users/Administrator/.cargo/bin:$PATH"          # Rust 不在 PATH 上
export NO_PROXY="127.0.0.1,localhost"                          # 本机有系统级 http_proxy(127.0.0.1:7897)，会把本机端口拦成 502
export INTERVIEWCOPILOT_PYTHON="C:/Users/Administrator/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
export CARGO_INCREMENTAL=0                                      # 见下：增量缓存会诱发 rustc ICE
```

- **`CARGO_INCREMENTAL=0` 是必需的**（2026-09-15 实测）：`target/debug/incremental` 损坏后
  rustc 1.98.1 会稳定报 `the compiler unexpectedly panicked. This is a bug`（ICE，exit 101），
  且**重跑必现**、与代码无关（同一棵树在损坏前刚编过并通过 124 条）。
  关掉增量即恢复。**不要去 `rm -rf target/debug/incremental`**——本机有批量删除守卫
  （~50 文件/回合），改环境变量更快更安全。

- **`cargo test` 里凡是会拉起真实 sidecar 的集成测试**（`audio_ws_e2e`、`inject_e2e`）
  **必须**有后两个变量，否则报错/超时，**与代码无关**。缺了它们时 `audio_ws_e2e` 3 条全红。
- 隔离 venv：`~/.workbuddy-ai/binaries/python/envs/default`（不是 versions/3.13.12）。
  该 venv 需按 `sidecar/requirements-lock.txt` 装齐：`websockets`（缺了 uvicorn `ws="auto"`
  对 `/audio/stream` 直接 **404**，不是 403）、`openpyxl`、`pypdf`、`pytest`、`transformers`。
- **`cargo test` 的失败要区分「代码错」与「环境缺」**：先看是不是 ModuleNotFoundError /
  连不上 sidecar，再怀疑代码。
- **偶发 `SIGTERM`（exit 1，无输出）**：`cargo test` 全量、`git push`、`tasklist` 都遇到过，
  同一条命令**直接重跑即成功**。不要据此判定失败，务必用独立命令复核实际状态
  （push 后用 `git ls-remote --heads origin` 对 sha，而不是相信 push 的退出码）。
- 全量 `cargo test`（会同时起多个 python sidecar 子进程）容易撞资源；拆成
  `--lib` + 逐个 `--test <name>` 更稳，结果等价。

## 判定「是不是我改坏的」

- 本机 `.git/refs/remotes/**` 写入被静默丢弃 → `git status -sb` 的 `[gone]` 是假的，
  核实远端用 `git ls-remote --heads origin`。
- `target/` 里偶发 `os error 5 拒绝访问` 是环境噪声，不是 lint/编译错误；重跑即可。
- 归因方法：`git status --porcelain -- <dir>` 看改动文件集，确认失败点是否落在改动集内。

## 音频管线（M2）事实

- `Vad` trait 故意 `!Send` → **每路一个 worker 线程自建 VAD 实例**，不能共享。
- `CapturePipeline` **没有 `flush()`**：停止时重采样器内部积压（FFT block + output_delay）
  不会变成帧 → 真实采集停表会丢最后几百 ms。已知、未修、不在 M2-8 范围内。
- 端点常量：`FRAME_MS=30` / `START_WINDOW=5` / `START_MIN_VOICED=3` /
  `END_SILENCE_FRAMES=17` / `MIN_KEEP_FRAMES=9` / `MAX_SEG_FRAMES=500` / `HOLD_CAPACITY=21`
  （`endpoint.rs` 注释为准，`service.rs` docstring 的 22 是旧值）。
- 段边界与 sidecar 的换算：`start_ms = onset*30`，`end_ms = (offset+1)*30`；
  sidecar 侧 `duration_ms = ts_end - ts_start`。
- 黄金向量唯一来源：`sidecar/src/audio_eval/endpoint.py::detect_segments`。
  **不要用 Rust 重写一份参考实现**去比对（那是"拿我的实现当标准"）——
  经 `scripts/endpoint_reference.py` 子进程调用同一份 Python 实现。
