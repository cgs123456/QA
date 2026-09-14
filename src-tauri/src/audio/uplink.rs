//! WS uplink: Rust → sidecar `/audio/stream` (R9–R14).
//!
//! One connection carries **one audio path** (the sidecar enforces this: the
//! first `segment_start` fixes the path and later conflicts are ignored), so
//! one [`AudioUplink`] exists per path. It owns three things the rest of the
//! audio chain must not duplicate:
//!
//! 1. **The wire sequence number.** R10 gives audio frames and JSON event
//!    frames a *shared* seq space, and the sidecar checks continuity across
//!    both (`seq_gap`). Two independent counters would collide the moment an
//!    event is interleaved between two audio frames, so a single counter is
//!    stamped at **enqueue** time. A frame evicted by the bounded queue
//!    therefore leaves a visible gap — which is the point: the peer can see
//!    that we dropped instead of silently receiving a hole.
//! 2. **The bounded outbound queue** (R13). [`OUT_QUEUE_CAPACITY`] frames,
//!    drop-oldest + counted. The capture thread can never be parked by a slow
//!    or wedged socket.
//! 3. **The downlink bridge.** Every `asr_*` message becomes a Tauri event so
//!    the UI can subscribe. Emission goes through [`EventSink`], so the mapping
//!    is testable without a Tauri runtime.
//!
//! R14: transcript text lives in the downlink payload and is handed to the sink
//! verbatim. Nothing in this module logs a payload, and the config's `Debug`
//! redacts the token.
//!
//! # Runtime
//!
//! [`AudioUplink::connect`] is async and spawns its reader/writer tasks, so it
//! must be called from inside a Tokio runtime — Tauri's async runtime is Tokio,
//! so `tauri::async_runtime::spawn` is a valid caller.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use futures_util::stream::{SplitSink, SplitStream};
use futures_util::{SinkExt, StreamExt};
use serde::Serialize;
use serde_json::Value;
use tokio::net::TcpStream;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::header::AUTHORIZATION;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::{Error as WsError, Message};
use tokio_tungstenite::{connect_async, MaybeTlsStream, WebSocketStream};

use super::frame::{encode_ws_event, encode_ws_frame, Frame16k};

/// R13 bound on frames waiting for the socket. ≈3.8 s of 30 ms frames — deep
/// enough to ride out a scheduler or GC hiccup, shallow enough that a stalled
/// connection cannot buffer unbounded audio in memory.
pub const OUT_QUEUE_CAPACITY: usize = 128;
/// How long [`AudioUplink::shutdown`] waits for the writer before aborting it.
pub const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(2);

/// Tauri event names the frontend subscribes to (R12 downlink kinds).
pub const EVT_ASR_START: &str = "asr://start";
pub const EVT_ASR_PARTIAL: &str = "asr://partial";
pub const EVT_ASR_FINAL: &str = "asr://final";
pub const EVT_ASR_ERROR: &str = "asr://error";

type Ws = WebSocketStream<MaybeTlsStream<TcpStream>>;
type WsSink = SplitSink<Ws, Message>;
type WsStream = SplitStream<Ws>;

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// Which capture path a connection carries. Serializes to exactly the strings
/// the sidecar accepts (`VALID_PATHS`), so a mismatch is a compile-time-ish
/// concern rather than a silent `violations` increment on the far side.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
pub enum PathLabel {
    #[serde(rename = "loopback")]
    Loopback,
    #[serde(rename = "mic")]
    Mic,
}

impl PathLabel {
    pub const ALL: [PathLabel; 2] = [PathLabel::Loopback, PathLabel::Mic];

    pub const fn as_str(self) -> &'static str {
        match self {
            PathLabel::Loopback => "loopback",
            PathLabel::Mic => "mic",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UplinkError {
    /// Token could not be expressed as a header value (control characters).
    BadToken,
    /// The handshake was refused with 401/403 — the sidecar rejected our token.
    Unauthorized {
        status: u16,
    },
    /// Handshake failed for a non-auth reason (unexpected HTTP status).
    Handshake(String),
    /// Could not reach the sidecar at all.
    Connect(String),
    /// The uplink has been shut down; nothing more can be enqueued.
    Closed,
    /// Event body failed local validation (would have tripped the sidecar's
    /// `violations` counter instead).
    BadEvent(&'static str),
    /// Event carried a different path than this connection owns.
    PathMismatch {
        expected: &'static str,
        got: String,
    },
    Serialize(String),
}

impl std::fmt::Display for UplinkError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            UplinkError::BadToken => write!(f, "token is not a valid header value"),
            UplinkError::Unauthorized { status } => {
                write!(f, "sidecar rejected the token (HTTP {status})")
            }
            UplinkError::Handshake(s) => write!(f, "websocket handshake failed: {s}"),
            UplinkError::Connect(s) => write!(f, "cannot reach sidecar: {s}"),
            UplinkError::Closed => write!(f, "uplink is closed"),
            UplinkError::BadEvent(field) => write!(f, "malformed vad event: {field}"),
            UplinkError::PathMismatch { expected, got } => {
                write!(
                    f,
                    "event path {got} does not match connection path {expected}"
                )
            }
            UplinkError::Serialize(s) => write!(f, "event serialization failed: {s}"),
        }
    }
}

impl std::error::Error for UplinkError {}

/// Where a connection should point. `Debug` redacts the token (R8/R14).
#[derive(Clone, PartialEq, Eq)]
pub struct UplinkConfig {
    pub port: u16,
    pub token: String,
    pub path: PathLabel,
}

impl UplinkConfig {
    pub fn new(port: u16, token: impl Into<String>, path: PathLabel) -> Self {
        Self {
            port,
            token: token.into(),
            path,
        }
    }

    pub fn url(&self) -> String {
        format!("ws://127.0.0.1:{}/audio/stream", self.port)
    }
}

impl std::fmt::Debug for UplinkConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("UplinkConfig")
            .field("port", &self.port)
            .field("path", &self.path)
            .field("token", &"<redacted>")
            .finish()
    }
}

/// Downlink delivery. The production impl is [`TauriSink`]; tests use a
/// recording double, so the `asr_*` → `asr://*` mapping needs no Tauri runtime.
pub trait EventSink: Send + Sync + 'static {
    /// `payload` is the downlink JSON body **verbatim** and may contain
    /// transcript text: it is for the UI only and must never be logged (R14).
    fn emit(&self, event: &str, payload: Value);
}

/// Bridges downlink messages onto the Tauri event bus.
pub struct TauriSink {
    app: tauri::AppHandle,
}

impl TauriSink {
    pub fn new(app: tauri::AppHandle) -> Self {
        Self { app }
    }
}

impl EventSink for TauriSink {
    fn emit(&self, event: &str, payload: Value) {
        use tauri::Emitter;
        // A missing webview / closed window is not an audio-pipeline failure.
        let _ = self.app.emit(event, payload);
    }
}

/// R12 downlink kind → Tauri event name.
pub fn tauri_event_name(kind: &str) -> Option<&'static str> {
    match kind {
        "asr_start" => Some(EVT_ASR_START),
        "asr_partial" => Some(EVT_ASR_PARTIAL),
        "asr_final" => Some(EVT_ASR_FINAL),
        "asr_error" => Some(EVT_ASR_ERROR),
        _ => None,
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct UplinkSnapshot {
    pub frames_sent: u64,
    pub events_sent: u64,
    /// Frames evicted by the R13 drop-oldest policy.
    pub dropped: u64,
    pub downlink_messages: u64,
    pub unknown_downlink: u64,
    pub malformed_downlink: u64,
    /// Close code received from the peer (1008 = auth/policy rejection).
    pub close_code: Option<u16>,
    pub closed: bool,
}

#[derive(Debug, Default)]
pub struct UplinkStats {
    frames_sent: AtomicU64,
    events_sent: AtomicU64,
    dropped: AtomicU64,
    downlink: AtomicU64,
    unknown_downlink: AtomicU64,
    malformed_downlink: AtomicU64,
    close_code: AtomicU64,
    closed: AtomicBool,
}

impl UplinkStats {
    fn snapshot(&self) -> UplinkSnapshot {
        let code = self.close_code.load(Ordering::Relaxed);
        UplinkSnapshot {
            frames_sent: self.frames_sent.load(Ordering::Relaxed),
            events_sent: self.events_sent.load(Ordering::Relaxed),
            dropped: self.dropped.load(Ordering::Relaxed),
            downlink_messages: self.downlink.load(Ordering::Relaxed),
            unknown_downlink: self.unknown_downlink.load(Ordering::Relaxed),
            malformed_downlink: self.malformed_downlink.load(Ordering::Relaxed),
            close_code: if code == 0 { None } else { Some(code as u16) },
            closed: self.closed.load(Ordering::Relaxed),
        }
    }
}

enum Outbound {
    Frame(Vec<u8>),
    Close,
}

/// Bounded, drop-oldest, async-consumer queue (R13).
///
/// `std::sync::mpsc` cannot express this: a `SyncSender` can only reject the
/// *newest* item and cannot evict the head. `tokio::sync::mpsc` has the same
/// shape. The producer here is a capture thread (sync), the consumer is a
/// Tokio task, so the queue is a mutex-protected deque plus a `Notify` — a
/// stored permit means a push between the failed pop and the await still wakes
/// the consumer, so there is no lost-wakeup race.
struct OutQueue {
    inner: Mutex<VecDeque<Outbound>>,
    cap: usize,
    dropped: AtomicU64,
    closed: AtomicBool,
    notify: tokio::sync::Notify,
}

impl OutQueue {
    fn new(cap: usize) -> Arc<Self> {
        Arc::new(Self {
            inner: Mutex::new(VecDeque::with_capacity(cap)),
            cap,
            dropped: AtomicU64::new(0),
            closed: AtomicBool::new(false),
            notify: tokio::sync::Notify::new(),
        })
    }

    /// Never blocks and never grows past `cap`: when full the OLDEST item is
    /// evicted and counted. A `Close` marker is the newest item, so it is never
    /// the one evicted.
    fn push(&self, item: Outbound) {
        if self.closed.load(Ordering::Relaxed) {
            return;
        }
        {
            let mut q = lock(&self.inner);
            if q.len() >= self.cap {
                q.pop_front();
                self.dropped.fetch_add(1, Ordering::Relaxed);
            }
            q.push_back(item);
        }
        self.notify.notify_one();
    }

    /// Resolves to `None` once a `Close` marker has been consumed.
    async fn pop(&self) -> Option<Outbound> {
        loop {
            if let Some(item) = lock(&self.inner).pop_front() {
                return Some(item);
            }
            if self.closed.load(Ordering::Relaxed) {
                return None;
            }
            self.notify.notified().await;
        }
    }

    fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }
}

/// One WS connection to the sidecar for one audio path.
pub struct AudioUplink {
    path: PathLabel,
    queue: Arc<OutQueue>,
    stats: Arc<UplinkStats>,
    /// Single wire counter shared by audio and event frames (R10).
    next_seq: Mutex<u32>,
    tasks: Mutex<Vec<tokio::task::JoinHandle<()>>>,
}

impl std::fmt::Debug for AudioUplink {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AudioUplink")
            .field("path", &self.path)
            .field("stats", &self.stats())
            .finish_non_exhaustive()
    }
}

impl AudioUplink {
    /// Perform the handshake (so auth failures surface immediately) and spawn
    /// the reader/writer tasks.
    pub async fn connect(cfg: UplinkConfig, sink: Arc<dyn EventSink>) -> Result<Self, UplinkError> {
        let request = build_request(&cfg)?;
        let (ws, _resp) = connect_async(request)
            .await
            .map_err(|e| map_connect_error(&e))?;
        let (write, read) = ws.split();

        let queue = OutQueue::new(OUT_QUEUE_CAPACITY);
        let stats = Arc::new(UplinkStats::default());

        let writer = tokio::spawn(writer_loop(write, queue.clone(), stats.clone()));
        let reader = tokio::spawn(reader_loop(read, sink, stats.clone()));

        Ok(Self {
            path: cfg.path,
            queue,
            stats,
            next_seq: Mutex::new(0),
            tasks: Mutex::new(vec![writer, reader]),
        })
    }

    pub fn path(&self) -> PathLabel {
        self.path
    }

    pub fn stats(&self) -> UplinkSnapshot {
        let mut snap = self.stats.snapshot();
        // The queue is the authoritative drop counter.
        snap.dropped = self.queue.dropped();
        snap
    }

    /// Enqueue one 30 ms frame.
    ///
    /// The wire seq is stamped here, not taken from `frame.seq`: R10 shares one
    /// seq space with event frames, and `frame.seq` is the *capture-side*
    /// producer counter (used for local continuity bookkeeping). Mixing the two
    /// would collide as soon as an event lands between two audio frames.
    pub fn push_frame(&self, frame: &Frame16k) -> Result<(), UplinkError> {
        let seq = self.take_seq()?;
        let wire = encode_ws_frame(seq as u16, frame.ts_ms as u32, &frame.pcm_f32);
        self.queue.push(Outbound::Frame(wire.to_vec()));
        self.stats.frames_sent.fetch_add(1, Ordering::Relaxed);
        Ok(())
    }

    /// Enqueue a VAD event frame (`segment_start` / `segment_end` / `vad_state`).
    ///
    /// Validated locally against the same rules the sidecar applies, so a bad
    /// event is an immediate error here rather than a silent `violations`
    /// increment on the far side.
    pub fn push_event(&self, event: &Value) -> Result<(), UplinkError> {
        let ts_ms = validate_event(event, self.path)?;
        let body = serde_json::to_vec(event).map_err(|e| UplinkError::Serialize(e.to_string()))?;
        let seq = self.take_seq()?;
        self.queue
            .push(Outbound::Frame(encode_ws_event(seq as u16, ts_ms, &body)));
        self.stats.events_sent.fetch_add(1, Ordering::Relaxed);
        Ok(())
    }

    fn take_seq(&self) -> Result<u32, UplinkError> {
        let mut seq = lock(&self.next_seq);
        let out = *seq;
        *seq = seq.wrapping_add(1);
        Ok(out)
    }

    /// Stop accepting frames and close the socket politely.
    pub async fn shutdown(&self) {
        self.queue.closed.store(true, Ordering::Relaxed);
        self.queue.push(Outbound::Close);
        self.queue.notify.notify_one();

        let handles = std::mem::take(&mut *lock(&self.tasks));
        for mut h in handles {
            if tokio::time::timeout(SHUTDOWN_TIMEOUT, &mut h)
                .await
                .is_err()
            {
                h.abort();
            }
        }
        self.stats.closed.store(true, Ordering::Relaxed);
    }
}

/// `Authorization: Bearer <token>` — R9: header, never a URL query (a query
/// string would land in uvicorn's access log, contradicting R14).
fn build_request(cfg: &UplinkConfig) -> Result<impl IntoClientRequest, UplinkError> {
    let mut req = cfg
        .url()
        .into_client_request()
        .map_err(|e| UplinkError::Handshake(e.to_string()))?;
    let value = HeaderValue::from_str(&format!("Bearer {}", cfg.token))
        .map_err(|_| UplinkError::BadToken)?;
    req.headers_mut().insert(AUTHORIZATION, value);
    Ok(req)
}

fn map_connect_error(e: &WsError) -> UplinkError {
    match e {
        // Starlette's `websocket.close()` before `accept()` reaches a real ASGI
        // server as a plain HTTP response (uvicorn: 403), not as a WS close
        // frame. Accept either shape as "token rejected".
        WsError::Http(resp) => {
            let status = resp.status().as_u16();
            if status == 401 || status == 403 {
                UplinkError::Unauthorized { status }
            } else {
                UplinkError::Handshake(format!("unexpected HTTP {status}"))
            }
        }
        other => UplinkError::Connect(other.to_string()),
    }
}

/// Mirrors the sidecar's `_on_event` checks. Returns the event's `ts_ms` so the
/// frame header can carry it too.
fn validate_event(event: &Value, path: PathLabel) -> Result<u32, UplinkError> {
    let kind = event
        .get("event")
        .and_then(Value::as_str)
        .ok_or(UplinkError::BadEvent("event"))?;
    if !matches!(kind, "segment_start" | "segment_end" | "vad_state") {
        return Err(UplinkError::BadEvent("event"));
    }
    let got = event
        .get("path")
        .and_then(Value::as_str)
        .ok_or(UplinkError::BadEvent("path"))?;
    if got != path.as_str() {
        return Err(UplinkError::PathMismatch {
            expected: path.as_str(),
            got: got.to_string(),
        });
    }
    let ts = event
        .get("ts_ms")
        .and_then(Value::as_u64)
        .ok_or(UplinkError::BadEvent("ts_ms"))?;
    Ok(ts as u32)
}

async fn writer_loop(mut sink: WsSink, queue: Arc<OutQueue>, stats: Arc<UplinkStats>) {
    while let Some(item) = queue.pop().await {
        match item {
            Outbound::Frame(bytes) => {
                if sink.send(Message::binary(bytes)).await.is_err() {
                    break;
                }
            }
            Outbound::Close => {
                let _ = sink.close().await;
                break;
            }
        }
    }
    stats.closed.store(true, Ordering::Relaxed);
}

async fn reader_loop(mut stream: WsStream, sink: Arc<dyn EventSink>, stats: Arc<UplinkStats>) {
    while let Some(msg) = stream.next().await {
        match msg {
            Ok(Message::Text(text)) => dispatch_downlink(text.as_str(), sink.as_ref(), &stats),
            Ok(Message::Close(frame)) => {
                if let Some(f) = frame {
                    stats
                        .close_code
                        .store(close_code_u64(f.code), Ordering::Relaxed);
                }
                break;
            }
            // The sidecar only ever sends JSON text; binary/ping/pong are not
            // part of the contract and are ignored rather than treated as data.
            Ok(_) => {}
            Err(_) => break,
        }
    }
    stats.closed.store(true, Ordering::Relaxed);
}

fn close_code_u64(code: CloseCode) -> u64 {
    match code {
        CloseCode::Library(code) => code as u64,
        CloseCode::Reserved(code) => code as u64,
        CloseCode::Normal => 1000,
        CloseCode::Away => 1001,
        CloseCode::Protocol => 1002,
        CloseCode::Unsupported => 1003,
        CloseCode::Status => 1005,
        CloseCode::Abnormal => 1006,
        CloseCode::Invalid => 1007,
        CloseCode::Policy => 1008,
        CloseCode::Size => 1009,
        CloseCode::Extension => 1010,
        CloseCode::Error => 1011,
        CloseCode::Restart => 1012,
        CloseCode::Again => 1013,
        _ => 0,
    }
}

fn dispatch_downlink(text: &str, sink: &dyn EventSink, stats: &UplinkStats) {
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        stats.malformed_downlink.fetch_add(1, Ordering::Relaxed);
        return;
    };
    let kind = value.get("type").and_then(Value::as_str).unwrap_or("");
    match tauri_event_name(kind) {
        Some(name) => {
            stats.downlink.fetch_add(1, Ordering::Relaxed);
            sink.emit(name, value);
        }
        None => {
            stats.unknown_downlink.fetch_add(1, Ordering::Relaxed);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::audio::frame::FRAME_SAMPLES;
    use serde_json::json;
    use std::net::SocketAddr;
    use std::sync::Mutex as StdMutex;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;
    use tokio_tungstenite::accept_async;

    /// Records what the UI would have received.
    #[derive(Default)]
    struct RecordingSink {
        events: StdMutex<Vec<(String, Value)>>,
    }

    impl RecordingSink {
        fn names(&self) -> Vec<String> {
            lock(&self.events).iter().map(|(n, _)| n.clone()).collect()
        }
        fn payloads(&self) -> Vec<Value> {
            lock(&self.events).iter().map(|(_, v)| v.clone()).collect()
        }
    }

    impl EventSink for RecordingSink {
        fn emit(&self, event: &str, payload: Value) {
            lock(&self.events).push((event.to_string(), payload));
        }
    }

    /// A fake sidecar that accepts the handshake and hands the socket to `f`.
    async fn spawn_server<F, Fut>(f: F) -> u16
    where
        F: FnOnce(WebSocketStream<TcpStream>) -> Fut + Send + 'static,
        Fut: std::future::Future<Output = ()> + Send,
    {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let ws = accept_async(stream).await.unwrap();
            f(ws).await;
        });
        port
    }

    /// A fake server that answers the handshake with a non-101 status, which is
    /// how a real ASGI server reports "closed before accept".
    async fn spawn_http_reject(status: u16) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut buf = [0u8; 2048];
            let _ = stream.read(&mut buf).await;
            let body = b"denied";
            let resp = format!(
                "HTTP/1.1 {status} Forbidden\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
                body.len()
            );
            let _ = stream.write_all(resp.as_bytes()).await;
            let _ = stream.write_all(body).await;
            let _ = stream.flush().await;
        });
        port
    }

    async fn drain_frames(ws: &mut WebSocketStream<TcpStream>, n: usize) -> Vec<Vec<u8>> {
        let mut out = Vec::with_capacity(n);
        while out.len() < n {
            match ws.next().await {
                Some(Ok(Message::Binary(b))) => out.push(b.to_vec()),
                Some(Ok(_)) => {}
                Some(Err(_)) | None => break,
            }
        }
        out
    }

    fn frame_with(ts_ms: u64, fill: f32) -> Frame16k {
        Frame16k::from_f32(0, ts_ms, [fill; FRAME_SAMPLES])
    }

    #[test]
    fn downlink_kind_maps_to_exact_tauri_event_names() {
        assert_eq!(tauri_event_name("asr_start"), Some("asr://start"));
        assert_eq!(tauri_event_name("asr_partial"), Some("asr://partial"));
        assert_eq!(tauri_event_name("asr_final"), Some("asr://final"));
        assert_eq!(tauri_event_name("asr_error"), Some("asr://error"));
        assert_eq!(tauri_event_name("something_else"), None);
    }

    #[test]
    fn path_label_matches_the_wire_contract() {
        assert_eq!(PathLabel::Loopback.as_str(), "loopback");
        assert_eq!(PathLabel::Mic.as_str(), "mic");
        // Must agree with the capture-side labels (one path, one name).
        assert_eq!(PathLabel::Mic.as_str(), crate::audio::cpal_mic::PATH_LABEL);
        assert_eq!(
            serde_json::to_value(PathLabel::Loopback).unwrap(),
            json!("loopback")
        );
    }

    #[test]
    fn config_debug_never_leaks_the_token() {
        let cfg = UplinkConfig::new(8123, "super-secret-token", PathLabel::Loopback);
        let dbg = format!("{cfg:?}");
        assert!(!dbg.contains("super-secret-token"));
        assert!(dbg.contains("<redacted>"));
        assert_eq!(cfg.url(), "ws://127.0.0.1:8123/audio/stream");
    }

    #[test]
    fn validate_event_rejects_bad_shapes_locally() {
        let p = PathLabel::Loopback;
        assert_eq!(
            validate_event(&json!({"ts_ms": 1, "path": "loopback"}), p),
            Err(UplinkError::BadEvent("event"))
        );
        assert_eq!(
            validate_event(&json!({"event": "nope", "ts_ms": 1, "path": "loopback"}), p),
            Err(UplinkError::BadEvent("event"))
        );
        assert_eq!(
            validate_event(&json!({"event": "segment_start", "ts_ms": 1}), p),
            Err(UplinkError::BadEvent("path"))
        );
        assert_eq!(
            validate_event(&json!({"event": "segment_start", "path": "loopback"}), p),
            Err(UplinkError::BadEvent("ts_ms"))
        );
        // A bool is not an int (the sidecar makes the same distinction).
        assert_eq!(
            validate_event(
                &json!({"event": "vad_state", "path": "loopback", "ts_ms": true}),
                p
            ),
            Err(UplinkError::BadEvent("ts_ms"))
        );
    }

    #[test]
    fn validate_event_enforces_one_path_per_connection() {
        let p = PathLabel::Mic;
        assert_eq!(
            validate_event(
                &json!({"event": "segment_start", "path": "loopback", "ts_ms": 5}),
                p
            ),
            Err(UplinkError::PathMismatch {
                expected: "mic",
                got: "loopback".to_string()
            })
        );
        assert_eq!(
            validate_event(
                &json!({"event": "segment_start", "path": "mic", "ts_ms": 5}),
                p
            ),
            Ok(5)
        );
    }

    #[tokio::test]
    async fn out_queue_drops_oldest_and_counts() {
        let q = OutQueue::new(2);
        for i in 0..5u8 {
            q.push(Outbound::Frame(vec![i]));
        }
        assert_eq!(q.dropped(), 3);
        let mut got = Vec::new();
        while let Some(item) = lock(&q.inner).pop_front() {
            match item {
                Outbound::Frame(b) => got.push(b[0]),
                Outbound::Close => break,
            }
        }
        assert_eq!(
            got,
            vec![3, 4],
            "the newest survive, the oldest are evicted"
        );
    }

    #[tokio::test]
    async fn out_queue_push_never_blocks_when_full() {
        // R13: 100k pushes into a 2-slot queue must return immediately.
        let q = OutQueue::new(2);
        for i in 0..100_000u32 {
            q.push(Outbound::Frame(i.to_le_bytes().to_vec()));
        }
        assert_eq!(q.dropped(), 99_998);
    }

    #[tokio::test]
    async fn out_queue_pop_wakes_on_push() {
        let q = OutQueue::new(4);
        let producer = q.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(20)).await;
            producer.push(Outbound::Frame(vec![7]));
        });
        match q.pop().await {
            Some(Outbound::Frame(b)) => assert_eq!(b, vec![7]),
            other => panic!("expected frame, got {other:?}", other = other.is_none()),
        }
    }

    #[tokio::test]
    async fn missing_or_wrong_token_surfaces_as_unauthorized() {
        // Real uvicorn answers a pre-accept close with HTTP 403; the Rust client
        // must classify that as "token rejected", not as a generic error.
        for status in [401u16, 403] {
            let port = spawn_http_reject(status).await;
            let cfg = UplinkConfig::new(port, "wrong-token", PathLabel::Loopback);
            let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::default());
            let err = AudioUplink::connect(cfg, sink).await.unwrap_err();
            assert_eq!(err, UplinkError::Unauthorized { status });
        }
    }

    #[tokio::test]
    async fn non_auth_http_failure_is_not_reported_as_unauthorized() {
        let port = spawn_http_reject(500).await;
        let cfg = UplinkConfig::new(port, "t", PathLabel::Loopback);
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::default());
        let err = AudioUplink::connect(cfg, sink).await.unwrap_err();
        assert_eq!(
            err,
            UplinkError::Handshake("unexpected HTTP 500".to_string())
        );
    }

    #[tokio::test]
    async fn unreachable_sidecar_is_a_connect_error() {
        // Bind then drop, so the port is almost certainly free.
        let port = {
            let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
            l.local_addr().unwrap().port()
        };
        let cfg = UplinkConfig::new(port, "t", PathLabel::Loopback);
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::default());
        assert!(matches!(
            AudioUplink::connect(cfg, sink).await.unwrap_err(),
            UplinkError::Connect(_)
        ));
    }

    #[tokio::test]
    async fn hundred_frames_and_an_event_share_one_contiguous_seq_space() {
        // DoD: 100 audio frames then segment_end. R10 puts both kinds in ONE seq
        // space, so the event must be seq 100 — not seq 0, and not seq 1.
        let port = spawn_server(|mut ws| async move {
            let got = drain_frames(&mut ws, 101).await;
            assert_eq!(got.len(), 101, "expected 100 audio frames + 1 event frame");
            for (i, f) in got.iter().enumerate() {
                assert_eq!(f[0], if i < 100 { 0x00 } else { 0x01 }, "type byte {i}");
                let seq = u16::from_le_bytes([f[1], f[2]]);
                assert_eq!(seq as usize, i, "shared seq space broke at frame {i}");
            }
            for f in &got[..100] {
                assert_eq!(f.len(), 1927, "audio frames are fixed 1927B");
            }
            // Header ts_ms is little-endian and matches the audio timeline.
            let last = &got[99];
            assert_eq!(
                u32::from_le_bytes([last[3], last[4], last[5], last[6]]),
                99 * 30
            );
            let body = String::from_utf8(got[100][7..].to_vec()).unwrap();
            assert_eq!(
                body,
                r#"{"event":"segment_end","path":"loopback","ts_ms":3000}"#
            );
        })
        .await;

        let cfg = UplinkConfig::new(port, "tok", PathLabel::Loopback);
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::default());
        let up = AudioUplink::connect(cfg, sink).await.unwrap();
        for i in 0..100u64 {
            up.push_frame(&frame_with(i * 30, 0.25)).unwrap();
        }
        up.push_event(&json!({"event": "segment_end", "path": "loopback", "ts_ms": 3000}))
            .unwrap();
        tokio::time::sleep(Duration::from_millis(200)).await;
        let snap = up.stats();
        assert_eq!(snap.frames_sent, 100);
        assert_eq!(snap.events_sent, 1);
        assert_eq!(snap.dropped, 0);
        up.shutdown().await;
    }

    #[tokio::test]
    async fn downlink_asr_messages_become_tauri_events() {
        let port = spawn_server(|mut ws| async move {
            for msg in [
                json!({"type": "asr_start", "segment_id": "seg_1", "path": "loopback", "ts_ms": 0}),
                json!({"type": "asr_partial", "segment_id": "seg_1", "text": "半", "path": "loopback", "ts_ms": 30}),
                json!({"type": "asr_final", "segment_id": "seg_1", "text": "最终", "path": "loopback", "ts_ms": 0, "duration_ms": 3000}),
                json!({"type": "asr_error", "segment_id": "seg_2", "error": "provider_timeout", "path": "loopback", "ts_ms": 0}),
            ] {
                ws.send(Message::text(msg.to_string())).await.unwrap();
            }
            // Unknown kinds must not reach the UI.
            ws.send(Message::text(json!({"type": "future_thing"}).to_string()))
                .await
                .unwrap();
            ws.send(Message::text("{not json")).await.unwrap();
            tokio::time::sleep(Duration::from_millis(150)).await;
        })
        .await;

        let cfg = UplinkConfig::new(port, "tok", PathLabel::Loopback);
        let recorder = Arc::new(RecordingSink::default());
        let sink: Arc<dyn EventSink> = recorder.clone();
        let up = AudioUplink::connect(cfg, sink).await.unwrap();
        tokio::time::sleep(Duration::from_millis(300)).await;

        assert_eq!(
            recorder.names(),
            vec!["asr://start", "asr://partial", "asr://final", "asr://error"]
        );
        // Payload is forwarded verbatim (R14: it carries transcript text).
        let finals = recorder.payloads();
        assert_eq!(finals[2]["text"], json!("最终"));
        assert_eq!(finals[2]["duration_ms"], json!(3000));

        let snap = up.stats();
        assert_eq!(snap.downlink_messages, 4);
        assert_eq!(snap.unknown_downlink, 1);
        assert_eq!(snap.malformed_downlink, 1);
        up.shutdown().await;
    }

    #[tokio::test]
    async fn peer_close_code_1008_is_recorded() {
        let port = spawn_server(|mut ws| async move {
            ws.close(Some(tokio_tungstenite::tungstenite::protocol::CloseFrame {
                code: CloseCode::Policy,
                reason: "unauthorized".into(),
            }))
            .await
            .unwrap();
        })
        .await;

        let cfg = UplinkConfig::new(port, "tok", PathLabel::Mic);
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::default());
        let up = AudioUplink::connect(cfg, sink).await.unwrap();
        tokio::time::sleep(Duration::from_millis(200)).await;
        let snap = up.stats();
        assert_eq!(snap.close_code, Some(1008));
        assert!(snap.closed);
        up.shutdown().await;
    }

    #[tokio::test]
    async fn path_mismatch_is_refused_before_it_reaches_the_wire() {
        let port = spawn_server(|mut ws| async move {
            // Nothing should arrive: the mismatch is caught client-side.
            let got = drain_frames(&mut ws, 1).await;
            assert!(got.is_empty(), "a mismatched event must never be sent");
        })
        .await;

        let cfg = UplinkConfig::new(port, "tok", PathLabel::Mic);
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::default());
        let up = AudioUplink::connect(cfg, sink).await.unwrap();
        let err = up
            .push_event(&json!({"event": "segment_start", "path": "loopback", "ts_ms": 0}))
            .unwrap_err();
        assert_eq!(
            err,
            UplinkError::PathMismatch {
                expected: "mic",
                got: "loopback".to_string()
            }
        );
        assert_eq!(up.stats().events_sent, 0);
        up.shutdown().await;
    }

    #[tokio::test]
    async fn two_paths_keep_independent_seq_and_sockets() {
        // DoD: dual-path non-interference — two uplinks, two seq spaces.
        let loopback_port = spawn_server(|mut ws| async move {
            let got = drain_frames(&mut ws, 3).await;
            let seqs: Vec<u16> = got
                .iter()
                .map(|f| u16::from_le_bytes([f[1], f[2]]))
                .collect();
            assert_eq!(seqs, vec![0, 1, 2], "loopback seq space");
        })
        .await;
        let mic_port = spawn_server(|mut ws| async move {
            let got = drain_frames(&mut ws, 2).await;
            let seqs: Vec<u16> = got
                .iter()
                .map(|f| u16::from_le_bytes([f[1], f[2]]))
                .collect();
            assert_eq!(seqs, vec![0, 1], "mic seq space restarts at 0");
        })
        .await;

        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::default());
        let lb = AudioUplink::connect(
            UplinkConfig::new(loopback_port, "t", PathLabel::Loopback),
            sink.clone(),
        )
        .await
        .unwrap();
        let mic = AudioUplink::connect(
            UplinkConfig::new(mic_port, "t", PathLabel::Mic),
            sink.clone(),
        )
        .await
        .unwrap();

        lb.push_frame(&frame_with(0, 0.1)).unwrap();
        mic.push_frame(&frame_with(0, 0.2)).unwrap();
        lb.push_frame(&frame_with(30, 0.1)).unwrap();
        mic.push_frame(&frame_with(30, 0.2)).unwrap();
        lb.push_frame(&frame_with(60, 0.1)).unwrap();

        tokio::time::sleep(Duration::from_millis(200)).await;
        assert_eq!(lb.stats().frames_sent, 3);
        assert_eq!(mic.stats().frames_sent, 2);
        lb.shutdown().await;
        mic.shutdown().await;
    }

    #[tokio::test]
    async fn shutdown_is_safe_to_call_and_stops_accepting() {
        let port = spawn_server(|mut ws| async move {
            let _ = drain_frames(&mut ws, 1).await;
        })
        .await;
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::default());
        let up = AudioUplink::connect(UplinkConfig::new(port, "t", PathLabel::Loopback), sink)
            .await
            .unwrap();
        // shutdown is idempotent, and pushes after it are accepted but never
        // delivered (the queue is closed).
        up.shutdown().await;
        up.shutdown().await;
        up.push_frame(&frame_with(0, 0.0)).unwrap();
        assert!(up.stats().closed);
    }

    #[tokio::test]
    async fn localhost_port_is_actually_bound() {
        // Guards the test harness itself: spawn_server must yield a live port.
        let port = spawn_server(|_ws| async {}).await;
        let addr: SocketAddr = format!("127.0.0.1:{port}").parse().unwrap();
        assert!(tokio::net::TcpStream::connect(addr).await.is_ok());
    }
}
