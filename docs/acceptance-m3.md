# M3（核心功能闭环）验收报告（PRD §6.3）

> 执行时间：2026-09-15，B 机（AMD Ryzen 5 5500 / 12 逻辑核 / 31.9GB / Python 3.13.14 / cargo 1.98.1 / pnpm 9 / node v20.17.0 缺）。
> 执行方式：本机可跑项全部实跑（命令 + 输出摘录见下）；需外部输入项如实标记，不估分、不借数。
> 结论：**12 通过 / 3 部分通过 / 1 未达标（R19 缺数据）**（P13 更新：#13 改判通过、#14 改判部分通过）。
> `m3-core-features` tag **暂缓**（剩余阻塞项均非「缺代码」，见 §4）。

## 0. 机器与可比性

| 项目 | 规格 |
|---|---|
| CPU | AMD Family 25 Model 80，12 逻辑核 |
| 内存 | 31.9GB 总 / 20.3GB 可用 |
| Python | 3.13.14（venv `C:\tmp\evalvenv`） |
| Rust | cargo 1.98.1 / rustc 1.98.1 / MSVC 2022 |
| node / pnpm | **本机无**（venv 隔离；前端工具链不在 PATH） |
| vendor二进制 | `sidecar/vendor/libsimple.dll` 就位（1.5MB） |
| 模型权重 | faster-whisper-base + paraformer-zh + sense-voice + bge-small-zh-v1.5（本地） |

> 对比：M1 无 cargo、M2 早版本缺 vendor/node，本版环境阻塞项已全消。

## 1. 逐项验收（PRD §6.3 重建表）

| # | 测试项（标准） | 结论 | 证据（命令 + 输出摘录） |
|---|---|---|---|
| 1 | **P1 匹配根因修复 + v2 基线**（Top-3 ≥0.85 / 零答错 / 100 题） | **通过** | 实现先行落地（`hybrid_rank.py` 精确门/向量门/AND 语义已锁）、`pytest tests/test_hybrid_rank.py -v` **7 passed**（三结构性融合 0.5/0.5/0.75 精确断言）；独立复现脚本 `scripts/eval_calibrate.py` 以 demo 集 17 条重跑 **Top-3=1.0、零答错**；真题集 59 题（49 scored + 10 null）重跑 **Top-3=0.49、零答错、null 拒答 10/10**——**已在 M1 §1#6 记录为未达标**，此处**仅确认实现已落地并可复现**，指标达标待 D6 重标定 + 扩题（见 §3.A)。**P12 增补（P6 已闭，2026-09-16）**：100 题冻结集全链标定落盘（`docs/eval-final.md`），定稿 `w=(1.5,3.0,1.0,4.0)/TH_DIRECT=0.65/TH_MAYBE=0.60`，终值 **Top-3 0.9080 / null 13/13 / direct 51对5错 / 答错6 / FC(非null)0.3218**（与初值持平，未越红线），详见 §8。 |
| 2 | **P2 SSE/流式契约**（`retrieval`→`chunk(s)`→`done` 顺序、不丢不重、禁缓冲头） | **通过** | `pytest tests/test_qa_stream.py -v` **6 passed**（含 `test_sse_order`/`test_sse_no_buffering`/`test_sse_chunk_order`）；真机 `curl -N` 抓包确认 `SSE_ORDER=['retrieval','done']`（direct）与 `['retrieval','decision','sources',...'chunk','done']`（fallback）零乱序。 |
| 3 | **R17 固定答案 <200ms**（直连、零 LLM） | **通过** | 真机：`DIRECT_ORDER=['retrieval','done']`，**2.4ms** 完链（M1 §1#9）；P10 后端零改动、检索路径无回归。 |
| 4 | **F6.1 模糊检索 <5s / 首字 <1s**（检索侧） | **部分通过** | 检索侧实测：ask→首个 `retrieval` 事件 **1.4ms**（M1 §1#10）；LLM 首字仍阻塞于 Ollama 缺失（同 M1/G1）。 |
| 5 | **P5 Embedding 基建**（local bge-512 / cloud text-embedding-3-large-3072） | **通过** | `pytest tests/test_embedding.py -v` **32 passed**（含 provider 切换、维度校验、批量、降级、缓存键）；`sidecar/src/embedding/embedder.py` 双 provider 就位、`settings.yaml` 已含 `embedding.provider` 与 `embedding.dim`；cloud 路由落 `generation/router.py` 但**未联测**（网络不可达，见 §2.2）。 |
| 6 | **P8 可用性收尾**（设置持久化/导入导出/主题/快捷键/托盘/开机自启） | **通过（后端 + 实测三件套）** | `pytest tests/test_settings.py tests/test_export_import.py -v` **4+15=19 passed**；`tsc --noEmit` **0 错误**（P12 本机实测，非类型推断）；`eslint src --max-warnings=0` **0 错误**（P12 本机实测）；`vitest run` **167 passed / 12 files**（P12 本机实测，per-file 见 §8）；**页面目检需 Tauri 壳**（见 §3.B）。 |
| 7 | **P9 导出恢复 + 本地 CLI**（JSON/MD 双格式、校验+幂等、CLI 打包） | **通过** | `pytest tests/test_export_import.py -v` **15 passed**（JSON 往返×2/确定性/分块流式=整包/MD 稳定/截断锁定/缺列容错/覆盖语义/坏快照×4/列锁定/CLI 子进程×3/构建脚本作用域/同库碰撞换新）；`scripts/build-cli-win.ps1` 生成 `dist/cli/qa.exe`，**Smoke：`qa --help` / `qa import --help` / `qa export --help` 全 0 退出码**。**P12 勘误**：原措辞“JSON/Excel/PDF 三格式”不确——导出只有 **JSON（忠实通道）+ MD（规范回放）双格式**（`exporter.py` 模块注释 + 15 用例全是 JSON/MD，无 Excel/PDF 导出代码）；Excel/PDF 是**导入**方向（P1/P2）。PRD F4.5/F11.6 在仓内无定义（`git grep "F4\.[0-9]"` 零命中；README 注 PRD 待落仓），故按代码事实勘误；若 PRD 回仓后确有三格式导出要求，则为真缺口、开新项（见 §8）。 |
| 8 | **P10 简单高亮集成**（后端 `simple_highlight` + 端点片段 + 前端渲染 + research-sck） | **通过** | **后端**：`pytest tests/test_highlight.py -v` **11 passed**（列锁定结构+行为 / 中文词级 / simple 字级 / 拼音 / 自定义标记 / 非法列+空表达式 / 怪查询不炸 / 端点片段 / answer 透传）；`fts5_search.py` 常量 `COL_QUESTION=1/COL_ANSWER=2`（R4 双锁定）、`generation/router.py` `_hl_map` 透传（**fuse/hybrid_rank 零改动**）；`/knowledge/search` 自动透传 `hl_question`/`hl_answer`。**前端**：`src/lib/highlight.ts`（escape-then-replace，XSS 单测）+`Search.tsx` HitPreview（有标渲染/无标回落 60 字）+`qa.ts` `QASource.hl_*`。vitest **P12 本机实测 167 passed / 12 files**（其中 `highlight.test.ts` **8** 用例、`liveqa.test.ts` **30**；原“highlight 6 + liveqa +1 未跑”已过期，per-file 枚举见 §8）。**研究**：`docs/research-sck.md`（仅文档，Phase 4 八项清单，版本断言待真机核）。 |
| 9 | **SCK 预研文档落档**（能力/限制/清单） | **通过** | `docs/research-sck.md` 存在：macOS 13+、屏幕+系统音、8 项 Phase 4 清单（权限/音频tap/多显/停止语义/编码/沙箱/能耗/落盘），无代码。 |
| 10 | **诊断端点 + 面板**（计数/限额/设备行真值/`capture.status` 真实） | **通过** | `pytest tests/test_diagnostics.py tests/test_diagnostics_degrade.py -v` **4+6=10 passed**（新增状态迁移：在连 `connected+active_paths` / 断开 `disconnected`）；Rust 侧 Tauri 命令返回 `current_device()` + `CaptureStats(seq/dropped/errors)`；前端 `capture.test.ts` 28 用例（类型层面）；**目检仍待 Tauri 壳**。 |
| 11 | **全量回归**（pytest/cargo/clippy/fmt；vendor/工具链零债务） | **通过** | **pytest 393 passed / 1 skipped**（382 + `test_highlight` 11；skip 同源 `webrtcvad` 可选依赖）；**cargo test --lib 146 passed** / `--features audio-testharness 160 passed**；**clippy --all-targets -- -D warnings 0 告警**；**fmt --check 0**；vendor 就位。 |
| 12 | **R19 嘈杂子集 VAD/端点边界准确率**（真实声学） | **未达标（缺数据）** | `sidecar/tests/audio_samples/manifest.json` 6 槽全 `missing`。harness 本机双 provider 已定标（`audio_regression.py --self-test` → **SELFTEST_PASS**），样本一到即出数。 |
| 13 | **G1 生成答案 ≤6s**（分解耗时，固定/生成双路） | **通过（云端路径，P13）** | 固定答案三 Provider 全 PASS（faster-whisper **2202.3ms** / paraformer **190.1ms** / sensevoice **210.6ms**，含 QA 6.82ms，预算 4000ms，见 M2 §2）；生成答案云端 openai 实测 **5.50s ≤ 6s PASS**（P13：ASR 2202.3ms + QA/LLM `TOTAL_S=3.30s`，`FIRST_TOKEN_S=3.09s ACTION=llm`，gpt-4o-mini，`benchmark.md` 已落数；Ollama 本地路径仍缺服务，但按 M2 §7 纪律任一路达标即可关账）。**local-first 默认不变**。 |
| 14 | **云端 Provider 真机联调**（OpenAI/Anthropic/Gemini 首字/总耗时/动作） | **部分通过（openai 已闭，P13）** | 网络可达已正名（`docs/network-baseline.md` 取代旧 TCP 超时结论，见 §2.2 增补）。openai：`FIRST_TOKEN_S=3.09 TOTAL_S=3.30 ACTION=llm`（P13，轮换后 key，进程注入未落盘）；claude/gemini/groq 仍缺 key（exit 2 未跑）；ollama 缺本地服务；custom 未配。 |

## 2. 两项必须如实列示

### 2.1 1b 收尾台账现状（endpoint/接线/R19/G1/tag —— 按届时实际状态）

| 项 | 现状 | 解除条件 | 解除后第一件事 |
|---|---|---|---|
| **endpoint.rs** 状态机 | **已闭合**（2026-09-15，M2 §1#7 第三版改判） | —— | 已完成：`cargo test --test endpoint_parity` 4 passed（18309 帧 / 89 段 / 边界误差 0 帧） |
| **采集→VAD→端点→uplink→sidecar** 接线 | **已闭合**（C1b，注入 E2E `inject_e2e` 3 passed，双路互不污染） | —— | 已完成：真 VAD 决策 → 真端点 → 真 uplink → 真 sidecar |
| **R19 嘈杂子集** | **未闭合**（6 样本槽全空） | 主人投喂 6 段带标注 wav（见 `sidecar/tests/audio_samples/README.md`） | `audio_regression.py` 出表1 + Rust 侧 webrtc 绝对值复核 + 定默认 provider |
| **G1 生成答案 ≤6s** | **云端路径 PASS（P13）** | 合成总账 2202.3 + 3300 = **5502.3ms ≤ 6s**（`FIRST_TOKEN_S=3.09`，openai gpt-4o-mini；Ollama 本地仍缺，按 M2 §7 纪律关账） | 跑 `scripts/e2e_llm.py --provider {ollama,openai}`，把 `FIRST_TOKEN_S/TOTAL_S/ACTION` 落 `benchmark.md`，并同步关掉 P4 首字延迟台账 |
| **tag m2-audio-pipeline** | **暂缓**（剩余 4 项，全非「缺代码」） | 见 M2 §4：R19 / G1 / 平台真机 / 前端目检 | 用户明确指示或外部输入到位后再判 |

> 关键纪律：**R19 与 C1b 注入 E2E 不能混为一谈**——C1b 用合成语音证明「管线正确」（边界误差 0 帧），R19 要真实声学环境下的边界准确率，两者口径不同。

### 2.2 云端 Provider 真机联调探针（网络可达性，不碰 key）

```
2026-09-15 B 机探测：
- TCP 8.8.8.8:443       → TimeoutError (1s)
- TCP 1.1.1.1:443       → TimeoutError (1s)
- TCP api.openai.com:443 → TimeoutError (1s)
结论：外网不可达。任何云端 LLM/Embedding 路径在本机**无法实测**。
未发送任何 HTTP 请求，未触碰任何 API key/secret。

**P13 增补：上段结论作废**（仪器误判）。`docs/network-baseline.md` 已立档：
1s 超时裸 TCP 探针撞上 TUN 首包抖动即判死；HTTP 层三次运行 12/12 个 401/400
（对端活着并拒绝假 key），云端四家**可达**。此后可达性只看 HTTP 状态码。
云端联测缺的不再是网络，是 key——P13 已用轮换后 key 收掉 openai 三数（#13/#14）。
```

> 如需在本机闭合 P5 cloud embedding / G1 生成答案 / M3-14 云端联调，**必须先解决网络可达性**（代理/防火墙/策略路由），再提供凭据。

## 3. 未关项与阻塞的精确状态

### 3.A 未达标 1 项
- **R19 嘈杂子集**：数据缺口（同上）。关闭三条件：嘈杂样本 + 表1 + Rust 复核。

### 3.B 环境/真机阻塞 4 项
| 项 | 阻塞性质 | 解除条件 |
|---|---|---|
| G1 生成答案（云端路径） | ~~网络不可达 + 无 key~~ → **P13 已闭**（网络可达正名 + openai 5.50s PASS） | 网络可达 + 主人提供 key（或本机装 Ollama） |
| P5 cloud embedding 联测 | 网络不可达 | 网络可达 + key |
| M2-13 平台真机 | 缺 macOS/Linux 机器 | 各跑 `cargo test` + 采集回听 + 双路 ts |
| 前端页面目检 | 缺 Tauri 壳 | `pnpm tauri dev` 可启动，逐项走查 |

> **本机可写码工作量已归零**：上述 4 项**没有一项是「缺代码」**，全是环境/数据/外部输入。

## 4. Tag 判定：暂缓 `m3-core-features`

| 阻塞项 | 性质 | 清除条件 | 清除动作 |
|---|---|---|---|
| R19 真实声学 | 数据缺口 | 6 样本到位 | `audio_regression.py` 产出表1 + 定默认 |
| G1 生成答案（云端） | ~~环境+网络+凭据~~ → **P13 已闭**（openai 合成总账 5502.3ms ≤ 6s；local-first 默认不变） | 网络可达 + key / 或 Ollama | 跑联测脚本落数 |
| 平台真机 | 环境 | macOS/Linux 机器 | 补实测数字 |
| 前端目检 | 环境 | Tauri 壳 | 逐项目检 + 截图 |

**建议**：M3 的**代码与合成链路**已完整且全绿（pytest 393 / cargo 160 / clippy/fmt 0），若认可「真机数据、跨平台、云端联调属后续里程碑」，可由用户明确指示打 tag 覆盖本判定（M1/M2 同款程序）。

**本报告不自行改判 tag**：R19 是 PRD 明写验收项，缺真实数据就不算通过。

## 5. 诚实记录

1. **前端三件套已补跑（P12，2026-09-16，B 机本 shell，node v22.22.2）**：`vitest run --reporter=verbose` **167 passed / 12 files**（per-file 见 §8；原“highlight 6 + liveqa +1 未跑”已过期——实测 `highlight.test.ts` 为 **8** 用例、`liveqa.test.ts` 为 **30**，后者与既有 159 台账内计数一致）；`tsc --noEmit` **0**（初跑 2 错误，系 P5 遗留 `useEmbedding.ts` 的 `refetchInterval` 首参误写成 `data`——React Query v5 该回调首参是 `Query`，运行时轮询永不触发；已修为 `(query) => query.state.data…`，行为变更即轮询恢复，见 §8）；`eslint src tests/e2e --max-warnings=0` **0**。若 taskP7 会话有 node 且顺带覆盖，以其记录为准——现以本轮实测为准。
2. **endpoint_reference.py 子进程调用 python**：`inject_e2e` 两测依赖 python 可执行文件，本机需把 venv `Scripts` 加入 PATH 才跑通（已验证）。
3. **P1 Top-3 0.49 仍未达标**：实现已落地并可复现（`eval_calibrate.py`），指标达标属 D6 查询理解 + 重标定范畴，非当场可修，已记待办（M1 §1#6）。
4. **云端 key 纪律**：主人会话中曾明文提供 OpenAI key，**本轮未实测、未落盘任何文件**；若本会话记录会被分享/归档，该 key 应先轮换。

## 6. 基线快照（阈值全默认，未改）

真题集（59 题：49 scored + 10 null）：Top-3/5=0.490；direct=0.051；Fail-Closed=0.932；null 题 FC=1.000（10/10）；零答错（25 miss 全为空池拒答）。
延迟：field 0.11 / fts 9.44 / vec 2.53 / embed 2.44 / fuse 0.02ms。详见 `docs/eval-baseline.md`。

## 7. 延期台账更新（2026-09-15 立；本节是延期项的**唯一权威**）

> 与 M2 §7 合并，去重后统一编号。

### 7.1 M2→M3 合并四项（全部非「缺代码」，本机不可推进）

| 项 | 性质 | 阻塞于谁 | 解除条件（可判定） | 解除后第一件事 | 立账日 |
|---|---|---|---|---|---|
| **R19 真实声学边界** | 数据缺口 | 主人投喂 | 6 槽非空 + 能跑出对比表1 | `audio_regression.py` 出表1 + 复核 + 定默认 | 2026-09-14 |
| **平台真机** | 环境 | 需 macOS/Linux | 两台机器各跑全套 + 双路 ts | 补 §2 门槛表 | 2026-09-14 |
| **前端目检** | 环境 | 需 Tauri 壳 | 壳可启动 + 面板可见 | 逐项走查 + 截图归档 | 2026-09-14 |

### 7.2 1b-HK 残项（Phase 1b 收尾遗留）

| 项 | 现状 | 解除条件 | 解除后第一件事 | 立账日 |
|---|---|---|---|---|
| **`dist-sidecar/` 重建** | 仍是 task-10 旧包（已批准重建） | 本机批量删除守卫允许时 | 按 `benchmark.md` 口径重建，核对 `requirements-lock.txt` | 2026-09-15 |
| **CI Python 3.11 matrix** | PRD 要求 3.11；本机 3.12/3.13，**未在 3.11 验证** | CI 配出 3.11 job | 跑全量 pytest + pip check，并列记入测试计数台账 | 2026-09-15 |

### 7.3 已闭合（移出台账，留痕防复活）

- **G1 生成答案 ≤6s（云端）** → 2026-09-16 闭合（P13：openai 合成总账 5502.3ms ≤ 6s + P4 首字台账 openai 行关闭 + P5 cloud 台账关闭；local-first 默认不变）。

- **M2-7 端点状态机缺件** → 2026-09-15 闭合（`endpoint.rs` 落地，边界误差 0 帧，M2 §1#7）。
- **`cargo fmt --check` 4 文件失败** → 2026-09-15 闭合（`c4c682a` 单独 chore，全仓 0）。
- **task-14 「错 token → 1008」偏差** → 主人接受 403 为准（2026-09-15），PRD 措辞挂回仓时更新，`api-contract.md` 已记实测。

## 8. P12 增补记（2026-09-16，B 机本 shell）

> 本节是 M3 报告在 P12 任务中的增补入口：P6 收口结果（#1 证据更新）、row6/8 前端证据改实测、row7 勘误。旧节原文一律保留，只加“P12 增补/勘误”注记，不改写历史结论。

### 8.1 前端三件套实测（row6/row8 证据来源）

- `npx.cmd vitest run --reporter=verbose` → **167 passed / 12 files**（exit 0，24.29s）。
- per-file（逐文件枚举，以此为准；P12 预期“159 + highlight 6 + liveqa 1”作废）：
  trigger 50 / liveqa 30 / capture 28 / importFlow 15 / api 7 /
  ReviewPanel 6 / EmbeddingConfig 6 / ColumnMapping 5 / Knowledge 4 / windowView 4 / sse 4 /
  **highlight 8** = **167**。即 167 − 159 = 8 全部来自 `highlight.test.ts`；
  `liveqa.test.ts` 的 30 与既有 159 台账内计数一致（M3 §5.1 原“liveqa +1 未跑”过期），
  `highlight.test.ts` 实测为 8（原记 6 不确）。
- `npx.cmd tsc --noEmit` → **0**（初跑 2 错误，修后 0，见 8.3）。
- `npx.cmd eslint src tests/e2e --max-warnings=0` → **0**。
- `pytest sidecar/tests -q`（`NO_PROXY=127.0.0.1,localhost`，默认 python 3.13.14）→
  **396 passed / 1 skipped**（91.20s；1 skipped 同源 `webrtcvad` 可选依赖，与既有台账同源）。

### 8.2 P6 收口结果（#1 证据更新）

- 评测集冻结：`questions_100.jsonl` 100 题 = 87 scored + 13 null（13%，硬约束 10–15）。
- 定稿常量（`hybrid_rank.py` / `vector_search.py` / `field_lookup.py`；PRD §3.3 已同步）：
  `W=(1.5, 3.0, 1.0, 4.0)` Σw=9.5 / `TH_DIRECT=0.65` / `TH_MAYBE=0.60` / `GAP=0.15` /
  `LINK_MODE=entity_scaled` / `DIST_CUTOFF=0.8` / `CONTAINMENT_MIN_LEN=2` /
  `CONTAINMENT_IN_ALIAS=false`。
- 终值（`eval_calibrate.py --source`）：Top-3 **0.9080**（≥0.85 ✅）/ null **13/13** ✅ /
  direct 51对5错（与初值持平，未越红线 ✅）/ 答错 **6**（初值 18）/
  非 null 题 FC 率 0.3218（与初值持平，未劣化 ✅）。
- 五步对比 + 两个副作用（向量路缺席天花板 0.579 / 纯字段直查 0.158，均已修并钉测试）见
  `docs/eval-final.md`。已知盲区：100 题 **0 道纯字段直查题**，补题后需重跑五步。

### 8.3 顺带修（P5 遗留，HEAD 全绿所必需）

- `src/hooks/useEmbedding.ts` 两处 `refetchInterval: (data) => …` → `(query) => query.state.data…`。
  根因：所装 React Query v5（`^5.102.8`）该回调首参是 `Query` 实例（tsc 错误原文为证），
  旧写法 tsc 报 2 处 TS2339，且运行时 `query.rebuilding` 恒 `undefined` → 轮询永不触发
  （重建进度条不会自己动，也不崩）。此 bug 在 2026-09-15 已被发现并记入 memory，
  当时“改的方向对、落点错了”（写成 `(data)`），本轮彻底修掉，tsc 2 → 0。
  归属：P5 遗留，随 P8 提交落（P8 Settings 降级/Embedding 区消费该 hook），此处留痕。

### 8.4 row7 裁定（双格式；PRD 无定义）

- 仓内 PRD 裁定：`git grep -n "F4\.5\|F11\.6"` 全仓仅命中 `docs/PROGRESS.md:1165`（P9 条目标题自称），
  `git grep "F4\.[0-9]"` 除 pnpm-lock 哈希外零命中；`README.md` 注“`docs/PRD.md`（v1.5，按任务约定落仓；
  本骨架任务暂未拷入，待后续任务补）”——**PRD 不在仓内，F4.5/F11.6 无权威定义**。
- 代码事实：`exporter.py` 模块注释明写两条通道（JSON 忠实 / MD 回放），
  `test_export_import.py` 15 用例（§8.1 口径：JSON 往返×2/确定性/分块流式/MD 稳定/截断锁定/
  缺列容错/覆盖语义/坏快照/列锁定/CLI×3/构建脚本/同库碰撞）**无一涉及 Excel/PDF 导出**；
  Excel/PDF 是导入方向（P1/P2）。故 row7 按**双格式**勘误（JSON/MD + CLI）。
- 真缺口条件：PRD 回仓后若 F4.5/F11.6 确有“导出 Excel/PDF”要求，则为真缺口、开新项；
  在此之前不估分、不开项。

### 8.5 1b-HK 残项复核（本轮）

- `dist-sidecar/`：仍是 task-10 旧包（与 §7.2 一致）。重建需清旧包大量文件，
  触发本机批量删除守卫（~50 文件/回合），时间盒内未执行——仍挂账，关闭条件不变
  （守卫允许时按 `benchmark.md` 口径重建，核对 `requirements-lock.txt`）。
- CI Python 3.11：`.github/workflows/ci.yml` 已全 job 配 `PYTHON_VERSION: "3.11"`
  （lint/pytest/vitest/build-tauri/size-regression 均用之），“配出 3.11 job”在 CI 侧已满足；
  本地 `C:\tmp\py311\python.exe`（3.11.9）无 pytest 且全量 lock 含 torch 系重依赖，
  时间盒内未装——本地 3.11 实测仍缺，关闭条件：CI 配 matrix 或本地 py311 跑全量
  `pytest` + `pip check` 并列记入台账。

---

**文件清单（供 G0 切分）**：
- 新增：`docs/acceptance-m3.md`、`docs/research-sck.md`、`sidecar/tests/test_highlight.py`、`src/lib/highlight.ts(+test)`、`sidecar/src/diagnostics/degrade.py`、`sidecar/tests/test_diagnostics_degrade.py`、`sidecar/src/knowledge/exporter.py`、`cli/{__init__,__main__,commands}.py`、`scripts/build-cli-win.ps1`、`sidecar/tests/test_export_import.py`、`scripts/eval_calibrate.py`、`docs/archive/PROGRESS-2026-09-14.md`。
- 改动：`sidecar/src/retrieval/hybrid_rank.py`、`sidecar/src/embedding/embedder.py`、`sidecar/src/generation/router.py`、`sidecar/src/routers/{knowledge,qa,settings}.py`、`sidecar/src/knowledge/{compiler,validator,field_extractor,stores,parsers/*}.py`、`sidecar/src/retrieval/{fts5_search,field_lookup}.py`、`src/lib/{qa,liveqa,sse,trigger,capture,highlight,api,importFlow,windowView}.ts`、`src/pages/{Search,Settings,LiveQA,Knowledge,TeleprompterWindow}.tsx`、`docs/{api-contract,benchmark,PROGRESS,eval-baseline}.md`、`src-tauri/src/{audio,manager,endpoint,service,settings,protocol,push,fallback,keychain,degradation}.rs`。
- 与他任务交错：`PROGRESS.md` 全文、`docs/acceptance-m2.md`（§7 合并）。