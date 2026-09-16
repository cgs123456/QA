# Sidecar API 契约（PRD §3.4 落地版）

> 本文档是代码的契约视图；冲突时以实现为准并同步回本文档。
> 鉴权（R2）：除 `GET /health` 外全部端点要求
> `Authorization: Bearer <token>`（`secrets.compare_digest`），失败 401。
> Token 经 stdout 握手一次性传递，仅内存持有（R8）。

## 握手（stdout，flush=True， exactly one line）

```json
{"protocol_version":"1.0","port":54321,"auth_token":"...","capabilities":["health","qa"],"models_loaded":[]}
```

Tauri 校验 `protocol_version`，不匹配 → `sidecar://degraded`（version_mismatch），不发请求。

## 端点

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/health` | 无 | `{"status":"ok","version","nonce"}`（nonce 为本次启动随机值，未启动/直连 app 对象时为 null；防端口复用占位者） |
| GET | `/sidecar/info` | 是 | `{"version","capabilities"}`（鉴权闭环证明） |
| POST | `/knowledge/compile` | 是 | `{store_id?,store_name?,format,content,filename?}` → `{store_id,stats}`；400 非法输入/JSON 失败；404 store 不存在 |
| POST | `/knowledge/import/preview` | 是 | P1 dry-run：`{format:excel\|pdf,content_b64,filename?}` → Excel 逐 sheet 列映射提案（含每列样例 3 行）/ PDF 条目预览；不写库；400 坏文件/超限；422 扫描件（`{"error":"unsupported","reason":"scanned","message",pages}`，明确拒绝不静默） |
| POST | `/knowledge/import/commit` | 是 | P1 确认导入：`{store_id?,store_name?,format,content_b64,filename?,mapping?}`（Excel mapping 必填，PDF 仅允许 `{"entity"}` 或省略）→ 服务端重解码/重解析/严格校验映射后短事务编译，返回同 compile 形状 `{store_id,stats}`（PDF 另有 `pdf_unsupported_pages`）；400 映射非法/空导入；422 扫描件；404 store 不存在 |
| GET | `/knowledge/list` | 是 | stores 列表（含 qa/field 计数与 is_current） |
| GET | `/knowledge/search` | 是 | `?q=&store_id?`（缺省当前库）→ `{store_id,query,field_hits,fts_hits,timings_ms}`；fts_hits 每行附 `hl_question`/`hl_answer`（F2.3 全列打标片段，标记串固定 `<mark>`，缺席=该行打标失败、调用方回落原文；XSS 转义是前端职责，见 `src/lib/highlight.ts`） |
| POST | `/store` | 是 | `{name,template_id?}` → store 详情；400 空名 |
| GET | `/store/{id}` | 是 | 详情 + 计数；404 |
| DELETE | `/store/{id}` | 是 | 级联清理（含 vec 手工清）；404 |
| PUT | `/stores/current` | 是 | `{store_id}` 单选切换；404 |
| POST | `/settings/llm-secret` | 是 | `{provider,api_key}` → `{"stored":provider}`；不回显 key；400 空值（F6.1 全量：claude/gemini/groq 复用同通道，secret_slot 即 provider 名） |
| GET | `/llm/providers` | 是 | LLM 目录快照（F6.1 设置页 provider 下拉渲染）→ `{"providers":[{name,display,needs_key,secret_slot,default_model,base_url,stream,note}]}`；内容无关，无 key |
| GET | `/embedding/provider` | 是 | 向量配置快照（F5.3 设置页 EmbeddingConfig 初始渲染）→ `{"active","dim","table","rebuilding":null\|{rebuild_id,status,from,to,total,done,tokens,error},"available":[{name,display,kind,dim,table,needs_key,note,ready}]}`；内容无关，无 key、无向量字节 |
| POST | `/embedding/provider` | 是 | `{provider}` 切换向量检索：同名→200 空操作；未知→400；在途重建→409；不可达（本地缺权重/云端缺 key/live 探测失败）→409 `{"error": kind}` 且保持原选择与旧表不变；可达→200 起后台重建并回 `{"active"(仍旧),"rebuilding":{…queued}}`，完成才原子翻转 |
| GET | `/embedding/rebuild/{id}` | 是 | 重建进度轮询（downloader 同形）→ `{"rebuild_id","status","from","to","total","done","tokens","error"}`；未知/过期 id→404 |
| POST | `/qa/ask` | 是 | `{question,store_id?,provider?,fallbacks?}` → `{"task_id"}`；400 空问/无当前库；404 store 不存在（P8：`fallbacks` 为备选序列，首选失败按序尝试；省略即单 provider） |
| GET | `/qa/stream?task_id=` | 是 | SSE（见下）；未知/过期 task → 404 |
| POST | `/model/download` | 是 | `{model?}`（缺省 bge-small-zh-v1.5；ASR 权重传 `sense-voice` / `paraformer-zh` / `faster-whisper-base`）→ `{"download_id"}`；400 未知模型 |
| GET | `/model/download/{id}` | 是 | `{"download_id","status","files":{name:{"downloaded","total","done"}},"error"}`；未知 id → 404 |
| GET | `/asr/providers` | 是 | ASR 目录 + 当前选择（F6.2 设置页初始渲染）→ `{selected,spec,ready,with_chain,available[]}`；entry 含 `name/display/kind/model/note/ready`，内容无关（R14） |
| PUT | `/asr/provider` | 是 | `{name}` 切换 provider，**即时生效**（下一段音频起用，无需重启；在途段仍用旧引用）；未知名 → 400；构造失败（如云端缺 key）→ 409 `{"error": kind}` 且保持原选择不变 |
| GET | `/asr/latency` | 是 | 低延迟模式能力快照（task18，设置页开关渲染）→ `{enabled,cuda,note}`，内容无关 |
| PUT | `/asr/latency` | 是 | `{enabled}` 开/关低延迟模式，只影响**下一段**；开启要求 CUDA 就绪，否则 409 `{"error":"unavailable"}`；关闭永远允许 |
| GET | `/diagnostics/audio` | 是 | 最近一次音频 WS 连接的内容无关计数 + 限额（task19 诊断面板）→ `{last_connection|null,limits,capture:{status:pending}}`；从未建连是合法 null，不是 404 |
| GET | `/diagnostics/degrade` | 是 | 三链降级计数（P8，R14 内容无关）→ `{chains:{asr\|llm\|embedding:{total,by_kind,by_provider}}}`；sidecar 重启清零 |
| POST | `/session/begin` | 是 | 开一次会话（S2）：**幂等** —— 已在会话中不新建、不重复写 `session_begin`，回同一个 id。→ `{session_id,already,counters}` |
| POST | `/session/end` | 是 | 关当前会话：`{session_id?,closed,counters}`；没有会话 → `closed=false`（幂等，不报错）；`session_id` 不匹配则**不关别人的会话**（`end_mismatch`） |
| GET | `/session/status` | 是 | 当前会话 + 计数 + 每类账行的计数与**字段名**：`{session_id,source,counters,event_counts,event_fields}`；R14 内容无关（`event_fields` 只回 key，不回 value） |
| POST | `/session/event` | 是 | 记一次链路事实：`{event_type,stage?,metadata?}` → `{recorded,session_id}`；无会话或类型不在白名单 → `recorded=false` + `late_events_rejected` |
| GET | `/sessions/settings` | 是 | 录制设置 → `{enabled,retention_days,updated_ms}`；`retention_days=null` = 永久。**文件不存在时回保守默认**（`enabled=false`），不报错 |
| PUT | `/sessions/settings` | 是 | 改录制设置（只改传了的字段）；`retention_days ∉ {null,7,30}` → 400 |
| GET | `/sessions/usage` | 是 | 会话数据量 → `{sessions,events,payload_bytes,oldest_ms,newest_ms,active_session_id}` |
| POST | `/sessions/purge` | 是 | 清除**全部**会话账行（物理删除）；`confirm≠true` → 400；有活动会话 → 409 |
| GET | `/session/{id}/export` | 是 | 单会话导出 → `{filename,content}`。**只有 JSON**：MD 渲染器不存在，按裁定不新建 |
| GET | `/sessions` | 是 | 会话列表（S1）：**时间倒序** + 分页 `?limit=&offset=`（limit 1–200，越界 422；offset 越界回空页不报错）→ `{sessions[],total,limit,offset}`；每项 `{session_id,started_ms,last_ms,events,store_id,closed}`。排序键是 `MIN(ts_ms)` 不是行 id（回填的旧行 id 可能更小） |
| GET | `/session/{id}` | 是 | 一次会话的**完整时间线**（S1）：按行 id 升序（同毫秒也定序）→ `{session_id,events[],count}`；每条含 `{id,event_type,stage,ts_ms,store_id,metadata}`；未知 id → **404**（不回空列表）。**只读**：不写任何行 |
| DELETE | `/session/{id}` | 是 | **物理删除**该会话全部账行（S1）→ `{session_id,deleted}`；未知 id → 404；目标是**当前正开着的会话** → 409 `{"error":"session_active"}`（先 `/session/end`） |
| GET | `/rehearsal/categories` | 是 | 当前库的类目分布（S5，供陪练下拉）→ `{store_id,categories:[{category,count}],total}`；类目取自**库里实际取值**，NULL 归入 `(未分类)`；无当前库 → 400 |
| POST | `/rehearsal/draw` | 是 | 抽题（S5）：`{store_id?,category?,n=10,seed?}` → `{store_id,questions[],drawn,pool,seed}`；**无放回**；`n` 越界（1–50）→ 400；`n` 超过池子 → 抽满并如实回 `drawn`；**`seed` 给定即确定性**（同 seed 同顺序） |
| POST | `/rehearsal/verdict` | 是 | 记一次自评（S5）：`{qa_id,category?,verdict,store_id?,question_text?,official_answer?,user_answer?}`，`verdict ∈ {correct,partial,unknown}` → 其它值 400 → `{recorded,session_id}`；无会话/录制关闭 → `recorded=false`（**不是错误**）；账行含文本（question_text/official_answer/user_answer），应用层日志无文本（R14） |

## Tauri 命令（不走 sidecar HTTP）

> 手机伴侣（S8）**不是 sidecar 端点**：监听方是 Tauri 主进程里的 Rust
> `companion` 模块，直连局域网里的手机浏览器。安全边界见
> `docs/companion-security.md`。

| 命令 | 入参 | 返回 | 说明 |
|---|---|---|---|
| `start_companion` | — | `CompanionStart` | 只绑 **RFC1918 局域网 IPv4** `:54322` 并生成 token；返回 `{info, qrSvg}`，`qrSvg` 编码的是 `http://<lan_ip>:54322/?token=<token>`。**挑不到局域网地址就报错**（fail-closed） |
| `stop_companion` | — | `CompanionInfo` | 关监听 + 断开全部手机（先发 `revoked` 再 `Close(4001)`）+ 清 token |
| `get_companion_status` | — | `CompanionInfo` | `{state: stopped\|listening\|connected, lanIp, port, clients[{id,sinceMs}]}`；**不含 token** |
| `revoke_companion_client` | `clientId` | `bool` | 吊销单台设备；服务继续监听（吊销 ≠ 停止） |
| `rotate_companion_token` | — | `CompanionStart` | 换 token 出新二维码，旧连接全部失效 |
| `broadcast_to_companion` | `cards` | `null` | 推卡片给已连手机；载荷与 `teleprompter://cards` 完全一致（`LiveCard`，字段 `atMs`）；无连接时是 no-op |

类型与帧格式（含 `protocolVersion`、关闭码 4001）见 `docs/companion-security.md` §4。

## SSE（GET /qa/stream，禁缓冲头）

响应头：`Content-Type: text/event-stream`、`Cache-Control: no-cache`、
`X-Accel-Buffering: no`。帧格式 `data: {...}\n\n`，顺序固定：

1. `{"type":"retrieval","action","top1_score","warnings","store_id","sources"}`（恒先到达；
   direct/fail 的 sources 取自结果；action 含 direct/maybe_*/fail_closed/error）
2. `{"type":"generation","chunk":...}` × N（仅 maybe 档）
3. `{"type":"done","result":{"type","text","sources","llm_calls","provider","degraded",...}}`（P8：`provider` 为实际出力的 LLM（direct/fail_closed/error 为 null），`degraded` 为 `["备选:kind",...]` 逐级失败摘要，复用 asr_final degraded 模式；Fail-Closed 不进链，恒为 null/[]）

task 在读毕或 TTL 60s 后清理；客户端断开取消后台任务。

## 会话账（S2：`session_events`）

**会话 = 一次采集启停**。Rust 侧 `CaptureService::start()` 发 `POST /session/begin`、
`stop()` 发 `POST /session/end`（token 走既有 Bearer 通道，**不开新通道**；
音频 WS 契约一字未改）。会话存在期间链路上的事实以 **content-free** 形式落
`session_events`（R14：应用层日志无文本），但 **rehearsal 类事件的 metadata 含文本**
（question_text/official_answer/user_answer），以便导出/回放还原语境。落行者一律是
sidecar（调用点是 `routers/audio.py` 与 `routers/qa.py`、`routers/rehearsal.py`），
落行入口只有一个：`routers/session.py::record_event()`。

| event_type | stage | metadata |
|---|---|---|
| `session_begin` | 采集源（`capture`） | `session_id` |
| `session_end` | — | `session_id`、`duration_ms` |
| `asr_final` | 路（`loopback`/`mic`） | `session_id`、`segment_id`、`duration_ms`、`provider`、`degraded_levels`、`dropped_oldest` |
| `asr_error` | 路 | `session_id`、`segment_id`、`error`、`degraded_levels` |
| `qa_exchange` | `qa` | `session_id`、`action`、`provider`、`llm_calls`、`sources`、`warnings`、`top1_score`、`store_id`、`elapsed_ms` |
| `rehearsal` | `rehearsal` | `session_id`、`store_id`、`verdict`、`category`、`qa_id`、`question_text`、`official_answer`、`user_answer` |

**列与 metadata 双写（S1 ↔ S2 交接）**：v002（`v002_session_timeline.py`）把
会话归属与写入时刻提成了列 `session_id` / `store_id` / `ts_ms`，时间线索引为
`COALESCE(session_id, json_extract(metadata,'$.session_id'))`。写入方（`log_event`
→ `record_event`）**同时**填列与 metadata：列给索引（不走 JSON 提取），
metadata 让一行自解释。故新行走"列命中"、旧行（仅 metadata）走回退，**同一个索引**。
`ts_ms` 是**写入时刻**的 epoch 毫秒，段内相对时间在 `metadata` 里，两者别混。

**录制开关（S4 / F11.3 用户面）**：`POST /session/begin` 受 `enabled` 门禁 ——
关着时返回 `{"session_id": null, "already": false, "recording": false, counters}`，
**不建会话、不落任何行、不计 `late_events_rejected`**（那两条讲的是"有会话时的迟到"，
这里连会话都没有）。Rust 侧据此记为 `SessionStats.disabled_skips`，**不**算降级。
保留策略的到期清理跑在 sidecar 启动时（`main.init_storage`），幂等，跳过活动会话。

三条裁定（任务书未定处）：

1. **不可达 → 放弃 + 计数，不缓冲重放。** Rust 侧只加 `begin_misses` /
   `end_misses`（`SessionStats`），本地不排队不补发；**失败不回滚采集** ——
   采到音频却因为"记账没成功"而拒绝开流，是把观测面当成了依赖。
   代价如实记录：那一段音频不属于任何会话。
2. **幂等放在对端。** 重复 begin 不新建会话、不重复写 `session_begin`，
   只计 `begin_duplicate` 并回同一个 id。客户端崩了就没机会去重，
   而对端是唯一知道"当前会话是谁"的一方。
3. **end 之后的迟到事件一律拒收**（计 `late_events_rejected`，不落库）。

### 读与删（S1）

写路径一律 **best-effort**（失败只计数、不穿透主链路）；读路径**相反** ——
调用方明确来要数据，拿不到就该报错（库故障 → 500），而不是回空列表让人
以为"这段时间没发生过事"。三条读侧裁定：

1. **回放只读。** 两个 GET 不写任何行 —— 包括"顺手补一行统计 / 标记已读"。
   回放必须可重复：跑两次结果相同（`test_read_endpoints_never_write` 钉住）。
2. **找不到就 404。** 会话 id 由对端发下来，查不到就是查不到（拼错 / 库被清过 /
   指向了别的库）。回 200 + 空列表会把这三种情况全部伪装成"这次会话什么都没发生"。
3. **删除是物理的**，不是软删、不是打标记 —— 隐私主张要求"用户说删就真的没了"，
   软删会在库文件里留下可恢复的残影。**正在开的会话拒绝删（409）**：
   删了会留下半截账，且下一次 event 立刻把它写回来，比拒绝更糟。

**R21（会话账不进检索路径）**：`session_events` 不建任何 FTS/vec 虚拟表，
`src/retrieval/**` 与 `/knowledge/search` 的源码里 `session_events` 零命中。
四道锁见 `sidecar/tests/test_session_not_searchable.py`（含"灌 500 行账后
检索结果逐字不变"的行为断言）。
   否则"一次会话有多少段"这个断言就失去意义 —— 下一会话的段会混进上一会话。
   Rust 侧同一规则由**世代号**守：停止之后到达的 begin 回执不许把状态改回"已开"
   （`stale_results_rejected`）。

## 模型下载进度通道（落地裁定）

PRD 只定义 POST 触发；进度轮询约定如下：`status ∈ queued/downloading/done/error`；
`files[name] = {downloaded,total,done}`；`error` 仅失败时有值（种类文本，无敏感内容）。
下载在后台线程跑（不断事件循环），断点续传 + SHA256（见 `models/downloader.py`）。
ASR 权重（`sense-voice` / `paraformer-zh`）走同一通道：续传/校验/进度语义不变，
前端按模型键分别发起、分别轮询。

## WS（Phase 1b，预留未实现）

## WS（Phase 1b，`WS /audio/stream`）

鉴权（R9）：`Authorization: Bearer <token>` Header（accept 之前校验），
缺失/错误 → close code=1008。禁止 URL query 传 token。

> **线上实测（task-14，真实 uvicorn）**：`accept` 之前 `close()` 在真实 ASGI 服务器上
> 表现为 **HTTP 403**（握手不完成，客户端看不到 WS close 帧）；`code=1008` 只在
> FastAPI `TestClient`（进程内直连 ASGI）下可见。两种形态都被视为"token 被拒"：
> Rust 客户端 `UplinkError::Unauthorized{status:401|403}` 覆盖前者，
> `UplinkStats::close_code=Some(1008)` 覆盖后者。
> 另：uvicorn `ws="auto"` 缺少 `websockets`/`wsproto` 时**不降级**，而是对本端点
> 返回 404（音频链路整体失效）——依赖与打包收集见 `sidecar/pyproject.toml`
> 与 `sidecar/build.py`。

### 上行帧（Rust → sidecar，二进制，R10）

帧头 `[1B type][2B seq_le][4B timestamp_ms_le]`：
- `type=0x00` 音频：负载 float32 LE ×480 samples（1920B），归一化 [-1,1]，
  整帧 7+1920=1927B；长度不对 → 忽略 + 计数。
- `type=0x01` 事件：负载 UTF-8 JSON（`segment_start`/`segment_end`/`vad_state`，
  格式见 PRD §3.5，path 须为 loopback|mic）。

落地裁定（线格式不变）：**一连接承载一路**，path 由本连接首个 `segment_start`
确立并强制（冲突忽略 + 计数）；**seq 全帧共享一序号空间**（每路独立计数，
音频/事件统一编号，跳号计数 + 重同步）；`segment_id` 由 sidecar 按连接分配
（`seg_{n}`）；同 path 重复 start 则旧段 interrupted（asr_error）。

Rust 生产端（1b-3 落地）：`audio::frame::encode_ws_frame(seq, ts_ms, &pcm_f32)` 直出上述
1927B；`seq` 每帧自增（内部 u32，线上按契约截断为 u16 回绕），`ts_ms` = 帧序号 × 30
（**样本计数派生，非挂钟**）。采集侧 `audio::loopback::CapturePipeline` 已保证 seq/ts
单调、样本守恒，且设备切换只换 resampler、不重启序号空间。

### 下行消息（sidecar → Rust/前端，同连接 JSON，R12）

`asr_start / asr_partial（默认不发；低延迟模式开启时按 task18 下发）/ asr_final / asr_error`，
必含 `segment_id` / `path` / `ts_ms`；partial 另含 `text`（至今确认的前缀，全量替换语义，
**与本段 final 同一 `segment_id`**）；final 另含 `text` + `duration_ms`
（起止取事件时间戳，非挂钟）；error 的 `error` 取值：
`provider_timeout|provider_error|all_providers_failed`（ provider 故障）、
`unavailable`（provider 不可用：权重未就位 / 依赖未装）、
`manual_input_required`（降级链全灭 → 前端切手动输入，task15）、
`no_provider`（未配置 ASR）、`interrupted`（同 path 重复 start 顶掉旧段）、
`dropped_overload`、`segment_truncated`（见下）。

**task15 扩充**：`asr_final` 增 `provider`（**实际出力**的那一级名，如
`faster-whisper` / `sensevoice` / `paraformer` / `local-backup` / `cloud-rest`）；发生过降级时另增
`degraded`，为 `["provider:error_kind", ...]` 的逐级失败摘要。
`asr_error` 在降级链全灭时同样带 `degraded`。两者均内容无关（R14）。

**`asr_start` 语义（task15 裁定）**：在 **segment_start（缓冲开启）** 下发，
即「本段 ASR 开始处理」。转写实际发生在 segment_end；若把 `asr_start` 挪到彼时，
前端在整段说话期间收不到任何"已开始"信号，且需改本契约 + Rust `asr://start` 映射
+ 既有 e2e 断言。故保持现语义不变（task15 原文「转写开始 → asr_start」按此理解）。


### 背压与日志（R13/R14）

- 有界：每连接待转写 ≤8（超则本段丢弃 + `dropped_overload` + 计数）；
  单段缓冲 ≤32MB（超则自动封段转写部分）；接收循环永不 await 转写
  （fire-and-forget + 完成计数）；下行发送串行锁 + 5s 超时，超时/断开即清理。
- content-free：音频模块零打印；转写文本只进下行 JSON（单测以 capsys 锁定）。

## Tauri 事件面（Rust → 前端，task-16）

下行 JSON 由 Rust `audio/uplink.rs::TauriSink` **原样**转发到 Tauri 事件总线，
事件名由 `tauri_event_name()` 映射（kind → `asr://*`）。前端只订阅事件名，不解析 WS。

| 事件名 | 载荷 | 产生处 | 说明 |
|---|---|---|---|
| `asr://start` | 下行 `asr_start` JSON 原样 | `uplink.rs` | 语义见上（segment_start） |
| `asr://partial` | 下行 `asr_partial` JSON（含 `text` 确认前缀，与 final 同 `segment_id`） | `uplink.rs` | 仅低延迟模式开启时 sidecar 下发（task18）；关闭时不发 |
| `asr://final` | 下行 `asr_final` JSON（含 `text`/`path`/`ts_ms`/`duration_ms`/`provider`/`degraded?`） | `uplink.rs` | 提词触发入口 |
| `asr://error` | 下行 `asr_error` JSON | `uplink.rs` | |
| `teleprompter://trigger` | 空（`null`） | `shortcuts.rs` | F1.6 手动提词；前端取最近一段转写，**打断 3s 锁定期** |
| `capture://toggle` | 空（`null`） | `shortcuts.rs`、`tray.rs` | F1.6 采集开关；M2-8（C1b）已接线，消费方是 `commands::audio::spawn_toggle_listener`。**托盘「开始/停止采集」复用同一个事件名**（`tray.rs` 有断言钉住），当前灰置 |
| `capture://state` | `ServiceSnapshot` | `commands/audio.rs` | 采集状态回推；前端据此渲染，不自己猜 |
| `teleprompter://cards` | `LiveCard[]` | 前端 `hooks/useLiveQA.ts`（`broadcast: true`） | **前端 → 前端**（主窗 → 提词窗）。提词窗不跑自己的会话，只渲染这个载荷，避免同一问题发两次检索 |

**契约要点**：

- `asr://final` / `asr://partial` 的载荷含转写正文，属 UI 专用：**不得落日志**（R14）。
  `teleprompter://cards` 同理（它装的就是卡片正文）。
- 快捷键事件只带 `null` 载荷，不带任何上下文 —— 「最近一段转写」由前端的状态机持有。
  理由：Rust 侧不保存转写文本，就不存在「文本被日志/崩溃转储带出」的路径。
- 快捷键绑定：`CmdOrCtrl+Shift+R`（采集开关）、`CmdOrCtrl+Shift+Space`（手动提词）。
  注册失败（被其它程序占用）**不阻断启动**，仅在 stderr 记录失败清单。
- 采集路径矩阵（与 `audio/mod.rs` 头注释一致）：Windows = 系统回环 + 麦克风；
  Linux = 监听 + 麦克风；macOS = **仅麦克风**（UI 必须明示，`captureNotice()`）。

## Tauri 命令面（taskP7：窗口与自启）

| 命令 | 参数 | 返回 | 说明 |
|---|---|---|---|
| `get_overlay_status` | — | `OverlayStatus` | `{label, present, visible, capture_exclusion}`；`capture_exclusion ∈ applied\|best_effort\|unsupported` |
| `set_overlay` | `visible: bool` | `OverlayStatus` | **重建入口**：销毁后再 `visible=true` 走同一段 `Plan::Create` |
| `toggle_overlay` | — | `OverlayStatus` | 真值取自 Rust，不取调用方 |
| `destroy_overlay` | — | `OverlayStatus` | 销毁窗口，主应用不受影响 |
| `get_autostart` | — | `AutostartState` | `{enabled, available, note}`；`available=false` 表示**读数不可信**（不是「已关闭」） |
| `set_autostart` | `enabled: bool` | `AutostartState` | 写失败回 `available=false` + 原因，**不阻断应用** |

**契约要点**：

- 提词窗 label 固定为 `teleprompter`（`stealth::overlay::OVERLAY_LABEL`），
  已在 `capabilities/default.json` 的 `windows` 里授权。
- 提词窗身份靠 Rust 注入的全局变量 `window.__INTERVIEW_COPILOT_VIEW__ = "teleprompter"`
  （`stealth::overlay::init_script`），前端 `lib/windowView.ts` 按它分叉；两处字面量有单测对齐。
- 窗口与捕获排除的边界（做什么、**永远不做什么**）见 `docs/stealth-boundary.md`。
