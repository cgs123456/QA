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
| GET | `/knowledge/list` | 是 | stores 列表（含 qa/field 计数与 is_current） |
| GET | `/knowledge/search` | 是 | `?q=&store_id?`（缺省当前库）→ `{store_id,query,field_hits,fts_hits,timings_ms}` |
| POST | `/store` | 是 | `{name,template_id?}` → store 详情；400 空名 |
| GET | `/store/{id}` | 是 | 详情 + 计数；404 |
| DELETE | `/store/{id}` | 是 | 级联清理（含 vec 手工清）；404 |
| PUT | `/stores/current` | 是 | `{store_id}` 单选切换；404 |
| POST | `/settings/llm-secret` | 是 | `{provider,api_key}` → `{"stored":provider}`；不回显 key；400 空值 |
| POST | `/qa/ask` | 是 | `{question,store_id?,provider?}` → `{"task_id"}`；400 空问/无当前库；404 store 不存在 |
| GET | `/qa/stream?task_id=` | 是 | SSE（见下）；未知/过期 task → 404 |
| POST | `/model/download` | 是 | `{model?}`（缺省 bge-small-zh-v1.5）→ `{"download_id"}`；400 未知模型 |
| GET | `/model/download/{id}` | 是 | `{"download_id","status","files":{name:{"downloaded","total","done"}},"error"}`；未知 id → 404 |

## SSE（GET /qa/stream，禁缓冲头）

响应头：`Content-Type: text/event-stream`、`Cache-Control: no-cache`、
`X-Accel-Buffering: no`。帧格式 `data: {...}\n\n`，顺序固定：

1. `{"type":"retrieval","action","top1_score","warnings","store_id","sources"}`（恒先到达；
   direct/fail 的 sources 取自结果；action 含 direct/maybe_*/fail_closed/error）
2. `{"type":"generation","chunk":...}` × N（仅 maybe 档）
3. `{"type":"done","result":{"type","text","sources","llm_calls",...}}`

task 在读毕或 TTL 60s 后清理；客户端断开取消后台任务。

## 模型下载进度通道（落地裁定）

PRD 只定义 POST 触发；进度轮询约定如下：`status ∈ queued/downloading/done/error`；
`files[name] = {downloaded,total,done}`；`error` 仅失败时有值（种类文本，无敏感内容）。
下载在后台线程跑（不断事件循环），断点续传 + SHA256（见 `models/downloader.py`）。

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

`asr_start / asr_partial（本阶段不发，整段转写）/ asr_final / asr_error`，
必含 `segment_id` / `path` / `ts_ms`；final 另含 `text` + `duration_ms`
（起止取事件时间戳，非挂钟）；error 的 `error` 取值：
`provider_timeout|provider_error|all_providers_failed`（ provider 故障）、
`unavailable`（provider 不可用：权重未就位 / 依赖未装）、
`manual_input_required`（降级链全灭 → 前端切手动输入，task15）、
`no_provider`（未配置 ASR）、`interrupted`（同 path 重复 start 顶掉旧段）、
`dropped_overload`、`segment_truncated`（见下）。

**task15 扩充**：`asr_final` 增 `provider`（**实际出力**的那一级名，如
`faster-whisper` / `local-backup` / `cloud-rest`）；发生过降级时另增
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
| `asr://partial` | 下行 `asr_partial` JSON | `uplink.rs` | 本阶段 sidecar 不发（整段转写） |
| `asr://final` | 下行 `asr_final` JSON（含 `text`/`path`/`ts_ms`/`duration_ms`/`provider`/`degraded?`） | `uplink.rs` | 提词触发入口 |
| `asr://error` | 下行 `asr_error` JSON | `uplink.rs` | |
| `teleprompter://trigger` | 空（`null`） | `shortcuts.rs` | F1.6 手动提词；前端取最近一段转写，**打断 3s 锁定期** |
| `capture://toggle` | 空（`null`） | `shortcuts.rs` | F1.6 采集开关；**采集服务未接线**，前端仅显示提示 |

**契约要点**：

- `asr://final` 的载荷含转写正文，属 UI 专用：**不得落日志**（R14）。
- 快捷键事件只带 `null` 载荷，不带任何上下文 —— 「最近一段转写」由前端的状态机持有。
  理由：Rust 侧不保存转写文本，就不存在「文本被日志/崩溃转储带出」的路径。
- 快捷键绑定：`CmdOrCtrl+Shift+R`（采集开关）、`CmdOrCtrl+Shift+Space`（手动提词）。
  注册失败（被其它程序占用）**不阻断启动**，仅在 stderr 记录失败清单。
- 采集路径矩阵（与 `audio/mod.rs` 头注释一致）：Windows = 系统回环 + 麦克风；
  Linux = 监听 + 麦克风；macOS = **仅麦克风**（UI 必须明示，`captureNotice()`）。
