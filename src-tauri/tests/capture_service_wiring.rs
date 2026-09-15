//! 采集服务接线测试（M2-7 第 2/3 项）。
//!
//! 三件事必须被证明，而且只能在这层证明：
//!
//! 1. **双路独立 worker**：两路各有自己的 VAD 实例与端点，各自算出段序列；
//!    两路不会串味（样本按 path 不同，断言两路段序列各归各的）。
//! 2. **事件合并进统一上行队列**：`segment_start`/`segment_end`/`vad_state`
//!    与音频帧走同一条出站队列，因此线上是**一个连续的序号空间**（R10），
//!    且顺序恒为 `segment_start → 帧 → segment_end`。
//! 3. **启动语义**：uplink 连不上是致命的（服务保持停止）；单路设备打不开
//!    不是致命的（另一路照跑，失败原因落在该路）。
//!
//! 用假 sidecar 而不是真 sidecar：这里要的是逐条线上记录，不是真 ASR。
//! 真全链（真 sidecar + 真模型）在 `synth_e2e.rs`。

mod support;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use interview_copilot_lib::audio::loopback::AudioError;
use interview_copilot_lib::audio::service::{
    CaptureService, SelfCheckStatus, ServiceSnapshot, SourceFactory,
};
use interview_copilot_lib::audio::uplink::EventSink;
use interview_copilot_lib::audio::PathLabel;
use serde_json::Value;
use support::fake_sidecar::FakeSidecar;
use support::signal::Script;
use support::source::{factory_for, PushSource, REAL_FRAME_PACE};

/// 记录下行事件（不落盘、不打印：下行可能含转写文本，R14）。
#[derive(Default)]
struct RecordingSink {
    count: AtomicUsize,
}

impl EventSink for RecordingSink {
    fn emit(&self, _event: &str, _payload: Value) {
        self.count.fetch_add(1, Ordering::Relaxed);
    }
}

/// 双语音串：静音 8 / 语音 15 / 静音 24 / 语音 15 / 静音 24 = 86 帧（2.58s）。
///
/// 间隔给到 24 帧（720ms）而不是规格下限 17 帧，是刻意的余量：真实
/// webrtc-vad 在语音尾部可能多报几帧 voiced，17 帧的间隔在极端情况下会被
/// 吃掉；这里要测的是接线，不是把 VAD 逼到边界。
fn dual_utterance(seed: u64) -> Vec<f32> {
    let mut s = Script::new();
    s.silent(8)
        .voiced(15, seed)
        .silent(24)
        .voiced(15, seed ^ 0x5EED)
        .silent(24);
    s.samples
}

const TOTAL_FRAMES: usize = 86;

#[tokio::test]
async fn two_paths_run_independent_workers_and_share_one_seq_space_each() {
    let sidecar = FakeSidecar::start(2).await;

    let factory = factory_for(
        |path: PathLabel| {
            dual_utterance(match path {
                PathLabel::Loopback => 0xA11CE,
                PathLabel::Mic => 0xB0B,
            })
        },
        16_000,
        REAL_FRAME_PACE,
    );
    let svc = CaptureService::with_source_factory(factory);

    let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::default());
    let snap = svc
        .start(sidecar.port, "test-token", sink)
        .await
        .expect("采集服务应能启动");

    assert!(snap.running, "start 后服务应为运行态");
    assert_eq!(snap.paths.len(), 2, "两路都应在快照里可见");

    // 等两路都报出 path（= 都收到了 segment_start），再等段收完。
    assert!(
        sidecar.wait_for_paths(Duration::from_secs(10)).await,
        "两路都应在 10s 内报出 path"
    );

    let loopback = sidecar.conn_for("loopback").expect("loopback 连接");
    let mic = sidecar.conn_for("mic").expect("mic 连接");

    // 两路分别等（`&&` 会短路，异步等待不能短路，否则第二路永远没被等）。
    let lb_ok = loopback.wait_for_segments(2, Duration::from_secs(15)).await;
    let mic_ok = mic.wait_for_segments(2, Duration::from_secs(15)).await;
    let ok = lb_ok && mic_ok;
    if !ok {
        // 失败时把线上记录与快照一起打出来：这两样是定位"哪一环没接上"的唯一证据，
        // 只报"段数不足"等于让下一个人从头再查一遍。
        for (name, conn) in [("loopback", &loopback), ("mic", &mic)] {
            eprintln!("=== {name}: {} records ===", conn.len());
            for r in conn.snapshot() {
                match r.event_name() {
                    Some(e) => eprintln!("  [event {e}] seq={} ts={}", r.seq(), r.ts_ms()),
                    None => eprintln!("  [audio] seq={} ts={}", r.seq(), r.ts_ms()),
                }
            }
        }
        eprintln!("=== snapshot === {:#?}", svc.snapshot());
    }
    assert!(ok, "两路都应在 15s 内各自收满 2 段");

    for (name, conn) in [("loopback", &loopback), ("mic", &mic)] {
        let records = conn.snapshot();
        assert!(
            records.len() >= TOTAL_FRAMES / 2,
            "{name}: 线上记录数 {} 太少，管线可能没跑起来",
            records.len()
        );

        // --- 段结构：两段，各自 start/end 配对 ---
        assert_eq!(conn.count_event("segment_start"), 2, "{name}: 段数应为 2");
        assert_eq!(conn.count_event("segment_end"), 2, "{name}: 段收尾数应为 2");
        assert!(
            conn.count_event("vad_state") >= 1,
            "{name}: 2.5s 的流应有至少一次 vad_state 心跳"
        );

        // --- R10：音频帧与事件共用一个**连续**序号空间 ---
        let seqs: Vec<u16> = records.iter().map(|r| r.seq()).collect();
        let mut expected = seqs[0];
        for (i, got) in seqs.iter().enumerate() {
            assert_eq!(
                *got, expected,
                "{name}: 第 {i} 条记录 seq={got}，期望 {expected}（序号空间不连续）"
            );
            expected = expected.wrapping_add(1);
        }

        // --- 顺序：start → 帧 → end ---
        let mut depth = 0i32;
        let mut audio_in_segment = 0usize;
        for (i, r) in records.iter().enumerate() {
            match r.event_name() {
                Some("segment_start") => {
                    assert_eq!(depth, 0, "{name}: 第 {i} 条 start 出现在段内（段重叠）");
                    depth = 1;
                    audio_in_segment = 0;
                }
                Some("segment_end") => {
                    assert_eq!(depth, 1, "{name}: 第 {i} 条 end 没有对应的 start");
                    assert!(
                        audio_in_segment > 0,
                        "{name}: 第 {i} 条 end 之前没有任何音频帧（空段不该上线）"
                    );
                    depth = 0;
                }
                _ => {
                    if r.is_audio() {
                        assert_eq!(
                            depth, 1,
                            "{name}: 第 {i} 条音频帧落在段外（会被判 unsolicited）"
                        );
                        audio_in_segment += 1;
                    }
                }
            }
        }
        assert_eq!(depth, 0, "{name}: 流结束时仍有未封口的段");

        // 段时长落在冻结规格内（最短 9 帧 / 最长 500 帧）。
        for (k, pair) in segment_spans(&records).into_iter().enumerate() {
            assert!(
                (9..=500).contains(&pair),
                "{name}: 第 {k} 段 {pair} 帧，越出 [9, 500]"
            );
        }
    }

    // --- 两路是独立的两条连接，不是同一条被复用 ---
    assert!(
        loopback.path().as_deref() == Some("loopback") && mic.path().as_deref() == Some("mic"),
        "两路应各带自己的 path"
    );
    // 样本按 path 不同 → 线上音频内容必须不同（串味的直接证据）。
    assert_ne!(
        first_audio_head(&loopback.snapshot()),
        first_audio_head(&mic.snapshot()),
        "两路音频内容相同，说明两路可能共享了同一份源/同一路 worker"
    );

    // --- 快照：两路各有自己的 VAD 实例与自检结论 ---
    let snap = svc.snapshot();
    assert!(snap.running);
    for p in &snap.paths {
        assert_eq!(
            p.vad_provider, "webrtc-vad",
            "{}: 应有自己的 VAD 实例",
            p.path
        );
        assert_eq!(
            p.self_check.status,
            SelfCheckStatus::Pass,
            "{}: 真实帧率下自检应通过，实际 {:?}",
            p.path,
            p.self_check
        );
        assert!(
            p.device.contains("假源"),
            "{}: 设备名应来自 CaptureEvent::Started，实际 {}",
            p.path,
            p.device
        );
        assert_eq!(p.vad_errors, 0, "{}: 帧/率契约不应被破坏", p.path);
        assert_eq!(p.hold_overflow, 0, "{}: 候选段缓存不应溢出", p.path);
        assert!(p.segments_sent >= 2, "{}: 应已送出至少 2 段", p.path);
    }

    let stopped = svc.stop().await;
    assert!(!stopped.running, "stop 后服务应为停止态");
    // 停止不该把诊断面板清空：上一次运行的终态要留着，而且计数是 join 之后的终值。
    assert_eq!(
        stopped.paths.len(),
        2,
        "stop 后仍应能看到上一次运行的两路诊断"
    );
    for p in &stopped.paths {
        assert!(!p.running, "{}: 停后 worker 不该还在跑", p.path);
        assert!(
            p.source_stats.frames_emitted > 0,
            "{}: 停后应留下源的真实计数（而不是归零）",
            p.path
        );
        assert!(
            p.uplink.closed,
            "{}: stop() 应已优雅关闭 uplink（closed 必须为真，不能留一句过期的'未关闭'）",
            p.path
        );
    }
}

#[tokio::test]
async fn start_is_idempotent_and_stop_then_start_works() {
    let sidecar = FakeSidecar::start(4).await;
    let factory = factory_for(
        |path: PathLabel| dual_utterance(path as u64 + 1),
        16_000,
        REAL_FRAME_PACE,
    );
    let svc = CaptureService::with_source_factory(factory);
    let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::default());

    svc.start(sidecar.port, "t", sink.clone())
        .await
        .expect("第一次启动");
    let first = svc.snapshot();
    assert!(first.running);

    // 幂等：重复 start 不叠第二套线程。
    let second = svc
        .start(sidecar.port, "t", sink.clone())
        .await
        .expect("重复启动");
    assert!(second.running);
    assert_eq!(
        second.paths.len(),
        first.paths.len(),
        "重复 start 不该多出路径"
    );
    assert_eq!(
        sidecar.connections().len(),
        2,
        "重复 start 不该多建连接，实际 {}",
        sidecar.connections().len()
    );

    svc.stop().await;
    assert!(!svc.snapshot().running);

    // 再启一次：应能重新建立连接。
    svc.start(sidecar.port, "t", sink).await.expect("重启");
    assert!(svc.snapshot().running);
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while sidecar.connections().len() < 4 && std::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert_eq!(sidecar.connections().len(), 4, "重启应新建两路连接");
    svc.stop().await;
}

#[tokio::test]
async fn a_rejected_uplink_is_fatal_and_leaves_the_service_stopped() {
    // 一个只会回 403 的裸 TCP 服务端：模拟 token 被拒。
    let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind");
    let port = listener.local_addr().expect("addr").port();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut s) = stream else { break };
            use std::io::{Read, Write};
            let mut buf = [0u8; 2048];
            let _ = s.read(&mut buf);
            let _ = s.write_all(b"HTTP/1.1 403 Forbidden\r\ncontent-length: 0\r\n\r\n");
            let _ = s.flush();
        }
    });

    let factory = factory_for(
        |path: PathLabel| dual_utterance(path as u64 + 7),
        16_000,
        REAL_FRAME_PACE,
    );
    let svc = CaptureService::with_source_factory(factory);
    let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::default());

    let err = svc
        .start(port, "wrong-token", sink)
        .await
        .expect_err("鉴权被拒时 start 必须失败");
    assert!(
        err.contains("403") || err.to_lowercase().contains("reject"),
        "错误信息应说明鉴权被拒，实际：{err}"
    );

    let snap = svc.snapshot();
    assert!(!snap.running, "鉴权失败后服务必须保持停止态");
    assert!(
        snap.last_error.is_some(),
        "失败原因应留在快照里，而不是只返回一次错误"
    );
    assert!(
        snap.paths.is_empty(),
        "连 uplink 都没成，不该有任何一路在跑"
    );
}

#[tokio::test]
async fn one_broken_path_does_not_take_down_the_other() {
    let sidecar = FakeSidecar::start(1).await;

    // loopback 打不开（模拟 macOS 无回环 / 设备被独占），mic 正常。
    let factory: Arc<SourceFactory> = Arc::new(|path: PathLabel| match path {
        PathLabel::Loopback => Err(AudioError::Unsupported("本平台无系统回环".into())),
        PathLabel::Mic => Ok(Box::new(PushSource::new(
            "假源 (mic)",
            16_000,
            dual_utterance(0xC0FFEE),
        ))),
    });

    let svc = CaptureService::with_source_factory(factory);
    let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::default());
    let snap = svc
        .start(sidecar.port, "t", sink)
        .await
        .expect("单路失败不该致命");

    assert!(snap.running, "另一路还活着，服务就是运行态");

    let loopback = snap
        .paths
        .iter()
        .find(|p| p.path == "loopback")
        .expect("失败的那路也应在快照里可见（否则用户看不到为什么没声）");
    assert!(loopback.error.is_some(), "loopback 应记录失败原因");

    let mic = snap
        .paths
        .iter()
        .find(|p| p.path == "mic")
        .expect("mic 路应在快照里");
    assert!(mic.error.is_none(), "mic 不该被 loopback 的失败牵连");

    // mic 真的在采：等它出现并收满 2 段。
    assert!(
        sidecar.wait_for_path("mic", Duration::from_secs(10)).await,
        "mic 路应在 10s 内报出 path"
    );
    let conn = sidecar.conn_for("mic").expect("mic 连接");
    assert!(
        conn.wait_for_segments(2, Duration::from_secs(15)).await,
        "mic 路应在 15s 内收满 2 段"
    );

    svc.stop().await;
    assert!(!svc.snapshot().running);
}

/// DoD(item 3)：**停止 = 对未闭合段补发 `segment_end` → 优雅关 WS（flush 不是可选项）**。
///
/// 素材刻意**没有尾部静音**：段在素材播完后仍然是开着的，只有 `stop()` 才会封口。
/// 这一条同时钉住两件事：
/// - worker 的收尾路径（`ep.finish()` → `send_end`）真的会补发；
/// - 那条 `segment_end` 真的**上得了线** —— 它排在出站队列里，关闭帧在它之后，
///   所以关停必须"先 flush 再关"。若 `close()` 走了会吞掉 `Close` 标记的老路径，
///   对端看到的就是异常断开（1006）而不是干净关闭（1000）。
#[tokio::test]
async fn stop_flushes_the_open_segment_before_closing_the_socket() {
    let sidecar = FakeSidecar::start(2).await;
    let factory = factory_for(
        |path: PathLabel| {
            let mut s = Script::new();
            // 8 帧静音 + 40 帧语音，**不补尾部静音** → 段停在"开着"的状态。
            s.silent(8).voiced(40, path as u64 + 0xF00D);
            s.samples
        },
        16_000,
        REAL_FRAME_PACE,
    );
    let svc = CaptureService::with_source_factory(factory);
    let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::default());
    svc.start(sidecar.port, "t", sink).await.expect("启动");

    assert!(
        sidecar
            .wait_for_path("loopback", Duration::from_secs(10))
            .await,
        "loopback 应在 10s 内报出 path"
    );
    let conn = sidecar.conn_for("loopback").expect("loopback 连接");
    assert!(
        conn.wait_for(20, Duration::from_secs(15)).await,
        "段内音频帧应在 15s 内到齐（实际 {} 条）",
        conn.len()
    );
    assert_eq!(
        conn.count_event("segment_end"),
        0,
        "素材没有尾部静音，段此刻必须仍是开着的（否则这条测试没测到收尾路径）"
    );

    svc.stop().await;

    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while conn.count_event("segment_end") < 1 && std::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    let records = conn.snapshot();
    assert_eq!(
        conn.count_event("segment_end"),
        1,
        "停止时必须对未闭合段补发 segment_end；线上只有 {} 条记录",
        records.len()
    );
    // 顺序：补发的 end 必须在最后一条音频帧之后（不能先关后补）。
    let last_audio = records
        .iter()
        .rposition(|r| r.is_audio())
        .expect("应有音频帧");
    let end_pos = records
        .iter()
        .position(|r| r.event_name() == Some("segment_end"))
        .expect("应有 segment_end");
    assert!(
        last_audio < end_pos,
        "segment_end 必须排在最后一条音频帧之后（实际 audio@{last_audio} / end@{end_pos}）"
    );
    assert!(
        conn.closed.load(Ordering::SeqCst),
        "补发之后应真的优雅关掉连接（对端应看到关闭帧，而不是超时断开）"
    );
}

// ------------------------------------------------------------------ 断言辅助
/// 每条 `segment_start`..`segment_end` 之间的音频帧数。
fn segment_spans(records: &[support::fake_sidecar::Recorded]) -> Vec<usize> {
    let mut spans = Vec::new();
    let mut cur: Option<usize> = None;
    for r in records {
        match r.event_name() {
            Some("segment_start") => cur = Some(0),
            Some("segment_end") => {
                if let Some(n) = cur.take() {
                    spans.push(n);
                }
            }
            _ => {
                if r.is_audio() {
                    if let Some(n) = cur.as_mut() {
                        *n += 1;
                    }
                }
            }
        }
    }
    spans
}

/// 第一条音频帧的前 8 个样本（用于证明两路内容不同）。
fn first_audio_head(records: &[support::fake_sidecar::Recorded]) -> Vec<u32> {
    records
        .iter()
        .find_map(|r| match r {
            support::fake_sidecar::Recorded::Audio { head, .. } => {
                Some(head.iter().map(|f| f.to_bits()).collect())
            }
            _ => None,
        })
        .unwrap_or_default()
}

/// `ServiceSnapshot` 在测试里会被打印，确保它不泄露 token（R8）。
#[test]
fn service_snapshot_debug_does_not_leak_a_token() {
    let snap = ServiceSnapshot {
        running: false,
        last_error: None,
        paths: Vec::new(),
    };
    let s = format!("{snap:?}");
    assert!(!s.contains("Bearer"));
    assert!(!s.contains("token"));
}
