"""生成端点状态机的跨语言复核 fixture（M2-7 DoD：与 Python 参考实现同一段样本上边界误差 <1 帧）。

为什么要有这个脚本：`sidecar/src/audio_eval/endpoint.py` 是**规格的参考实现**，
`src-tauri/src/audio/endpoint.rs` 是生产实现。DoD 要求两者在同一段合成样本上
边界误差 <1 帧 —— 那就必须有一个**双方都读的、落在仓里的**样本，而不是
各跑各的再口头声称一致。

样本 = **逐帧 voiced 决策序列**。这是端点状态机的输入契约本身
（`endpoint.py` 的入参就是 `list[bool]`），所以它才是"同一段样本"的正确形态：
把 VAD 也算进来的话，比的就不再是状态机，而是 libfvad 的 Python wheel
与 Rust crate 的二进制差异（那条账记在 `docs/audio-regression.md` §1，
需要真实录音，属于 R19 而非 M2-7）。

样本构成：
- 逐条覆盖全部边界的**脚本串**（起始窗口、最短段 9/8 帧、hangover 16/17 帧、
  500 帧强制切段、切段后 onset 钳位、流末封口、丢弃段不推进 floor、稀疏误触发）；
- 定种子随机串（12 条，语音概率 0.15–0.85，含长串语音以逼出强制切段）。

输出：`testdata/endpoint_fixture.json`。Rust 侧 `tests/endpoint_parity.rs` 读同一文件。

用法：
    python scripts/gen_endpoint_fixture.py [--check]

`--check` 只生成到内存并与仓里的文件比对（CI/回归用，不写盘）。
"""

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sidecar" / "src"))

from audio_eval.endpoint import detect_segments  # noqa: E402

OUT = ROOT / "testdata" / "endpoint_fixture.json"


def runs(*spec) -> list:
    """`(帧数, voiced)` 串 → 逐帧决策。"""
    out: list = []
    for n, v in spec:
        out.extend([bool(v)] * n)
    return out


def scripted_cases() -> list:
    """逐条钉边界的脚本串。每条都写清它在钉什么。"""
    cases = []

    def add(name: str, why: str, decisions: list):
        cases.append({"name": name, "why": why, "decisions": decisions})

    add(
        "quiet_single_utterance",
        "最朴素的一条：静音 → 一段语音 → 静音",
        runs((20, False), (30, True), (40, False)),
    )
    add(
        "two_utterances_synthetic_dual_string",
        "合成双语音串（与 audio_regression 自检同形）：两段独立语音",
        runs((15, False), (40, True), (30, False), (45, True), (30, False)),
    )
    add(
        "start_exactly_three_of_five",
        "起始窗口恰好 3/5 voiced：必须触发，onset 取窗口内第一个 voiced",
        runs((2, False), (20, True), (30, False)),
    )
    add(
        "two_of_five_never_starts",
        "任意 5 帧窗口只有 2 帧 voiced：永不触发（阈值边界）",
        runs(*([(2, True), (3, False)] * 40)),
    )
    add(
        "min_keep_exactly_nine_frames",
        "最短段恰好 9 帧跨度：保留",
        runs((2, False), (9, True), (30, False)),
    )
    add(
        "min_keep_eight_frames_dropped",
        "最短段恰好 8 帧跨度：丢弃",
        runs((2, False), (8, True), (30, False)),
    )
    add(
        "hangover_sixteen_frames_continues",
        "16 帧静音（<17）不封口：后面的语音仍属同一段",
        runs((9, True), (16, False), (10, True), (40, False)),
    )
    add(
        "hangover_seventeen_frames_closes",
        "17 帧静音封口：切成两段",
        runs((9, True), (17, False), (10, True), (40, False)),
    )
    add(
        "forced_cut_thousand_voiced_frames",
        "500 帧强制切段：1000 帧连续语音 → 两段 back-to-back",
        runs((1000, True)),
    )
    add(
        "forced_cut_with_sixteen_frame_gaps",
        "段内 16 帧静音间隙把段撑到 500 帧：封口帧恒为 onset+499",
        runs(*(
            [(10, True)] + [(16, False), (1, True)] * 30 + [(16, False), (30, True)]
        )),
    )
    add(
        "onset_clamped_after_forced_cut",
        "切段后 onset 以封口 +1 为下限钳位，两段不得交叠",
        runs((2, False), (700, True), (40, False)),
    )
    add(
        "segment_open_at_end_of_stream",
        "流结束时还开着的段：参考实现收尾 `if in_seg: _close(last_voiced)`",
        runs((30, True)),
    )
    add(
        "short_segment_at_end_of_stream",
        "流结束时的短段仍按最短段规则丢弃",
        runs((4, True)),
    )
    add(
        "dropped_segment_does_not_advance_floor",
        "短段丢弃不推进 floor：紧随其后的真段 onset 不被无谓推后",
        runs((2, False), (4, True), (20, False), (20, True), (30, False)),
    )
    add(
        "sparse_blips_all_dropped",
        "孤立单帧 voiced（噪声毛刺）：全部丢弃，零段",
        runs(*([(1, True), (9, False)] * 30)),
    )
    add(
        "leading_and_trailing_silence_only",
        "纯静音：零段",
        runs((300, False)),
    )
    add(
        "three_segments_with_varied_lengths",
        "三段不等长，含一段刚好 9 帧的边界段",
        runs((10, False), (60, True), (25, False), (9, True), (25, False), (3, True), (25, False)),
    )
    return cases


def random_cases(count: int = 12) -> list:
    """定种子随机串：语音概率与游程长度都变，用来兜住脚本没想到的组合。"""
    cases = []
    for k in range(count):
        rng = random.Random(0xC0FFEE + k)
        p = 0.15 + 0.7 * (k / max(1, count - 1))
        n = 1200
        decisions = []
        # 交替"语音段"与"静音段"，段长服从几何分布 —— 比独立同分布更容易撞出
        # 长语音（强制切段）与恰好卡在 9/17 边界的段。
        voiced = False
        left = 0
        for _ in range(n):
            if left == 0:
                voiced = (rng.random() < p)
                mean = 40 if voiced else 20
                left = max(1, int(rng.expovariate(1.0 / mean)))
            decisions.append(voiced)
            left -= 1
        cases.append(
            {
                "name": f"random_seed_{k:02d}_p{p:.2f}",
                "why": f"定种子随机串（voiced 概率 {p:.2f}，游程几何分布）",
                "decisions": decisions,
            }
        )
    return cases


def encode(decisions: list) -> str:
    """决策序列 → '0101...' 紧凑串（Rust 侧逐字符解析，无歧义）。"""
    return "".join("1" if d else "0" for d in decisions)


def build() -> dict:
    cases = scripted_cases() + random_cases()
    out_cases = []
    total_segments = 0
    for c in cases:
        segs = detect_segments(c["decisions"])
        total_segments += len(segs)
        out_cases.append(
            {
                "name": c["name"],
                "why": c["why"],
                "frames": len(c["decisions"]),
                "voiced_frames": sum(1 for d in c["decisions"] if d),
                "decisions": encode(c["decisions"]),
                "segments": [[s, e] for (s, e) in segs],
            }
        )
    body = {
        "generator": "scripts/gen_endpoint_fixture.py",
        "reference": "sidecar/src/audio_eval/endpoint.py",
        "input_contract": "逐帧 voiced 决策（30ms/帧）；样本 = 决策序列本身",
        "spec": {
            "start_window": 5,
            "start_min_voiced": 3,
            "end_silence_frames": 17,
            "min_keep_frames": 9,
            "max_seg_frames": 500,
            "frame_ms": 30,
        },
        "cases": out_cases,
    }
    # 内容指纹：样本变了必须能一眼看出（不是靠 diff 数行）。
    digest = hashlib.sha256(
        "\n".join(c["decisions"] for c in out_cases).encode("ascii")
    ).hexdigest()
    body["decisions_sha256"] = digest
    body["total_cases"] = len(out_cases)
    body["total_reference_segments"] = total_segments
    return body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比对仓里的文件，不写盘")
    args = ap.parse_args()

    body = build()
    text = json.dumps(body, ensure_ascii=False, indent=2) + "\n"

    if args.check:
        if not OUT.is_file():
            print(f"FAIL: {OUT} 不存在，先跑一次不带 --check 的生成")
            return 1
        if OUT.read_text(encoding="utf-8") != text:
            print(f"FAIL: {OUT} 与当前参考实现/样本不一致（规格或样本被改过）")
            return 1
        print(f"FIXTURE_CHECK_PASS cases={body['total_cases']} "
              f"segments={body['total_reference_segments']} "
              f"sha256={body['decisions_sha256'][:16]}")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"cases={body['total_cases']} reference_segments={body['total_reference_segments']}")
    print(f"decisions_sha256={body['decisions_sha256']}")
    for c in body["cases"]:
        print(f"  {c['name']:<44} frames={c['frames']:<5} "
              f"voiced={c['voiced_frames']:<5} segs={c['segments']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
