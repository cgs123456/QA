"""流式部分结果：LocalAgreement 前缀确认 + 滚动缓冲探测调度（task18）。

算法参考 whisper_streaming 的 LocalAgreement（注意其后继是 SimulStreaming），
**仅借思想、自行实现**：上游是为 whisper 写的在线包装，本项目要同时服务
faster-whisper / SenseVoice / Paraformer 三路 provider，且输入是已按 30ms
切好的 float32 帧、输出要进既有 `asr_partial` 下行契约 —— 直接搬运反而要
削足适履。

## 规则（DoD 口径）

1. 语音活跃期，每 `probe_every_frames` 帧（默认 50 帧 ≈ 1.5s）对**滚动缓冲**
   （本段至今的全部音频）转写一次；
2. 连续两次假设的**公共前缀**才确认为可输出文本（`LocalAgreement.update`）；
3. 已确认文本**只增不减**（单调：候选前缀必须以已确认文本开头，否则按兵不动，
   绝不在下行里撤回已吐出的字）；
4. 静音期暂停探测（见 `SegmentStreamer` 的能量门；不是 VAD —— 分段判定仍在
   Rust 侧（R11），这里只决定“值不值得花一次转写”，见下）；
5. `segment_end` 到达时仍走整段转写出 `asr_final`（task15 路径不动）；
   partial 与 final 共用同一个 `segment_id`（坑位要求，由调用方保证：
   streamer 只产文本，id 由 `routers/audio.py` 的段状态机填写）。

## 为什么是“前缀一致”而不是“直接吐最新假设”

单次滚动转写的前缀经常在下一轮被修正（尤其句尾）。直接吐最新假设，
前端会出现“字在跳动”；两次一致才确认，用最多一轮的延迟换来已输出部分
**永不撤回** —— 提词场景下“稳定”比“早 1.5s”重要。

## R14

本模块零打印；转写文本只作返回值。能量值、候选前缀长度等诊断量由调用方
决定是否记录，本模块不记日志。
"""

import math
import struct

# 30ms / 480 samples / 16kHz：与 routers/audio.py 的线格式同源。
FRAME_MS = 30
FRAME_SAMPLES = 480

# 默认探测节拍：50 帧 ≈ 1.5s（DoD“每 1-2s”区间内取整）。
DEFAULT_PROBE_EVERY_FRAMES = 50
# 缓冲至少攒到这么多才值得第一次探测（~1s，太短的音频转写全是幻觉）。
DEFAULT_MIN_PROBE_FRAMES = 33
# 静音能量门（RMS）：低于此值视为静音。初值，待真机标定（见 benchmark.md 记账）。
DEFAULT_SILENCE_RMS = 0.01
# 最近这么多帧全静音 → 暂停探测（17 帧 ≈ 0.5s）。
DEFAULT_SILENCE_WINDOW_FRAMES = 17


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF
            or 0xF900 <= o <= 0xFAFF or 0x3040 <= o <= 0x30FF
            or 0xAC00 <= o <= 0xD7AF)


def tokenize(text: str) -> list:
    """把假设切成前缀比较用的 token：CJK 逐字，非 CJK 按连续段。

    join 回来必须无损（`"".join(tokenize(t)) == t`，单测锁定）——
    否则“已确认文本”与 provider 原文对不上，前端做高亮/去重全错。
    """
    tokens: list = []
    buf: list = []

    def _flush():
        if buf:
            tokens.append("".join(buf))
            del buf[:]

    for ch in text or "":
        if _is_cjk(ch):
            _flush()
            tokens.append(ch)
        elif ch.isspace():
            _flush()
            tokens.append(ch)
        else:
            buf.append(ch)
    _flush()
    return tokens


def common_prefix(a: list, b: list) -> list:
    """两 token 序列的最长公共前缀。"""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return a[:i]


def frame_rms(payload: bytes) -> float:
    """单帧（float32 LE ×480）能量。长度非法 → 按静音处理（不抛：门控不配崩链路）。

    不用 numpy：sidecar 不依赖它（torch 的 warning 已证明这点），480 点纯
    stdlib 足够（每帧一次，~33次/s，可忽略）。
    """
    if len(payload) != FRAME_SAMPLES * 4:
        return 0.0
    total = 0.0
    for (v,) in struct.iter_unpack("<f", payload):
        total += v * v
    return math.sqrt(total / FRAME_SAMPLES)


class LocalAgreement:
    """两次一致才确认的单调前缀策略（whisper_streaming LocalAgreement-2 思想）。

    - `update(hypothesis)` → 本轮新确认的**增量**（"" 表示无新增）；
    - `confirmed` 属性是至今确认的全文（只增不减）；
    - 候选前缀不以已确认文本开头时（假设摇摆），保持原确认不动 ——
      宁可少吐，不撤回。
    """

    def __init__(self):
        self._prev_tokens: list | None = None
        self.confirmed: str = ""

    def update(self, hypothesis: str) -> str:
        tokens = tokenize(hypothesis or "")
        if self._prev_tokens is None:
            # 第一轮假设没有“上一次”可对照，只记录不确认。
            self._prev_tokens = tokens
            return ""
        candidate = "".join(common_prefix(self._prev_tokens, tokens))
        self._prev_tokens = tokens
        if len(candidate) > len(self.confirmed) and candidate.startswith(self.confirmed):
            delta = candidate[len(self.confirmed):]
            self.confirmed = candidate
            return delta
        return ""


class SegmentStreamer:
    """一段音频内的流式探测状态机（无 IO，routers/audio.py 驱动）。

    - `on_frame(payload)` → `"probe" | None`：攒够节拍且非静音时请求一次探测；
      调用方负责真正调 provider（需 await），拿到文本后调 `on_hypothesis`；
    - `on_hypothesis(text)` → 确认全文（与上次下发相同则调用方不必重发）；
    - `snapshot_pcm()` → 当前滚动缓冲（本段至今全部帧拼接）；
    - 静音计数只看最近窗口：说话一回来立刻恢复（不锁死）。
    """

    def __init__(self, *,
                 probe_every_frames: int = DEFAULT_PROBE_EVERY_FRAMES,
                 min_probe_frames: int = DEFAULT_MIN_PROBE_FRAMES,
                 silence_rms: float = DEFAULT_SILENCE_RMS,
                 silence_window_frames: int = DEFAULT_SILENCE_WINDOW_FRAMES):
        self._probe_every = max(1, probe_every_frames)
        self._min_frames = max(1, min_probe_frames)
        self._silence_rms = silence_rms
        self._silence_window = max(1, silence_window_frames)
        self._frames: list = []
        self._recent_rms: list = []
        self._since_probe = 0
        self.agreement = LocalAgreement()
        self.probes_requested = 0

    @property
    def buffered_frames(self) -> int:
        return len(self._frames)

    def speaking(self) -> bool:
        """最近窗口内有过语音能量（空窗口按“活跃”计：段首还没攒够帧时不误杀）。"""
        if not self._recent_rms:
            return True
        return max(self._recent_rms) >= self._silence_rms

    def on_frame(self, payload: bytes) -> str | None:
        """喂一帧。返回 `"probe"` 表示调用方应发起一次滚动转写（快照见 snapshot）。"""
        self._frames.append(payload)
        rms = frame_rms(payload)
        self._recent_rms.append(rms)
        if len(self._recent_rms) > self._silence_window:
            del self._recent_rms[0:len(self._recent_rms) - self._silence_window]
        self._since_probe += 1
        if len(self._frames) < self._min_frames:
            return None
        if self._since_probe < self._probe_every:
            return None
        if not self.speaking():
            # 静音暂停转写：节拍计数**不清零** —— 说话回来时立刻探，
            # 而不是再等一整轮（延迟敏感路径上 1.5s 很贵）。
            return None
        self._since_probe = 0
        self.probes_requested += 1
        return "probe"

    def snapshot_pcm(self) -> bytes:
        return b"".join(self._frames)

    def on_hypothesis(self, text: str) -> str:
        """把一次探测的文本喂给确认策略，返回至今确认的全文。"""
        self.agreement.update(text)
        return self.agreement.confirmed


__all__ = [
    "DEFAULT_MIN_PROBE_FRAMES", "DEFAULT_PROBE_EVERY_FRAMES",
    "DEFAULT_SILENCE_RMS", "DEFAULT_SILENCE_WINDOW_FRAMES",
    "FRAME_MS", "FRAME_SAMPLES", "LocalAgreement", "SegmentStreamer",
    "common_prefix", "frame_rms", "tokenize",
]
