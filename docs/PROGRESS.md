# PROGRESS.md — InterviewCopilot

## 已完成
- task-1 骨架：monorepo（pnpm create tauri-app react-ts 模板，改名 interview-copilot；注：模板为 React 19，PRD 写 React 18.3+，按模板为准）+ PRD §4.1 目录占位 + .gitignore（排除 sidecar/vendor 二进制与 dict）+ scripts/fetch-vendor.py（来源/版本说明文档）
- sidecar 最小 app：GET /health → {"status":"ok","version":"0.1.0"}；main.py：bind 127.0.0.1:0 取端口 → token_urlsafe(32)（本任务只生成不校验）→ uvicorn 绑定成功后再 print 握手 JSON（flush=True）；绑定失败非零退出
- Rust：src-tauri/src/sidecar/{mod,protocol,manager,degradation}.rs；manager 经 INTERVIEWCOPILOT_PYTHON / INTERVIEWCOPILOT_SIDECAR_ENTRY 启动、逐行读 stdout、10s 超时、解析握手存 port/token；protocol 校验 protocol_version=="1.0" + 单测 3 例（合法/非法JSON/版本不匹配）；lib.rs 接线 setup 拉起 + get_sidecar_status；audio/security/commands/shortcuts/tray/updater 为空占位
- 前端：App.tsx 经 invoke("get_sidecar_status") 轮询显示 sidecar connected/disconnected（+port）
- task-2 生命周期+鉴权：sidecar core/auth.py（verify_token 依赖注入，secrets.compare_digest，缺失/错误→401，token 仅内存）；除 /health 外全部路由挂载（新增 GET /sidecar/info 作鉴权闭环证明路由）；main.py 启动时 init_token；Rust supervise（1s health 轮询、崩溃检测、退避 1s/2s/4s max_restarts=3、health 恢复清零计数、版本不匹配→sidecar://degraded 事件 version_mismatch、无任何握手失败残留孤儿、RunEvent::Exit taskkill /T /F 清理进程树）；前端 src/lib/api.ts（invoke 拿 port/token + fetch 自动带头）+ src/pages/Degraded.tsx 占位降级页（原因+查看日志+重试）；commands 新增 get_sidecar_credentials/get_sidecar_degraded/retry_sidecar_start

## 当前
- task-3 P0 冒烟 **PASS（2026-09-14），P0 关闭**：vendor 由我从上游取数落盘
  （simple v0.7.1 windows-x64，SHA256 已验）；五步全绿，全量 pytest 11 passed。

## 待人工验证
- 需 Rust + MSVC Build Tools 机器：`cargo test`（protocol 3 例 + degradation 3 例 + manager 2 例）通过；`$env:INTERVIEWCOPILOT_PYTHON="<python>"; pnpm tauri dev` 壳启动且日志可见 `[sidecar] handshake parsed: port=...`；前端显示 sidecar connected
- task-2 人工 DoD（有工具链机器）：① 手动 kill sidecar 的 python 进程 → 日志可见 `restart 1/3 in 1s`… 最多 3 次并恢复 health（计数在 health 恢复后清零）；② 连续 kill 致 4 连败 → 前端降级页（reason=restart_exhausted）+ 事件 sidecar://degraded；③ 伪造 protocol_version（如改 main.py PROTOCOL_VERSION="9.9"）→ 降级页 reason=version_mismatch；④ 应用退出后 tasklist 无残留 python sidecar 进程
- Python 为 3.13.14（本机可用版本），PRD 要求 3.11——后续 CI/打包时需按 3.11 锁定验证

## 实测数字档案
- sidecar 独立 DoD（verify-sidecar.py）：HANDSHAKE_JSON_OK（protocol_version 1.0、port int、auth_token str≥32、capabilities/models_loaded list；token 已打码不落盘）；HEALTH_STATUS=200 BODY={"status":"ok","version":"0.1.0"}；结论 SIDECAR_STANDALONE_DOD_PASS（2026-09-14）
- pytest sidecar/tests：1 passed（test_health_returns_ok），pytest 9.1.1 / fastapi 0.141.1 / uvicorn 0.52.4
- 前端：pnpm install 成功（react 19.3.0、@tauri-apps/api 2.11.1、cli 2.11.4、vite 8.3.0）；`npx tsc --noEmit` 无输出通过；`pnpm build` 成功（dist/index.html 0.48kB）
- protocol 逻辑等价验证（Python 镜像，cargo 本机不可用）：valid-ok / invalid-json-rejected-ok / version-mismatch-rejected-ok
- 未跑：cargo test、pnpm tauri dev（本机无 cargo/rustc/link；原因如实记录，非跳过）
- task-2 鉴权真机（verify-auth.py，2026-09-14）：HEALTH_NO_TOKEN=200；INFO_NO_TOKEN=401；INFO_WRONG_TOKEN=401；INFO_WITH_TOKEN=200 → AUTH_DOD_PASS
- task-2 pytest：6 passed（test_health 1 + test_auth 5：无token/错token/畸形头→401，对token→200 且 401 体不含 token）
- task-2 前端：`npx tsc --noEmit` 通过（exit 0）；`pnpm build` 成功（21 modules，dist/assets/index-74NERvRi.js 223.03kB）
- task-2 Rust：degradation/manager 单测已写 8 例（degradation 3：mismatch 触发事件/合法无emit/非法JSON无emit；manager 2：退避表/ mismatch 判别；protocol 3 沿用）——本机无工具链，待人工 `cargo test`；R8 复核：token 仅出现在内存结构/IPC 与测试占位值，无日志/事件/错误串泄漏
- task-3 P0 冒烟（2026-09-14）：初版测试按诊断纪律停于前置缺失（vendor 空）；
  用户授权后我从上游取数：simple v0.7.1 `libsimple-windows-x64.zip`
 （5,473,005 bytes，sha256 `7f03cc28…bed0b` 实测一致）→ `libsimple.dll`（1.5MB）+ dict 五文件落盘
  （gitignored，本地专用；三件套之外另需 idf.utf8/stop_words.utf8——见下）。
  中途实测关键发现：缺 `idf.utf8` 时 `jieba_query` 触发扩展内 `abort()`（`KeywordExtractor.hpp:97`），
  进程直接 `Fatal Python error: Aborted`（不可捕获）；补齐后五步全绿。
  计时：load 0.9ms / jieba_dict ~0ms（懒加载）/ 建表 0.3ms / 插入 44.6ms /
  jieba_query 首查 511.8ms / simple_query 热查 0.1ms；sqlite_version=3.53.1。
  全量 pytest：**11 passed**。报告见 `docs/compatibility.md`（PASS）。
  后续待办：sidecar 启动自检必须先验 dict 完整性（abort 不可捕获，重启循环救不了缺文件）。
