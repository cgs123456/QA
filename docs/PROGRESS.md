# PROGRESS.md — InterviewCopilot

## 已完成
- task-10 收尾（2026-09-14）：模型下载进度通道（POST id + GET 轮询，落地裁定写回 api-contract.md）+
  Settings 页（Provider 选择持久化/API Key→keyring/模型下载进度）+ Degraded 完整版
  （stderr 落盘 + sidecar_log_tail + 重试打通）+ 锁文件（requirements-lock 42 pins/clean-venv 复现，
  pnpm-lock 在仓；Cargo.lock 待工具链）+ docs/models.md/api-contract.md +
  eslint（0 warning）+ CI 五 job + 体积回归脚本；附带修包自污染 bug（DB 写进 bundle）。
- task-9 可见面（2026-09-14）：lib/api.ts 完整（REST+SSE+token 注入+401/网络/超时错误分类）+
  lib/sse.ts（纯帧解析 + 来源标签）+ zustand stores（app/knowledge）+ TanStack Query hooks
  （useStore CRUD/compile + useQA 两段式流式可取消）+ Search 页（命中标签 + 答案三态卡）+
  Knowledge 页（建库/切换/删除 + md/json 导入 + 编译统计）+ App 导航；后端配合：
  sources 含 routes、vec 失败降级不断链、retrieval 事件带 store_id/warnings。
  测试：vitest 9 + Playwright qa_sse.spec.ts（API 契约级，无需壳/浏览器）+ pytest 98。
- task-8 流式契约与护栏（2026-09-14）：routers/qa.py（POST /qa/ask 建 task 后台跑 → task_id；
  GET /qa/stream SSE：retrieval→generation*→done；读毕/TTL60s 清理；未知/过期 404；
  is_disconnected 1s 轮询 + finally 取消后台；禁缓冲头）+ core/prompt_guard.py（PRD §3.9，
  provider 重导出收敛）+ 注入用例集（标签伪造转义）。
- task-7 答案分派（2026-09-14）：generation/provider.py（Protocol 流式 generate+embed；
  Ollama 本地流式默认 qwen2.5:7b + OpenAI SSE + Custom 占位；30s 超时走 error 路径四 kind；
  SYSTEM_PROMPT/build_prompt PRD §3.9 归 provider）+ router.py（三路分派对接 Task 6 判定：
  direct 取 field_value/official_answer 零 LLM；maybe 以 top1 为 context（multi 含 top1+top2 且同显两条，
  默认裁定集中 select_contexts）；fail_closed 固定文案「知识库未命中」；LLM 异常走 error 不降级编造）+
  密钥通道（core/secrets.py 内存持有 + POST /settings/llm-secret 认证推送不回显 +
  Rust keyring 读存/F6.3 + 握手后推送 + Linux 加密文件占位 Phase 2）+ scripts/e2e_ollama.py 人工验收脚本。
- task-6 向量融合基线（2026-09-14）：models/registry（bge ONNX 来源+SHA256 pin）+ downloader
  （MS→hf-mirror→HF/断点续传/SHA/进度）；bge-small-zh-v1.5 ONNX 落盘 sidecar/models/（gitignored，
  onnx 41KB SHA 与 LFS 一致，data 90MB 一致，tokenizer 双文件实测 pin）；
  embedder（ort CPU/批量/mean池化/L2归一化512维）+ vector_search（vec0/L2/s=1-d/2/d<0.6/top5/rejected过滤）；
  compiler embeddings 同事务位 + SIGKILL 回滚零残留；hybrid_rank（归一化加权非RRF，字段entity==category联动，
  decide 四档）+ eval_full；基线报告 docs/eval-baseline.md（阈值未改）。
- task-5 文本闭环（2026-09-14）：parsers（markdown H1→entity/H2疑问→QA/陈述→字段首段/Q&A块；json 实体/qa_pairs/列表三形）+
  validator（必填/usage集合/批内去重）+ field_vocab.json（3 组）/extractor（归一化+别名展开表）+
  stores.py（CRUD/单选current/级联删vec手工清）+ compiler（单短事务写qa+fields+aliases，embeddings 参数预留 R6 位，
  冲突覆盖记 field_overwritten/vocab_miss content-free 事件）+ retrieval（field_lookup 归一化精确 s=1.0；
  fts5 双路 bm25<-0.5 top5 s=a/(a+0.5)，rejected 全过滤）+ routers（compile/list/store CRUD/current/search）+
  eval 骨架（schema/loader/20 条/runner）。结构注记：knowledge/stores.py 为防 routers/测试逻辑重复的新增小模块；
  GET /knowledge/search 为 DoD（HTTP 测字段<200ms）驱动的最小 F2.1 骨架（无 LLM/融合）。
- task-3 P0 冒烟 PASS（2026-09-14），P0 风险关闭（vendor 上游取数，SHA256 已验，五步全绿）
- task-4 建库+打包（2026-09-14）：database/（connection 单连接/WAL/双扩展/绝对锚定BASE=env→_MEIPASS→sidecar；
  schema user_version 迁移框架；v001 全量 DDL：stores/qa_pairs/fields/field_aliases+idx/ft_qa+ai/ad/au/vec_local512/vec_cloud3072/session_events）；
  main.py 启动执行迁移（失败非零退出无握手）；build.py onedir + 三平台脚本 + verify-packaged.py 四校验；
  产物 45.4MiB、System32 下启动两次 ALL PASS、spawn→health ~1.0-1.2s（<5s 预算）
- task-1 骨架：monorepo（pnpm create tauri-app react-ts 模板，改名 interview-copilot；注：模板为 React 19，PRD 写 React 18.3+，按模板为准）+ PRD §4.1 目录占位 + .gitignore（排除 sidecar/vendor 二进制与 dict）+ scripts/fetch-vendor.py（来源/版本说明文档）
- sidecar 最小 app：GET /health → {"status":"ok","version":"0.1.0"}；main.py：bind 127.0.0.1:0 取端口 → token_urlsafe(32)（本任务只生成不校验）→ uvicorn 绑定成功后再 print 握手 JSON（flush=True）；绑定失败非零退出
- Rust：src-tauri/src/sidecar/{mod,protocol,manager,degradation}.rs；manager 经 INTERVIEWCOPILOT_PYTHON / INTERVIEWCOPILOT_SIDECAR_ENTRY 启动、逐行读 stdout、10s 超时、解析握手存 port/token；protocol 校验 protocol_version=="1.0" + 单测 3 例（合法/非法JSON/版本不匹配）；lib.rs 接线 setup 拉起 + get_sidecar_status；audio/security/commands/shortcuts/tray/updater 为空占位
- 前端：App.tsx 经 invoke("get_sidecar_status") 轮询显示 sidecar connected/disconnected（+port）
- task-2 生命周期+鉴权：sidecar core/auth.py（verify_token 依赖注入，secrets.compare_digest，缺失/错误→401，token 仅内存）；除 /health 外全部路由挂载（新增 GET /sidecar/info 作鉴权闭环证明路由）；main.py 启动时 init_token；Rust supervise（1s health 轮询、崩溃检测、退避 1s/2s/4s max_restarts=3、health 恢复清零计数、版本不匹配→sidecar://degraded 事件 version_mismatch、无任何握手失败残留孤儿、RunEvent::Exit taskkill /T /F 清理进程树）；前端 src/lib/api.ts（invoke 拿 port/token + fetch 自动带头）+ src/pages/Degraded.tsx 占位降级页（原因+查看日志+重试）；commands 新增 get_sidecar_credentials/get_sidecar_degraded/retry_sidecar_start

## 当前
- task-11 M1 验收 + review 完毕待提交：`docs/acceptance-m1.md`（13 通过/1 待定/1 部分/1 待人工，
  无失败，tag 暂缓）+ `docs/review-m1.md`（独立对抗审查：实质问题 8 项修 7 受 1，附带修历史 bug 2）。
  全量：pytest 108 + vitest 10 + Playwright 1 + tsc/eslint 0 + build/check_size 通过。

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
- task-4（2026-09-14）：迁移单测 8 例 + connection 锚定 6 例，全量 pytest **25 passed**；
  dev 启动验证 user_version=1 + journal_mode=wal；打包实测见 `docs/benchmark.md`
 （onedir 47,583,035 bytes=45.4MiB；spawn→握手 1.13s/1.01s，spawn→health 1.15s/1.03s；
  System32 下四校验两次 ALL PASS）。实测修正两处：① BASE 开发态取 sidecar/（非项目根，
  否则 vendor 路径错位，迁移单测已锁定）；② PyInstaller 6 不自动记入 sidecar/src 到 pathex，
  build.py 显式 --paths 修复（漏收录则启动即 ModuleNotFoundError）。pyproject 新增 sqlite-vec>=0.1。
- task-5（2026-09-14）：全量 pytest **44 passed**（旧 25 + 新 19：compiler 6 / retrieval 6 / routers 5 / eval 2）。
  DoD：MD 三表（qa=2/fields=2/aliases=7）内容正确；JSON（qa=2/fields=2/aliases=3，退货期限归一化，
  vocab_miss=1 事件）；中文 jieba + 拼音 simple 命中；字段 HTTP timings_ms.field<200ms（实测 ~0.05ms）；
  rejected 直接+HTTP 双排除；PUT current 后无 store_id 检索跟随新库（B 有命中→A 无命中）。
  eval demo（8 QA，20 题）：top3/top5=1.0（17 scored），field_hit=0.176，avg field 0.05ms / fts 26ms（含首查懒加载）。
  实测发现（已写进 fts5_search.py 注释，引擎按 PRD 初值未动，示例题按关键词式校准）：
  ① jieba/simple_query 为 AND 语义（含虚词），自然长问句易整体落空——D6 应先关键词提取；
  ② FTS5 idf=ln((N-n+0.5)/(n+0.5))，N=2/n=1 时 idf=ln(1)=0，合法命中也被 -0.5 门限滤掉——
  阈值必须真实规模语料标定，禁止为迁就 demo 调门限。
- task-6（2026-09-14）：全量 pytest **67 passed**（旧 44 + 新 23：downloader 5 / embedder 4 /
  vector 5 / hybrid 7 / rollback 1 / eval_full 1）。DoD：① SIGKILL 回滚零残留（victim 4000 行编译中被
  TerminateProcess，重启后 qa=0/vec=0/孤儿 0，且断言非正常退出、无 Traceback，排除假阳性）；
  ② 三结构性案例绿（0.500/0.500/0.750 精确相等）+ 全分 ∈[0,1]（越界钳入）+ decide 六边界；
  ③ 基线报告 `docs/eval-baseline.md` 落盘（阈值未改）。
  模型：onnx-community/bge-small-zh-v1.5-ONNX（base BAAI），onnx 经 ModelScope 首镜像（无降级）、
  tokenizer.json 经 hf-mirror（ModelScope 首试超时自动降级，顺序纪律有效）；pyproject 新增 onnxruntime/transformers。
  关键结构发现（hybrid_rank 注释 + 基线解读）：0.75 可达**必须**字段证据联动到同一 qa 候选
  （entity==category，同源 H1），否则直接返回档死亡——联动已实现（二值结构性，强度仍由 s_i 表达）；
  另发现 vec 门限 s=0.7 与 PRD 结构表 (3.0+1.5)/6.0=0.750 的字面算术差 0.033（全门限 s 和应为 0.783），
  按 PRD 字面锚定 0.750，引擎未动，留待标定。
  基线现象（demo 8QA/20题，默认阈值）：Top-3/5=1.0 但 Fail-Closed=0.80、direct=0.15——
  字段联动需精确相等（自然问法极少逐字等于 field_name）+ 纯模糊上限恒 0.5；
  null 题 FC=1.0（方向正确）。标定纪律重申：不得为提 direct 召回调权重/阈值，动则记录重跑。
- task-7（2026-09-14）：全量 pytest **85 passed**（旧 67 + 新 18：providers 7 / answer_router 7 / settings 4）。
  DoD：direct（退货期限→官方答案，FakeProvider 调用计数 0，generate 未被触碰）；
  Fail-Closed（无匹配/空问 →「知识库未命中」，计数 0）；maybe 单/双（monkeypatched 确定性：
  0.45 边界 single、gap 0.005 multi，context=[top1(+top2)]，sources 同显，chunks 顺序 decision→sources→chunk→done）；
  error 路径（ProviderError timeout →「答案生成失败，请稍后重试。」不编造）。
  provider 流解析经 httpx MockTransport 真流验证（Ollama JSON-lines / OpenAI SSE / 401映射/拒连映射）。
  R8：grep 确认 Rust/sidecar 无 key 日志；settings 响应不回显（单测断言）；reqwest 错误仅 URL/状态。
  待人工：① Ollama e2e（本机 11434 拒连）：`ollama serve` + `ollama pull qwen2.5:7b` 后跑
  `python scripts/e2e_ollama.py` 看 chunk 时间戳流式；  ② Rust：`cargo test`（含 fallback 占位确定性单测） + 首个 cargo build 生成 Cargo.lock 入仓
  + `cargo audit`（PRD 供应链要求顺手做）。
- task-8（2026-09-14）：全量 pytest **97 passed**（旧 85 + 新 12：qa_stream 6 / prompt_guard 5 + 碰撞回归 1）。
  DoD：真机 SSE（建库→编译→ask→stream）：direct[retrieval→done]官方答案、fail「知识库未命中」、
  无缓冲头（no-cache/x-accel-buffering:no）、读毕复读 404；TTL 短注单测 404；断开→取消后台+清记录
  （单循环直驱确定性单测——附带发现：TestClient 不投递断开，生产靠 is_disconnected 1s 轮询，
  已在实现中）；注入 6 用例全绿（标签伪造转义为全角，资料保真，问题恒末位）。
  实测修一真 bug：显式 id 跨库重导撞全局主键 → compile 500（IntegrityError）；现换新 id + embedding 跟随迁移，
  回归单测锁定（同一教训：dev 库curl残留 store 已清；eval 固定 id 的种子跨库编译不再炸）。
  已知后续（本次不动，记账）：routers 写路径尚未持 connection.write_lock 串行（R3 字面要求），
  并发编译/删除与后台检索同发时可能 SQLITE_BUSY——D 路由硬化时收。
- task-9（2026-09-14）：pytest **98 passed** + vitest **9 passed** +
  Playwright `qa_sse.spec.ts` **1 passed（4.4s，无需浏览器）** + `tsc` 0 + `pnpm build` 成功（75 modules）。
  spec 覆盖 DoD 机器部分：建库→导种子（qa=8）→direct（官方答案/零LLM）→fail 文案→建B库切当前库→
  无 store 提问 retrieval.store_id==B→删A后 404；无缓冲头断言；sidecar 子进程 DB 隔离在系统临时目录。
  后端配合改动：sources 加 routes（标签用）、vec 失败降级不断链（+单测）、retrieval 带 store_id/warnings。
  新增依赖：zustand / @tanstack/react-query / vitest / @playwright/test（均为 PRD 栈或测试工具）。
- 人工 E2E 走查（需 Tauri 壳，`$env:INTERVIEWCOPILOT_PYTHON="<python>"` 后 `pnpm tauri dev`）：
  ① 壳启动 sidecar connected；② 知识管理页建库「走查库」；③ 导入含字段+问答的 md（stats qa=2/fields=2）；
  ④ 手动查找页问「公司成立时间」→固定答案卡 + 字段直查标签；⑤ 问无匹配串→知识库未命中卡；
  ⑥ 建第二库导其他内容→切换当前库→同问指向新库（命中变化）；⑦ 删除库→列表消失（级联）。
  不做项（本次未碰）：视觉打磨、暗色主题、Rehearsal 页。
- task-10（2026-09-14）：pytest **104 passed**（dev）且隔离 venv **103 passed**（回滚 1 例初败后定位修复，
  见下）+ vitest 9 + Playwright 1 + eslint 0 + tsc 0 + build 成功 + check_size PASS + CI YAML 合法（5 job）。
  DoD：① 模型下载通道 5 单测 + **断网续传 e2e**（95MB 真字节首传 2MB 断开→同镜像 Range 续传→SHA，
  附带修真 bug：传输截断 read() 静默 b"" 无异常，靠长度裁决转重试）；② kill×4 降级+重试转人工
  （需壳；Rust supervise 逻辑未变：4 连败→restart_exhausted 事件→降级页→重试按钮调 retry_sidecar_start）；
  ③ CI 本地验证：pytest/vitest/eslint/tsc/build/check_size 全过，clippy/tauri-build 为 CI-only（无工具链）。
  锁文件：requirements-lock.txt 42 pins（clean venv freeze；隔离 venv 全绿复现）+ pnpm-lock 在仓；
  Cargo.lock 待首个 cargo build 生成后入仓（无工具链）；models.md（bge/libsimple/vec）+ api-contract.md 落盘。
  包自污染 bug（已修）：DB 默认路径曾落 `_internal/`，每次启动 +230KB 且只读安装即崩；
  现打包态走用户数据目录（单测锁定不在 bundle 内），两次 verify 总字节完全一致；
  基线重定 BUNDLE_BYTES=200060425（190.8 MiB，增量 ≈145MB 均为 task-6/7 已立项 embedding 栈）。
  附带修：dist/ 与 sidecar 产物目录冲突（vite 构建清空 dist 误删过 sidecar 包）→ dist-sidecar/ 分离。
  回滚初败根因（记账精度）：Windows TerminateProcess 后句柄释放竞态，父进程立即重连 WAL 报 disk I/O error——
  测试加退避重连吸收（数据断言未放宽）；顺带证明 kill 确实命中事务窗口。
  待人工：① 连续 kill×4→降级页出现→点重试恢复（`pnpm tauri dev` 后 tasklist 找 python sidecar 进程 kill）；
  ② Rust 全链：`cargo test` + build（产 Cargo.lock）+ `cargo audit`；③ Ollama e2e（task-7 遗留）。
- task-11 M1 验收（2026-09-14）：`docs/acceptance-m1.md` 落盘——16 项：13 通过 / 1 待定（#6 需用户 100 题，
  demo 代理 17/17）/ 1 部分通过（#10 检索侧 1.4ms，LLM 首字待 Ollama）/ 1 待人工走查（#13 降级页渲染，
  链路单测全绿）；无失败项，当场修：无（验收中新发现为 0；历史 bug 均已在各自任务修完）。
  融合三案精确复核：0.5/0.5/0.75（独立算术 + 7 单测）；FTS 有数据验证全清单复核：无一处空表断言（R4）。
  tag 判定：`m1-text-pipeline` 暂缓（打 tag 会夸大完备性；三外部条件见验收报告 §4，用户可指示覆盖）。
- 待办（外部阻塞，主人明确）：① 用户 100 题 → 重跑基线 → 更新验收报告 → 打 tag；
  ② Ollama 可用 → `scripts/e2e_ollama.py` 补首字数字；③ 有壳机器 → 降级页目检 + kill×4 + UI 走查；
  ④ 有 Rust 工具链 → cargo test/build/audit + Cargo.lock 入仓。
- task-11 review 修（2026-09-14，详见 `docs/review-m1.md`）：R3 写串行落地（RLock + stores 三写 +
  compiler 事务外层持有——此前定义零执行）；TASKS/_JOBS 泄漏加 TTL 清理；下载非预期异常落终态；
  端口 TOCTOU 加 health nonce（main/app/单测/契约同步）；FTS 异常收窄（仅语法兜底，缺表/缺扩展/I-O
  上抛，单测锁定双向）；后台异常堆栈进 stderr（Rust 落盘文件 → 降级页可查）；健康检查连接泄漏修复。
  附带修：app.py 路由重复定义（/health、/sidecar/info 各两份，行为恰无害，现删；路由表重查 14 唯一）；
  前端取消失效（signal 被自建 controller 覆盖，cancel 永不到达 + 误报超时，现透传 + 原样抛 AbortError，
  悬挂服务单测锁定）；pyproject 描述过期更新。
  接受项（有记录）：vec blanket 降级（缺模型为预期态，warnings 为观测通道）；FastAPI 0.141 路由表象。
  债务：Rust 零编译验证 / Ollama e2e / 标定 / 前端止于逻辑层 / _embedder 单例无失效处理 /
  进程内存语义（TASKS/_JOBS/secrets 重启即失，符合 TTL 语义）。
