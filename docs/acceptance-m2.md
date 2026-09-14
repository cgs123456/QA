# M2（音频链路）验收报告（PRD §6.2，2026-09-14 起）

> PRD 文件不在仓内：下表按 1b 任务史（1b-1～task19 的 R9–R14、端点、VAD、ASR、
> 时延、平台范围）+ 本任务的两道硬门槛重建；凡重建项均标“（重建）”，
> 与原文有出入以 PRD 为准并同步回本文档。
> 执行方式：本机可跑项全部实跑（命令 + 输出摘录见下）；需外部输入项如实标记，
> 不估分、不借数。
> 结论：**9 通过 / 2 有条件通过（门槛1固定答案：仅 paraformer 配置；M2-9 切换：历史证据）/
> 1 阻塞（门槛1生成答案：无 Ollama）/ 4 未达标（端点状态机、R19、平台真机、vendor 债务）**。
> `m2-audio-pipeline` tag **暂缓**（见 §4）。

## 1. 逐项验收（§6.2 重建表）

| # | 测试项（标准） | 结论 | 证据（命令 + 输出摘录） |
|---|---|---|---|
| 1 | WS 鉴权 R9（无/错 token → 1008；真实 uvicorn 403 形态已记录） | 通过 | `pytest sidecar/tests/test_audio_protocol.py` 17 passed（含 `test_auth_missing_close_1008` / `test_auth_wrong_close_1008`）；task-14 的 403 实测形态记入 api-contract |
| 2 | 序号/seg_id/单连接一路 R10（共享序号空间、跳号重同步、`seg_{n}`） | 通过 | 同上 17 passed（含跳号、冲突路径、重复 start→interrupted）；`duration_ms` 帧数×30ms（±1 帧，benchmark 已记） |
| 3 | VAD 只在 Rust 侧、吃 int16 R11（双 provider 同形状） | 通过 | `vad.rs`：`Vad` trait（非 Send 诚实契约）+ `WebrtcVad`（16kHz 钉死 + reset 补偿）+ `SileroVad` stub； complications 见 1b-4（cargo 本机不可跑，代码走读 + 历史 `cargo test` 记录） |
| 4 | 下行契约 R12（asr_start/final/error + provider/degraded + partial 同段 id） | 通过 | 协议单测 + `test_low_latency.py` 10 passed（partial 与 final 同 `segment_id`、迟到探测丢弃）；契约见 api-contract WS/事件面两节 |
| 5 | 背压有界 R13（待转写≤8、单段≤2000 帧丢最旧、发送锁+5s 超时） | 通过 | 单测锁定丢最旧/overload/截断告警各计数；门槛2 soak 实证 604s 零 overload（§2） |
| 6 | 内容无关 R14（零打印、文本只进下行、日志无密钥/原文） | 通过 | `test_diagnostics.py` 断言诊断响应无转写文本；M1 抽检(stderr 136B 零命中）同纪律沿用；本轮报告只记转写长度不记内容 |
| 7 | 端点状态机（起始 5 帧 3 voiced；结束 ~500ms hangover；边界误差 <1 帧；双路互不干扰） | **未达标（缺件）** | `endpoint.rs` 仍未落地（PROGRESS 下一项①）；参考实现 + 单测在仓（`audio_eval/endpoint.py`），合成定标通过；真机边界误差待端点落地 + 用户录音（audio-regression §6） |
| 8 | VAD 默认 + R19 关闭（嘈杂子集对比数据在案） | **未达标（缺数据）** | 对比 harness 就绪并定标（webrtc/silero 真跑），但 6 样本槽全空；默认保持 webrtc-vad；关闭清单见 audio-regression §3 |
| 9 | 三 Provider 可切换（whisper/SenseVoice/Paraformer 同一样本出文本；切换免重启；断点续传） | 有条件通过 | task17：横向对比数字在案（benchmark §三 Provider）+ `test_asr_switch.py` 8 条；本机复跑受阻（api_client fixture 缺 vendor，见 #14）；断点续传由 downloader Range 机制 + 本轮 148MB/243MB 两次真实下载成功佐证 |
| 10 | 整段时延口径（segment_end→asr_final，真实 WS；超时按 3× 段时长派生） | 通过 | task-15 实测 + 本轮门槛1复测（faster-whisper 18.0s / paraformer 1.14s，同机，见 §2）；派生超时单测锁定 |
| 11 | 低延迟门控（默认关；开需 CUDA；关即 task15 行为） | 通过 | `test_low_latency.py` 10 passed + `test_local_agreement.py` 13 passed；本机开 → 409（门控单测锁定）；GPU 首片段待 CUDA 机器（task-18 记账） |
| 12 | 诊断端点 + 面板（计数 + 限额；设备行不编造） | 通过（后端）/ 待目检（前端） | `test_diagnostics.py` 3 passed + 鉴权覆盖表已同步；Settings 面板已写，`tsc` 本机不可跑（无 node_modules） |
| 13 | 平台矩阵（Win 回环+插拔重建；macOS mic；Linux monitor；双路 ts<50ms；面板显设备+偏差） | **未达标（缺真机）** | 仅 Windows 设备枚举实测（Conexant + NVIDIA 虚拟音频）；其余为程序 + 待认领（audio-regression §5–§6）；面板设备行 pending 已明示 |
| 14 | 全量回归（pytest/vitest/Playwright；vendor/工具链债务） | 有条件通过 | 本机 pytest **137 passed**；16 error + 1 fail 全系缺 `sidecar/vendor/libsimple.dll`（GitHub 不通取不到，P0 纪律停等）；cargo/vitest/tsc 本机不可跑（一贯记录） |
| G1 | 硬门槛1：固定答案 ≤4s / 生成答案 ≤6s（分解耗时） | **固定 PASS（paraformer 配置）/ 生成阻塞** | 见 §2：paraformer 合成总账 **1147ms**；faster-whisper 18.0s FAIL（同机高负载）；生成路径无 Ollama 不可测（M1#10 同状） |
| G2 | 硬门槛2：双路 10 分钟零丢段/零污染/内存不泄漏增长 | **通过（sidecar 范围）** | 见 §2：100+100 段全收、hash 零失配、RSS +3.6MB/604s；Rust 采集侧（设备/重建/ts 偏差）不在本门槛内，待 §6 程序 |

## 2. 两道硬门槛详录

### G1 CPU 整段路径（2026-09-14，本机，Windows AMD64 / Haswell Family 6 Model 60 / 4 核 / Python 3.12.10）

前置状态（必须先写，否则数字不可比）：本机被无关进程占满——`xray.exe` 140% +
`tun2proxy` 90%（`.workbuddy-ai/topcpu.txt` 实录），推理仅剩约 1.5 核老 CPU；
内存可用仅 1.3GB/7.9GB。以下数字是**高负载下的诚实读数**，不是安静箱体成绩。

权重（两次真实下载，下载器 SHA 全过，与 registry 逐字节一致）：
`faster-whisper-base/model.bin` 145,217,532B（188s）；
`paraformer-zh/model.int8.onnx` 243,371,218B（354s）。

复现：`python scripts/e2e_m2_gate1.py --provider {faster-whisper,paraformer} --seconds 3 --repeat 3`
（真实权重 + 真实 uvicorn + 真实 WS；音频合成——时延有效文本无意义，文本只记长度）。

| 配置 | 冷加载 | segment_end→asr_final（中位/最小/最大，n=3） | +直接路径 2.4ms（M1#9 同代码） | 合成总账 | 4000ms |
|---|---|---|---|---|---|
| faster-whisper base/int8/CPU（默认） | 5543ms | **17998.6** / 17368.4 / 18417.1 | +2.4 | **18001ms** | **FAIL** |
| paraformer int8/CPU | 7186ms | **1144.6** / 1131.2 / 1439.7 | +2.4 | **1147ms** | **PASS（余量 3.5×）** |

分解说明：问答直接路径本机跑不起来（QA 链需 vendor，GitHub 不通），引用 M1 同代码路径
ask→done 2.4ms；传输开销已含在 ASR 段内；VAD 判定在 Rust 侧（30ms/帧）不在本区间
（bench 口径沿用）。合成总账 = ASR 实测 + 2.4ms——唯一的非实测加项已单列，
不混入“端到端实测”字样。

**方案定档建议（数据驱动，请主人裁定）**：中文场景默认 provider 切到 `paraformer`
（本机高负载仍 1.14s；task17 横向对比中文最快且内存更小；代价：非中文场景不可用，
需按语言选路或保留 faster-whisper 为多语种 fallback——切换机制 task17 已就绪，
改的只是一行默认值）。默认暂未改，等明确指示。

生成答案 ≤6s：**阻塞，不可判**。Ollama 127.0.0.1:11434 拒连（M1#10 同状）；
LLM 段无数字。参考分解：paraformer 路径已用 1147ms，留给 LLM 约 4.8s 预算——
是否够用待 Ollama 就绪后 `scripts/e2e_ollama.py` + 本脚本联测。

### G2 双路 10 分钟 soak（2026-09-14，本机，sidecar 范围）

复现：`python scripts/e2e_m2_gate2_soak.py --minutes 10 --seg-s 5 --gap-s 1`
（两条真实 WS 连接 loopback/mic，按 30ms 真实节拍各 600s，每路 100 段；
HashProvider 全局注入：`text = path#sha256(pcm)[:16]`，逐段核对——串路必失配）。

```
loopback: segs=100 ends=100 finals=100 timeouts=0 mismatch(seg/path/hash)=0/0/0 e2e_med=6.6ms
mic:      segs=100 ends=100 finals=100 timeouts=0 mismatch(seg/path/hash)=0/0/0 e2e_med=6.3ms
rss_base=71.1MB growth=3.6MB budget=50.0MB wall=604s
GATE2_PASS
```

- 无丢段：ends_sent == finals_received（100/100 双路），零超时，segment_id 连续
  （任一段错序/错 id 即记 mismatch，全零）。
- 无交叉污染：path 标签 + pcm hash 双核对 200/200 全对（provider 全局共享、
  缓冲按连接隔离——错一路即现形）。
- 无泄漏增长：RSS 71.1MB → +3.6MB/604s（含 Python 分配器碎片量级），远低于 50MB 线。
- e2e 中位 ~6ms 系 HashProvider 下限口径（推理容量见 G1，不混为一谈）。
- 范围外（未测，不断言）：Rust 采集侧设备枚举/插拔重建/双路 ts 偏差——
  程序与待认领见 audio-regression §5–§6。

## 3. R19 与端点：未关项的精确状态（防“ someday”烂尾）

- R19（默认 VAD）：保持 webrtc-vad；关闭三条件（嘈杂样本 + 表1 + Rust 复核）全空；
  对比 harness 已定标，用户录音一到即出数。
- 端点状态机：`endpoint.rs` 未落地是 M2-7 唯一的缺件；参考实现 + 15 单测在仓，
  落地后须在同一样本上复核（runner 口径已冻结，不随实现漂移）。
- 真人声闭环：task-15/1b-4/task-19 的三处“合成不能替代”声明仍然有效；
  用户录音同时关闭 WER、VAD 对比、端点边界三笔账（audio-regression §6⑤）。

## 4. Tag 判定：暂缓 `m2-audio-pipeline`

| 阻塞项 | 性质 | 清除条件 |
|---|---|---|
| M2-7 端点状态机缺件 | 功能缺口 | `endpoint.rs` 落地 + 边界误差<1 帧实测 |
| M2-8 R19 未关 | 数据缺口 | 嘈杂子集表1 + Rust 复核 + 定默认 |
| G1 生成答案 | 环境（无 Ollama） | Ollama 就绪后联测 ≤6s |
| M2-13 平台真机 | 环境（无 cargo/无三机） | §6 程序逐项贴数 |
| vendor 债务 | 环境（GitHub 不通） | 取数后全量 pytest 复绿 + QA 链真机联测 |
| M2-12 前端目检 | 环境（无 node_modules/壳） | `tsc` + 诊断面板目检 |

通过项已用 cited 命令 + 输出摘录锁定；待办已记入 PROGRESS（沿用 task-19 余量条目，
另加本轮 G1 定档建议与 tag 判定）。用户可明确指示“按当前范围打 tag”以覆盖本判定
（M1 §4 同款程序）。
