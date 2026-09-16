# 实测档案：sidecar onedir 打包 + ASR 时延（task-4 建，task-10 / task-15 扩）

> 本文收录两类实测：**打包体积与启动**（第 1–5 节，task-4/10）与
> **ASR 时延**（末节，task-15）。两者口径不同，勿混用。

> 打包范围：sidecar-only 体积与启动。整包 Standard / Full 两档待 Tauri 壳联调后定（D4）。

## 方法

```sh
python sidecar/build.py                       # PyInstaller onedir
python scripts/verify-packaged.py             # §3.13 四条校验（cwd=C:\Windows\System32）
```

## 体积（onedir 总和）

| 项 | 值 |
|---|---|
| `dist-sidecar/interviewcopilot-sidecar` 总计 | **200,060,425 bytes（190.8 MiB，两次运行一致）** |
| TOP1 `interviewcopilot-sidecar.exe` | 30.92 MiB（含 transformers/numpy 纯代码） |
| TOP2 `_internal/numpy.libs/libscipy_openblas64…dll` | 19.64 MiB |
| TOP3 `_internal/onnxruntime/capi/*_pybind11_state.pyd` | 18.23 MiB |
| TOP4 `_internal/onnxruntime/capi/onnxruntime.dll` | 17.59 MiB |
| TOP5 `_internal/hf_xet/hf_xet.pyd` | 9.06 MiB |

机器行（体积回归用）：`BUNDLE_BYTES=200060425`（Windows onedir 口径）。

### task-15 重测（2026-09-14，新增 faster-whisper 运行时）

| 项 | 值 |
|---|---|
| 总计 | **332,926,165 bytes（317.5 MiB）** |
| 较 task-10 基线 | **+132,865,740 bytes（+126.7 MiB）** |
| TOP1 `_internal/ctranslate2/ctranslate2.dll` | 56.55 MiB |
| TOP2 `interviewcopilot-sidecar.exe` | 31.44 MiB |
| TOP3 `_internal/numpy.libs/libscipy_openblas64…dll` | 19.64 MiB |
| TOP4 `_internal/av.libs/avcodec-62-…dll` | 18.37 MiB |
| TOP5 `_internal/onnxruntime/capi/*_pybind11_state.pyd` | 18.23 MiB |
| TOP6 `_internal/av.libs/libx265-…dll` | 12.16 MiB |

机器行（新基线）：`BUNDLE_BYTES=332926165`。

增量构成（collect 生效核对）：`ctranslate2` 39 项 **59.4 MiB**、`av`（含 `av.libs`
的 ffmpeg 系 DLL）**33.9 MiB**、`websockets` 72 项仍在（task-14 的 collect 未被破坏）。
**这两项 collect 必须留**：ctranslate2 的原生 DLL 按名字加载、`av` 是
`faster_whisper/audio.py` 的顶层 import，静态分析都看不到，漏了会「装得上但跑不了」。

**优化候选（记录，不做）**：`av.libs` 的 ffmpeg 系 DLL（avcodec/libx265 等 ~30 MiB）
本管线用不到——我们只把 numpy 数组喂给 `model.transcribe`，不经过 `decode_audio`。
裁掉需替换 `faster_whisper/audio.py` 的顶层 import 或做 exclude，列入后续体积优化。

> 口径注记：本次复测在**项目外临时目录**完成。原 workpath 无法重建——
> PyInstaller `--clean` 要清理 `build-sidecar/` 下 211 个中间文件，
> 被本机**批量删除守卫**（阈值 50 文件/回合）拦截，非沙箱层可解。
> 故 `dist-sidecar/` 内目前仍是 task-10 的旧包（190.8 MiB，**不含** ctranslate2/av）；
> 换机器或手动清空 `build-sidecar/` 后跑 `python sidecar/build.py` 即可复原标准路径。
> 两者的 gitignore 均生效，产物不入库。

> 路径注记（task-10）：sidecar 产物目录为 `dist-sidecar/`（构建目录 `build-sidecar/`）——
> 前端 vite 产物占用 `dist/` 且构建会清空它，实测一次误删 sidecar 包后分离。

### 基线沿革

- task-4：47,583,035 bytes（45.4 MiB）——embedding 栈（task-6/7）进包之前。
- task-10：200,060,425 bytes（190.8 MiB）——增量 ≈145MB 全部来自已立项范围：
  onnxruntime（~36MB）+ numpy/scipy-openblas（~35MB）+ transformers/tokenizers/
  huggingface-hub/hf-xet/safetensors 等（~40MB）+ 打包后 exe 内嵌代码（~25MB）。
  属范围驱动的台阶，非回归——基线据此重定，1.15× 继续看守未来回归。
  体积优化（裁下载期依赖、确认懒加载）列入后续，本任务只记录不做。
- task-15：332,926,165 bytes（317.5 MiB）——增量 +126.7 MiB 来自 faster-whisper
  运行时（ctranslate2 59.4 + av 33.9 + 附带 ~33），属已立项范围（本地 ASR），
  非回归。基线据此重定；**新基线 1.15× 上限 ≈ 383 MiB**，
  下个任务若再加体积需先核对是否越线。

### 自污染 bug（已修，教训）

- 现象：同一包连续两次测体积漂移（task-10 实测 200,059,722 → 200,290,258）。
- 根因：打包态 DB 默认路径落在 `_MEIPASS`（=`_internal/`）内，每次启动建库迁移，
  包自己弄脏自己；且只读安装（Program Files）下首写即崩。
- 修复：`default_db_path()` 打包态改走 OS 用户数据目录
 （Win `%LOCALAPPDATA%/InterviewCopilot/data`，单测锁定“不在 bundle 内”）；
  `default_model_dir()` 同理。实测：两次 `verify-packaged` 总字节完全一致，
  `_internal/data` 不存在，DB 落 `%LOCALAPPDATA%/InterviewCopilot/data/`。

## 启动（spawn → 可用，两次样本，cwd=System32）

| 样本 | spawn→握手 | spawn→/health 200 |
|---|---|---|
| 1（task-10） | 1.13s | 1.15s |
| 2（task-10） | 1.01s | 1.03s |
| 3（task-15，含 faster-whisper 运行时） | **2.79s** | **2.81s** |

对照 PRD §3.12（sidecar 后台预热 <5s，含拼音 ~500ms + 结巴词典 ~4s）：**通过**。
task-10 时余量很大（~1.0s）；**task-15 后涨到 2.8s，余量降到 ~2.2s**——
增量来自更大的 `_internal`（多出 ctranslate2/av 的 DLL 加载与目录扫描），
非权重加载（provider 是懒加载，启动路径不碰它）。仍达标，但这是后续加依赖时要盯的项。
差异说明：v0.7.1 的词典为懒加载——`jieba_dict()` 调用本身 ~0ms，
~0.5s 成本发生在首次中文查询（冒烟 jieba_query 首查 511.8ms），
不在启动路径上；PRD 的 ~4s 系旧版本行为假设。

## §3.13 四条校验（打包产物，cwd=System32，两次 ALL PASS）

1. bundle 内 vendor 文件逐个存在（无 datas 静默空目录）。
2. 任意 CWD 启动 → 合法握手 JSON（_MEIPASS 绝对锚定，绑定完成才打印）。
3. 打包件内 libsimple + dict 绝对路径加载 → 插入中文 →
   jieba_query / simple_query 命中 → rebuild（CWD 无关）。
4. `/health` → 200。

## 打包工程记录

- 工具：PyInstaller 6.22.3（Python 3.13.14，sqlite-vec 0.1.9，simple v0.7.1）。
- 实测坑：PyInstaller 6 不把脚本所在 `sidecar/src` 自动记入 pathex
  （`Analysis-00.toc` 实证），sibling 模块（app/core/database）被静默漏掉，
  产物启动即 `ModuleNotFoundError: No module named 'app'`——
  `build.py` 已用显式 `--paths sidecar/src` 修复；`verify-packaged.py`
  第 2 条（启动即验）是该类问题的永久兜底。
- `sqlite_vec/vec0.dll` 经 `--collect-all sqlite_vec` 收包（TOCs 实证为 BINARY）。

## 待办

- （已关闭，task-10）生产数据目录替代 `_internal/data`：见上节“自污染 bug”。
- 体积优化候选：裁剪下载期依赖（hf-xet/safetensors）、onnxruntime 精简——列入后续。

---

# ASR 时延实测（task-15，2026-09-14）

> 口径：**segment_end 发出 → asr_final 收到**，走**真实 uvicorn + 真实 WS 传输**
> （不是 `TestClient`——进程内客户端不反映真实链路）。VAD 判定本身在 Rust 侧
> （30ms/帧），不在本区间内。冷加载单列：它在 sidecar 启动预热路径上，
> 与「每段转写」是两笔账。

## 复现

```sh
python scripts/bench_asr_latency.py --durations 3,15 --repeat 2
```

## 结果

provider `faster-whisper` base / int8 / CPU；环境 Windows AMD64，Python 3.13.14。

| 项 | 值 |
|---|---|
| 冷加载（首次权重加载） | **2790 ms** |
| 3.0s 段（100 帧） | 中位 **1903 ms**（1896 / 1910） |
| 15.0s 段（500 帧） | 中位 **1888 ms**（1878 / 1899） |

段时长由 sidecar 按**帧数 × 30ms** 报回：实测 100 帧 → `duration_ms=3000`、
500 帧 → `duration_ms=15000`，与端点规格一致（±1 帧内）。

## 结论与影响

1. **冷加载 2790 ms**，与 task15 坑位预警的「首次 2–3s」吻合。故 provider 必须是
   **进程内单例**（`faster_whisper_provider._MODEL_CACHE`）；若每段重载，
   1.9s 的转写会变成 4.7s。
2. **转写耗时与段长几乎无关**（3s 与 15s 都在 ~1.9s）。原因：whisper 按 30s 窗口
   编码，≤30s 的段落在同一窗口内，算力是**常数级**而非线性。
   → 降级超时按「3× 段时长」派生（3s 段 9s、15s 段 45s），余量 4.7× / 23×。
   也说明**固定 10s 超时对本档模型并不危险**；但换成更大档位（small/medium）或
   长于 30s 的段后线性假设不成立，故仍按段时长派生，不写死。

## 未测（挂起项，不粉饰）

- **中文真人样本 → 文本正确（人耳比对）**：需真实录音，按「真实数据先跳过」挂起。
  本节音频为**合成信号**（谐波堆 + 音节包络）——足以驱动真实算力路径，故时延有效；
  但**转写文本无意义**，不得据此判断识别质量。
- 长段（>30s）与更大权重档位的时延：端点规格单段上限 15s，超出场景不在本轮范围。
- 双路并发（loopback + mic 同时说话）时延：本轮为单路串行口径。

---

# 三 Provider 同样本横向对比（task17，2026-09-14）

> 口径：同一批样本（`zh.wav` 5.59s 中文 / `en.wav` 7.15s 英文）分别走三个本地
> provider（均为整段转写、CPU）。时延 = segment_end 发出 → asr_final 收到
> （含 WS 传输，与上节 task-15 口径一致）。
> 复现：`python scripts/bench_asr_latency.py --provider {local,sensevoice,paraformer}`。

## 延迟与输出

| 样本 | sensevoice | paraformer | faster-whisper（base/int8） |
|---|---|---|---|
| zh 5.59s | **365 ms** `开饭时间早上九点至下午五点` | **315 ms** `开放时间早上九点至下午五点` | **1059 ms** `開放時間早上9點,墜下5點` |
| en 7.15s | **467 ms** 近满分级 | **409 ms** 垃圾输出（中文专用，预期内） | **958 ms** 可用 |

相对观察（非 WER，上游 README 无参考转写、真 WER 无法计算，需用户提供标注数据后补）：

1. **sensevoice 的 zh 输出与上游官方 non-ITN 示例逐字一致** → sherpa-onnx 接线正确性已交叉验证。
2. **faster-whisper 本样本输出繁体**（`開放時間…墜下5點`，且有错字）：base 档中文场景弱于两个专用模型，延迟也是 ~3×。
3. **paraformer 是中文专用**：中文最快（315 ms），英文不可用 —— 产品内需按场景提示，不可静默选用。
4. 延迟量级：sherpa-onnx 两路均为 ~300–470 ms（约为 faster-whisper 的 1/3）。

## 体积账

| 项 | 值 |
|---|---|
| sense-voice 权重 | 239,233,841 + 315,894 B（**约 228 MiB**，见 `docs/models.md`） |
| paraformer-zh 权重 | 243,371,218 + 75,756 B（**约 232 MiB**） |
| sherpa-onnx 运行时 | `sherpa-onnx` + `sherpa-onnx-core` 两 wheel 合计约 **18 MiB** |
| funasr 方案（已否决） | 拖 PyTorch **~2 GB** —— 在 317.5 MiB 安装包定位下差两个数量级 |

权重不进包（经 `/model/download` 按 SHA256 落盘），故打包体积增量仅运行时
约 18 MiB；当前 1.15× 上限 ≈ 383 MiB，余量充足。

## 未测（挂起项，不粉饰）

- **真 WER**：需带标注的中文/英文评测集（含专有名词/中英混说）+ 人耳复核。
  本节只有延迟与相对观察，不得据此宣称识别率数字。
- 长段（>30s）与双路并发下的三路对比：单路串行口径。

---

# 流式部分结果决策记录（task18，2026-09-14）

## 环境实测（本机，决定“跳过 GPU 验证”的依据）

| 项 | 值 |
|---|---|
| `nvidia-smi` | 不存在（无 NVIDIA 驱动/GPU） |
| `torch.cuda.is_available()` | `False`（CPU 版 torch，`numpy` 未装不影响结论） |
| CPU | 4 核 |
| `onnxruntime` | 未安装；`CUDAExecutionProvider` 无 |

结论：**本机无 CUDA，`PUT /asr/latency {enabled:true}` 按设计返回 409**
（单测锁定该门控）。DoD 的“GPU 环境首片段 ~1.5s（实测落盘）”在本机
**无法执行，按任务标题记为决策跳过**，不伪造数字。

## CPU 默认关闭的理由（记录在案，不是懒惰）

- task-15 实测：faster-whisper base 在 CPU 上整段转写 ~1.9s（3s/15s 段几乎
  同耗时，whisper 按 30s 窗编码）。
- 流式每 ~1.5s 对滚动缓冲做一次**全量**转写：在 CPU 上等于给本已 1.9s 的
  转写再叠加 1 probe/1.5s 的负载，延迟收益为负（探一次 ~1.9s，确认还要等
  第二轮），只剩“早看到跳动的字”的负体验。
- 故 CPU 上默认关闭是**算力账**的结果：`describe_latency().note` 与设置页
  文案都明示“CPU 请保持关闭”，开关在 `cuda=false` 时直接禁用（409 双保险）。

## 已交付（无 GPU 可验证的部分，本机实测）

- `asr/local_agreement.py`：前缀确认单调性、节拍帧计数派生、静音暂停/即时恢复
  —— `test_local_agreement.py` **13 passed**；
- 开关门控 + 关闭回归（零 partial、provider 单次全量调用、与 final 同段 id、
  探测失败不下行 error、迟到探测丢弃）—— `test_low_latency.py` **10 passed**；
- 开启路径的端到端时延（首片段 ~1.5s）待 GPU 机器执行以下步骤后落盘到本节：
  1. `PUT /asr/latency {enabled:true}` → 200（`cuda:true`）；
  2. 说话 5s，记录首个 `asr_partial` 的 `ts_ms` 与到达时间差；
  3. 确认同一 `segment_id` 的 `asr_final` 全文与 partial 前缀一致（无撤回）。

## 待标定（初值，不代表结论）

- `DEFAULT_SILENCE_RMS = 0.01`、`SILENCE_WINDOW ≈ 0.5s`：合成信号上有效，
  真机底噪未知，GPU 联调时与首片段时延一起标定。

---

# 多 LLM 首字延迟实测（F6.1 全量，2026-09-15）

> 口径：`scripts/e2e_llm.py --provider {ollama,openai,claude,gemini,groq,custom}`
> 经 `answer_stream` 全链路（检索→判定→LLM），`FIRST_TOKEN_S` = 提问发出 →
> 首个 `chunk` 到达；`TOTAL_S` = 到 `done`。direct/fail_closed 无 chunk
> 属正常（`FIRST_TOKEN_S=null`），不代表 LLM 故障。
> 复现：`python scripts/e2e_llm.py --provider <名> [--model …] [--api-key …]`；
> key 解析顺序 `--api-key` > 环境变量（openai `OPENAI_API_KEY` /
> claude `ANTHROPIC_API_KEY|CLAUDE_API_KEY` / gemini `GEMINI_API_KEY|GOOGLE_API_KEY` /
> groq `GROQ_API_KEY`）> 内存密钥库；缺 key 即 exit 2 记延期，不伪造数字。

## 真机联调结论（本机，2026-09-15）

| provider | 可达性 | 首字延迟 | 备注 |
|---|---|---|---|
| ollama | 不可达（本地） | — | `127.0.0.1:11434` 连接拒绝（`[connection]`，2.13s 后失败）；`ollama serve` 未运行。本机直连探针确认非代码问题。`e2e_llm.py --provider ollama` 默认问题命中 direct（`+0.74s decision direct`），未触达 LLM，属正常短路。 |
| openai | 网络可达、缺 key 延期 | — | dummy key 探针回 `HTTP 401`（0.87s），证明到 `api.openai.com` 网络通；无真实 key，`e2e_llm.py` 按设计 exit 2，不跑真机。 |
| claude | 网络可达、缺 key 延期 | — | dummy key 回 `auth HTTP 401`（0.57s）；`e2e_llm.py --provider claude` 无 key 即 exit 2（已验证）。 |
| gemini | 网络可达、缺 key 延期 | — | dummy key 回 `auth HTTP 400`（0.52s，Google 无效 key 用 400，已映射为 auth）；同上 exit 2 延期。 |
| groq | 网络可达、缺 key 延期 | — | dummy key 回 `auth HTTP 401`（0.52s）；同上 exit 2 延期。 |
| custom | 未配置延期 | — | 无默认端点，需 `--model <base_url>` + 自建服务；本机无服务，记延期。 |

**延期台账（网络/key，非代码）：** 本机六 provider 均无可跑真机的 LLM 首字延迟——
ollama 缺本地服务，openai/claude/gemini/groq 缺真实 API Key（网络本身可达，
探针均在 <1s 内拿到鉴权类 HTTP 状态，非超时/断网），custom 缺自建端点。
Mock 覆盖不放松：`test_llm_providers.py` **39 passed**（含每家 happy-path/
401/429/500/协议/超时 + 四档判定 ×3 家），`pytest tests/` 全绿
**281 passed, 1 skipped**。有 key / 有服务的机器按上节复现命令跑出
`FIRST_TOKEN_S=…` 后追加到本表即关闭台账。

## 实现要点（防坑记录）

- 三家 SSE 各自独立解析器，不共用状态机：Claude 跟踪 `event: content_block_delta`
  只取 `delta.text`（ping/message_delta/裸 data 全丢弃）；Gemini 逐 `data:` 取
  `candidates[0].content.parts[*].text` 拼接（空 candidates 跳过，无 `[DONE]`）；
  Groq 与 OpenAI 同形但独立实现（`choices[0].delta.content` + `[DONE]`）。
- 错误 kind：401/403 → `auth`（Gemini 另含 400，Google 无效 key 用 400）；
  429 → `rate_limited` 单列（Groq 免费档严格，供 P8 降级链区分，单测锁定
  `!= "http"`）；其余非 200 → `http`；key 永不进异常文本（R8，单测断言）。
- 目录：`generation/provider.py::LLM_CATALOG` + `GET /llm/providers`
 （内容无关快照），设置页 `Settings.tsx::PROVIDERS` 六项，密钥全复用
  `POST /settings/llm-secret`。

# Cloud embedding 重建实测（F5.3 双基建，2026-09-15）

> 口径：`python scripts/bench_embedding.py [--api-key $OPENAI_API_KEY] [--n 20]`
> 对 text-embedding-3-large 做 N 条批量 embed，输出机器行
> `EMBED_N=… EMBED_S=… TOKENS=… DIM=3072`（耗时含限流退避等待；
> TOKENS 为服务端 `usage.prompt_tokens` 求和，只记数不记文）。
> key 解析顺序 `--api-key` > `OPENAI_API_KEY` 环境变量；缺失即 exit 2
> 记延期，不伪造数字。

## 真机联调结论（本机，2026-09-15）

| 项 | 结果 |
|---|---|
| key | 缺（`OPENAI_API_KEY` 为空，`--api-key` 未传） |
| 实测 | 未跑（脚本按设计 `EMBEDDING_DEFERRED=1 reason=no-key`，exit 2，已验证） |
| 本地 leg | bge 权重在位（`test_embedder.py` NEED_MODEL 未跳过即证；缺则单测跳过 loudly） |

**延期台账（key，非代码）：** 无真实 OpenAI Key，本机无法实测 cloud 重建耗时
与成本。Mock 覆盖不放松：`test_embedding.py` **32 passed**（含批量切分/
429 退避/成本日志无文本/维度守卫/切换全流程/失败不翻转/不断链/SIGKILL 回滚），
`pytest tests/` 全绿。有 key 的机器跑 `scripts/bench_embedding.py --n 20`
（及全量重建的 `tokens` 进度），把 `EMBED_N/EMBED_S/TOKENS` 行追加到本表即关闭台账。

# Linux keyring fallback 延期台账（P8/F6.3，无 Linux 真机，2026-09-15）

| 项 | 结果 |
|---|---|
| 代码路径 mock 覆盖 | `cargo test --lib security` 16 passed（fallback 10 + keychain 6，三态全覆盖） |
| Linux 真机 | 未跑（本机 Windows；`/etc/machine-id` 读取、0o600 落盘、libsecret 缺失分支需 Linux） |
| 关闭条件 | Linux 机器：① 无桌面密钥环环境读 key 报加密文件路径错误而非明文回退；② 存取 roundtrip；③ `stat -c %a` 为 600；④ 换 machine-id 后读失败。把四行结论贴回即关闭。 |
