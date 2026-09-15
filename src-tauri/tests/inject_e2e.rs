//! 注入 E2E（feature = `audio-testharness`）：合成语音 → 真实全链 → 真实 sidecar。
//!
//! # 这条测试到底证明了什么
//!
//! 链路里**除素材之外没有一样是替身**：
//!
//! ```text
//! SyntheticSource(真 WAV) → CapturePipeline(真重采样/真分帧) → FrameQueue(真 R13 有界队列)
//!   → worker 线程（真 webrtc-vad + 真端点状态机）→ AudioUplink(真出站队列/真 WS) → 真 sidecar
//! ```
//!
//! 于是可以钉住四件只有在"整条接通"时才成立的事：
//!
//! 1. **段边界与 C1a 参考端点同源一致**：喂给参考实现的决策串来自 worker 的真实
//!    VAD（`CaptureService::with_decision_tap` 观察点），不是测试自己重跑一遍 VAD
//!    猜的。所以比的是「同一串决策」下两个端点实现 —— 边界误差 < 1 帧。
//! 2. **序号连续**（R10）：一条连接内音频帧与事件帧共用一个连续序号空间。
//! 3. **`dropped == 0`**：真实节拍下队列不该溢出（R13 只在真跟不上时才生效）。
//! 4. **`duration_ms == 帧数 × 30ms`**（真 sidecar 的 `asr_final` 回执）。
//!
//! # 为什么必须用 `Pace::Realtime`
//!
//! `Pace::Fast` 会在几十帧内把 64 格的 `FrameQueue` 撑爆并**真的丢帧** ——
//! 那是 R13 在正常工作，但段边界就没意义了（丢帧会被端点当空洞，段会被迫封口）。
//! 所以：**断言边界与 `dropped == 0` 的测试一律 `Realtime`**。想看丢帧路径请写
//! 另一个用 `Fast` 的测试，别把两件事混在一条里。
//!
//! # 观察到的帧数比素材短一截（不是丢帧）
//!
//! `testdata/synth_utterance_zh.wav` 是 22 050Hz 单声道 165 949 样本 = 7.526s
//! = **250 帧**。E2E 里 `decisions.len()` 只有 **224**，少 26 帧 —— 因为测试在
//! 第二段封口（= 最后一个 voiced 帧 + 17 帧 hangover）之后就 `stop()` 了，
//! 源线程停在素材尾部之前，剩下的是**尾部静音**。这不是丢帧：
//! - `synthetic.rs::the_shipped_synth_wav_feeds_without_sample_loss` 钉住了
//!   "整个素材分块喂进去一帧不丢（250/250）"；
//! - 本文件里的 `dropped == 0` / `holes == 0` 钉住了"已推的那部分一帧不丢"。
//!
//! 顺带记一个**生产链路既有的性质**（不是本任务引入的，也不在本任务范围内）：
//! `CapturePipeline` 没有 `flush()`，所以停止时还留在 `backlog`（不足一个 rubato
//! 输入块）、`ready`（不足一帧）与 rubato 内部延迟里的样本永远不会变成帧。
//!
//! **实测上界（2026-09-15，见 `audio::resample::tests` 的
//! `stop_time_in_flight_tail_is_bounded_by_the_buffers_themselves`）**：
//! 16 kHz 直通 / 44.1 kHz / 48 kHz 最坏 **< 1 帧（≈30 ms）**，
//! 22.05 kHz 最坏 **≈2 帧（59.4 ms）**，硬上界 ≤ 3 帧（90 ms）。
//! 即**数量级是几十毫秒，不是几百毫秒**；而且其中大部分不可挽回——
//! 最后那个不满 480 的零头本来就发不出去（帧是定长 30 ms）。
//! 真要 flush 最多补回一帧，故判定**不值得**给生产链加这条路径。
//!
//! 需要 `INTERVIEWCOPILOT_PYTHON`（或 PATH 上有 `python`）；`INTERVIEWCOPILOT_SKIP_E2E=1` 跳过。

#![cfg(feature = "audio-testharness")]

mod support;

use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use interview_copilot_lib::audio::loopback::{AudioError, LoopbackSource};
use interview_copilot_lib::audio::service::{
    CaptureService, DecisionTap, SelfCheckStatus, SourceFactory,
};
use interview_copilot_lib::audio::synthetic::{Pace, SyntheticSource};
use interview_copilot_lib::audio::uplink::EventSink;
use interview_copilot_lib::audio::PathLabel;
use serde_json::{json, Value};
use support::fake_sidecar::{FakeSidecar, Recorded};
use support::real_sidecar::{python_exe, repo_root, skipped, start_sidecar, TOKEN};
use support::signal::Script;

const FRAME_MS: u64 = 30;
/// 真 WAV 的时长 ≈7.5s，加上 sidecar 建连/收尾，30s 是宽松上界。
const WAIT: Duration = Duration::from_secs(30);

// ------------------------------------------------------------------ 素材与工厂

fn wav_path() -> PathBuf {
    repo_root().join("testdata").join("synth_utterance_zh.wav")
}

/// mic 路的注入素材：静音 20 / 类语音 40 / 静音 30 = 90 帧（2.7s）→ 恰好 1 段。
///
/// 刻意与 loopback 的 WAV **不同素材**：两路的段数与内容都会不同，
/// 于是"串味"会在断言上直接暴露，而不是靠碰巧都通过。
fn mic_material() -> Vec<f32> {
    let mut s = Script::new();
    s.silent(20).voiced(40, 0xC0FFEE).silent(30);
    s.samples
}

fn factory(wav: PathBuf, pace: Pace) -> Arc<SourceFactory> {
    Arc::new(
        move |path: PathLabel| -> Result<Box<dyn LoopbackSource>, AudioError> {
            match path {
                PathLabel::Loopback => {
                    Ok(Box::new(SyntheticSource::from_wav(&wav)?.with_pace(pace)))
                }
                PathLabel::Mic => Ok(Box::new(
                    SyntheticSource::from_samples("注入 (mic)", 16_000, 1, mic_material())
                        .with_pace(pace),
                )),
            }
        },
    )
}

// ------------------------------------------------------------------ 观察点

/// 逐帧决策记录（worker 线程写、测试线程读）。
#[derive(Default)]
struct DecisionLog {
    rows: Mutex<Vec<(PathLabel, u64, bool)>>,
}

impl DecisionLog {
    fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    fn tap(self: &Arc<Self>) -> DecisionTap {
        let me = self.clone();
        Arc::new(move |path: PathLabel, index: u64, voiced: bool| {
            me.rows
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .push((path, index, voiced));
        })
    }

    /// 某一路按帧序排好的 voiced 序列。
    ///
    /// **顺带钉住连续性**：观察点记录的就是端点状态机的输入，所以序号必须无缝。
    /// 有洞就说明有帧没进端点（丢帧/分类失败）—— 那时刻点参考复核就失去意义
    /// （比的是两条不同的决策串），必须在这里就炸，而不是让后面的比对给出
    /// 一个"边界差了几帧"的假线索。
    fn decisions_for(&self, path: PathLabel) -> Vec<bool> {
        let mut rows: Vec<(u64, bool)> = self
            .rows
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .filter(|(p, _, _)| *p == path)
            .map(|(_, i, v)| (*i, *v))
            .collect();
        rows.sort_by_key(|(i, _)| *i);
        for (k, (i, _)) in rows.iter().enumerate() {
            assert_eq!(
                *i,
                k as u64,
                "{}: 决策序号不连续（第 {k} 条是 {i}）—— 有帧没进端点，参考复核无意义",
                path.as_str()
            );
        }
        rows.into_iter().map(|(_, v)| v).collect()
    }

    fn voiced_ratio(&self, path: PathLabel) -> f64 {
        let d = self.decisions_for(path);
        if d.is_empty() {
            return 0.0;
        }
        d.iter().filter(|v| **v).count() as f64 / d.len() as f64
    }
}

// ------------------------------------------------------------------ 参考端点

static TMP_SEQ: AtomicUsize = AtomicUsize::new(0);

/// 把决策串交给 C1a 参考端点（`scripts/endpoint_reference.py` → `endpoint.py`），
/// 拿回参考段的 ms 边界 `[(start_ms, end_ms)]`。
///
/// 走子进程而不是在 Rust 里重写：重写一遍就等于"拿我的实现当标准"。
fn reference_segments_ms(decisions: &[bool], tag: &str) -> Vec<(u64, u64)> {
    let text: String = decisions
        .iter()
        .map(|d| if *d { '1' } else { '0' })
        .collect();
    let n = TMP_SEQ.fetch_add(1, Ordering::Relaxed);
    let file = std::env::temp_dir().join(format!(
        "ic-inject-decisions-{}-{tag}-{n}.txt",
        std::process::id()
    ));
    std::fs::write(&file, &text).expect("写决策文件");
    let out = Command::new(python_exe())
        .arg(repo_root().join("scripts").join("endpoint_reference.py"))
        .arg("--decisions-file")
        .arg(&file)
        .output()
        .unwrap_or_else(|e| panic!("跑不动 {}：{e}", python_exe()));
    let _ = std::fs::remove_file(&file);

    assert!(
        out.status.success(),
        "endpoint_reference.py 失败（exit {:?}）：\n{}",
        out.status.code(),
        String::from_utf8_lossy(&out.stderr)
    );
    let v: Value = serde_json::from_slice(&out.stdout).expect("参考脚本必须输出 JSON");
    assert_eq!(
        v["frames"].as_u64().unwrap(),
        decisions.len() as u64,
        "参考脚本读到的帧数与决策数不一致"
    );
    v["ms"]
        .as_array()
        .expect("ms 数组")
        .iter()
        .map(|p| {
            let a = p.as_array().expect("每段是二元组");
            (
                a[0].as_u64().expect("start_ms"),
                a[1].as_u64().expect("end_ms"),
            )
        })
        .collect()
}

// ------------------------------------------------------------------ 线上记录

/// 从线上记录里配出 `[(segment_start.ts_ms, segment_end.ts_ms)]`。
fn wire_segments(records: &[Recorded], path: &str) -> Vec<(u64, u64)> {
    let mut out = Vec::new();
    let mut open: Option<u64> = None;
    for r in records {
        match r.event_name() {
            Some("segment_start") => {
                assert!(
                    open.is_none(),
                    "{path}: 出现段重叠（上一个 start 还没 end）"
                );
                open = Some(r.ts_ms() as u64);
            }
            Some("segment_end") => {
                let s = open
                    .take()
                    .unwrap_or_else(|| panic!("{path}: 出现没有 start 的 end"));
                out.push((s, r.ts_ms() as u64));
            }
            _ => {}
        }
    }
    assert!(open.is_none(), "{path}: 流结束时仍有未封口的段");
    out
}

/// R10：一条连接内序号必须连续。
fn assert_contiguous_seq(records: &[Recorded], path: &str) {
    assert!(!records.is_empty(), "{path}: 线上一条记录都没有");
    let mut expected = records[0].seq();
    for (i, r) in records.iter().enumerate() {
        assert_eq!(
            r.seq(),
            expected,
            "{path}: 第 {i} 条 seq={} 期望 {expected}（序号空间不连续）",
            r.seq()
        );
        expected = expected.wrapping_add(1);
    }
}

/// 参考复核：线上边界 vs 参考边界，误差必须 < 1 帧。
fn assert_matches_reference(ours: &[(u64, u64)], reference: &[(u64, u64)], path: &str) {
    assert_eq!(
        ours.len(),
        reference.len(),
        "{path}: 段数不一致 —— 线上 {ours:?}，参考 {reference:?}"
    );
    let mut worst_ms = 0u64;
    for (i, (a, b)) in ours.iter().zip(reference.iter()).enumerate() {
        let err = a.0.abs_diff(b.0).max(a.1.abs_diff(b.1));
        worst_ms = worst_ms.max(err);
        assert!(
            err < FRAME_MS,
            "{path}: 第 {i} 段边界误差 {err}ms（{} 帧）≥ 1 帧 —— 线上 {a:?}，参考 {b:?}",
            err / FRAME_MS
        );
        assert_eq!(a, b, "{path}: 第 {i} 段与参考不一致");
    }
    println!(
        "INJECT_REFERENCE_PASS path={path} segments={} worst_boundary_error_frames={}",
        ours.len(),
        worst_ms / FRAME_MS
    );
}

// ------------------------------------------------------------------ 下行记录

#[derive(Default)]
struct DownlinkSink {
    events: Mutex<Vec<(String, Value)>>,
}

impl DownlinkSink {
    fn payloads_for(&self, name: &str) -> Vec<Value> {
        self.events
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .filter(|(n, _)| n == name)
            .map(|(_, v)| v.clone())
            .collect()
    }

    fn count(&self, name: &str) -> usize {
        self.payloads_for(name).len()
    }
}

impl EventSink for DownlinkSink {
    fn emit(&self, event: &str, payload: Value) {
        self.events
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push((event.to_string(), payload));
    }
}

async fn wait_for_finals(
    sink: &Arc<DownlinkSink>,
    path: &str,
    n: usize,
    timeout: Duration,
) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        let got = sink
            .payloads_for("asr://final")
            .into_iter()
            .filter(|p| p["path"] == json!(path))
            .count();
        if got >= n {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
}

// ------------------------------------------------------------------ 测试

/// DoD(item 7)：段序列与 C1a 黄金向量同源参考边界误差 < 1 帧 + seq 连续 + dropped=0。
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn injected_speech_matches_the_c1a_reference_boundaries_on_the_wire() {
    if skipped() {
        eprintln!("SKIP: INTERVIEWCOPILOT_SKIP_E2E set");
        return;
    }
    let wav = wav_path();
    assert!(wav.is_file(), "缺少合成素材 {}", wav.display());

    let sidecar = FakeSidecar::start(2).await;
    let log = DecisionLog::new();
    let svc = CaptureService::with_source_factory(factory(wav.clone(), Pace::Realtime))
        .with_decision_tap(log.tap());
    let sink: Arc<dyn EventSink> = Arc::new(DownlinkSink::default());
    svc.start(sidecar.port, "test-token", sink)
        .await
        .expect("采集服务应能启动");

    assert!(
        sidecar.wait_for_path("loopback", WAIT).await,
        "loopback 应在 30s 内报出 path（= 收到 segment_start）"
    );
    let conn = sidecar.conn_for("loopback").expect("loopback 连接");
    let ok = conn.wait_for_segments(2, WAIT).await;
    let stopped = svc.stop().await;
    if !ok {
        eprintln!("=== 线上记录（{} 条）===", conn.len());
        for r in conn.snapshot() {
            match r.event_name() {
                Some(e) => eprintln!("  [event {e}] seq={} ts={}", r.seq(), r.ts_ms()),
                None => eprintln!("  [audio] seq={} ts={}", r.seq(), r.ts_ms()),
            }
        }
        eprintln!("=== 快照 === {:#?}", stopped);
    }
    assert!(ok, "loopback 应在 30s 内收满 2 段");

    let p = stopped
        .paths
        .iter()
        .find(|p| p.path == "loopback")
        .expect("loopback 快照");
    // --- 契约与 R13 ---
    assert_eq!(p.vad_errors, 0, "帧/率契约不该被破坏");
    assert_eq!(p.holes, 0, "不该出现端点空洞（有洞说明丢帧了）");
    assert_eq!(p.source_stats.frames_dropped, 0, "真实节拍下源不该丢帧");
    assert_eq!(p.uplink.dropped, 0, "真实节拍下出站队列不该丢帧");
    assert_eq!(p.push_errors, 0, "不该有入队失败");
    assert_eq!(p.source_stats.errors, 0);
    assert_eq!(p.error, None, "该路不该有错误");
    assert!(
        p.self_check.status == SelfCheckStatus::Pass,
        "真实节拍下自检应通过：{:?}",
        p.self_check
    );

    let records = conn.snapshot();
    assert_contiguous_seq(&records, "loopback");

    let decisions = log.decisions_for(PathLabel::Loopback);
    assert_eq!(
        decisions.len() as u64,
        p.source_stats.frames_emitted,
        "观察点记录的帧数应等于源真实产出的帧数（少一帧就说明有帧没进端点）"
    );
    let reference = reference_segments_ms(&decisions, "loopback");
    let ours = wire_segments(&records, "loopback");
    assert_matches_reference(&ours, &reference, "loopback");

    println!(
        "INJECT_E2E_PASS frames={} voiced_ratio={:.3} segments={} seq_len={} \
         source_frames={} uplink_frames={} dropped=0",
        decisions.len(),
        log.voiced_ratio(PathLabel::Loopback),
        ours.len(),
        records.len(),
        p.source_stats.frames_emitted,
        p.uplink.frames_sent,
    );
}

/// DoD(item 7)：真 sidecar 的 `asr_final.duration_ms == 帧数 × 30ms`（±1 帧）。
///
/// 顺带用 sidecar **自己的**计数器复核 `seq_gap`/`dropped_oldest`/`violations`
/// 全为 0 —— 这是唯一一处"对端的说法"（而不是我们的自述）。
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn injected_audio_reaches_the_real_sidecar_with_frame_accurate_durations() {
    if skipped() {
        eprintln!("SKIP: INTERVIEWCOPILOT_SKIP_E2E set");
        return;
    }
    let wav = wav_path();
    assert!(wav.is_file(), "缺少合成素材 {}", wav.display());
    let Some(sidecar) = start_sidecar(&[]).await else {
        return;
    };

    let log = DecisionLog::new();
    let svc = CaptureService::with_source_factory(factory(wav.clone(), Pace::Realtime))
        .with_decision_tap(log.tap());
    let sink = Arc::new(DownlinkSink::default());
    let sink_dyn: Arc<dyn EventSink> = sink.clone();
    svc.start(sidecar.port, TOKEN, sink_dyn)
        .await
        .expect("采集服务应能连上真 sidecar");

    let ok = wait_for_finals(&sink, "loopback", 2, WAIT).await;
    let stopped = svc.stop().await;
    assert!(
        ok,
        "loopback 应在 30s 内回 2 条 asr_final；实际 final={} error={}",
        sink.count("asr://final"),
        sink.count("asr://error")
    );
    assert_eq!(
        sink.count("asr://error"),
        0,
        "不该有 asr_error：{:?}",
        sink.payloads_for("asr://error")
    );

    let decisions = log.decisions_for(PathLabel::Loopback);
    let reference = reference_segments_ms(&decisions, "loopback");
    let finals: Vec<Value> = sink
        .payloads_for("asr://final")
        .into_iter()
        .filter(|p| p["path"] == json!("loopback"))
        .collect();
    assert_eq!(finals.len(), reference.len(), "final 数应等于参考段数");

    let mut worst_frames = 0u64;
    for (i, (f, (s, e))) in finals.iter().zip(reference.iter()).enumerate() {
        let ts = f["ts_ms"].as_u64().expect("ts_ms");
        let dur = f["duration_ms"].as_u64().expect("duration_ms");
        assert_eq!(ts, *s, "第 {i} 段的 ts_ms 应等于参考起点");
        let expect = e - s;
        let err = dur.abs_diff(expect);
        worst_frames = worst_frames.max(err / FRAME_MS);
        assert!(
            err < FRAME_MS,
            "第 {i} 段 duration_ms={dur} 与 帧数×30ms={expect} 差 {} 帧（≥1 帧）",
            err / FRAME_MS
        );
    }

    // --- 对端自己的计数器（唯一不来自我们自述的证据）---
    let client = reqwest::Client::new();
    let resp = client
        .get(format!(
            "http://127.0.0.1:{}/diagnostics/audio",
            sidecar.port
        ))
        .header("Authorization", format!("Bearer {TOKEN}"))
        .send()
        .await
        .expect("取 sidecar 诊断");
    assert_eq!(resp.status().as_u16(), 200, "诊断端点应 200");
    let diag: Value = resp.json().await.expect("诊断应为 JSON");
    let last = &diag["last_connection"];
    assert!(!last.is_null(), "sidecar 应已记下最近一次连接：{diag}");
    for key in [
        "seq_gap",
        "dropped_oldest",
        "dropped_overload",
        "violations",
        "unsolicited_audio",
        "malformed",
        "interrupted",
    ] {
        assert_eq!(
            last[key],
            json!(0),
            "sidecar 的 {key} 应为 0（对端口径）：{last}"
        );
    }

    println!(
        "INJECT_SIDECAR_PASS segments={} worst_duration_error_frames={worst_frames} \
         sidecar_seq_gap=0 sidecar_dropped_oldest=0 frames={}",
        finals.len(),
        decisions.len(),
    );

    // --- 我们自己的口径也要干净（丢帧会让 duration 断言失去意义）---
    let p = stopped
        .paths
        .iter()
        .find(|p| p.path == "loopback")
        .expect("loopback 快照");
    assert_eq!(p.holes, 0, "不该有端点空洞");
    assert_eq!(p.vad_errors, 0, "不该有 VAD 错误");
    assert_eq!(p.source_stats.frames_dropped, 0, "源不该丢帧");
    assert_eq!(p.uplink.dropped, 0, "出站队列不该丢帧");
    assert_eq!(p.push_errors, 0, "不该有入队失败");
}

/// DoD(item 7)：双路注入互不污染 —— 两路各自的段序列都必须匹配各自的参考。
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn two_injected_paths_keep_independent_segments_and_references() {
    if skipped() {
        eprintln!("SKIP: INTERVIEWCOPILOT_SKIP_E2E set");
        return;
    }
    let wav = wav_path();
    assert!(wav.is_file(), "缺少合成素材 {}", wav.display());

    let sidecar = FakeSidecar::start(2).await;
    let log = DecisionLog::new();
    let svc = CaptureService::with_source_factory(factory(wav.clone(), Pace::Realtime))
        .with_decision_tap(log.tap());
    let sink: Arc<dyn EventSink> = Arc::new(DownlinkSink::default());
    svc.start(sidecar.port, "test-token", sink)
        .await
        .expect("采集服务应能启动");

    assert!(
        sidecar.wait_for_paths(WAIT).await,
        "两路都应在 30s 内报出 path"
    );
    let loopback = sidecar.conn_for("loopback").expect("loopback 连接");
    let mic = sidecar.conn_for("mic").expect("mic 连接");
    let lb_ok = loopback.wait_for_segments(2, WAIT).await;
    let mic_ok = mic.wait_for_segments(1, WAIT).await;
    let stopped = svc.stop().await;
    assert!(lb_ok && mic_ok, "两路都应各自收满段（loopback 2 / mic 1）");

    // --- 段数按素材各归各的（WAV 双语音 = 2 段；合成类语音 = 1 段）---
    assert_eq!(loopback.count_event("segment_start"), 2, "loopback 段数");
    assert_eq!(mic.count_event("segment_start"), 1, "mic 段数");
    assert_eq!(loopback.count_event("segment_end"), 2);
    assert_eq!(mic.count_event("segment_end"), 1);

    // --- 各自的边界都必须匹配各自的参考 ---
    for (name, conn, path) in [
        ("loopback", &loopback, PathLabel::Loopback),
        ("mic", &mic, PathLabel::Mic),
    ] {
        let records = conn.snapshot();
        assert_contiguous_seq(&records, name);
        let decisions = log.decisions_for(path);
        let reference = reference_segments_ms(&decisions, name);
        let ours = wire_segments(&records, name);
        assert_matches_reference(&ours, &reference, name);
        // 序号各自从 0 开始：两个独立的序号空间，不是一条被复用的连接。
        assert_eq!(records[0].seq(), 0, "{name}: 序号空间应各从 0 开始");
    }

    // --- 互不污染的直接证据 ---
    let lb_decisions = log.decisions_for(PathLabel::Loopback);
    let mic_decisions = log.decisions_for(PathLabel::Mic);
    assert_ne!(
        lb_decisions.len(),
        mic_decisions.len(),
        "两路帧数应不同（素材时长不同）—— 相同说明两路可能共享了同一份源"
    );
    assert_ne!(lb_decisions, mic_decisions, "两路决策序列不该相同");
    for p in &stopped.paths {
        assert_eq!(p.holes, 0, "{}: 不该有空洞", p.path);
        assert_eq!(p.vad_errors, 0, "{}: 不该有 VAD 错误", p.path);
        assert_eq!(p.source_stats.frames_dropped, 0, "{}: 不该丢帧", p.path);
    }
    let lb_snap = stopped.paths.iter().find(|p| p.path == "loopback").unwrap();
    let mic_snap = stopped.paths.iter().find(|p| p.path == "mic").unwrap();
    assert!(
        lb_snap.source_device.contains("synth_utterance_zh"),
        "loopback 设备名应指向注入的 WAV：{}",
        lb_snap.source_device
    );
    assert!(
        mic_snap.source_device.contains("mic"),
        "mic 设备名应指向注入的 mic 素材：{}",
        mic_snap.source_device
    );

    println!(
        "INJECT_DUAL_PATH_PASS loopback_frames={} mic_frames={} \
         loopback_segments=2 mic_segments=1",
        lb_decisions.len(),
        mic_decisions.len()
    );
}
