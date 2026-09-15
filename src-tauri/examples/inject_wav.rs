//! 注入人工走查工具：把一段 WAV 当成一路采集源，走**完整生产链路**送到 sidecar。
//!
//! ```text
//! cargo run --features audio-testharness --example inject_wav -- \
//!     --wav testdata/synth_utterance_zh.wav --path loopback [--realtime]
//!     [--port 8123] [--token <token>] [--max-ms 0]
//! ```
//!
//! # 它为什么存在
//!
//! 真机验收要"对着麦克风说话、系统里放一段音频"，不可复现也不可自动化。
//! 这个工具把同一件事变成一条命令：素材是文件，链路是生产的
//! （`CapturePipeline` → `FrameQueue` → worker 真 VAD → 真端点 → 真 uplink → 真 sidecar）。
//!
//! # `--realtime` 与默认节拍的区别（重要）
//!
//! - **不加 `--realtime`（`Pace::Fast`）**：能推多快推多快。R13 的有界队列会在
//!   几十帧内溢出并**真的丢帧** —— 这是正确行为，但会让"段边界"失去意义。
//!   适合快速验证"传输通了、序号连续、sidecar 收到段"。
//! - **加 `--realtime`**：按真实 30ms/帧推（墙钟自校正）。想看/听真实节奏、
//!   想让段边界可复现，就用它。
//!
//! 工具会把观察到的段边界、帧数、丢帧数、seq 连续性打成一张报告，
//! 并按 R14 只打**计数与边界**，不打音频、不打转写。
//!
//! # 前置
//!
//! 需要一个能收音频的 sidecar。测试用对端：
//! `python scripts/serve_audio_e2e.py --port 8123 --token <token> --nonce n`
//! 然后 `--port 8123 --token <token>`。没有对端时会在启动阶段就报错退出
//! （不会静默空转）。

use std::process::ExitCode;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use interview_copilot_lib::audio::synthetic::{Pace, SyntheticSource};
use interview_copilot_lib::audio::{
    platform_source_factory, CaptureService, EventSink, PathLabel, ServiceSnapshot, SourceFactory,
};
use serde_json::Value;

/// 只计数、不落盘：下行可能含转写文本（R14）。
#[derive(Default)]
struct CountingSink {
    events: Mutex<Vec<(String, Value)>>,
    total: AtomicUsize,
}

impl CountingSink {
    fn count(&self, name: &str) -> usize {
        self.events
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .filter(|(n, _)| n == name)
            .count()
    }
}

impl EventSink for CountingSink {
    fn emit(&self, event: &str, payload: Value) {
        self.total.fetch_add(1, Ordering::Relaxed);
        self.events
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push((event.to_string(), payload));
    }
}

struct Args {
    wav: String,
    path: PathLabel,
    realtime: bool,
    port: u16,
    token: String,
    repeat: bool,
}

fn usage() -> &'static str {
    "usage: inject_wav --wav <file.wav> [--path loopback|mic] [--realtime] \
     [--port N] [--token T] [--repeat]"
}

fn parse_args() -> Result<Args, String> {
    let mut wav = None;
    let mut path = PathLabel::Loopback;
    let mut realtime = false;
    let mut port = 8123u16;
    let mut token = "e2e-token".to_string();
    let mut repeat = false;

    let argv: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < argv.len() {
        let need = |i: usize| -> Result<&String, String> {
            argv.get(i + 1)
                .ok_or_else(|| format!("{} 缺少取值\n{}", argv[i], usage()))
        };
        match argv[i].as_str() {
            "--wav" => {
                wav = Some(need(i)?.clone());
                i += 2;
            }
            "--path" => {
                path = match need(i)?.as_str() {
                    "loopback" => PathLabel::Loopback,
                    "mic" => PathLabel::Mic,
                    other => return Err(format!("未知 path: {other}")),
                };
                i += 2;
            }
            "--port" => {
                port = need(i)?
                    .parse()
                    .map_err(|_| "--port 必须是端口号".to_string())?;
                i += 2;
            }
            "--token" => {
                token = need(i)?.clone();
                i += 2;
            }
            "--realtime" => {
                realtime = true;
                i += 1;
            }
            "--repeat" => {
                repeat = true;
                i += 1;
            }
            "--help" | "-h" => return Err(usage().to_string()),
            other => return Err(format!("未知参数 {other}\n{}", usage())),
        }
    }

    Ok(Args {
        wav: wav.ok_or_else(|| format!("--wav 是必填\n{}", usage()))?,
        path,
        realtime,
        port,
        token,
        repeat,
    })
}

#[tokio::main]
async fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("{e}");
            return ExitCode::from(2);
        }
    };
    match run(args).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("[FAIL] {e}");
            ExitCode::FAILURE
        }
    }
}

async fn run(args: Args) -> Result<(), String> {
    let wav_path = std::path::PathBuf::from(&args.wav);
    if !wav_path.is_file() {
        return Err(format!("找不到 WAV：{}", wav_path.display()));
    }
    let material =
        interview_copilot_lib::audio::synthetic::read_wav(&wav_path).map_err(|e| e.to_string())?;

    println!("=== inject_wav ===");
    println!("wav           : {}", wav_path.display());
    println!(
        "material      : {}Hz / {}ch / {} 单声道样本 / {}ms",
        material.input_rate,
        material.channels,
        material.samples.len(),
        material.duration_ms()
    );
    println!("path          : {}", args.path.as_str());
    println!(
        "pace          : {}",
        if args.realtime {
            "realtime (30ms/帧)"
        } else {
            "fast（会触发 R13 丢帧，段边界不可复现）"
        }
    );

    // 两路都要能构造：生产工厂给出真实的另一路（麦克风），注入源顶替指定那一路。
    // 这样"注入一路 + 真实一路"也是可跑的，不是把服务改成单路。
    let target = args.path;
    let wav_for_factory = wav_path.clone();
    let repeat = args.repeat;
    let realtime = args.realtime;
    let base = platform_source_factory();
    let factory: Arc<SourceFactory> = Arc::new(move |path: PathLabel| {
        if path == target {
            let mut src = SyntheticSource::from_wav(&wav_for_factory)?;
            src = src
                .with_pace(if realtime { Pace::Realtime } else { Pace::Fast })
                .with_repeat(repeat);
            Ok(Box::new(src))
        } else {
            base(path)
        }
    });

    let svc = CaptureService::with_source_factory(factory);
    let sink = Arc::new(CountingSink::default());
    let sink_dyn: Arc<dyn EventSink> = sink.clone();

    let started = Instant::now();
    let snap = svc
        .start(args.port, &args.token, sink_dyn)
        .await
        .map_err(|e| format!("采集服务启动失败（sidecar 未就绪或 token 不对？）：{e}"))?;
    println!("start         : {:?}", started.elapsed());
    print_snapshot(&snap);

    // 素材时长 + 余量（真实节拍下要等它推完；快节拍下只等传输收尾）。
    let wait_ms = if realtime {
        material.duration_ms() + 3_000
    } else {
        3_000
    };
    println!("waiting       : {wait_ms}ms（等段收完）");
    tokio::time::sleep(Duration::from_millis(wait_ms)).await;

    let stopped = svc.stop().await;
    print_snapshot(&stopped);

    println!("\n=== downlink（只报计数，R14）===");
    println!("asr://start   : {}", sink.count("asr://start"));
    println!("asr://partial : {}", sink.count("asr://partial"));
    println!("asr://final   : {}", sink.count("asr://final"));
    println!("asr://error   : {}", sink.count("asr://error"));

    // --- 判定 -------------------------------------------------------------
    let mut failed = Vec::new();
    let path_snap = stopped
        .paths
        .iter()
        .find(|p| p.path == args.path.as_str())
        .ok_or_else(|| "快照里没有目标 path".to_string())?;

    println!("\n=== 目标路报告（{}）===", path_snap.path);
    println!("device        : {}", path_snap.source_device);
    println!("vad           : {}", path_snap.vad_provider);
    println!("self_check    : {:?}", path_snap.self_check.status);
    println!("frames_emitted: {}", path_snap.source_stats.frames_emitted);
    println!("frames_dropped: {}", path_snap.source_stats.frames_dropped);
    println!("source_errors : {}", path_snap.source_stats.errors);
    println!("vad_errors    : {}", path_snap.vad_errors);
    println!("segments_sent : {}", path_snap.segments_sent);
    println!("holes         : {}", path_snap.holes);
    println!("uplink frames : {}", path_snap.uplink.frames_sent);
    println!("uplink events : {}", path_snap.uplink.events_sent);
    println!("uplink dropped: {}", path_snap.uplink.dropped);
    println!("uplink reconn : {}", path_snap.uplink.reconnects);
    println!("close_code    : {:?}", path_snap.uplink.close_code);
    println!("push_errors   : {}", path_snap.push_errors);
    println!("error         : {:?}", path_snap.error);

    if path_snap.vad_errors > 0 {
        failed.push(format!("{} 帧 VAD 分类失败", path_snap.vad_errors));
    }
    if path_snap.push_errors > 0 {
        failed.push(format!("{} 次上行入队失败", path_snap.push_errors));
    }
    if path_snap.error.is_some() {
        failed.push(format!("该路有错误：{:?}", path_snap.error));
    }
    if path_snap.segments_sent == 0 {
        failed.push("一段都没送出去（VAD 没检出语音？看 self_check）".to_string());
    }
    if !args.realtime && path_snap.source_stats.frames_dropped > 0 {
        println!(
            "note          : fast 节拍丢了 {} 帧（R13 正常工作）；段边界不可复现，\
             要看边界请加 --realtime",
            path_snap.source_stats.frames_dropped
        );
    }
    if args.realtime && path_snap.source_stats.frames_dropped > 0 {
        failed.push(format!(
            "realtime 节拍仍丢了 {} 帧：worker 跟不上，这是真问题",
            path_snap.source_stats.frames_dropped
        ));
    }
    if sink.count("asr://final") == 0 {
        failed.push("sidecar 没回任何 asr_final（对端是 serve_audio_e2e.py 吗？）".to_string());
    }

    if failed.is_empty() {
        println!("\nverdict       : PASS");
        Ok(())
    } else {
        for f in &failed {
            eprintln!("[FAIL] {f}");
        }
        Err(format!("{} 项不通过", failed.len()))
    }
}

fn print_snapshot(snap: &ServiceSnapshot) {
    println!(
        "service       : running={} last_error={:?} paths={}",
        snap.running,
        snap.last_error,
        snap.paths.len()
    );
    for p in &snap.paths {
        println!(
            "  [{}] device={:?} segs={} frames={} dropped={} error={:?}",
            p.path,
            p.source_device,
            p.segments_sent,
            p.source_stats.frames_emitted,
            p.source_stats.frames_dropped,
            p.error
        );
    }
}
