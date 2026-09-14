"""task18 单测：低延迟开关门控 + 关闭时回归（Task15 一致）+ 流式部分结果。

- runtime 开关门控：纯逻辑，本机可跑；
- audio 接线：只 import `routers.audio`（不经过 `app`，故不需要 sqlite_vec），
  用假 ws + 直接驱动 `_on_event`/`_on_audio`，覆盖“关闭零 partial”“开启出
  partial 且与 final 同 segment_id”“静音零探测”“探测失败不下行 error”；
- 真实权重/GPU 的首片段时延由 `docs/benchmark.md` 负责，不由单测负责。
"""

import asyncio
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import routers.audio as audio_mod  # noqa: E402
from asr import runtime as asr_runtime  # noqa: E402
from asr.provider import ASRError, StubASRProvider  # noqa: E402


def _frame(fill: float) -> bytes:
    return struct.pack("<480f", *([fill] * 480))


class _FakeWS:
    def __init__(self):
        self.sent: list = []

    async def send_json(self, payload: dict):
        self.sent.append(payload)


@pytest.fixture()
def _clean():
    audio_mod.set_asr_provider(None)
    audio_mod.set_low_latency_override(None)
    old_flag = asr_runtime._LOW_LATENCY
    old_probe = asr_runtime._CUDA_PROBE
    asr_runtime._LOW_LATENCY = False
    yield
    audio_mod.set_asr_provider(None)
    audio_mod.set_low_latency_override(None)
    asr_runtime._LOW_LATENCY = old_flag
    asr_runtime._CUDA_PROBE = old_probe


def _run(coro):
    return asyncio.run(coro)


async def _drive(frames, fill=0.1, ts0=0):
    """开段 → N 帧 → 收尾（不断开），返回 (conn, sent)。调用方负责断言前已 settle。

    注意：探测是 `create_task` 的后台任务，feed 完必须 `await sleep` 让它们跑完。
    """
    ws = _FakeWS()
    conn = audio_mod._Conn(ws)
    await audio_mod._on_event(conn, {"event": "segment_start", "path": "loopback",
                                     "ts_ms": ts0})
    for i in range(frames):
        await audio_mod._on_audio(conn, ts0 + (i + 1) * 30, _frame(fill))
        if i % 10 == 9:
            # 真实 WS 里每帧之间都有 receive() 让出事件循环；这里模拟之，
            # 否则后台探测任务永远排不上（在途忙则跳过会正确地丢掉节拍）。
            await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    await audio_mod._on_event(conn, {"event": "segment_end", "path": "loopback",
                                     "ts_ms": ts0 + frames * 30})
    for _ in range(20):
        if conn.in_flight <= 0 and conn.probe_in_flight <= 0:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    return conn, ws.sent


# ---- 开关门控 ----

def test_latency_default_off(_clean):
    assert asr_runtime.is_low_latency() is False
    d = asr_runtime.describe_latency()
    assert d["enabled"] is False
    assert isinstance(d["cuda"], bool)
    assert "note" in d


def test_enable_without_cuda_is_unavailable(_clean):
    asr_runtime._CUDA_PROBE = False
    with pytest.raises(ASRError) as e:
        asr_runtime.set_low_latency(True)
    assert e.value.kind == "unavailable"
    assert asr_runtime.is_low_latency() is False


def test_enable_with_cuda_then_disable(_clean):
    asr_runtime._CUDA_PROBE = True
    d = asr_runtime.set_low_latency(True)
    assert d["enabled"] is True
    assert asr_runtime.is_low_latency() is True
    # 关闭永远允许（不需要 CUDA）。
    asr_runtime._CUDA_PROBE = False
    assert asr_runtime.set_low_latency(False)["enabled"] is False


def test_cuda_available_returns_bool_without_crashing(_clean):
    asr_runtime._reset_cuda_probe()
    assert isinstance(asr_runtime.cuda_available(), bool)


# ---- 关闭时回归：与 Task15 逐字一致 ----

def test_disabled_emits_no_partial_and_single_full_call(_clean):
    """DoD 回归：开关关闭 → 只有 asr_start/asr_final，provider 只调一次（全量）。"""
    stub = StubASRProvider(text="整段文本", name="stub")
    audio_mod.set_asr_provider(stub)
    audio_mod.set_low_latency_override(False)

    conn, sent = _run(_drive(60, fill=0.1))
    types = [m["type"] for m in sent]
    assert types == ["asr_start", "asr_final"], types
    assert "asr_partial" not in types
    final = sent[1]
    assert final["segment_id"] == "seg_1"
    assert final["text"] == "整段文本"
    # provider 只被调一次，且是 60 帧整段（不是滚动切片）。
    assert len(stub.calls) == 1
    assert stub.calls[0][0] == 60 * audio_mod.AUDIO_PAYLOAD_BYTES
    assert conn.counters["partial_probes"] == 0
    assert conn.counters["partials_sent"] == 0


def test_disabled_by_runtime_flag_not_only_override(_clean):
    """覆盖为 None 时跟随 runtime 开关（默认关）—— 同样零 partial。"""
    audio_mod.set_asr_provider(StubASRProvider(text="x", name="stub"))
    audio_mod.set_low_latency_override(None)
    _, sent = _run(_drive(60, fill=0.1))
    assert [m["type"] for m in sent] == ["asr_start", "asr_final"]


# ---- 开启时：partial 与 final 同 segment_id ----

def test_enabled_emits_partial_then_final_with_same_segment_id(_clean):
    audio_mod.set_asr_provider(StubASRProvider(text="开放时间早上九点", name="stub"))
    audio_mod.set_low_latency_override(True)

    conn, sent = _run(_drive(110, fill=0.1))
    partials = [m for m in sent if m["type"] == "asr_partial"]
    finals = [m for m in sent if m["type"] == "asr_final"]
    assert len(finals) == 1
    assert len(partials) >= 1, [m["type"] for m in sent]
    # 坑位要求：partial 与 final 必须是同一 segment_id。
    assert {m["segment_id"] for m in partials} == {finals[0]["segment_id"]} == {"seg_1"}
    # 确认文本单调：后一个 partial 是前一个的前缀扩展（从不撤回）。
    texts = [m["text"] for m in partials]
    for a, b in zip(texts, texts[1:]):
        assert b.startswith(a), (a, b)
    assert finals[0]["text"] == "开放时间早上九点"
    assert conn.counters["partials_sent"] == len(partials)
    assert conn.counters["partial_probes"] >= 2


def test_silence_suspends_probing_even_when_enabled(_clean):
    audio_mod.set_asr_provider(StubASRProvider(text="x", name="stub"))
    audio_mod.set_low_latency_override(True)

    conn, sent = _run(_drive(60, fill=0.0))
    assert [m["type"] for m in sent] == ["asr_start", "asr_final"]
    assert conn.counters["partial_probes"] == 0


def test_probe_failure_never_emits_asr_error(_clean):
    """探测失败只丢弃；交代由 final 给（此处 final 同样失败 → 恰好一个 error）。"""
    audio_mod.set_asr_provider(
        StubASRProvider(fail_with=ASRError("unavailable", "nope"), name="stub"))
    audio_mod.set_low_latency_override(True)

    _, sent = _run(_drive(110, fill=0.1))
    errors = [m for m in sent if m["type"] == "asr_error"]
    assert [m["type"] for m in sent].count("asr_partial") == 0
    assert len(errors) == 1  # 仅 final 的那一个
    assert errors[0]["error"] == "unavailable"


def test_late_probe_after_segment_end_is_dropped(_clean):
    """段已结束才完成的迟到探测不得下行（禁“先 final 后 partial”同 id 乱序）。"""
    audio_mod.set_asr_provider(StubASRProvider(text="迟到", name="stub"))
    ws = _FakeWS()
    conn = audio_mod._Conn(ws)

    async def _scenario():
        await audio_mod._on_event(conn, {"event": "segment_start", "path": "loopback",
                                         "ts_ms": 0})
        seg = conn.open_seg
        assert seg["streamer"] is None  # 开关默认关
        seg["streamer"] = __import__("asr.local_agreement",
                                     fromlist=["SegmentStreamer"]).SegmentStreamer()
        # 段已结束（final 已发），此时迟到的探测任务才跑完。
        conn.open_seg = None
        conn.probe_in_flight += 1
        await audio_mod._probe(conn, seg)

    _run(_scenario())
    assert [m["type"] for m in ws.sent] == ["asr_start"]
    assert conn.probe_in_flight == 0
