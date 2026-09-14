"""task19 1b 回归 runner：VAD 边界误差 / WER / 端到端延迟 三张表。

输入：`sidecar/tests/audio_samples/manifest.json`（用户录制的真实录音 +
标注；当前 6 槽全空 —— 实测表保持 PENDING，不拿合成数充数）。

三张表的口径：
1. **VAD 边界误差**：wav → 两 provider 逐帧决策（webrtc mode 2 / silero 阈值 0.5）
   → 参考端点（`audio_eval.endpoint`，PROGRESS ① 参数逐字实现）→ 检出段
   vs 标注段配对：平均绝对 onset/offset 误差（ms）+ missed/false 计数。
2. **WER**：整段 wav → `--asr` 指定 provider 转写 → 与标注 transcript 比
   CER（zh）/ WER（en）。`stub` 时记 PENDING（替身文本无意义）；权重未就位
   也记 PENDING（原因写明，不估分）。
3. **端到端延迟**：每个标注段 span → 真实 uvicorn + 真实 WS（复用
   `bench_asr_latency.Server`）→ segment_end 发出 → asr_final 收到（中位，
   `--repeat` 次）。provider 用 stub 即测传输+协议下限；`--asr` 真权重即含推理。

用法：
    python scripts/audio_regression.py                        # 跑 manifest（全 PENDING 即 6 槽待录）
    python scripts/audio_regression.py --out docs/audio-regression.md   # 追加一节 dated run
    python scripts/audio_regression.py --self-test             # 合成定标：只证明管线通，SYNTHETIC 标签

R14：报告只含 WER 分子/分母，不贴转写全文；音频字节不进任何日志。
"""

import argparse
import asyncio
import hashlib
import json
import platform
import struct
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIDECAR_SRC = ROOT / "sidecar" / "src"
SAMPLES_DIR = ROOT / "sidecar" / "tests" / "audio_samples"
SCRIPTS_DIR = ROOT / "scripts"

sys.path.insert(0, str(SIDECAR_SRC))
sys.path.insert(0, str(SCRIPTS_DIR))

FRAME_MS = 30
FRAME_SAMPLES = 480
SAMPLE_RATE = 16000


# ---------------------------------------------------------------- manifest

def load_manifest(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_sample(samples_dir: Path, slot: dict) -> dict:
    """槽位就绪检查 → {"ok", "wav", "ann"} 或 {"ok": False, "reason"}。"""
    if slot.get("status") != "ready":
        return {"ok": False, "reason": f"status={slot.get('status')}（待用户录制）"}
    wav = samples_dir / slot["wav"]
    ann_p = samples_dir / slot["annotation"]
    if not wav.is_file():
        return {"ok": False, "reason": f"缺音频 {slot['wav']}"}
    if not ann_p.is_file():
        return {"ok": False, "reason": f"缺标注 {slot['annotation']}"}
    if slot.get("sha256") and sha256_of(wav) != slot["sha256"]:
        return {"ok": False, "reason": "wav sha256 与 manifest 不符（被改动过？）"}
    try:
        ann = json.loads(ann_p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        return {"ok": False, "reason": f"标注解析失败：{type(e).__name__}"}
    if not isinstance(ann.get("segments"), list):
        return {"ok": False, "reason": "标注缺 segments 列表"}
    return {"ok": True, "wav": wav, "ann": ann}


# ---------------------------------------------------------------- wav

def read_wav_mono16k(path: Path) -> list:
    """wav → [bytes(960B) int16 LE 帧]。非 16k/mono/16-bit 直接抛（不重采样——污染对比）。"""
    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise ValueError(
                f"{path.name}: 须 16k/mono/16-bit，实得 "
                f"{w.getframerate()}Hz/{w.getnchannels()}ch/{w.getsampwidth() * 8}bit")
        raw = w.readframes(w.getnframes())
    if len(raw) % 960:
        raise ValueError(f"{path.name}: 字节数非 960 对齐（{len(raw)}B）")
    return [raw[i:i + 960] for i in range(0, len(raw), 960)]


def frames_to_f32_wire(frames_i16: list, ts0: int = 0) -> list:
    """int16 帧 → WS 音频帧 bytes（含 7B 头，seq 从 0 起，调用方重排 seq）。"""
    out = []
    for i, f in enumerate(frames_i16):
        n = len(f) // 2
        ints = struct.unpack(f"<{n}h", f)
        flt = struct.pack(f"<{n}f", *[(v / 32768.0) for v in ints])
        out.append((struct.pack("<BHI", 0x00, 0, ts0 + i * FRAME_MS) + flt,
                    ts0 + i * FRAME_MS))
    return out


# ---------------------------------------------------------------- VAD + 端点

def vad_decisions(frames_i16: list) -> dict:
    """两 provider 逐帧决策 → {"webrtc-vad": [...], "silero-vad": [...]}（缺依赖即抛 VadUnavailable）。"""
    from audio_eval.vad_providers import SileroAdapter, WebrtcAdapter

    webrtc = WebrtcAdapter(2)
    silero = SileroAdapter()
    try:
        return {"webrtc-vad": webrtc.decide(frames_i16),
                "silero-vad": silero.decide(frames_i16)}
    finally:
        silero.reset()


def table_vad_boundary(slots_rows: list) -> str:
    """slots_rows: [(sample_id, provider, summary|None, err|None)] → markdown 表。"""
    lines = ["| 样本 | provider | 配上段 | 平均|onset|误差 | 平均|offset|误差 | missed | false |",
             "|---|---|---|---|---|---|---|"]
    for sid, prov, summ, err in slots_rows:
        if err is not None:
            lines.append(f"| {sid} | {prov} | PENDING（{err}） | — | — | — | — |")
        else:
            on = "—" if summ["mean_abs_onset_ms"] is None else f"{summ['mean_abs_onset_ms']:.0f}ms"
            off = "—" if summ["mean_abs_offset_ms"] is None else f"{summ['mean_abs_offset_ms']:.0f}ms"
            lines.append(f"| {sid} | {prov} | {summ['n_matched']} | {on} | {off} | "
                         f"{summ['n_missed']} | {summ['n_false']} |")
    return "\n".join(lines)


# ---------------------------------------------------------------- WER

def build_asr(name: str):
    """stub → 替身；真实 provider → 构造（权重检查由调用方做，缺即 PENDING）。"""
    if name == "stub":
        from asr.provider import StubASRProvider

        return StubASRProvider(text="stub", name="stub"), "stub", True
    if name == "faster-whisper":
        from asr.faster_whisper_provider import FasterWhisperProvider

        p = FasterWhisperProvider()
        return p, f"{p.name}/{p.model}/{p.compute_type}", p.is_ready()
    if name == "sensevoice":
        from asr.sensevoice_provider import SenseVoiceProvider

        p = SenseVoiceProvider()
        return p, f"{p.name}/int8", p.is_ready()
    if name == "paraformer":
        from asr.paraformer_provider import ParaformerProvider

        p = ParaformerProvider()
        return p, f"{p.name}/int8", p.is_ready()
    raise ValueError(f"未知 --asr：{name}")


def wav_to_f32_bytes(frames_i16: list) -> bytes:
    """int16 wav 帧 → provider 吃的 float32 LE bytes（与 WS 管线同变换，见 frame.rs）。"""
    out = bytearray()
    for f in frames_i16:
        n = len(f) // 2
        ints = struct.unpack(f"<{n}h", f)
        out += struct.pack(f"<{n}f", *[(v / 32768.0) for v in ints])
    return bytes(out)


# ---------------------------------------------------------------- 延迟（真实 uvicorn + WS，复用 bench.Server）

def latency_for_spans(server_port: int, token: str, frames_i16: list,
                      spans_ms: list, repeat: int, timeout_s: float) -> list:
    """每个标注 span → 起止帧 → WS round trip → 中位 ms。"""
    import websockets

    import bench_asr_latency as bench

    async def _one(span):
        s_ms, e_ms = span
        s_f = max(0, s_ms // FRAME_MS)
        e_f = min(len(frames_i16), (e_ms + FRAME_MS - 1) // FRAME_MS)
        seg_frames = frames_i16[s_f:e_f]
        wire = frames_to_f32_wire(seg_frames, ts0=s_f * FRAME_MS)
        uri = f"ws://127.0.0.1:{server_port}/audio/stream"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            cm = websockets.connect(uri, additional_headers=headers)
        except TypeError:
            cm = websockets.connect(uri, extra_headers=headers)
        async with cm as ws:
            seq = 0
            ts = s_f * FRAME_MS
            await ws.send(bench.event(seq, ts, {"event": "segment_start",
                                                "path": "loopback", "ts_ms": ts}))
            seq += 1
            await ws.recv()
            for w, fts in wire:
                await ws.send(struct.pack("<BHI", 0x00, seq & 0xFFFF, fts) + w[7:])
                seq += 1
            end_ts = (s_f + len(wire)) * FRAME_MS
            t0 = time.perf_counter()
            await ws.send(bench.event(seq, end_ts, {"event": "segment_end",
                                                    "path": "loopback", "ts_ms": end_ts}))
            while True:
                obj = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))
                if obj.get("type") in ("asr_final", "asr_error"):
                    return (time.perf_counter() - t0) * 1000.0, obj

    import statistics

    rows = []
    for span in spans_ms:
        samples = [asyncio.run(_one(span))[0] for _ in range(repeat)]
        rows.append({"span_ms": span, "n": repeat,
                     "median_ms": statistics.median(samples),
                     "min_ms": min(samples), "max_ms": max(samples)})
    return rows


# ---------------------------------------------------------------- 主流程

def run_manifest(manifest: dict, samples_dir: Path, asr_name: str,
                 repeat: int, timeout_s: float) -> dict:
    """跑完全部 ready 槽 → 三张表的行数据（PENDING 行带原因，不估分）。"""
    from audio_eval.endpoint import detect_segments, to_ms
    from audio_eval.metrics import error_rate, match_segments, summarize_boundary
    from audio_eval.vad_providers import VadUnavailable

    asr, asr_label, asr_ready = build_asr(asr_name)
    vad_rows, wer_rows, lat_rows = [], [], []
    lat_server = None
    token = "audio-reg-" + "z" * 32

    if asr_name == "stub":
        import bench_asr_latency as bench

        lat_server = bench.Server(asr, token)
        lat_server.__enter__()

    try:
        for slot in manifest["samples"]:
            sid = slot["id"]
            lang = slot.get("lang", "zh")
            chk = check_sample(samples_dir, slot)
            if not chk["ok"]:
                reason = chk["reason"]
                for prov in ("webrtc-vad", "silero-vad"):
                    vad_rows.append((sid, prov, None, reason))
                wer_rows.append((sid, None, reason))
                lat_rows.append((sid, None, reason))
                continue
            try:
                frames = read_wav_mono16k(chk["wav"])
            except ValueError as e:
                reason = f"wav 不合格：{e}"
                for prov in ("webrtc-vad", "silero-vad"):
                    vad_rows.append((sid, prov, None, reason))
                wer_rows.append((sid, None, reason))
                lat_rows.append((sid, None, reason))
                continue
            ann = chk["ann"]
            expected = [(s["start_ms"], s["end_ms"]) for s in ann["segments"]]

            # 表1：VAD 边界。
            try:
                dec = vad_decisions(frames)
            except VadUnavailable as e:
                for prov in ("webrtc-vad", "silero-vad"):
                    vad_rows.append((sid, prov, None, f"VAD 依赖缺失：{e}"))
                dec = {}
            for prov, decisions in dec.items():
                detected = [to_ms(s) for s in detect_segments(decisions)]
                m = match_segments(expected, detected)
                vad_rows.append((sid, prov, summarize_boundary(m), None))

            # 表2：WER。
            if asr_name == "stub":
                wer_rows.append((sid, None, "stub 替身文本无意义（换 --asr 真权重重跑）"))
            elif not asr_ready:
                wer_rows.append((sid, None, f"{asr_label} 权重未就位"))
            else:
                hyp = asyncio.run(asr.transcribe(wav_to_f32_bytes(frames), 16000, "loopback"))
                r = error_rate(ann.get("transcript", ""), hyp or "", lang)
                wer_rows.append((sid, r, None))

            # 表3：延迟。
            if lat_server is None:
                lat_rows.append((sid, None, "延迟表需 stub 管线（--asr stub）；真权重延迟见 benchmark"))
            else:
                spans = [(s["start_ms"], s["end_ms"]) for s in ann["segments"]]
                rows = latency_for_spans(lat_server.port, token, frames, spans,
                                         repeat, timeout_s)
                lat_rows.append((sid, rows, None))
    finally:
        if lat_server is not None:
            lat_server.__exit__(None, None, None)
    return {"vad": vad_rows, "wer": wer_rows, "lat": lat_rows, "asr": asr_label}


def render_report(result: dict, manifest_path: str, note: str = "") -> str:
    env = f"{platform.system()} {platform.machine()}, Python {platform.python_version()}"
    when = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    L = [f"## 回归运行（{when}，asr={result['asr']}）", ""]
    if note:
        L += [note, ""]
    L += [f"manifest: `{manifest_path}`；环境：{env}  ", ""]
    L += ["### 表1 VAD 边界误差（参考端点：起始 5 帧 3 voiced / 结束 17 帧 hangover / "
          "最短 250ms 丢弃 / 最长 15s 强制切）", ""]
    L += [table_vad_boundary(result["vad"]), ""]
    L += ["### 表2 WER（zh 按字 CER / en 按词；只含分子分母，不贴全文）", ""]
    L += ["| 样本 | errors | total | rate |", "|---|---|---|---|"]
    for sid, r, err in result["wer"]:
        if err is not None:
            L.append(f"| {sid} | PENDING（{err}） | — | — |")
        else:
            L.append(f"| {sid} | {r['errors']} | {r['total']} | {r['rate']:.1%} |")
    L += ["", "### 表3 端到端延迟（segment_end 发出 → asr_final 收到，真实 uvicorn + WS）", ""]
    L += ["| 样本 | span(ms) | 样本数 | 中位 | 最小 | 最大 |",
          "|---|---|---|---|---|---|"]
    for sid, rows, err in result["lat"]:
        if err is not None:
            L.append(f"| {sid} | PENDING（{err}） | — | — | — | — |")
        else:
            for r in rows:
                L.append(f"| {sid} | {r['span_ms'][0]}–{r['span_ms'][1]} | {r['n']} | "
                         f"{r['median_ms']:.0f}ms | {r['min_ms']:.0f}ms | {r['max_ms']:.0f}ms |")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- 合成自检（只证管线通，SYNTHETIC）

def _synth_wav(path: Path, kind: str):
    """合成定标 wav：语音串（谐波堆+音节包络）/ 静音 / 噪声。**不是真人声，不进实测表**。

    布局按整帧数取（30ms/帧），总字节恒为 960 倍数；spans 即实际布局的 ms。
    """
    import math

    sr = SAMPLE_RATE
    FR = sr * FRAME_MS // 1000  # 480 点/帧

    def zeros(n_frames):
        return [0] * (n_frames * FR)

    if kind == "silence":
        pcm = zeros(133)  # 3990ms
        spans = []
    elif kind == "noise":
        st = 0x1234
        pcm = []
        for _ in range(133 * FR):
            st = (st * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
            pcm.append(int((((st >> 33) / (1 << 31)) - 1.0) * 3000))
        spans = []
    else:  # speech: 17f 静音 + 67f 谐波串 + 33f 静音 + 67f 谐波串 + 尾静音凑 200f
        def burst(n_frames, f0=140.0):
            o = []
            for i in range(n_frames * FR):
                t = i / sr
                env = 0.6 + 0.4 * math.sin(2 * math.pi * 3.5 * t)
                s = sum(math.sin(2 * math.pi * f0 * h * t) / h for h in range(1, 6))
                o.append(int(max(-1.0, min(1.0, 0.3 * env * s / 2.5)) * 20000))
            return o

        pcm = zeros(17) + burst(67) + zeros(33) + burst(67, 170.0)
        pcm += [0] * (200 * FR - len(pcm))
        spans = [(17 * 30, 84 * 30), (117 * 30, 184 * 30)]  # (510,2520),(3510,5520)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack(f"<{len(pcm)}h", *pcm))
    return spans


def self_test() -> int:
    """合成定标全链路：VAD 双 provider + 端点 + WS 延迟 + WER 计算。断言只做结构性的。"""
    import tempfile

    from audio_eval.endpoint import detect_segments, to_ms
    from audio_eval.metrics import error_rate, match_segments, summarize_boundary
    from audio_eval.vad_providers import VadUnavailable

    tmp = Path(tempfile.mkdtemp(prefix="audio-reg-selftest-"))
    fails = []

    def check(name, cond, detail=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
        if not cond:
            fails.append(name)

    # 1) 纯静音：两 provider 全静音，端点零段（ saturation 反方向的基本盘）。
    zw = tmp / "silence.wav"
    _synth_wav(zw, "silence")
    frames = read_wav_mono16k(zw)
    try:
        dec = vad_decisions(frames)
        check("silence/webrtc-all-silence", not any(dec["webrtc-vad"]))
        check("silence/silero-all-silence", not any(dec["silero-vad"]))
        check("silence/endpoint-empty",
              detect_segments(dec["webrtc-vad"]) == [] and
              detect_segments(dec["silero-vad"]) == [])
    except VadUnavailable as e:
        check("vad-deps-present", False, str(e))

    # 2) 语音串：webrtc 应检出（libfvad 确定性）；端点成段；配对逻辑贯通。
    sw = tmp / "speech.wav"
    spans = _synth_wav(sw, "speech")
    frames = read_wav_mono16k(sw)
    try:
        dec = vad_decisions(frames)
        wsegs = [to_ms(s) for s in detect_segments(dec["webrtc-vad"])]
        check("speech/webrtc-finds-segments", len(wsegs) >= 1, f"检出 {len(wsegs)} 段")
        ssegs = [to_ms(s) for s in detect_segments(dec["silero-vad"])]
        m = match_segments(spans, wsegs)
        s = summarize_boundary(m)
        check("speech/match-struct-ok", s["n_matched"] >= 1, f"配上 {s['n_matched']}/2")
        # silero 在合成串上不断言检出（未知行为不断言），只断言帧对齐。
        check("speech/silero-frame-aligned", len(dec["silero-vad"]) == len(frames))
        print(f"[INFO] silero 合成串检出 {len(ssegs)} 段（不定标，只记录布线正常）")
    except VadUnavailable as e:
        check("vad-deps-present", False, str(e))

    # 3) WER 计算贯通（纯函数，不依赖 ASR）。
    r = error_rate("开放时间早上九点", "开放时间早上九点", "zh")
    check("wer/identical-zero", r["rate"] == 0.0)
    r = error_rate("hello world", "hello", "en")
    check("wer/deletion-half", r["rate"] == 0.5)

    # 4) WS 延迟管线贯通（stub，下限口径）。
    import bench_asr_latency as bench
    from asr.provider import StubASRProvider

    token = "selftest-" + "z" * 32
    with bench.Server(StubASRProvider(text="stub", name="stub"), token) as srv:
        rows = latency_for_spans(srv.port, token, frames, [spans[0]], 1, 30.0)
    check("latency/one-row", len(rows) == 1 and rows[0]["median_ms"] > 0,
          f"{rows[0]['median_ms']:.0f}ms")

    print("SELFTEST_" + ("PASS" if not fails else f"FAIL({fails})"))
    return 0 if not fails else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(SAMPLES_DIR / "manifest.json"))
    ap.add_argument("--samples-dir", default=str(SAMPLES_DIR))
    ap.add_argument("--asr", choices=["stub", "faster-whisper", "sensevoice", "paraformer"],
                    default="stub")
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--out", default=None, help="追加一节 dated run 到指定 md")
    ap.add_argument("--self-test", action="store_true",
                    help="合成定标（只证管线通，输出带 SYNTHETIC 标签，不进实测表）")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    manifest = load_manifest(Path(args.manifest))
    result = run_manifest(manifest, Path(args.samples_dir), args.asr,
                          args.repeat, args.timeout)
    body = render_report(result, args.manifest)
    print(body)
    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = (ROOT / out).resolve()
        with open(out, "a", encoding="utf-8") as f:
            f.write("\n" + body)
        print(f"[已追加到 {out}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
