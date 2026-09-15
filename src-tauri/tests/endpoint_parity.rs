//! 跨语言复核：Rust 端点状态机 vs Python 参考实现（M2-7 DoD）。
//!
//! DoD 原文：「与 Python 参考实现在同一段合成样本上复核：边界误差 <1 帧」。
//!
//! 「同一段样本」的形态是**逐帧 voiced 决策序列** —— 那是端点状态机的输入契约
//! 本身（`endpoint.py` 的入参就是 `list[bool]`）。样本由
//! `scripts/gen_endpoint_fixture.py` 生成，参考段列表由 `endpoint.py` 算出，
//! 一起落在 `testdata/endpoint_fixture.json`。本测试读**同一个文件**，
//! 所以两边不可能各跑各的。
//!
//! 覆盖：起始窗口 3/5、最短段 9/8 帧、hangover 16/17 帧、500 帧强制切段、
//! 切段后 onset 钳位、流末封口、丢弃段不推进 floor、稀疏误触发，
//! 外加 12 条定种子随机串。
//!
//! 边界误差口径：帧为单位，`max(|Δonset|, |Δoffset|)`。两侧都是帧量化结果，
//! 所以「<1 帧」等价于**逐帧完全一致**；本测试直接断言相等，
//! 同时把误差数字打出来（0 与"没测"必须能区分）。

use std::path::PathBuf;

use interview_copilot_lib::audio::endpoint::{
    detect_segments, Segment, END_SILENCE_FRAMES, MAX_SEG_FRAMES, MIN_KEEP_FRAMES,
    START_MIN_VOICED, START_WINDOW,
};
use serde_json::Value;
use sha2::{Digest, Sha256};

fn fixture_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src-tauri has a parent")
        .join("testdata")
        .join("endpoint_fixture.json")
}

fn load_fixture() -> Value {
    let path = fixture_path();
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| {
        panic!(
            "cannot read {}: {e}\n\
             run `python scripts/gen_endpoint_fixture.py` to regenerate it",
            path.display()
        )
    });
    serde_json::from_str(&text).expect("fixture must be valid JSON")
}

/// 决策串 → 逐帧 bool（'0'/'1'，无分隔符，见生成脚本）。
fn parse_decisions(s: &str) -> Vec<bool> {
    s.chars()
        .map(|c| match c {
            '1' => true,
            '0' => false,
            other => panic!("unexpected character in decisions: {other:?}"),
        })
        .collect()
}

fn parse_segments(v: &Value) -> Vec<Segment> {
    v.as_array()
        .expect("segments must be an array")
        .iter()
        .map(|pair| {
            let pair = pair.as_array().expect("a segment is a 2-element array");
            Segment {
                onset: pair[0].as_u64().expect("onset is a frame index"),
                offset: pair[1].as_u64().expect("offset is a frame index"),
            }
        })
        .collect()
}

#[test]
fn fixture_pins_the_frozen_spec_constants() {
    // 规格冻结：Rust 常量漂移会让两侧"看起来都对"，所以这里硬钉。
    let f = load_fixture();
    let spec = &f["spec"];
    assert_eq!(spec["start_window"].as_u64().unwrap(), START_WINDOW as u64);
    assert_eq!(
        spec["start_min_voiced"].as_u64().unwrap(),
        START_MIN_VOICED as u64
    );
    assert_eq!(
        spec["end_silence_frames"].as_u64().unwrap(),
        END_SILENCE_FRAMES
    );
    assert_eq!(spec["min_keep_frames"].as_u64().unwrap(), MIN_KEEP_FRAMES);
    assert_eq!(spec["max_seg_frames"].as_u64().unwrap(), MAX_SEG_FRAMES);
    assert_eq!(spec["frame_ms"].as_u64().unwrap(), 30);
}

#[test]
fn fixture_decisions_are_the_ones_the_reference_ran_on() {
    // 样本指纹：改了样本却没重跑参考实现，这里会先炸。
    let f = load_fixture();
    let mut hasher = Sha256::new();
    let cases = f["cases"].as_array().expect("cases array");
    let joined = cases
        .iter()
        .map(|c| c["decisions"].as_str().expect("decisions string"))
        .collect::<Vec<_>>()
        .join("\n");
    hasher.update(joined.as_bytes());
    let got = format!("{:x}", hasher.finalize());
    assert_eq!(
        got,
        f["decisions_sha256"].as_str().unwrap(),
        "fixture decisions do not match their own sha256"
    );
    assert_eq!(cases.len(), f["total_cases"].as_u64().unwrap() as usize);
}

#[test]
fn every_case_matches_the_python_reference_within_one_frame() {
    let f = load_fixture();
    let cases = f["cases"].as_array().expect("cases array");
    let mut checked_frames = 0u64;
    let mut checked_segments = 0usize;
    let mut worst_error = 0u64;
    let mut report = Vec::new();

    for case in cases {
        let name = case["name"].as_str().unwrap();
        let decisions = parse_decisions(case["decisions"].as_str().unwrap());
        assert_eq!(
            decisions.len() as u64,
            case["frames"].as_u64().unwrap(),
            "{name}: fixture frame count disagrees with its own decision string"
        );
        let reference = parse_segments(&case["segments"]);

        let ours = detect_segments(&decisions);

        // 段数必须一致：多一段/少一段都是边界错误，不是"误差小"。
        assert_eq!(
            ours.len(),
            reference.len(),
            "{name}: segment count differs — ours {ours:?}, reference {reference:?}"
        );
        for (i, (a, b)) in ours.iter().zip(reference.iter()).enumerate() {
            let err = a.onset.abs_diff(b.onset).max(a.offset.abs_diff(b.offset));
            worst_error = worst_error.max(err);
            // DoD：边界误差 < 1 帧（两侧都是帧量化，故等价于逐帧一致）。
            assert!(
                err < 1,
                "{name}: segment #{i} boundary error {err} frames \
                 (>= 1 frame) — ours {a:?}, reference {b:?}"
            );
            assert_eq!(a, b, "{name}: segment #{i} differs from the reference");
        }

        checked_frames += decisions.len() as u64;
        checked_segments += reference.len();
        report.push(format!("{name}: {} segs, 0 frames error", ours.len()));
    }

    // 样本量下限：样本被削到 3 条也算"全绿"，所以把量钉住。
    assert!(
        checked_frames >= 15_000,
        "fixture is too small to be evidence: {checked_frames} frames"
    );
    assert!(
        checked_segments >= 60,
        "fixture is too small to be evidence: {checked_segments} segments"
    );
    assert_eq!(worst_error, 0, "worst boundary error must be 0 frames");

    println!(
        "ENDPOINT_PARITY_PASS frames={checked_frames} segments={checked_segments} \
         worst_boundary_error_frames={worst_error}"
    );
    for line in &report {
        println!("  {line}");
    }
}

#[test]
fn boundary_cases_actually_exercise_every_rule() {
    // 防止"样本全绿但其实没测到某条规则"：逐条确认关键形态真的出现在 fixture 里。
    let f = load_fixture();
    let cases = f["cases"].as_array().unwrap();
    let by_name = |n: &str| -> Vec<Segment> {
        let c = cases
            .iter()
            .find(|c| c["name"] == n)
            .unwrap_or_else(|| panic!("fixture is missing case {n}"));
        parse_segments(&c["segments"])
    };

    // 起始窗口阈值
    assert_eq!(by_name("start_exactly_three_of_five").len(), 1);
    assert!(by_name("two_of_five_never_starts").is_empty());
    // 最短段边界
    assert_eq!(by_name("min_keep_exactly_nine_frames").len(), 1);
    assert!(by_name("min_keep_eight_frames_dropped").is_empty());
    // hangover 边界
    assert_eq!(by_name("hangover_sixteen_frames_continues").len(), 1);
    assert_eq!(by_name("hangover_seventeen_frames_closes").len(), 2);
    // 强制切段 + onset 钳位（两段不得交叠）
    let cut = by_name("forced_cut_thousand_voiced_frames");
    assert_eq!(cut.len(), 2);
    assert_eq!(cut[0].frames(), MAX_SEG_FRAMES);
    assert!(cut[0].offset < cut[1].onset, "segments must not overlap");
    let clamped = by_name("onset_clamped_after_forced_cut");
    assert_eq!(clamped.len(), 2);
    assert!(clamped[0].offset < clamped[1].onset);
    // 流末封口
    assert_eq!(by_name("segment_open_at_end_of_stream").len(), 1);
    assert!(by_name("short_segment_at_end_of_stream").is_empty());
    // 稀疏误触发
    assert!(by_name("sparse_blips_all_dropped").is_empty());

    // 随机串里必须真的出现过强制切段，否则那条规则只被脚本串覆盖过。
    let mut forced_cuts = 0usize;
    for c in cases {
        for s in parse_segments(&c["segments"]) {
            if s.frames() == MAX_SEG_FRAMES {
                forced_cuts += 1;
            }
        }
    }
    assert!(
        forced_cuts >= 2,
        "expected forced cuts in the fixture, found {forced_cuts}"
    );
}
