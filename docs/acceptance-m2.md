# M2（音频链路）验收报告（PRD §6.2）

> **本版为第二版，2026-09-15 在「本机」（下表 B 机）重跑。** 第一版（2026-09-14）
> 跑在另一台机器上，其环境缺 cargo / git / vendor 二进制 / node_modules，
> 导致 6 项记「环境阻塞」。本机这些依赖齐备，故**逐项重跑并把阻塞项换成实测**。
> 两版数字**不可直接比较**（机器规格差一个量级，见 §0）。
>
> PRD 文件不在仓内：下表按 1b 任务史（1b-1～task19 的 R9–R14、端点、VAD、ASR、
> 时延、平台范围）+ 本任务的两道硬门槛重建；凡重建项均标「（重建）」，
> 与原文有出入以 PRD 为准并同步回本文档。
> 执行方式：本机可跑项**全部实跑**（命令 + 输出摘录见下）；需外部输入项如实标记，
> 不估分、不借数。
>
> **结论：11 通过 / 1 部分通过（平台矩阵：Windows 本机实测 + 全平台 cargo 测试绿，
> macOS/Linux 真机缺）/ 2 未达标（M2-7 端点状态机缺件、M2-8 R19 缺真实嘈杂数据）**；
> 门槛1 **固定答案三 Provider 全 PASS**（生成答案仍阻塞：无 Ollama）；
> 门槛2 **GATE2_PASS**。
> `m2-audio-pipeline` tag **暂缓**（剩余 4 项均非本机可闭，见 §4）。

## 0. 机器与可比性（先写，否则数字不可比）

| | A 机（第一版，2026-09-14） | **B 机（本版，2026-09-15）** |
|---|---|---|
| CPU | Haswell Family 6 Model 60，**4 核** | **AMD Family 25 Model 80，12 逻辑核** |
| 内存 | 7.9GB 总 / **1.3GB 可用** | 31.9GB 总 / **20.3GB 可用** |
| 负载 | `xray.exe` 140% + `tun2proxy` 90%（推理仅剩 ~1.5 核） | 无异常占满（跑门槛前已确认） |
| Python | 3.12.10 | **3.13.14** |
| Rust 工具链 | **无 cargo**（Rust 项一律不可验证） | **cargo/rustc 1.98.1 + MSVC 2022** |
| git | 无 | 2.55.0.windows.3 |
| `sidecar/vendor/libsimple.dll` | **缺**（GitHub 不通取不到）→ 16 error + 1 fail | **就位**（1.5MB） |
| node_modules | 无（vitest/tsc 不可跑） | 就位（pnpm 布局，154 包） |
| 模型权重 | faster-whisper-base + paraformer-zh | + **sense-voice**（三 provider 齐） |

结论：A 机的「环境阻塞」在 B 机**全部消失**，因此本版是更完整的验收；
A 机的两项 FAIL（faster-whisper 18.0s、pytest 16 error）**经本机复测证明是机器负载与缺件所致，
不是代码缺陷**——见 §2 与 §1#14。

## 1. 逐项验收（§6.2 重建表）

| # | 测试项（标准） | 结论 | 证据（命令 + 输出摘录） |
|---|---|---|---|
| 1 | WS 鉴权 R9（无/错 token → 1008；真实 uvicorn 403 形态已记录） | **通过** | `pytest tests/test_audio_protocol.py` → **17 passed**（含 `test_auth_missing_close_1008` / `test_auth_wrong_close_1008`）；A 机 task-14 的 403 实测形态记入 api-contract（**偏差仍待裁定**，见 §3） |
| 2 | 序号/seg_id/单连接一路 R10（共享序号空间、跳号重同步、`seg_{n}`） | **通过** | 同 17 passed（含跳号、冲突路径、重复 start→interrupted）；`duration_ms` 帧数×30ms（±1 帧） |
| 3 | VAD 只在 Rust 侧、吃 int16 R11（双 provider 同形状） | **通过（本机可编译验证）** | `cargo test --lib` → **74 passed**（含 `audio::vad::*`、`audio::loopback::tests::pipeline_ten_minute_timestamp_budget`）；`cargo clippy --all-targets -D warnings` **0 告警**。A 机此项只能走读，本机为真编译 + 真测试 |
| 4 | 下行契约 R12（asr_start/final/error + provider/degraded + partial 同段 id） | **通过** | `test_audio_protocol` 17 + `test_low_latency` **10 passed**（partial 与 final 同 `segment_id`、迟到探测丢弃）+ `test_local_agreement` **13 passed** |
| 5 | 背压有界 R13（待转写≤8、单段≤2000 帧丢最旧、发送锁+5s 超时） | **通过** | 协议单测锁定丢最旧/overload/截断告警各计数；门槛2 soak **601s 零 timeout 零 overload**（§2） |
| 6 | 内容无关 R14（零打印、文本只进下行、日志无密钥/原文） | **通过** | `test_diagnostics` **3 passed** 断言诊断响应无转写文本；本报告只记转写**长度**不记内容；`test_asr_text_clean` 13 passed（清洗后文本只进下行） |
| 7 | 端点状态机（起始 5 帧 3 voiced；结束 ~500ms hangover；边界误差 <1 帧；双路互不干扰） | **未达标（缺件）** | `src-tauri/src/audio/endpoint.rs` **仍不存在**（`audio/mod.rs` 未导出该模块）。参考实现 + 单测在仓且本机跑绿（`audio_eval/endpoint.py`，`test_audio_eval` **15 passed**）。缺件是唯一原因，非环境 |
| 8 | VAD 默认 + R19 关闭（嘈杂子集对比数据在案） | **未达标（缺数据）** | `sidecar/tests/audio_samples/manifest.json` 6 槽**全 `missing`**（无 wav/标注）。对比 harness 本机**双 provider 全跑通**（`audio_regression.py --self-test` → **SELFTEST_PASS**，webrtc + silero 均实跑），样本一到即出数。默认保持 webrtc-vad |
| 9 | 三 Provider 可切换（whisper/SenseVoice/Paraformer 同一样本出文本；切换免重启；断点续传） | **通过（本机可跑）** | A 机此项因缺 vendor 只能记「历史证据」，**本机可跑**：`test_asr_switch` **8 passed**（含「同一 WS 连接内切换后下一段换 provider」）+ `test_asr_text_clean` **13 passed** + `test_asr_fallback` **25 passed**；断点续传 `test_downloader` **5 passed**（含 `test_resume_from_part`）；横向对比见 `docs/benchmark.md` |
| 10 | 整段时延口径（segment_end→asr_final，真实 WS；超时按 3× 段时长派生） | **通过（三 provider 全部实测）** | 门槛1 本机复测（§2）：faster-whisper **2195.5ms** / paraformer **183.3ms** / sensevoice **203.8ms**（n=3，真实 uvicorn + 真实 WS）；派生超时单测锁定 |
| 11 | 低延迟门控（默认关；开需 CUDA；关即 task15 行为） | **通过** | `test_low_latency` 10 + `test_local_agreement` 13 passed；本机无 CUDA → 开启请求 409（门控单测锁定）；GPU 首片段仍待 CUDA 机器（task-18 记账） |
| 12 | 诊断端点 + 面板（计数 + 限额；设备行不编造） | **通过（后端 + 前端类型/构建）** | `test_diagnostics` 3 passed + `test_auth_coverage` 2 passed（R2 覆盖表含 `/diagnostics/audio`、`/asr/providers`、`/asr/latency`）；前端 `tsc --noEmit` **0 错误** + `eslint` **0 错误** + `vite build` 成功；面板见 `Settings.tsx:397-426`（计数/限额/`capture.status=pending` 显式不编造）。**目检仍待 Tauri 壳** |
| 13 | 平台矩阵（Win 回环+插拔重建；macOS mic；Linux monitor；双路 ts<50ms；面板显设备+偏差） | **部分通过** | **Windows 设备枚举本机实测**（`Win32_SoundDevice`，4 个）：`Realtek High Definition Audio`、`NVIDIA High Definition Audio`、`AMD High Definition Audio Device`、`NVIDIA Virtual Audio Device (Wave Extensible) (WDM)`；全平台音频代码 `cargo test` **78 passed**（lib 74 + `audio_frames_wav` 1 + `audio_ws_e2e` 3）；`audio::resample::tests::ten_minutes_no_drift` 锁定 10min ts 偏差 30ms（<50ms）。**插拔重建 / macOS / Linux 仍缺真机**（§6） |
| 14 | 全量回归（pytest/vitest/Playwright；vendor/工具链债务） | **通过（全绿）** | **pytest 229 passed / 0 failed / 0 skipped**（A 机为 137 passed + 16 error + 1 fail，全系缺 vendor）；**vitest 89 passed**（4 文件）；**tsc 0**；**eslint 0**；**cargo test 全目标 78 passed**；**clippy 0 告警**；**playwright 1 passed**；`vite build` 成功（82 modules，js 286.19kB/gzip 89.10kB）；`check_size.py` → **SIZE_REGRESSION_PASS**。唯一未绿：`cargo fmt --check`（4 个既有文件，**非本次引入**，见 §3） |
| G1 | 硬门槛1：固定答案 ≤4s / 生成答案 ≤6s（分解耗时） | **固定答案三 Provider 全 PASS / 生成答案阻塞** | 见 §2：faster-whisper 合成总账 **2202.3ms**、paraformer **190.1ms**、sensevoice **210.6ms**，预算 4000ms。生成路径**无 Ollama 仍不可测**（与 A 机同状） |
| G2 | 硬门槛2：双路 10 分钟零丢段/零污染/内存不泄漏增长 | **通过（GATE2_PASS）** | 见 §2：双路各 100 段全收、hash 零失配、RSS +3.9MB/601s |

## 2. 两道硬门槛详录

### G1 CPU 整段路径（2026-09-15，B 机，Windows AMD64 / AMD Family 25 Model 80 / 12 逻辑核 / Python 3.13.14）

权重：`faster-whisper-base/model.bin` 145,217,532B、`paraformer-zh/model.int8.onnx` 243,371,218B、
`sense-voice/model.int8.onnx` 239,233,841B —— 三者 SHA256 与 registry 逐字节一致（task17 已复核）。

复现：`python scripts/e2e_m2_gate1.py --provider {faster-whisper,paraformer,sensevoice} --seconds 3 --repeat 3`
（真实权重 + 真实 uvicorn + 真实 WS；音频为合成类语音——**时延有效、文本无意义**，
按 R14 只记长度不记内容）。

| 配置 | 冷加载 | segment_end→asr_final（中位/最小/最大，n=3） | QA 直接路径（本机实测） | 合成总账 | 4000ms |
|---|---|---|---|---|---|
| faster-whisper base/int8/CPU（**当前默认**） | 2923.6ms | **2195.5** / 2179.8 / 2223.8 | +6.82 | **2202.3ms** | **PASS（余量 1.8×）** |
| paraformer int8/CPU | 1829.6ms | **183.3** / 181.9 / 191.0 | +6.82 | **190.1ms** | **PASS（余量 21×）** |
| sensevoice int8/CPU | 1473.9ms | **203.8** / 203.3 / 205.8 | +6.82 | **210.6ms** | **PASS（余量 19×）** |

**分解口径（比第一版更硬）**：问答那一项**不再是引用常量**。第一版因缺 vendor 只能引用
acceptance-m1 #9 的 2.4ms；本机 `sidecar/vendor/libsimple.dll` 就位，新增
`scripts/e2e_m2_gate1_qa.py` 实测**真实链路**：真实 sidecar 子进程（临时 DB）+ 真实 HTTP/SSE，
问题用**门槛原话**「发货周期是多久」（`seed_demo.json` 有对应官方答案 → `direct` 档、**零 LLM**），
n=10：**中位 6.82ms** / 最小 6.17 / 最大 23.13（预热 2420.27ms 已单列不计入）。
合成总账 = ASR 实测 + QA 实测，两项**都是实测**，无非实测加项。

> **撤回第一版的一项建议（重要）**：第一版据「faster-whisper 18.0s FAIL」建议
> 「中文默认切 paraformer」。**该建议作废**——A 机的 18.0s 是被 `xray.exe`/`tun2proxy`
> 占满（推理仅剩 ~1.5 核）所致；B 机同配置 **2195.5ms，PASS**。
> 因此**默认 provider 无需改动**（faster-whisper 保持多语种默认，paraformer/sensevoice
> 作为中文快速档可选，切换机制 task17 已就绪）。选型由 `docs/benchmark.md` 横向对比决定。

生成答案 ≤6s：**阻塞，不可判**。本机未安装 Ollama（`ollama` 不在 PATH，三个常见安装路径均无，
`127.0.0.1:11434` 真拒连——已确认非本机 http_proxy 干扰）。LLM 段无数字。
参考分解：最慢的 faster-whisper 路径用 2202.3ms，留给 LLM 约 **3.8s** 预算；
paraformer/sensevoice 路径留给 LLM 约 5.8s。是否够用待 Ollama 就绪后
`scripts/e2e_ollama.py` + 本脚本联测。

### G2 双路 10 分钟 soak（2026-09-15，B 机，sidecar 范围）

复现：`python scripts/e2e_m2_gate2_soak.py --minutes 10 --seg-s 5 --gap-s 1`
（两条真实 WS 连接 loopback/mic，按 30ms 真实节拍各 600s，每路 100 段；
HashProvider 全局注入：`text = path#sha256(pcm)[:16]`，逐段核对——串路必失配）。

```
loopback: segs=100 ends=100 finals=100 timeouts=0 mismatch(seg/path/hash)=0/0/0 e2e_med=1.5ms
mic:      segs=100 ends=100 finals=100 timeouts=0 mismatch(seg/path/hash)=0/0/0 e2e_med=1.3ms
rss_base=70.6MB growth=3.9MB budget=50.0MB wall=601s
GATE2_PASS
```

- **无丢段**：`ends_sent == finals_received`（100/100 双路），零超时，`segment_id` 连续
  （任一段错序/错 id 即记 mismatch，全零）。
- **无交叉污染**：path 标签 + pcm hash 双核对 **200/200 全对**（provider 全局共享、
  缓冲按连接隔离——错一路即现形）。
- **无泄漏增长**：RSS 70.6MB → **+3.9MB/601s**，远低于 50MB 线（A 机同项 +3.6MB，一致）。
- e2e 中位 ~1.4ms 系 HashProvider 下限口径（推理容量见 G1，不混为一谈）。
- 范围外（未测，不断言）：Rust 采集侧设备枚举/插拔重建/双路 ts 偏差——
  程序与待认领见 `docs/audio-regression.md` §5–§6。

## 3. 未关项与既有债务的精确状态（防「someday」烂尾）

**A. 未达标 2 项**
- **M2-7 端点状态机缺件**：`endpoint.rs` 不存在是唯一原因。规格已冻结
  （起始=最近 5 帧中 ≥3 帧 voiced；结束=连续 17 帧静音 hangover；最短段按**语音帧** ≥9 帧
  保留；最长段 500 帧强制切段；`vad_state` 心跳 1s），参考实现
  `sidecar/src/audio_eval/endpoint.py` + `test_audio_eval` 15 例在本机跑绿，
  落地后须在同一样本上复核（runner 口径已冻结，不随实现漂移）。
- **M2-8 R19 未关**：6 样本槽全空 → 嘈杂子集对比数据不存在。harness 本机已**双 provider 定标**
  （`SELFTEST_PASS`）。关闭三条件：嘈杂样本 + 表1 + Rust 侧 webrtc 绝对值复核。

**B. 环境/真机阻塞 3 项**
- **G1 生成答案**：无 Ollama（本机未安装）。
- **M2-13 平台真机**：Windows 设备枚举本机已实测；插拔重建、macOS、Linux 需对应机器。
- **M2-12 前端目检**：`tsc`/`eslint`/`vite build` 本机已绿；页面目检需 Tauri 壳。

**C. 既有债务（非本次引入，本机复现）**
- `cargo fmt --check` 在 4 个未触碰文件上失败：`security/keychain.rs:15`、
  `sidecar/degradation.rs:43`、`sidecar/manager.rs:26/293/305/372/473/495`
  （均为换行重排、无语义变化）。**本轮未跨文件重排**，避免把无关 diff 混进验收提交；
  是否全仓 `cargo fmt` 待裁定。
- **task-14 DoD 字面「错 token → 1008」vs 实测 403**：偏差仍在（已记 api-contract）。

**D. 真实数据缺口（合成不能替代，需用户投喂）**
- WER / VAD 边界误差 / 端点边界三笔账都卡在**同一份 6 样本录音 + 标注**
  （指南 `sidecar/tests/audio_samples/README.md`）。
- task-15 ASR 文本质量、1b-4 VAD 真人声自测、task-16 阈值标定（100 题集）同源阻塞。

## 4. Tag 判定：仍暂缓 `m2-audio-pipeline`

第一版的 6 项阻塞中，**4 项已由本机环境消除**（cargo/vendor/node_modules/前端工具链），
且两项 FAIL 经复测证明是机器负载所致。但剩余 4 项**均非本机可闭**：

| 阻塞项 | 性质 | 清除条件 |
|---|---|---|
| M2-7 端点状态机缺件 | 功能缺口（可写码） | `endpoint.rs` 落地 + 边界误差 <1 帧实测 |
| M2-8 R19 未关 | 数据缺口（需用户） | 嘈杂子集录音 + 表1 + Rust 复核 + 定默认 |
| G1 生成答案 | 环境（需装 LLM 运行时） | Ollama 就绪后联测 ≤6s |
| M2-13 平台真机 | 环境（需 macOS/Linux 机器） | 各跑 `cargo test` + 采集回听 + 双路 ts 贴数 |
| M2-12 前端目检 | 环境（需 Tauri 壳） | 诊断面板目检 |

**建议**：先做 M2-7（唯一可写码闭合项，且规格与参考实现都已冻结），
再等用户投喂 6 样本录音——两项一齐关掉后，剩余就只剩「装 Ollama」和「借机器」，
tag 才名副其实。用户也可明确指示「按当前范围打 tag」以覆盖本判定（M1 §4 同款程序）。
