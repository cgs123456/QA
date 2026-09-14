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

`WS /audio/stream`（Authorization Header 鉴权，无 token → code=1008）。
本阶段无实现，契约见 PRD §3.4。
