# PROGRESS.md — InterviewCopilot

## 已完成
- task-16 1b 产品面：什么时候响、响什么（2026-09-14）：触发策略 + 实时提词器 + F1.6 全局快捷键。
  **触发策略（纯函数，可脱离运行时单测）**：新增 `src/lib/trigger.ts` ——
  `questionVerdict`（PRD 规则表**按序短路**）、`DedupWindow`（3-gram Jaccard ≥0.85 / 窗口 60s / N=10）、
  `RefreshThrottle`（3s 锁、单槽队列、到期取**最后一条**、手动打断）。
  时间全部由调用方注入（模块内不读 `Date.now()`），否则「3 秒锁」「60 秒窗」只能靠 sleep 测，
  既慢又不稳；`RefreshThrottle` 用单槽覆盖而非真队列 —— 与「取队尾」等价但内存有界。
  **实时提词会话**：新增 `src/lib/liveqa.ts` —— 把判定/去重/锁定与**检索结果**缝成状态机
  （`ingestFinal` / `manual` / `drain` / `settle`），会话是 `cards` 的唯一所有者；
  另含平台矩阵（`detectPlatform` / `capturePaths` / `captureNotice`）。
  **接线**：`src/hooks/useLiveQA.ts`（订阅 `asr://final` + `teleprompter://trigger` + `capture://toggle`，
  250ms 轮询 drain；新检索取消上一次未完成的检索 ——「最新问题优先」与锁定期「取最后一条」同向）；
  `src/components/Teleprompter.tsx`（最近 3 条、最新在最上、来源标签、Fail-Closed 显示「知识库未命中」）；
  `src/pages/LiveQA.tsx` + App 新增「实时提词」页签（默认页签仍是「手动查找」，1a 行为不动）。
  **F1.6 全局快捷键**：`tauri-plugin-global-shortcut` + `src-tauri/src/shortcuts.rs`
  （`CmdOrCtrl+Shift+R` 采集开关 / `CmdOrCtrl+Shift+Space` 手动提词）；
  处理器按 `Shortcut` **相等性**派发而非字符串比对 —— `Shortcut` 的 `Display` 输出规范化形式
  （`Ctrl+Shift+R`），与 `DEFAULT_BINDINGS` 写的 `CmdOrCtrl+Shift+R` 字面不同，字符串比对会**静默失配**；
  注册失败不阻断启动（加速键可能被别的程序占用），返回失败清单由调用方记录。
  **重构**：`useQA` 的两段式 SSE 消费抽到 `src/lib/qa.ts::askQuestion`，1a 与 1b 共用一条协议路径
  （复制一份就等于给协议变更留两个改点，而这类副本通常有一份先腐坏）。
  全量：`vitest` **89 passed**（新增 trigger 50 + liveqa 29）、`tsc --noEmit` 0、`eslint` 0、`vite build` 成功。
  **三处落地裁定（需主人确认）**：
  ① **手动触发绕过去重**（`manualBypassesDedup` 默认 `true`）：PRD 去重小节只说「同一 session 内……复用上次结果」，
     未按触发来源区分；但手动触发是用户显式的「现在给我答案」，复用缓存会让按键看起来失灵 ——
     而用户按它，往往正是因为上一条答案不满足预期。代价是可能重复一次检索（~5–100ms）。
  ② **「无疑问词」= 全文不含疑问词**，而非「开头不是疑问词」：否则「我该怎么办」以「我」开头会被判陈述句跳过 ——
     那恰恰是最该检索的一类问题。
  ③ **句尾 `?` 在轻度归一化文本上判定**：PRD 先要求「归一化（去标点）」再要求「末尾含 `?`」，两条自相矛盾；
     实现取「中文语气词在归一化文本上判、`?` 在保留标点的文本上判」，两边都不丢信号。
  **行为变更（记档）**：SSE 流结束却未收到 `done` 事件时现在**抛错**（此前静默当成成功，UI 停在占位文案）。
  理由：按契约 sidecar 总会发 `done`，缺它就是传输被截断；截断的答案看起来像完整答案，
  对提词场景尤其危险（用户会把半句话念出去）。
  **裁剪（记档）**：采集服务（`endpoint.rs` 段端点状态机 + 采集生命周期）尚未落地，
  故 F1.6 的「开始/停止采集」当前只发 `capture://toggle` 事件、前端显示「采集服务尚未接线」。
  快捷键层不因此改动：它只做「键盘 → 事件」，不拥有采集状态。
- task-15 段→文本→下行 + Provider 失败自动切换（2026-09-14）：sidecar ASR 从「抽象 + 替身」
  变为「真 provider + 降级链」。
  **新增文件**：`asr/faster_whisper_provider.py`（本地 base/int8/CPU，整段转写、语言自动，
  `asyncio.to_thread` 卸载 CPU，**进程内单例**缓存权重）；`asr/cloud_rest.py`（OpenAI 兼容
  `/audio/transcriptions`，float32→16k 单声道 WAV 走标准库 `wave`，key 只进 Authorization 头）；
  `asr/fallback.py`（降级链）；`scripts/bench_asr_latency.py`（时延实测）。
  **`asr/provider.py`**：`transcribe(pcm_f32, sample_rate, path)` 增 `path` 标签（诊断用，
  不参与转写）；错误 kind 常量化（`unavailable`/`manual_input_required` 等）；
  `ASRError.attempts` 承载逐级失败摘要。`StubASRProvider` 增 `paths` 平行列表
  （`calls` 仍是 `(size, rate)` 二元组，既有断言零改动）。
  **`routers/audio.py`**：`asr_final` 增 `provider`（**实际出力**那一级）与 `degraded`
  （`["provider:kind", ...]`）；`asr_error` 在链全灭时同带 `degraded`；转写调用改走
  `_run_provider`（优先 `transcribe_with_detail`，普通 provider 回落 `transcribe`）。
  **降级链**：本地主 → 同类本地备选（配了才在）→ 云端 REST（**有 key 才在**）→ 纯 VAD + 手动输入。
  终级不是 provider：全灭时抛 `manual_input_required`（前端据此切手动输入；
  `manual_input=False` 则抛 `all_providers_failed`）。每降一级记**内容无关**事件
  （级号/provider 名/错误 kind/path/超时），R14 由单测以 `caplog` 断言。
  **超时按段时长派生**（`max(5s, 3×段时长)`），不写固定值：固定值会把「15s 段在 CPU int8 上
  要转 10s+」这一**容量事实**误判成**故障**并稳定降级。单次最坏耗时 = 级数 × 该超时（已记账）。
  **结果经返回值传递，不落实例状态**——同一条链被多段并发复用（待转写 ≤8），
  共享可变状态必然串味，故有 `ASRResult` 与 `transcribe_with_detail`。
  **`asr_start` 语义裁定**：保持「segment_start 时下发」，即「本段 ASR 开始处理」。
  task15 原文「转写开始 → asr_start」按此理解——转写实际在 segment_end 发生，
  若挪到彼时，前端在整段说话期间收不到任何"已开始"信号，且需改 api-contract +
  Rust `asr://start` 映射 + 既有 e2e 断言。已写入 `docs/api-contract.md`。
  **OpenAI Realtime WS 裁定为 Phase 2（本轮裁掉）**：① 它解决的是"边说边出字"，
  而本项目当前是**整段转写**（`asr_partial` 本阶段不发），上 Realtime 等于同时换掉
  端点语义与下行协议，属 W4 范围；② 可用性目标已由「本地主 + 本地备选 + 云端 REST」
  三级覆盖，Realtime 是**延迟**优化而非可用性兜底；③ 它是**云依赖**，与 local-first
  默认姿态相反，只应在用户显式选择时启用。
  **权重获取**：`models/registry.py` 新增 `faster-whisper-base` 条目（4 文件；
  `model.bin` 145,217,532 B 的 SHA256 取自 HF LFS oid（权威），三个非 LFS 小文件
  首下实测 pin；镜像 ModelScope → hf-mirror → HF）。实测 `ensure_model_files()`
  经 hf-mirror 全量落盘 **17s，四文件 SHA256 全部通过**（条目端到端验证）。
  **尺寸偏差记录**：task15 原文写「~75MB」，实测 base 档 **138.5 MiB**；
  ~75MB 对应 `tiny` 档。按 provider 规格取 `base`，尺寸以实测为准（已记 models.md）。
  **打包**：`build.py` 增 `--collect-all ctranslate2` + `--collect-all av`——
  ctranslate2 原生 DLL 按名字加载、`av` 是 `faster_whisper/audio.py` 的**顶层** import
  （`__init__.py` 一进来就要），静态分析都看不到，漏了会「装得上但跑不了」。
  `pyproject.toml` / `requirements-lock.txt` 加 `faster-whisper==1.2.1`、
  `ctranslate2==4.8.2`、`av==18.1.0`。
  数字：`pytest` **150 passed / 0 failed**（新增 `test_asr_fallback.py` 25 条）。
  打包复测（项目外临时目录）：**317.5 MiB**（+126.7 MiB），四条 §3.13 校验
  **ALL PASS**，spawn→health **2.81s**（task-10 为 1.03s，余量降到 ~2.2s）。
  时延见 `docs/benchmark.md`：冷加载 **2790 ms**、3s 段 **1903 ms**、15s 段 **1888 ms**
  ——**转写耗时与段长几乎无关**（whisper 按 30s 窗编码，≤30s 落在同一窗口，算力常数级）。
  **未做/挂起**：中文真人样本 → 文本正确（人耳比对，需真实录音，按「真实数据先跳过」挂起）；
  `asr_partial` 流式下行；`dist-sidecar/` 仍是 task-10 旧包（见下）。
- task-14 Rust→sidecar 音频 WS 全链路（2026-09-14）：Rust 侧新增 `audio/uplink.rs`（WS 客户端 +
  下行→Tauri 事件桥），sidecar 侧段缓冲按 R13 改造，并补上**真实 uvicorn** 的跨语言 e2e。
  **本次查出两个会让整条音频链路静默失效的问题（都是既有代码，非本次引入）**：
  ① **打包/依赖里缺 WebSocket 实现 → `/audio/stream` 全挂**。`pyproject.toml` 只声明
     `uvicorn>=0.30`，`requirements-lock.txt` 42 个 pin 里没有 `websockets`/`wsproto`。
     uvicorn 的 `ws="auto"` 在两者都缺时**不降级**，而是对升级请求直接返回 **404**——
     实测：装 `websockets` 前 no-token / wrong-token / good-token **三个用例全是 404**；
     装后 good-token 才 101。既有 pytest 全绿是因为 FastAPI `TestClient` 走进程内 ASGI，
     **完全绕过 uvicorn**，所以这个洞一直没被看见。
     修：`pyproject.toml` 加 `websockets>=13`；`requirements-lock.txt` 加 `websockets==17.1`；
     `sidecar/build.py` 加 `--collect-all websockets`（uvicorn 是**动态** import，
     PyInstaller 静态分析看不到，不 collect 则**打包产物照样 404**）。
  ② **鉴权失败的线上形态是 HTTP 403，不是 WS close 1008**。`accept` 之前 `close()` 在真实
     ASGI 服务器上表现为握手失败（uvicorn 回 403）；`code=1008` 只在 `TestClient` 下可见。
     **裁定：不改 sidecar**——「accept 之前拒绝」是有意的安全姿态（不为未鉴权对端建立 WS 会话），
     且 RFC 6455 §4.1 明确允许握手期返回 HTTP 错误；更重要的是 403 让 Rust 客户端**快速失败**
     （`connect` 直接返回 `Unauthorized`），而 1008 只能在连上之后再关闭，反而丢失 fail-fast。
     客户端两种形态都识别：`UplinkError::Unauthorized{401|403}` + `UplinkStats::close_code=Some(1008)`。
     → 与 DoD 字面「失败 1008」有偏差，**已在 `docs/api-contract.md` WS 节记录实测形态**，待主人裁定是否改为 accept-then-close。
  **顺手修一个真 bug（sidecar）**：`audio_stream` 的接收循环把**正常断开**（`websocket.disconnect`
  消息，不是异常）当成 `malformed += 1`，导致每个正常会话结束时该计数都虚增 1，计数失去意义。
  现按 `msg["type"] == "websocket.disconnect"` 正常退出；文本帧仍计 malformed。
  **R13 缓冲策略变更（1b-1 → task-14，需知会）**：1b-1 定为「单段 ≤32MB，超则自动封段转写部分」；
  task-14 明确要求「有界队列，满则丢最旧 + 告警（R13）」，故改为**丢最旧帧 + `dropped_oldest` 计数
  + 每段一次 `segment_truncated` 告警**，上限改为 `MAX_SEGMENT_FRAMES = 2000`（60s，是端点规格
  15s 的 4×）。代价：触顶时段首部音频被丢；收益：与 Rust 侧 `DropOldestQueue` 语义一致，
  且不会把 8.7 分钟音频整段送 ASR。
  Rust 侧设计裁定：① **wire seq 由 uplink 独占**（R10 要求音频与事件共享一个序号空间；两套计数器
  一旦交错就必然撞号），在**入队时**打号，因此被 R13 丢弃的帧在对端表现为 seq 空洞——丢帧可见；
  `Frame16k.seq` 保留为采集侧生产者计数。② 出站队列 `OUT_QUEUE_CAPACITY = 128`（≈3.8s），
  自建 mutex+deque+`Notify` 的**异步**丢最旧队列（std mpsc / tokio mpsc 都只能拒最新，不满足 R13）。
  ③ 下行经 `EventSink` trait 桥接（生产 `TauriSink` → `AppHandle::emit`；测试记录型替身），
  故 `asr_start/partial/final/error → asr://start|partial|final|error` 的映射无需 Tauri 运行时即可测。
  ④ 事件在客户端**先本地校验**（event 取值 / path 合法 / ts_ms 为 int / path 与本连接一致），
  把本该在对端 `violations` 计数里体现的错误变成即时 `UplinkError`。⑤ `UplinkConfig` 手写 `Debug`
  脱敏 token（R8/R14）。
  数字：`cargo test` **71 passed**（lib 67，其中 uplink 新增 19；集成 1；跨语言 e2e 3）
  + `clippy --all-targets -D warnings` **零告警** + rustfmt clean；
  `pytest` **124 passed**（`test_audio_protocol.py` 13→17）。
  **既有失败 1 例（与本次无关，环境性）**：`test_providers.py::test_ollama_connection_error`
  期望 `connection|timeout`，实际得 `http`（502）——该用例连 `http://127.0.0.1:1`，
  本沙箱出口代理对 127.0.0.1:1 回 502 而非拒连；`git diff` 确认未触碰 `generation/`。
  新增文件：`src-tauri/src/audio/uplink.rs`、`src-tauri/tests/audio_ws_e2e.rs`、
  `scripts/serve_audio_e2e.py`（真实 uvicorn 测试对端）。
  未做（记账）：uplink 断线**不自动重连**（只置 `closed` 标志，重连策略待定）；
  R13 丢帧"在对端可见为 seq 空洞"这一性质由单测覆盖，未做 e2e 断言（出站队列与 socket 缓冲
  竞态使其非确定）；Tauri 事件到前端的端到端订阅未走查（需壳）。
- 1b-4 VAD 抽象层（2026-09-14，**仅 `vad.rs`；`endpoint.rs` 与双路事件合并尚未做**）：
  `audio/vad.rs` 落地 `Vad` trait + `WebrtcVad`（默认 Provider）+ `SileroVad` 预留 stub。
  参数记录在案：**aggressiveness 初值 2**（`DEFAULT_AGGRESSIVENESS`，= libfvad mode 2
  `Aggressive`）；30ms / 480 samples / 16kHz 由 `const _` 断言在**编译期**钉死——若日后改
  `frame.rs` 使该组合不再是合法 libfvad 帧，编译直接失败，不会留到运行期静默。
  **R19 关闭动作与意外发现**：`cargo add webrtc-vad`（0.4.0，上游 2540 天未更新）编译通过
  （MSVC，31.56s），但**读源码后发现两个把"非语音"静默化的入口**，正是 R19 所指
  "已知音频兼容问题"，且二者都不是靠"参数配错"触发：
  ① `Vad::new()` 默认 **8 kHz**。crate 的 `is_voice_segment` 只接受 10/20/30ms 帧；480 samples
     在 8 kHz 下是 60ms 帧 → `Err(())`。调用方若把 `Err` 当 `false`，整路永久静音且零报错。
     → 构造时强制 `SampleRate::Rate16kHz`。
  ② 更隐蔽：`Vad::reset()` 即 `fvad_reset()`，实现为 `WebRtcVad_InitCore()` + `rate_idx = 0`，
     即**把采样率打回 8 kHz、模式打回 0**（`kDefaultMode = 0` = Quality）。分段状态机在段边界
     调 reset 是最自然的动作，一旦调用，此后每帧都非法 → 同一静默失效，但由**正常代码路径**
     触发而非误配。→ `WebrtcVad::reset()` 后重新钉回 rate+mode（`reapply_config`），
     并有回归测试同时证明"裸 crate 确实会掉"与"我们的封装不会掉"。
  设计裁定：① `Vad::is_voiced` 返回 `Result`，**不把 provider 错误折叠为 `false`**——
     "判为静音"与"未能判定"必须可区分；② 拒绝计数（`WebrtcVad::errors`）把该状态暴露出来（R13）；
     ③ trait **不要求 `Send`**：`webrtc_vad::Vad` 持裸 `*mut Fvad`，诚实的契约是"一实例一线程"，
     由各路 VAD worker 自行构造，不用 `unsafe impl Send` 绕过去；④ 手写 `Debug`，裸指针不进日志（R14）。
  silero：本轮**不引入 `ort`**（stub 不可构造，`is_voiced` 返回 `Unavailable`）。理由：silero 集成含
  ONNX 模型下载，是 W4 交付项且有独立的体积/延迟预算（PRD §3.6 / W4）；现 stub 只钉住 W4 需满足的形状。
  数字：`cargo test --lib` **48 passed**（audio 39，其中 vad 新增 13）；集成 **1 passed**；
  `clippy --all-targets -D warnings` **零警告**；`rustfmt --check` 干净。
  合成样本实测（webrtc-vad 0.4.0，版本已锁）：speech-like 合成信号 **100/100 帧 voiced**
  （mode 0/1/2/3 全部 100/100），纯静音 **0/100**。→ 该信号足以证"非退化"（确实在判语音），
  但**饱和到无法区分 aggressiveness 档位**，因此不能替代真实人声；真机人声自测仍是未做的独立步骤。
  未做（等下一步任务）：`endpoint.rs` 端点状态机、双路独立 VAD + 事件合并、DoD 边界误差 <1 帧测试。
- 1b-3 Rust 双路采集（2026-09-14）：`audio/` 八模块落地，三路来源（Windows WASAPI
  loopback / 全平台 cpal mic / Linux cpal monitor）共用一条采集管线与一个 trait。
  接手时是半成品且不可编译（17 个错误），本次修/建的实情：
  ① `loopback.rs` 用 `SyncSender` 实现"满则丢弃最旧"——**不可能**：生产端无法驱逐队首，
     `try_send` 只能拒最新，不满足 R13 字面。改为自建 `DropOldestQueue<T>`（有界 +
     丢弃最旧 + 计数 + 毒化容忍），帧与事件两条通道同一策略；测试锁定"10 万次推入
     2 槽队列立即返回"。
  ② `resample.rs` 的 rubato 5 导入路径/适配器 API 全错（`InterleavedSliceMut`、
     `rubato::audioadapter::Indexing` 不存在）→ 改为 `InterleavedSlice::new/new_mut`
     （同一类型两种构造）+ `rubato::Indexing`；新增 `downmix_interleaved_f32_to_mono`
     与 `decode_interleaved_f32_le_to_mono`（wasapi 热路径，单遍不落中间缓冲）。
  ③ `wasapi_loopback.rs` 半成品无法编译（`downmix_stereo_to_mono` 重复定义、调用未定义
     函数、误用不存在的 `crate::audio::loopback::stopped`、引用未声明的 `windows` crate、
     `AudioClient::new` 不存在）→ 按 wasapi 0.24 真实 API 重写。
  落地裁定（PRD 未细化处）：
  ① **原生格式读取而非假设**（PRD §3.5 坑）：wasapi 从 `Device::get_device_format()`
     取 rate/channels，只强制 f32 容器 + `autoconvert`，把重采样留给 rubato；
     cpal 走 `default_input_config()` 失败再退到 supported range（monitor 常无默认）。
  ② **回调永不阻塞**：cpal 数据回调只做 downmix + 入队（`DropOldestQueue`），
     resampler 与帧队列由独立 pump 线程持有；`cpal::Stream` 是 `!Send`，全程不跨线程。
  ③ **ts 由样本计数派生**（`frames*30ms`），seq 每帧自增 → 10 分钟漂移是构造性质
     而非测量结果；设备切换时 `set_input_rate` 只换 resampler，seq/ts 保持单调。
  ④ **设备变更**：wasapi 注册 `IMMNotificationClient` 默认渲染回调，回调只置原子标志
     （规范要求不得回调 enumerator），采集线程察觉后重建流并发 `DeviceChanged`/`StreamRebuilt`。
  ⑤ **两套错误机制**：启动失败经一次性 channel 同步返回给 `start()`（降级页可立刻渲染
     真实原因）；会话中途失败退避重试，5 连败才上报为错误事件。
  ⑥ 平台矩阵与 R12 对齐：Windows=loopback+mic、Linux=monitor+mic、macOS=mic-only，
     路径标签 `mic`/`loopback` 已按契约固定。
- 工具链（2026-09-14）：MSVC Build Tools 2022（17.14，MSVC 14.44 + Win11SDK 26100）+
  rustc/cargo 1.98.1 stable-msvc + cargo-audit 0.22.2；`cargo test` 9/9、`clippy` 零警告、
  `audit` 默认通过（576 crates，0 漏洞，9 允许警告）；`src-tauri/Cargo.lock` 入仓。
  Rust 历史欠账清零（test/clippy/audit/lock 全本地可跑）。
- 1b-1 Python WS 音频协议（2026-09-14，`c08ab44`）：落地裁定：一连接一路（首 segment_start
  确立强制）、seq 全帧共享序号空间、sidecar 分配 `seg_{n}`、重复 start 顶掉旧段、有界
  （待转写≤8/单段≤32MB/接收永不 await/发送锁+5s 超时）。
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
- Phase 1b 进行中：R9–R14 已接受；1b-1（Python WS 音频协议）已提交（`c08ab44`）。
  1b-3（Rust 双路采集到"可发送帧"）已提交（`c527511`）：`src-tauri/src/audio/` 八个模块
  （`frame` / `loopback` / `resample` / `cpal_common` / `cpal_mic` / `cpal_monitor` /
  `wasapi_loopback` / `mod`）——Windows loopback + 全平台 mic + Linux monitor 三路
  统一到 `LoopbackSource` trait 与同一条 `CapturePipeline`（原生率/声道 → downmix 单声道
  → rubato 16k → 480 帧 → int16(VAD) + float32(WS) 双输出）。
  **1b-4 `vad.rs` 已落地**：`Vad` trait + `WebrtcVad`（默认，aggressiveness 初值 2）
  + `SileroVad` stub（W4 用，本轮不引 `ort`）；R19 编译门通过，并查出两个静默失效入口
  （crate 默认 8 kHz / `reset()` 打回 8 kHz+mode 0），已封装补偿并有回归测试。
  **task-14 已提交（`fbe6f24`）**：WS 全链路（Rust `uplink.rs` + sidecar 段缓冲 R13 改造
  + 真实 uvicorn 跨语言 e2e），并查出「依赖缺 WS 实现 → /audio/stream 全 404」与
  「鉴权失败线上是 403 而非 1008」两个既有问题（详见上节）。
  **task-15 已提交（`596e348`）**：ASR 真 provider（faster-whisper base/int8/CPU，
  进程内单例）+ 四级降级链 + 下行 `provider`/`degraded` 扩充 + 权重 registry 条目
  + 打包 collect + 时延实测（详见上节与 `docs/benchmark.md`）。
  **task-16 已完成（未提交）**：触发策略（`src/lib/trigger.ts`，纯函数 + 时间注入）
  + 实时提词会话（`src/lib/liveqa.ts`）+ 接线与 UI（`useLiveQA` / `Teleprompter` / `LiveQA` 页）
  + F1.6 全局快捷键（`tauri-plugin-global-shortcut` + `src-tauri/src/shortcuts.rs`）（详见上节）。
  全量：`cargo test --lib` **74 passed**（+7 = shortcuts 新单测）、`clippy -D warnings` 零告警、
  `cargo check --all-targets` 零错误；`vitest` **89 passed**、`tsc` 0、`eslint` 0、`vite build` 成功；
  `pytest` **150 passed / 0 failed**（本轮未动 sidecar，沿用 task-15 结果）。
  **环境修正（重要，推翻此前记录）**：Rust 工具链**本机可用**，只是不在 git-bash 的 PATH 上 ——
  `C:/Users/Administrator/.cargo/bin/{cargo,rustc,rustfmt,clippy-driver}.exe`，rustc 1.98.1，
  配 MSVC 2022。故「本机无 Rust 工具链」的旧结论作废，Rust 侧改动**可以**本地编译验证
  （本轮 task-16 已实测：`cargo check` / `cargo test` / `cargo clippy` 全绿）。
  用法：`export PATH="/c/Users/Administrator/.cargo/bin:$PATH"`。
  **既有债务（本轮发现，非本次引入）**：`cargo fmt --check` 在 4 个未触碰文件上失败
  （`security/keychain.rs:15`、`sidecar/degradation.rs:43`、`sidecar/manager.rs:26/293/305/372/473/495`）——
  说明此前「rustfmt clean」的记录与当前 rustfmt 版本不符。本轮只格式化了 `shortcuts.rs`，
  未跨文件重排（避免制造无关 diff）；是否全仓 `cargo fmt` 待主人裁定。
  **下一项（等主人派单，按既定节奏逐步汇报）**：
  ① `endpoint.rs` 端点状态机（起始=最近 5 帧中 3 帧 voiced；结束=连续 ~500ms 静音 hangover；
     最短段 250ms 丢弃；最长段 15s 强制切段；`vad_state` 心跳每 1s）——**仍未开始**；
  ② 双路独立 VAD + 事件合并进统一 WS 上行队列（DoD：合成样本边界误差 <1 帧、双路互不干扰）；
  ③ 把 `Frame16k` 经 `encode_ws_frame` 推给 sidecar `/audio/stream` 的生产接线
     （目前 uplink 有完整单测与 e2e，但尚未接上采集管线）；
  ④ 采集服务落地后，F1.6 的 `capture://toggle` 才有真实消费方（task-16 已发事件、未接线）。
  **三项需主人裁定/知会**：① task-16 的三处落地裁定（手动触发绕过去重 / 「无疑问词」= 全文不含 /
  句尾 `?` 判定文本，详见上节）；② task-14 的 DoD 字面「错 token → 1008」与实测 403 的偏差
  （详见上节，已记 api-contract）；③ `dist-sidecar/` 仍是 task-10 旧包，
  重建被本机批量删除守卫拦住（详见 `docs/benchmark.md` 口径注记）。

## 待人工验证
- task-16 F1.6 快捷键真机（需 Tauri 壳 + 真实桌面，本机无壳）：
  ① `pnpm tauri dev` 后按 `CmdOrCtrl+Shift+Space` → 前端「实时提词」页应触发一次检索
     （需先有转写文本，否则 `manual()` 返回 null、无动作 —— 这是设计行为）；
  ② 按 `CmdOrCtrl+Shift+R` → 页面出现「已收到采集开关（F1.6）。采集服务尚未接线」提示；
  ③ 先占用 `Ctrl+Shift+R`（如另开一个注册同键的程序）再启动 → 应用**仍应正常启动**，
     stderr 可见 `[shortcuts] failed to register: ToggleCapture: ...`；
  ④ macOS 机器：页首应显示「采集路径：麦克风（仅麦克风（macOS 无系统回环采集））」，
     且不应出现「系统回环」。Linux：应显示「系统回环 + 麦克风」（PulseAudio monitor）。
- task-16 DoD 真机（**必做，需真实转写文本**）：① 说「发货周期是多久」→ 答案卡应在 ≤4s 内出现
  （固定答案路径，1a 实测 ask→done 2.4ms，预算几乎全给 ASR）；② 连说两句相似问题
  → 第二句卡片带「复用」标签且**不重新检索**（网络面板应只见一次 `/qa/ask`）。
  前置：采集服务未接线，本轮只能用合成 `asr_final` 事件或后续 `endpoint.rs` 落地后补测。
- task-16 标定（阻塞于评测集）：PRD 明示去重阈值 0.85 / 锁定期 3s / 去重窗口 60s 为**初值**，
  需 100 题评测集标定后写回。已记录的已知代价：3-gram Jaccard 对中文容错很窄 ——
  「发货周期是多久」vs「发货周期是多久呢」= 5/6 ≈ 0.833 < 0.85，ASR 多吐一个语气词即漏去重。
  代价不对称（漏去重多检索一次；误去重则把上一题答案冒充本题答案），故保留严格侧。
- task-15 ASR 识别质量（**必做，合成样本不能替代**）：本轮只测了**时延**，
  音频是合成信号（谐波堆 + 音节包络），足以驱动真实算力路径但**文本无意义**。
  需真实中文录音：① 录一段中文（含专有名词/中英混说更好），走完整链路
  （`segment_start` → 帧 → `segment_end`）收 `asr_final`；② **人耳比对**文本是否正确，
  并记录错字类型（同音字/漏字/英文串写）；③ 同时核对 `asr_final.duration_ms`
  与音频实际时长（±1 帧）。素材放 `sidecar/models/` 外，勿入库。
  复现入口：`python scripts/bench_asr_latency.py`（把合成音频换成真实 WAV 即可）。
- task-15 双路并发时延：本轮为单路串行口径（loopback + mic 同时说话未测）。
- 1b-4 VAD 真实人声自测（**必做，合成样本不能替代**）：合成 speech-like 信号在四个 mode 下
  都是 100/100 voiced（见上节），饱和到区分不出档位，只能证明"非退化"。必须用真实人声：
  ① 录一段"说话—停顿—说话"的真实 16k 单声道素材（或直接用 1b-3 的 `record_loopback`
     产物），过 `WebrtcVad` 看 voiced 比例是否随 aggressiveness 单调变化；
  ② 确认静音段与说话段的 voiced 比例有明显分离（而非全 voiced / 全 silence）；
  ③ 观察 `WebrtcVad::errors()` 全程为 0——非 0 即说明帧长/采样率契约被破坏。
  这是 W4 调参的输入，也是 R19 "无已知音频兼容问题"结论的最终依据。
- 1b-3 真机音频（需有声卡的 Windows 机器 + 人耳；本次按用户指示未跑）：
  ① `cargo run --release --example record_loopback -- 10 out.wav`——**先播放音乐再运行**，
     然后听 `out.wav` 确认内容正确。脚本自身已校验帧数/seq 连续/ts 派生/WAV 长度/
     int16↔float32 等长/丢弃计数，并在整段静音时明说"这只证明了链路、没证明内容"。
  ② 播放中切换默认渲染设备（插拔耳机/切输出）→ 事件应出现 `DeviceChanged` +
     `StreamRebuilt`，且 seq/ts 不重启（`set_input_rate` 只换 resampler）。
  ③ Linux monitor（PulseAudio）与 macOS mic-only 未在本机（Windows）验证，
     需对应平台各跑一次 `cargo test`。
- Rust 工具链**本机可用**（见「当前」节环境修正）：`export PATH="/c/Users/Administrator/.cargo/bin:$PATH"`
  后 `cargo check/test/clippy/fmt` 均可跑。`cargo test --lib` 现 **74 passed / 0 failed**、
  `clippy -D warnings` 零告警。余项为**壳**验证（需 Tauri 壳/真实桌面，与工具链无关）：
  `$env:INTERVIEWCOPILOT_PYTHON="<python>"; pnpm tauri dev` 壳启动且日志
  可见 `[sidecar] handshake parsed: port=...`；前端显示 sidecar connected
- task-2 人工 DoD（有工具链机器）：① 手动 kill sidecar 的 python 进程 → 日志可见 `restart 1/3 in 1s`… 最多 3 次并恢复 health（计数在 health 恢复后清零）；② 连续 kill 致 4 连败 → 前端降级页（reason=restart_exhausted）+ 事件 sidecar://degraded；③ 伪造 protocol_version（如改 main.py PROTOCOL_VERSION="9.9"）→ 降级页 reason=version_mismatch；④ 应用退出后 tasklist 无残留 python sidecar 进程
- Python 为 3.13.14（本机可用版本），PRD 要求 3.11——后续 CI/打包时需按 3.11 锁定验证

## 实测数字档案
- 1b-3（2026-09-14，Windows/MSVC + rustc 1.98.1）：`cargo test` **35 passed**（audio 26：
  loopback 9 / resample 6 / frame 4 / cpal_common 3 / cpal_mic 2 / wasapi 2；其余 9 为
  既有 sidecar protocol/degradation/manager/security）+ 集成测试 `audio_frames_wav`
  **1 passed**（5 s 48k 正弦 → 真管线 → WAV 落盘回读：样本数与内容逐字节一致、
  Goertzel 440Hz 比邻频强 >8×、int16 与 float32 等长、样本守恒 ≤1 帧）。
  `cargo clippy --all-targets` 代码告警 **0**（仅剩 target 增量目录 GC 的环境提示）、
  `rustfmt --check`（音频模块 + 新增 example/test）clean。
  记账：`cargo fmt` 顺带暴露出 3 个既有文件的 fmt 漂移（`security/keychain.rs`、
  `sidecar/degradation.rs`、`sidecar/manager.rs`，均为换行重排、无语义变化），
  已刻意回退未动，避免把无关重排混进本任务提交——留给后续任务或统一 fmt 时处理。
  rubato 结构参数实测：48k→16k 每帧需 1440 输入样本
  （in 1440 / out 480），内部延迟 240 输出帧（常量，不累积）；10 分钟 600 次推入
  16k 输入恰得 **20000 帧**、末帧 ts 599970ms 对 600000ms 偏差 **30ms**（<50ms 预算，
  且是帧量化而非漂移）。
- sidecar 独立 DoD（verify-sidecar.py）：HANDSHAKE_JSON_OK（protocol_version 1.0、port int、auth_token str≥32、capabilities/models_loaded list；token 已打码不落盘）；HEALTH_STATUS=200 BODY={"status":"ok","version":"0.1.0"}；结论 SIDECAR_STANDALONE_DOD_PASS（2026-09-14）
- pytest sidecar/tests：1 passed（test_health_returns_ok），pytest 9.1.1 / fastapi 0.141.1 / uvicorn 0.52.4
- 前端：pnpm install 成功（react 19.3.0、@tauri-apps/api 2.11.1、cli 2.11.4、vite 8.3.0）；`npx tsc --noEmit` 无输出通过；`pnpm build` 成功（dist/index.html 0.48kB）
- protocol 逻辑等价验证（Python 镜像，当时本机不可用 cargo）：valid-ok / invalid-json-rejected-ok / version-mismatch-rejected-ok —— **已作废**：工具链装好后 cargo test 本机可跑（见 1b-3 数字）
- 未跑：pnpm tauri dev 壳（需 `<python>` 环境变量，步骤见"待人工验证"）；1b-3 真机 10s 采集/回听（按用户指示跳过，原因如实记录）
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
- task-11 M1 验收（2026-09-14，task-12 已部分改判见下）：`docs/acceptance-m1.md` 落盘——16 项：13 通过 / 1 待定（#6 需用户 100 题，
  demo 代理 17/17）/ 1 部分通过（#10 检索侧 1.4ms，LLM 首字待 Ollama）/ 1 待人工走查（#13 降级页渲染，
  链路单测全绿）；无失败项，当场修：无（验收中新发现为 0；历史 bug 均已在各自任务修完）。
  融合三案精确复核：0.5/0.5/0.75（独立算术 + 7 单测）；FTS 有数据验证全清单复核：无一处空表断言（R4）。
  tag 判定：`m1-text-pipeline` 暂缓（打 tag 会夸大完备性；三外部条件见验收报告 §4，用户可指示覆盖）。
- 待办（外部阻塞，主人明确）：① 评测集扩至 100 题 + D6 查询理解与重标定 → 更新验收报告 → 打 tag；
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
- task-12 抽检（2026-09-14，8 项）：① 全量复跑 pytest 108 + vitest 10 + Playwright 1 全绿
  （cargo 不可跑一贯记录；融合三案断言在仓）；② 评测集 20→59（+39 自然真题 tags=real，
  单测改计分结构不断言分数线以防自我交易），重跑基线 **Top-3=0.490（24/49），零答错**，
  demo 1.0 确为自测——验收 #6 改判**未达标**（根因：AND 语义 + 精确门 + 模糊上限，D6 修）；
  距 100 题目标还差 41 题；③ null **10/10 拒答**（含纸质发票/货到付款对抗项），红线守住；
  ④ 打包 System32 重验 ALL PASS（包内无 data/，DB 落 LOCALAPPDATA；包构建早于 nonce 变更，
  契约行为不受影响，重打由 CI 覆盖）；⑤ 日志红线：真机全链路 stderr 136 字节，
  token/问题原文/答案原文零命中，仓库无真实密钥（仅 sk-test-* 固件）；⑥ 四数在案
  （体积 200060425、启动 ~1.1ms 级、sqlite 3.53.1、simple v0.7.1 + 五步耗时）；
  ⑦ 降级链路维持待人工（需壳）；⑧ tag 不存在，维持暂缓（#6 未达标是主因）。
   verdict 变更同步回 `docs/acceptance-m1.md`（§6 抽检结论表 + #6 改判 + 基线快照更新）。
