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
`no_provider`（未配置 ASR）、`interrupted`（同 path 重复 start 顶掉旧段）、
`dropped_overload`（见下）。

### 背压与日志（R13/R14）

- 有界：每连接待转写 ≤8（超则本段丢弃 + `dropped_overload` + 计数）；
  单段缓冲 ≤32MB（超则自动封段转写部分）；接收循环永不 await 转写
  （fire-and-forget + 完成计数）；下行发送串行锁 + 5s 超时，超时/断开即清理。
- content-free：音频模块零打印；转写文本只进下行 JSON（单测以 capsys 锁定）。
