# 1b 音频回归报告（task19 在建，2026-09-14 起）

> 定位：Phase 1b 收尾的数据底盘。VAD 边界误差 / WER / 端到端延迟三张表
> 跑在**用户录制的真实样本**上（`sidecar/tests/audio_samples/`，中英 ×
> 安静/嘈杂/多人 6 槽）；R19（默认 VAD Provider 裁决）的关闭条件是
> “默认 Provider 确定 + 对比数据在案”；平台兼容三行记录在 §5。
>
> **当前状态（诚实版）**：6 样本槽全空 → 三张实测表 PENDING；
> 管线本身已用合成定标验证（SELFTEST_PASS，§4）；
> VAD 默认保持 webrtc-vad，**R19 未关闭**（缺真实嘈杂数据，§3）。

复现入口：`python scripts/audio_regression.py [--out docs/audio-regression.md]`
（`--out` 追加一节 dated run，本文件手工维护的 §1–§5 不会被覆盖）；
管线定标：`python scripts/audio_regression.py --self-test`。

## 1. 方法（口径冻结，改口径需另起一节并注明）

- **样本**：16k / 单声道 / 16-bit WAV + 同名标注 json
 （`transcript` + `segments[{start_ms,end_ms}]` + `noise` + `speakers`），
  录制要求见 `sidecar/tests/audio_samples/README.md`。runner 拒收非 16k/mono/16-bit
  （不重采样——重采样会污染 VAD 对比）；`manifest.json` 的 sha256 对上才测。
- **表1 VAD 边界误差**：wav → 两 provider 逐帧决策（webrtc mode 2；
  silero 阈值 0.5，512 点窗 / hop 480 与 webrtc 帧边界对齐）→ 参考端点
  （`sidecar/src/audio_eval/endpoint.py`，参数逐字取 PROGRESS ①：
  起始 5 帧 3 voiced / 结束 17 帧 hangover / 最短 250ms 丢弃按语音帧计 /
  最长 15s 强制切段）→ 检出段 vs 标注段最大重叠贪心配对：
  平均 |onset| / |offset| 误差（ms）+ missed / false 计数。
  无配对时误差记 `—`（不是 0，0 会伪装成完美）。
  webrtc 侧是 Python wheel（libfvad 同算法、非同一二进制）——相对对比有效，
  绝对值须在 Rust 侧同一样本复核（步骤见 §3），不得直接当生产结论。
- **表2 WER**：整段 wav → `--asr` provider 转写 → zh 按字 CER / en 按词
  （小写）。`stub` 或权重未就位时记 PENDING（不估分）。
  报告只含 errors/total/rate，不贴转写全文（R14）。
- **表3 端到端延迟**：每个标注段 span → 真实 uvicorn + 真实 WS
  （复用 `bench_asr_latency.Server`）→ segment_end 发出 → asr_final 收到，
  `--repeat` 次中位。stub provider 测的是传输+协议下限；真权重才含推理。

## 2. 三张实测表（真实样本；2026-09-14：6 槽全空，全 PENDING）

### 表1 VAD 边界误差

| 样本 | provider | 配上段 | 平均\|onset\|误差 | 平均\|offset\|误差 | missed | false |
|---|---|---|---|---|---|---|
| zh-quiet-1 | webrtc-vad | PENDING（待用户录制） | — | — | — | — |
| zh-quiet-1 | silero-vad | PENDING（待用户录制） | — | — | — | — |
| zh-noisy-1 | webrtc-vad | PENDING（待用户录制） | — | — | — | — |
| zh-noisy-1 | silero-vad | PENDING（待用户录制） | — | — | — | — |
| zh-multi-1 | webrtc-vad | PENDING（待用户录制） | — | — | — | — |
| zh-multi-1 | silero-vad | PENDING（待用户录制） | — | — | — | — |
| en-quiet-1 | webrtc-vad | PENDING（待用户录制） | — | — | — | — |
| en-quiet-1 | silero-vad | PENDING（待用户录制） | — | — | — | — |
| en-noisy-1 | webrtc-vad | PENDING（待用户录制） | — | — | — | — |
| en-noisy-1 | silero-vad | PENDING（待用户录制） | — | — | — | — |
| en-multi-1 | webrtc-vad | PENDING（待用户录制） | — | — | — | — |
| en-multi-1 | silero-vad | PENDING（待用户录制） | — | — | — | — |

### 表2 WER

| 样本 | errors | total | rate |
|---|---|---|---|
| zh-quiet-1 | PENDING（待用户录制；另需 `--asr` 真权重，stub 不估分） | — | — |
| zh-noisy-1 | PENDING（待用户录制；另需 `--asr` 真权重） | — | — |
| zh-multi-1 | PENDING（待用户录制；另需 `--asr` 真权重） | — | — |
| en-quiet-1 | PENDING（待用户录制；另需 `--asr` 真权重） | — | — |
| en-noisy-1 | PENDING（待用户录制；另需 `--asr` 真权重） | — | — |
| en-multi-1 | PENDING（待用户录制；另需 `--asr` 真权重） | — | — |

### 表3 端到端延迟

| 样本 | span(ms) | 样本数 | 中位 | 最小 | 最大 |
|---|---|---|---|---|---|
| zh-quiet-1 | PENDING（待用户录制） | — | — | — | — |
| zh-noisy-1 | PENDING（待用户录制） | — | — | — | — |
| zh-multi-1 | PENDING（待用户录制） | — | — | — | — |
| en-quiet-1 | PENDING（待用户录制） | — | — | — | — |
| en-noisy-1 | PENDING（待用户录制） | — | — | — | — |
| en-multi-1 | PENDING（待用户录制） | — | — | — | — |

## 3. VAD 对比裁决（R19）：默认保持 webrtc-vad，R19 未关闭

**建议（可执行）**：默认 Provider **保持 `webrtc-vad`（aggressiveness 初值 2 不动）**，
`SileroVad` 继续 stub——管线结构不变（R11：两者都吃 int16，`Vad` trait 已钉死形状，
真到切换那天只是换构造源）。

**理由（按强度排序）**：
1. 无证据不切换：嘈杂子集真实对比数据不存在（样本槽全空），任何“silero 更好/
   webrtc 够用”的结论现在都是印象。1b-4 已证明合成信号在四个档全饱和，
   用合成数投票等于掷硬币——拒绝。
2. 现状代价不对称：webrtc 零权重、零下载、已有双静默失效入口的补偿 + 回归测试；
   silero 意味着 `ort` + ONNX 权重进包（体积/延迟预算见 PRD §3.6，W4 范围）。
   没数据就付这笔钱，不符合本地优先的纪律。
3. 方法已就绪：对比 harness（双适配器 + 参考端点 + 切段准确率/missed/false/分钟误触发率）
   已实现并定标，样本一到即出数（`--asr` 无关，VAD 表不依赖 ASR 权重）。

**R19 关闭条件（未满足，逐项打勾）**：
- [ ] 嘈杂子集（zh-noisy-1 + en-noisy-1）真实录音落盘 + 标注；
- [ ] runner 表1 在嘈杂子集上跑出双方数字（切段准确率 + 每分钟误触发率）；
- [ ] Rust 侧同一样本复核 webrtc 绝对值（`cargo test` + 录音，
  消除 Python wheel vs Rust crate 的二进制差异）；
- [ ] 按数据定默认（任一方向）并更新本节 + `vad.rs` 头注释的 “Provider choice is still open”。

## 4. 管线定标（合成，只证“表能跑”，2026-09-14 实跑）

`--self-test` SELFTEST_PASS（RC=0，Windows AMD64 / Python 3.12.10，
webrtcvad-wheels 2.0.14 / silero-vad 6.2.1 / onnxruntime 1.30.0）：

- 纯静音 3990ms：双方全静音、端点零段；
- 合成双语音串（510–2520ms，3510–5520ms）：webrtc 检出 2 段、配上 2/2；
  silero 同串检出 2 段（布线正常，不定标——合成行为不代表真人声）；
- WER 纯函数：全对 0.0、删一词 0.5；
- WS 延迟管线：stub 下限 2ms/段（含真实 uvicorn + WS 往返）。

以上数字**不得引用为性能结论**（合成饱和，见 1b-4）。

## 5. 平台兼容矩阵

| 平台 | 路径 | 状态（2026-09-14） |
|---|---|---|
| Windows | 回环（WASAPI 默认渲染） | 本机设备枚举见下；采集/插拔重建待真机步骤（cargo 缺失，本机不可构建 example） |
| Windows | 耳机插拔重建流 | 待真机：步骤见 §6①，期望 `DeviceChanged` + `StreamRebuilt` 且 seq/ts 不重启 |
| macOS | mic-only | 待 macOS 机器：`cargo test` + 10s 采集回听 |
| Linux | monitor + mic | 待 Linux 机器：`cargo test` + PulseAudio monitor 采集回听 |
| 双路 | ts 偏差 <50ms | 设计已保证（sample-count 派生，`loopback.rs` 10min 合成测试偏差 30ms）；真机双路实测待补 |

本机（Windows）音频设备枚举实测（`Win32_SoundDevice`，2026-09-14）：

- `NVIDIA Virtual Audio Device (Wave Extensible) (WDM)` — OK（注：仅虚拟音频驱动，
  本机无 NVIDIA GPU/`nvidia-smi`，与 task18 结论一致）
- `Conexant SmartAudio HD` — OK（板载声卡；回环源 = 其默认渲染混音，运行时由
  `wasapi_loopback` 打开默认渲染设备确定）

诊断面板：Settings 页「音频诊断」区轮询 `GET /diagnostics/audio`
（最近一次 WS 连接计数 + 限额，R14 内容无关；`test_diagnostics.py` 3 绿）。
**当前设备与实时双路偏差**两行由 Rust 采集服务拥有（`current_device()` /
`CaptureStats` 已在 trait 上），待 endpoint.rs 落地接线后由 Tauri 命令补——
面板已预留 `capture.status: pending` 位，不编造数据。

## 6. 真机步骤（task19 余量未关项，按平台认领）

① Windows 插拔重建：先播放音乐 → `cargo run --release --example record_loopback -- 10 out.wav`
→ 播放中拔/插耳机（或切换默认输出）→ 事件流应出现 `DeviceChanged` + `StreamRebuilt`，
且 seq/ts 不重启（`set_input_rate` 只换 resampler）；回听 `out.wav` 确认内容连续。
② macOS：`cargo test` 全绿 + `record_*` 10s 回听 + 页首显示 mic-only。
③ Linux：同上（monitor）+ `cargo test`。
④ 双路 ts：loopback + mic 同跑 10min，逐帧 `|ts_loopback - ts_mic|`（同序号）<50ms，
数字贴回本节。
⑤ 用户录制 6 样本 + 标注 → `python scripts/audio_regression.py --asr faster-whisper
--out docs/audio-regression.md` → 表1–3 落数 → 按 §3 清单关 R19。
