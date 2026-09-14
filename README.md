# InterviewCopilot（内部自用、本地优先）

语音转写 + RAG 知识检索 + 答案提示的桌面应用（Tauri 2 + React + Python sidecar + SQLite）。

权威文档：`docs/PRD.md`（v1.5，按任务约定落仓；本骨架任务暂未拷入，待后续任务补）。

## 骨架任务（task-1）最小链路

壳启动 → sidecar 被拉起 → stdout 握手被解析 → 前端显示 connected。

```sh
# sidecar 独立运行
python sidecar/src/main.py
# 健康检查（握手 JSON 中的 port）
curl http://127.0.0.1:<port>/health  # → {"status":"ok","version":"0.1.0"}

# 前端
pnpm install
pnpm build        # tsc + vite，无需 Rust
pnpm tauri dev    # 需 Rust + MSVC Build Tools；dev 态用 INTERVIEWCOPILOT_PYTHON 指定解释器
```

环境变量（dev）：

- `INTERVIEWCOPILOT_PYTHON`：Python 解释器（默认 `python`）
- `INTERVIEWCOPILOT_SIDECAR_ENTRY`：sidecar 入口（默认 `<repo>/sidecar/src/main.py`）

## 纪律摘要

- Python sidecar 是 SQLite 唯一访问者（R1）；本任务无 DB。
- 握手 `protocol_version=="1.0"`，除 `GET /health` 外鉴权后续任务实现（R2）。
- 日志 content-free；token 仅内存，不落盘（R8）。
