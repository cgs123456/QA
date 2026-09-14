//! Cross-language e2e: the real Rust uplink against a real uvicorn sidecar.
//!
//! The Rust unit tests in `audio::uplink` use a fake WS server, and the Python
//! suite uses FastAPI's in-process `TestClient`. Neither proves the two halves
//! agree on the wire. This test does: it spawns the actual ASGI app under
//! uvicorn (`scripts/serve_audio_e2e.py`) and drives it with [`AudioUplink`].
//!
//! It also pins two facts that the in-process tests cannot see:
//!
//! - **`/audio/stream` needs a WebSocket implementation.** uvicorn's `ws="auto"`
//!   does not degrade gracefully when neither `websockets` nor `wsproto` is
//!   installed — it answers **404** to the upgrade request, so the whole audio
//!   path is dead. (Found here; the dependency is now declared and collected.)
//! - **Auth rejection surfaces as HTTP 403, not a WS close 1008.** Starlette's
//!   `close()` before `accept()` becomes a plain HTTP response once a real ASGI
//!   server is involved. `TestClient` short-circuits that and reports 1008, which
//!   is why the Python suite's `code == 1008` assertion passes while a real
//!   client sees 403. `AudioUplink` treats both as `Unauthorized`.
//!
//! Set `INTERVIEWCOPILOT_SKIP_E2E=1` to skip (needs a Python interpreter with the
//! sidecar's dependencies on PATH, or `INTERVIEWCOPILOT_PYTHON`).

use std::io::{BufRead, BufReader};
use std::net::TcpListener;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use interview_copilot_lib::audio::frame::{Frame16k, FRAME_SAMPLES};
use interview_copilot_lib::audio::uplink::{
    AudioUplink, EventSink, PathLabel, UplinkConfig, UplinkError,
};
use serde_json::{json, Value};

const TOKEN: &str = "e2e-token-abcdefghijklmnopqrstuvwxyz012345";
const NONCE: &str = "audio-e2e";
const START_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Default)]
struct RecordingSink {
    events: Mutex<Vec<(String, Value)>>,
}

impl RecordingSink {
    fn snapshot(&self) -> Vec<(String, Value)> {
        self.events.lock().unwrap().clone()
    }

    fn names(&self) -> Vec<String> {
        self.snapshot().into_iter().map(|(n, _)| n).collect()
    }

    async fn wait_for(&self, name: &str, timeout: Duration) -> Option<Value> {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if let Some((_, payload)) = self.snapshot().into_iter().rev().find(|(n, _)| n == name) {
                return Some(payload);
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        None
    }
}

impl EventSink for RecordingSink {
    fn emit(&self, event: &str, payload: Value) {
        self.events
            .lock()
            .unwrap()
            .push((event.to_string(), payload));
    }
}

/// Kills the sidecar even if the test panics.
struct Sidecar {
    child: Child,
    port: u16,
}

impl Drop for Sidecar {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn repo_root() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src-tauri has a parent")
        .to_path_buf()
}

fn python_exe() -> String {
    std::env::var("INTERVIEWCOPILOT_PYTHON").unwrap_or_else(|_| "python".to_string())
}

fn free_port() -> u16 {
    let l = TcpListener::bind("127.0.0.1:0").expect("bind ephemeral");
    let port = l.local_addr().unwrap().port();
    drop(l);
    port
}

/// Spawn uvicorn and wait until `/health` echoes our nonce (TOCTOU-safe, same
/// discipline as the shell's supervisor).
async fn start_sidecar(extra_args: &[&str]) -> Option<Sidecar> {
    let root = repo_root();
    let script = root.join("scripts").join("serve_audio_e2e.py");
    if !script.is_file() {
        eprintln!("SKIP: {} not found", script.display());
        return None;
    }
    let port = free_port();
    let mut cmd = Command::new(python_exe());
    cmd.arg(&script)
        .arg("--port")
        .arg(port.to_string())
        .arg("--token")
        .arg(TOKEN)
        .arg("--nonce")
        .arg(NONCE);
    for a in extra_args {
        cmd.arg(a);
    }
    let mut child = match cmd
        .current_dir(&root)
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(c) => c,
        Err(e) => {
            eprintln!("SKIP: cannot spawn python ({}): {e}", python_exe());
            return None;
        }
    };

    // Surface the server's stderr if it dies during startup.
    let stderr = child.stderr.take().map(BufReader::new);

    let url = format!("http://127.0.0.1:{port}/health");
    let client = reqwest::Client::new();
    let deadline = Instant::now() + START_TIMEOUT;
    while Instant::now() < deadline {
        if let Ok(resp) = client.get(&url).send().await {
            if let Ok(body) = resp.json::<Value>().await {
                if body.get("status").and_then(Value::as_str) == Some("ok")
                    && body.get("nonce").and_then(Value::as_str) == Some(NONCE)
                {
                    return Some(Sidecar { child, port });
                }
            }
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }

    let mut detail = String::new();
    if let Some(mut r) = stderr {
        let mut line = String::new();
        while r.read_line(&mut line).unwrap_or(0) > 0 {
            detail.push_str(&line);
            line.clear();
        }
    }
    let _ = child.kill();
    let _ = child.wait();
    panic!("sidecar never became healthy on port {port}; stderr:\n{detail}");
}

fn skipped() -> bool {
    std::env::var("INTERVIEWCOPILOT_SKIP_E2E").is_ok()
}

#[tokio::test]
async fn full_chain_rust_client_against_real_sidecar() {
    if skipped() {
        eprintln!("SKIP: INTERVIEWCOPILOT_SKIP_E2E set");
        return;
    }
    let Some(sidecar) = start_sidecar(&[]).await else {
        return;
    };

    let recorder = Arc::new(RecordingSink::default());
    let sink: Arc<dyn EventSink> = recorder.clone();
    let up = AudioUplink::connect(
        UplinkConfig::new(sidecar.port, TOKEN, PathLabel::Loopback),
        sink,
    )
    .await
    .expect("authenticated connect must succeed");

    // segment_start, then 100 frames (exactly 3.0 s), then segment_end.
    up.push_event(&json!({"event": "segment_start", "path": "loopback", "ts_ms": 0}))
        .unwrap();
    for i in 0..100u64 {
        up.push_frame(&Frame16k::from_f32(0, i * 30, [0.25; FRAME_SAMPLES]))
            .unwrap();
    }
    up.push_event(&json!({"event": "segment_end", "path": "loopback", "ts_ms": 3000}))
        .unwrap();

    let start = recorder
        .wait_for("asr://start", Duration::from_secs(10))
        .await
        .expect("asr_start must arrive as a Tauri event");
    assert_eq!(start["segment_id"], json!("seg_1"));
    assert_eq!(start["path"], json!("loopback"));

    let fin = recorder
        .wait_for("asr://final", Duration::from_secs(15))
        .await
        .expect("asr_final must arrive as a Tauri event");
    // DoD: the buffered segment is 100 frames = 3000 ms, ±1 frame.
    assert_eq!(fin["duration_ms"], json!(3000));
    assert_eq!(fin["path"], json!("loopback"));
    assert_eq!(fin["text"], json!("e2e-transcript"));

    assert_eq!(recorder.names(), vec!["asr://start", "asr://final"]);

    let snap = up.stats();
    assert_eq!(snap.frames_sent, 100);
    assert_eq!(snap.events_sent, 2);
    assert_eq!(snap.dropped, 0, "no drops expected at 100 frames");
    assert_eq!(snap.downlink_messages, 2);
    assert_eq!(snap.close_code, None);

    up.shutdown().await;
}

#[tokio::test]
async fn real_sidecar_rejects_missing_and_wrong_tokens() {
    if skipped() {
        eprintln!("SKIP: INTERVIEWCOPILOT_SKIP_E2E set");
        return;
    }
    let Some(sidecar) = start_sidecar(&[]).await else {
        return;
    };

    for token in ["", "definitely-wrong"] {
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::default());
        let err = AudioUplink::connect(
            UplinkConfig::new(sidecar.port, token, PathLabel::Loopback),
            sink,
        )
        .await
        .expect_err("an unauthenticated connect must not succeed");
        // Observed against real uvicorn: Starlette closes before `accept()`, so
        // the client sees an HTTP 403 rather than a WS close frame with 1008.
        assert_eq!(
            err,
            UplinkError::Unauthorized { status: 403 },
            "token {token:?} must be rejected during the handshake"
        );
    }
}

#[tokio::test]
async fn real_sidecar_drops_oldest_when_a_segment_overruns() {
    if skipped() {
        eprintln!("SKIP: INTERVIEWCOPILOT_SKIP_E2E set");
        return;
    }
    // The production cap is 2000 frames (60 s), which this test cannot reach:
    // the uplink's own R13 outbound queue is 128 frames, so a burst larger than
    // that drops *before* the socket. Lower the sidecar's cap instead, which
    // tests the same policy without fighting the transport bound.
    let cap = 10usize;
    let Some(sidecar) = start_sidecar(&["--max-segment-frames", "10"]).await else {
        return;
    };

    let recorder = Arc::new(RecordingSink::default());
    let sink: Arc<dyn EventSink> = recorder.clone();
    let up = AudioUplink::connect(UplinkConfig::new(sidecar.port, TOKEN, PathLabel::Mic), sink)
        .await
        .unwrap();

    // cap + 3 frames with no segment_end: the sidecar must keep the newest `cap`
    // frames and warn exactly once.
    up.push_event(&json!({"event": "segment_start", "path": "mic", "ts_ms": 0}))
        .unwrap();
    let total = (cap + 3) as u64;
    for i in 0..total {
        up.push_frame(&Frame16k::from_f32(0, i * 30, [0.1; FRAME_SAMPLES]))
            .unwrap();
    }
    up.push_event(&json!({"event": "segment_end", "path": "mic", "ts_ms": total * 30}))
        .unwrap();

    let warn = recorder
        .wait_for("asr://error", Duration::from_secs(15))
        .await
        .expect("truncation must be reported to the client");
    assert_eq!(warn["error"], json!("segment_truncated"));
    assert_eq!(warn["path"], json!("mic"));

    let fin = recorder
        .wait_for("asr://final", Duration::from_secs(20))
        .await
        .expect("the segment must still be finalised");
    assert_eq!(fin["path"], json!("mic"));

    up.shutdown().await;
}
