"""VAD 适配器冒烟（task19）：真跑 webrtc/silero，断言只做“稳”的部分。

- 全零 → 全静音（两 provider 在此一致，且与生产单测的 silence 语义同源）；
- 帧计数/对齐：输出长度恒等于输入帧数（边界误差表的前提）；
- 非法帧长抛错（不吞数据问题）；
- silero 样本间 reset 可调（防 LSTM 状态泄漏）。

缺 `webrtcvad`/`onnxruntime`/`silero_vad` 时整文件 skip（测量依赖不进主锁文件）。
识别能力本身（真人声谁准）由回归样本集回答，不在这里断言。
"""

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

webrtcvad = pytest.importorskip("webrtcvad")
pytest.importorskip("onnxruntime")
pytest.importorskip("silero_vad")

from audio_eval.vad_providers import SileroAdapter, WebrtcAdapter  # noqa: E402


def _zeros(n=5):
    return [struct.pack("<480h", *([0] * 480)) for _ in range(n)]


def test_webrtc_all_modes_silence_on_zeros():
    for mode in (0, 1, 2, 3):
        assert WebrtcAdapter(mode).decide(_zeros()) == [False] * 5


def test_webrtc_bad_mode_rejected():
    with pytest.raises(ValueError):
        WebrtcAdapter(4)


def test_webrtc_bad_frame_length_raises():
    with pytest.raises(ValueError):
        WebrtcAdapter(2).decide([b"\x00" * 100])


def test_silero_zeros_silence_and_reset():
    s = SileroAdapter()
    assert s.decide(_zeros()) == [False] * 5
    s.reset()
    assert s.decide(_zeros()) == [False] * 5


def test_silero_bad_frame_length_raises():
    with pytest.raises(ValueError):
        SileroAdapter().decide([b"\x00" * 100])


def test_both_adapters_frame_aligned_output():
    n = 7
    assert len(WebrtcAdapter(2).decide(_zeros(n))) == n
    s = SileroAdapter()
    assert len(s.decide(_zeros(n))) == n
