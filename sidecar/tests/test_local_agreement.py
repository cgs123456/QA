"""task18 单测：LocalAgreement 前缀确认 + SegmentStreamer 调度。

全部纯逻辑（无权重、无 GPU、无 FastAPI），本机可跑。
真实权重的首片段时延由 `docs/benchmark.md` 负责，不由单测负责。
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from asr.local_agreement import (  # noqa: E402
    LocalAgreement,
    SegmentStreamer,
    common_prefix,
    frame_rms,
    tokenize,
)


def _frame(fill: float) -> bytes:
    return struct.pack("<480f", *([fill] * 480))


# ---- tokenize / 前缀 ----

def test_tokenize_roundtrip_lossless():
    for t in ["", "开放时间早上九点", "hello world", "中文 English 混排 123",
              "早上9点,下午5点", " a  b "]:
        assert "".join(tokenize(t)) == t


def test_tokenize_cjk_chars_are_single_tokens():
    assert tokenize("你好") == ["你", "好"]


def test_common_prefix():
    assert common_prefix(["开", "放"], ["开", "始"]) == ["开"]
    assert common_prefix([], ["开"]) == []
    assert common_prefix(["a"], ["a"]) == ["a"]


def test_frame_rms_silence_vs_speech():
    assert frame_rms(_frame(0.0)) == 0.0
    assert frame_rms(_frame(0.1)) > 0.09
    # 长度非法不抛，按静音处理（门控不配崩链路）。
    assert frame_rms(b"\x00\x01") == 0.0


# ---- LocalAgreement ----

def test_first_hypothesis_confirms_nothing():
    ag = LocalAgreement()
    assert ag.update("开放时间") == ""
    assert ag.confirmed == ""


def test_two_agreeing_prefixes_confirm():
    ag = LocalAgreement()
    ag.update("开放时间早上")
    delta = ag.update("开放时间早上九点")
    assert delta == "开放时间早上"
    assert ag.confirmed == "开放时间早上"


def test_confirmed_text_never_retracts():
    """第二轮假设摇摆（前缀对不上已确认）→ 保持不动，不撤回。"""
    ag = LocalAgreement()
    ag.update("开放时间早上")
    ag.update("开放时间早上九点")
    assert ag.confirmed == "开放时间早上"
    # 第三轮整个前缀都变了：宁可少吐，不撤回已下行的字。
    assert ag.update("明天休息") == ""
    assert ag.confirmed == "开放时间早上"


def test_confirmation_only_grows():
    ag = LocalAgreement()
    ag.update("开放")
    ag.update("开放时间")
    assert ag.confirmed == "开放"
    ag.update("开放时间早上九点")
    assert ag.confirmed == "开放时间"


def test_english_word_runs_dont_emit_half_words():
    ag = LocalAgreement()
    ag.update("hello wo")
    # 公共前缀是 ["hello", " "]：吐完整词+空格，不吐半个词。
    assert ag.update("hello world") == "hello "
    ag.update("hello world")
    assert ag.confirmed == "hello world"


# ---- SegmentStreamer ----

def test_no_probe_before_min_frames():
    st = SegmentStreamer(probe_every_frames=5, min_probe_frames=10)
    for _ in range(9):
        assert st.on_frame(_frame(0.1)) is None
    assert st.buffered_frames == 9


def test_probe_cadence_by_frame_count():
    """节拍由帧计数派生，不用挂钟（与 R10 的 ts_ms 同源纪律）。"""
    st = SegmentStreamer(probe_every_frames=5, min_probe_frames=5)
    actions = [st.on_frame(_frame(0.1)) for _ in range(12)]
    assert actions[4] == "probe"   # 第 5 帧
    assert actions[9] == "probe"   # 第 10 帧
    assert actions[5] is None
    assert st.probes_requested == 2
    assert len(st.snapshot_pcm()) == 12 * 480 * 4


def test_silence_suspends_probing_and_speech_resumes_instantly():
    st = SegmentStreamer(probe_every_frames=5, min_probe_frames=5,
                         silence_rms=0.01, silence_window_frames=3)
    for _ in range(5):
        st.on_frame(_frame(0.1))  # 第 5 帧 probe 一次
    assert st.probes_requested == 1
    # 静音 3 帧占满窗口 → 后续节拍全部暂停。
    for _ in range(10):
        assert st.on_frame(_frame(0.0)) is None
    assert st.probes_requested == 1
    # 说话一回来，下一帧立刻探（不等一整轮）。
    assert st.on_frame(_frame(0.1)) == "probe"
    assert st.probes_requested == 2


def test_on_hypothesis_returns_confirmed_text():
    st = SegmentStreamer()
    assert st.on_hypothesis("开放时间") == ""
    assert st.on_hypothesis("开放时间早上") == "开放时间"
