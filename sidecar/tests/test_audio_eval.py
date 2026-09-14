"""task19 测量包单测：端点状态机 / 指标 / 标注校验（全纯逻辑，本机可跑）。

VAD 适配器冒烟（webrtc/silero 真跑零输入）放在 `test_audio_eval_vad.py`
（需第三方包，缺时整文件 skip——测量依赖不进主锁文件，见 pyproject extra）。
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from audio_eval.endpoint import (  # noqa: E402
    END_SILENCE_FRAMES,
    MAX_SEG_FRAMES,
    MIN_KEEP_FRAMES,
    detect_segments,
    to_ms,
)
from audio_eval.metrics import error_rate, match_segments, summarize_boundary  # noqa: E402


def _run(runs):
    """[(帧数, voiced)] → 决策列表。"""
    out = []
    for n, v in runs:
        out += [v] * n
    return out


# ---- 端点状态机（参数与 PROGRESS ① 逐字对应） ----

def test_start_needs_3_of_5_onset_is_first_voiced():
    # 第 3 帧起连续语音：窗口 [0..4] 首次凑够 3 voiced 在 i=4，onset=第 3 帧。
    segs = detect_segments(_run([(3, False), (30, True), (30, False)]))
    assert segs == [(3, 32)]


def test_end_hangover_excluded_from_segment():
    # 20 帧语音 + 17 帧静音 + 更多静音：offset 落在最后语音帧，不含 hangover。
    segs = detect_segments(_run([(20, True), (40, False)]))
    assert segs == [(0, 19)]


def test_short_burst_below_250ms_dropped():
    # 5 帧语音（150ms）+ 足够 hangover：段长按语音帧计 → 丢弃。
    assert detect_segments(_run([(5, True), (40, False)])) == []


def test_min_keep_boundary_9_frames_kept_8_dropped():
    assert detect_segments(_run([(9, True), (40, False)])) == [(0, 8)]
    assert detect_segments(_run([(8, True), (40, False)])) == []


def test_max_15s_force_cut_splits_back_to_back():
    # 600 帧连续语音 → [0,499] + [500,599]（后段 100 帧 ≥9 保留）。
    segs = detect_segments([True] * 600)
    assert segs == [(0, 499), (500, 599)]
    assert MAX_SEG_FRAMES == 500


def test_eof_open_segment_closes_at_last_voiced():
    segs = detect_segments(_run([(3, False), (20, True)]))
    assert segs == [(3, 22)]


def test_all_silence_no_segments():
    assert detect_segments([False] * 100) == []


def test_two_segments_separated_by_long_silence():
    segs = detect_segments(_run([(20, True), (30, False), (20, True), (30, False)]))
    assert segs == [(0, 19), (50, 69)]


def test_to_ms_frame_closed_interval():
    assert to_ms((0, 0)) == (0, 30)
    assert to_ms((3, 32)) == (90, 990)
    assert END_SILENCE_FRAMES == 17
    assert MIN_KEEP_FRAMES == 9


# ---- 指标 ----

def test_cer_zh_char_level():
    r = error_rate("开放时间早上九点", "开放时间早上九点", "zh")
    assert (r["errors"], r["total"], r["rate"]) == (0, 8, 0.0)
    r = error_rate("开放时间", "开饭时间", "zh")
    assert (r["errors"], r["total"]) == (1, 4)


def test_wer_en_word_level_case_insensitive():
    r = error_rate("Hello world", "hello WORLD", "en")
    assert r["rate"] == 0.0
    r = error_rate("a b c", "a x c d", "en")
    assert r["errors"] == 2 and r["total"] == 3


def test_error_rate_empty_ref():
    assert error_rate("", "", "zh")["rate"] == 0.0
    assert error_rate("", "abc", "zh")["rate"] == 1.0


def test_match_segments_pairs_and_signs():
    m = match_segments([(600, 2400)], [(690, 2500)])
    assert m["missed"] == [] and m["false"] == []
    (ei, di, on, off), = m["pairs"]
    assert (ei, di) == (0, 0)
    assert on == 90 and off == 100  # 正 = 检出晚/拖尾


def test_match_segments_missed_and_false():
    m = match_segments([(0, 100), (500, 600)], [(0, 100), (900, 950)])
    assert m["missed"] == [1]
    assert m["false"] == [1]


def test_summarize_boundary_none_when_no_pairs():
    s = summarize_boundary({"pairs": [], "missed": [0], "false": [1, 2]})
    assert s["mean_abs_onset_ms"] is None  # 无配对记 None，不是 0
    assert (s["n_missed"], s["n_false"]) == (1, 2)
