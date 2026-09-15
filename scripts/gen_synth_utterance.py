"""生成"合成双语音串"音频 fixture（假源全链 E2E 的输入）。

为什么需要它：DoD 要求「假源全链 e2e 出答案卡（合成语音→固定答案）」。要让
**真实 faster-whisper + 真实检索**产出固定答案，音频必须是人能听懂的合成语音，
而不是 `speech_like` 那种只保证"VAD 会判成语音"的类语音信号——后者转写出来
是无意义文本，检索自然给不出官方答案。

所以这里用系统 TTS（Windows SAPI，中文语音）把 `sidecar/tests/eval/seed_demo.json`
里的两条**有官方答案**的问题念出来，中间夹静音，拼成一条双语音串：

    静音 0.6s ── "发货周期是多久" ── 静音 0.7s ── "退货期限是几天" ── 静音 0.6s

这条串一次喂进真实管线，应当被端点状态机切成**两段**，两段各自转写、
各自检索、各自出一张答案卡 —— 一次性验证「分段正确 + 转写可用 + 检索命中」。

为什么音频要落盘进仓：SAPI 的中文语音只在中文 Windows 上有，且同一句话
不同语音合成器读出来不一样。把 WAV 钉进 `testdata/` 才能让"同一段样本"
这句话成立（其他机器/CI 读到的是同一个文件）。

用法：
    python scripts/gen_synth_utterance.py            # 生成（需要中文 SAPI 语音）
    python scripts/gen_synth_utterance.py --check    # 只校验仓里的文件与元数据一致
"""

import argparse
import hashlib
import json
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_WAV = ROOT / "testdata" / "synth_utterance_zh.wav"
OUT_META = ROOT / "testdata" / "synth_utterance_zh.json"
SEED = ROOT / "sidecar" / "tests" / "eval" / "seed_demo.json"

# 与 seed_demo.json 逐字一致（转写文本要与 standard_question 对上，检索才命中）。
UTTERANCES = ["发货周期是多久", "退货期限是几天"]
LEAD_SILENCE_MS = 600
GAP_SILENCE_MS = 700
TAIL_SILENCE_MS = 600

# SAPI 音频格式常量：SAFT22kHz16BitMono = 22。
SAFT_22KHZ_16BIT_MONO = 22
SSFM_CREATE_FOR_WRITE = 3


def _synthesize(texts: list, tmpdir: Path) -> list:
    """逐句合成到临时 WAV，返回路径列表（Windows + SAPI 中文语音）。"""
    try:
        import comtypes.client  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover - 平台相关
        raise SystemExit(
            "需要 comtypes 才能驱动 SAPI 合成语音（pip install comtypes）；"
            f"原始错误：{e}"
        )

    voice = comtypes.client.CreateObject("SAPI.SpVoice")
    paths = []
    for i, text in enumerate(texts):
        path = tmpdir / f"utt_{i}.wav"
        stream = comtypes.client.CreateObject("SAPI.SpFileStream")
        stream.Format.Type = SAFT_22KHZ_16BIT_MONO
        stream.Open(str(path), SSFM_CREATE_FOR_WRITE, False)
        voice.AudioOutputStream = stream
        # 语速略慢（-1）：给 VAD 留出清晰的起止边界，也更接近真实语速。
        voice.Rate = -1
        voice.Speak(text)
        stream.Close()
        if not path.is_file() or path.stat().st_size <= 44:
            raise SystemExit(f"SAPI 未产出音频：{path}")
        paths.append(path)
    return paths


def _read_wav(path: Path):
    with wave.open(str(path), "rb") as w:
        params = (w.getnchannels(), w.getsampwidth(), w.getframerate())
        frames = w.readframes(w.getnframes())
    return params, frames


def _silence(params, ms: int) -> bytes:
    channels, width, rate = params
    n = int(rate * ms / 1000) * channels * width
    return b"\x00" * n


def build(tmpdir: Path) -> tuple:
    parts = _synthesize(UTTERANCES, tmpdir)
    params = None
    chunks = []
    for i, p in enumerate(parts):
        prm, frames = _read_wav(p)
        if params is None:
            params = prm
        elif prm != params:
            raise SystemExit(f"逐句格式不一致：{params} vs {prm}（{p}）")
        if i == 0:
            chunks.append(_silence(params, LEAD_SILENCE_MS))
        else:
            chunks.append(_silence(params, GAP_SILENCE_MS))
        chunks.append(frames)
    chunks.append(_silence(params, TAIL_SILENCE_MS))
    return params, b"".join(chunks), [p.stat().st_size for p in parts]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验仓里的文件")
    args = ap.parse_args()

    if args.check:
        if not OUT_WAV.is_file() or not OUT_META.is_file():
            print("FAIL: 音频 fixture 缺失，先跑一次不带 --check 的生成")
            return 1
        meta = json.loads(OUT_META.read_text(encoding="utf-8"))
        digest = hashlib.sha256(OUT_WAV.read_bytes()).hexdigest()
        if digest != meta["wav_sha256"]:
            print(f"FAIL: {OUT_WAV.name} 的 sha256 与元数据不符")
            return 1
        params, _ = _read_wav(OUT_WAV)
        if list(params) != meta["wav_format"]:
            print(f"FAIL: 音频格式变了：{params} vs {meta['wav_format']}")
            return 1
        print(
            f"SYNTH_FIXTURE_CHECK_PASS utterances={len(meta['utterances'])} "
            f"format={params} sha256={digest[:16]}"
        )
        return 0

    import tempfile

    with tempfile.TemporaryDirectory(prefix="synth-utt-") as td:
        params, pcm, sizes = build(Path(td))

    OUT_WAV.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT_WAV), "wb") as w:
        w.setnchannels(params[0])
        w.setsampwidth(params[1])
        w.setframerate(params[2])
        w.writeframes(pcm)

    channels, width, rate = params
    total_ms = int(len(pcm) / (rate * channels * width) * 1000)
    meta = {
        "generator": "scripts/gen_synth_utterance.py",
        "source": "Windows SAPI 中文语音（Microsoft Huihui）",
        "why": "假源全链 E2E 的输入：必须是可被真实 ASR 转写的合成语音，"
               "否则检索给不出固定答案",
        "utterances": UTTERANCES,
        "expected_official_answers": _official_answers(),
        "layout_ms": {
            "lead_silence": LEAD_SILENCE_MS,
            "gap_silence": GAP_SILENCE_MS,
            "tail_silence": TAIL_SILENCE_MS,
        },
        "wav_format": list(params),
        "wav_bytes": OUT_WAV.stat().st_size,
        "wav_ms": total_ms,
        "per_utterance_wav_bytes": sizes,
        "wav_sha256": hashlib.sha256(OUT_WAV.read_bytes()).hexdigest(),
    }
    OUT_META.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUT_WAV} ({meta['wav_bytes']} bytes, {total_ms} ms)")
    print(f"wrote {OUT_META}")
    for u, a in zip(UTTERANCES, meta["expected_official_answers"]):
        print(f"  {u!r} -> {a!r}")
    return 0


def _official_answers() -> list:
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    by_q = {p["standard_question"]: p["official_answer"] for p in seed["qa_pairs"]}
    out = []
    for u in UTTERANCES:
        hit = next((a for q, a in by_q.items() if u in q or q.rstrip("？?") == u), None)
        if hit is None:
            raise SystemExit(f"{u!r} 在 seed_demo.json 里没有官方答案，检索必然落空")
        out.append(hit)
    return out


if __name__ == "__main__":
    sys.exit(main())
