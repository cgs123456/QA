"""task19 1b 回归测量包：参考端点 + VAD 适配器 + 指标（纯逻辑）。

组成：
- `endpoint.py` —— PROGRESS 既定端点参数的**参考实现**（测量用，非生产路径；
  生产端点在 Rust `endpoint.rs`，落地时须在同一样本上复核本包数字）。
- `vad_providers.py` —— webrtc / silero 的 30ms 帧决策适配器（懒 import，
  缺依赖时报 `Unavailable` 而不是崩）。
- `metrics.py` —— CER/WER、边界匹配、三张表的纯计算。

R14：本包零打印、无日志；转写文本只作函数参数/返回值，不落盘
（报告只含 WER 分子，不贴全文——见 runner）。
"""
