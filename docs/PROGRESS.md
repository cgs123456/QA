# PROGRESS.md — InterviewCopilot

## 已完成
- task-1 骨架：monorepo（pnpm create tauri-app react-ts 模板，改名 interview-copilot；注：模板为 React 19，PRD 写 React 18.3+，按模板为准）+ PRD §4.1 目录占位 + .gitignore（排除 sidecar/vendor 二进制与 dict）+ scripts/fetch-vendor.py（来源/版本说明文档）
- sidecar 最小 app：GET /health → {"status":"ok","version":"0.1.0"}；main.py：bind 127.0.0.1:0 取端口 → token_urlsafe(32)（本任务只生成不校验）→ uvicorn 绑定成功后再 print 握手 JSON（flush=True）；绑定失败非零退出
- Rust：src-tauri/src/sidecar/{mod,protocol,manager,degradation}.rs；manager 经 INTERVIEWCOPILOT_PYTHON / INTERVIEWCOPILOT_SIDECAR_ENTRY 启动、逐行读 stdout、10s 超时、解析握手存 port/token；protocol 校验 protocol_version=="1.0" + 单测 3 例（合法/非法JSON/版本不匹配）；lib.rs 接线 setup 拉起 + get_sidecar_status；audio/security/commands/shortcuts/tray/updater 为空占位
- 前端：App.tsx 经 invoke("get_sidecar_status") 轮询显示 sidecar connected/disconnected（+port）

## 当前
- task-1 已完工待提交；本机已跑验收（见实测数字档案）；Rust 编译/运行验收转人工（本机无 Rust/MSVC，见待人工验证）

## 待人工验证
- 需 Rust + MSVC Build Tools 机器：`cargo test`（protocol 3 例）通过；`$env:INTERVIEWCOPILOT_PYTHON="<python>"; pnpm tauri dev` 壳启动且日志可见 `[sidecar] handshake parsed: port=...`；前端显示 sidecar connected
- Python 为 3.13.14（本机可用版本），PRD 要求 3.11——后续 CI/打包时需按 3.11 锁定验证

## 实测数字档案
- sidecar 独立 DoD（verify-sidecar.py）：HANDSHAKE_JSON_OK（protocol_version 1.0、port int、auth_token str≥32、capabilities/models_loaded list；token 已打码不落盘）；HEALTH_STATUS=200 BODY={"status":"ok","version":"0.1.0"}；结论 SIDECAR_STANDALONE_DOD_PASS（2026-09-14）
- pytest sidecar/tests：1 passed（test_health_returns_ok），pytest 9.1.1 / fastapi 0.141.1 / uvicorn 0.52.4
- 前端：pnpm install 成功（react 19.3.0、@tauri-apps/api 2.11.1、cli 2.11.4、vite 8.3.0）；`npx tsc --noEmit` 无输出通过；`pnpm build` 成功（dist/index.html 0.48kB）
- protocol 逻辑等价验证（Python 镜像，cargo 本机不可用）：valid-ok / invalid-json-rejected-ok / version-mismatch-rejected-ok
- 未跑：cargo test、pnpm tauri dev（本机无 cargo/rustc/link；原因如实记录，非跳过）
