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
//! # 断线重连（item 4，销既有债务）
//!
//! 一条 uplink 的**连接生命周期**由一个 supervisor 任务独占：握手 → 拆读写 →
//! 任一侧结束就退避重连（[`RECONNECT_BACKOFF`]：1s/2s/4s，共 3 次）。
//!
//! 三条不变量：
//!
//! - **`seq` 跨重连不重置**（R10 要求序号空间单调）。`next_seq` 挂在
//!   [`AudioUplink`] 上而不是 socket 上，所以重连后新连接的第一帧接着旧序号走。
//!   代价是**对端会在每条新连接上记一次 `seq_gap`**（它的 `expected_seq` 从 0
//!   开始）——这是"续用不重置"的必然结果，不是丢帧；真正要保证的是**一条连接
//!   内部连续**。
//! - **`ts_ms` 跨重连不重置**：时间戳由采集侧的样本计数派生，与连接无关。
//! - **退避耗尽 = 停止重连 + 发降级事件**，不无限重试（见 [`RECONNECT_BACKOFF`]）。
//!
//! 首连**不走退避**：[`AudioUplink::connect`] 直接返回握手结果，所以 token 被拒
//! （401/403）在启动时就暴露，而不是藏在 3 次重试之后。重连阶段遇到 401/403 也
//! 立刻放弃 —— 对一个刚被拒的 token 重试三次没有意义。
//!
//! 断连期间到达的帧**照旧进有界队列**（R13 丢最旧），所以重连成功后能补发一段
//! 积压；队列放不下的部分按 R13 计 `dropped`。唯一真正丢失的是"已出队但发送失败"
//! 的那一帧，单独计 `send_failures` —— 它会在线上留下一个可见的序号缺口，
//! 而不是被静默吞掉。
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

/// 断线重连的退避序列（item 4）：1s / 2s / 4s，共 3 次。
///
/// 语义裁定：**退避耗尽即停止重连**，并发一个 `capture://degraded` 事件出去。
/// 不无限重试的理由与 sidecar 监督器一致 —— 无上限的静默重试会让"采集在跑但
/// 送不出去"看起来像正常运行，而 R13 的有界队列也终将被填满丢帧。
pub const RECONNECT_BACKOFF: [Duration; 3] = [
    Duration::from_secs(1),
    Duration::from_secs(2),
    Duration::from_secs(4),
];

/// 重连次数用尽（或 token 被拒）时下行给 UI 的事件名。
///
/// 载荷内容无关（R14）：只有 path / 尝试次数 / 原因种类。
pub const EVT_CAPTURE_DEGRADED: &str = "capture://degraded";

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
    /// 重连退避覆盖（**仅测试**注入更短的值）。
    ///
    /// 生产不设，走 [`RECONNECT_BACKOFF`]。不把它做成公开字段，是为了让
    /// "退避序列"只有一个真值源 —— 测试改的是时长，不是策略。
    backoff: Option<Vec<Duration>>,
}

impl UplinkConfig {
    pub fn new(port: u16, token: impl Into<String>, path: PathLabel) -> Self {
        Self {
            port,
            token: token.into(),
            path,
            backoff: None,
        }
    }

    /// 用给定的退避序列替换默认值（测试专用：1s/2s/4s 会让单测白等 7 秒）。
    pub fn with_backoff(mut self, backoff: Vec<Duration>) -> Self {
        self.backoff = Some(backoff);
        self
    }

    fn backoff(&self) -> &[Duration] {
        self.backoff.as_deref().unwrap_or(&RECONNECT_BACKOFF)
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

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize)]
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
    /// 成功完成的重连次数（不含首连）。
    pub reconnects: u64,
    /// 退避耗尽（或重连时 token 被拒）而放弃重连 —— 此时 `closed` 为真，
    /// 且已下发 `capture://degraded`。
    pub reconnect_exhausted: bool,
    /// 已出队但发送失败、因而真正丢失的帧数（区别于 R13 的 `dropped`）。
    pub send_failures: u64,
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
    reconnects: AtomicU64,
    reconnect_exhausted: AtomicBool,
    send_failures: AtomicU64,
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
            reconnects: self.reconnects.load(Ordering::Relaxed),
            reconnect_exhausted: self.reconnect_exhausted.load(Ordering::Relaxed),
            send_failures: self.send_failures.load(Ordering::Relaxed),
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
    /// 只在 [`OutQueue::close`] 时通知一次：让正在退避等待的重连**立刻**醒来
    /// 退出，而不是把关停拖到下一个退避点（最长 4s）。
    close_notify: tokio::sync::Notify,
}

impl OutQueue {
    fn new(cap: usize) -> Arc<Self> {
        Arc::new(Self {
            inner: Mutex::new(VecDeque::with_capacity(cap)),
            cap,
            dropped: AtomicU64::new(0),
            closed: AtomicBool::new(false),
            notify: tokio::sync::Notify::new(),
            close_notify: tokio::sync::Notify::new(),
        })
    }

    /// Never blocks and never grows past `cap`: when full the OLDEST item is
    /// evicted and counted. A `Close` marker is the newest item, so it is never
    /// the one evicted.
    fn push(&self, item: Outbound) {
        if self.closed.load(Ordering::Relaxed) {
            return;
        }
        self.push_unchecked(item);
    }

    /// 关停：先拒绝新入队，再**绕过守卫**塞进 `Close`。
    ///
    /// 顺序与绕过都是必需的：`push` 开头的 `closed` 守卫会把 `Close` 一起吞掉，
    /// 于是 writer 排空队列后直接 `pop() -> None` 退出，**从不发关闭帧** ——
    /// 对端看到的是异常断开（1006）而不是干净关闭（1000）。flush 与优雅关闭
    /// 是两件事，必须都做到：先排空（队列 FIFO 保证 `Close` 在最后），再关。
    fn close(&self) {
        self.closed.store(true, Ordering::Relaxed);
        self.push_unchecked(Outbound::Close);
        // `notify_one` 而不是 `notify_waiters`：前者在没有等待者时**存一个 permit**，
        // 于是"关停落在 closed 检查与 select 注册之间"这个窗口里也不会丢掉通知
        // （后者只在恰好有注册等待者时生效，丢一次就要白等最长 4s 的退避）。
        self.close_notify.notify_one();
    }

    fn push_unchecked(&self, item: Outbound) {
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
    /// the supervisor that owns the connection lifecycle.
    ///
    /// 首连**不走退避**：握手失败直接 `Err`，所以 token 被拒在启动时就暴露。
    /// 之后这条连接的生命周期（含断线重连）由 supervisor 独占，见模块注释。
    pub async fn connect(cfg: UplinkConfig, sink: Arc<dyn EventSink>) -> Result<Self, UplinkError> {
        let ws = handshake(&cfg).await?;

        let queue = OutQueue::new(OUT_QUEUE_CAPACITY);
        let stats = Arc::new(UplinkStats::default());
        let supervisor = tokio::spawn(supervise(
            cfg.clone(),
            sink,
            queue.clone(),
            stats.clone(),
            ws,
        ));

        Ok(Self {
            path: cfg.path,
            queue,
            stats,
            next_seq: Mutex::new(0),
            tasks: Mutex::new(vec![supervisor]),
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
    ///
    /// **先 flush 再关**（item 3 的"flush 不是可选项"）：`close()` 只置位并塞入
    /// `Close` 标记，真正的排空由 writer 按 FIFO 完成 —— 所以停止采集时
    /// worker 补发的那条 `segment_end` 一定先上线，然后才是关闭帧。
    pub async fn shutdown(&self) {
        self.queue.close();

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

/// 握手（首连与重连共用同一份请求构造）。
async fn handshake(cfg: &UplinkConfig) -> Result<Ws, UplinkError> {
    let request = build_request(cfg)?;
    let (ws, _resp) = connect_async(request)
        .await
        .map_err(|e| map_connect_error(&e))?;
    Ok(ws)
}

/// 重连尝试的结果 —— 三态必须分开，否则"用户关停"会被误报成"退避耗尽"。
enum Reconnected {
    Yes(Box<Ws>),
    /// 退避耗尽或 token 被拒：已发降级事件。
    Exhausted,
    /// 关停中：不发降级事件（这不是故障）。
    ShuttingDown,
}

/// 一条连接的完整生命周期：拆读写 → 任一侧结束 → 退避重连，直到关停或耗尽。
async fn supervise(
    cfg: UplinkConfig,
    sink: Arc<dyn EventSink>,
    queue: Arc<OutQueue>,
    stats: Arc<UplinkStats>,
    first: Ws,
) {
    let mut ws = first;
    loop {
        // 每条连接重新开始观察对端关闭码：否则上一条连接留下的 1008 会让
        // 这一次的"异常断开"被误判成"被拒"。
        stats.close_code.store(0, Ordering::Relaxed);

        let (write, read) = ws.split();

        // `select!` 会 poll **所有**分支：若直接 select `&mut JoinHandle`，两侧同轮
        // 就绪时两个 handle 都会被 poll 到完成，随后的 `handle.await` 会 panic
        // （"JoinHandle polled after completion"）。所以 select 的是各自的一声
        // 通知，JoinHandle 只在下面 await 一次，永远安全。
        let (writer_done, writer_ended) = tokio::sync::oneshot::channel::<()>();
        let (reader_done, reader_ended) = tokio::sync::oneshot::channel::<()>();
        let writer = {
            let (queue, stats) = (queue.clone(), stats.clone());
            tokio::spawn(async move {
                writer_loop(write, queue, stats).await;
                let _ = writer_done.send(());
            })
        };
        let reader = {
            let (sink, stats) = (sink.clone(), stats.clone());
            tokio::spawn(async move {
                reader_loop(read, sink, stats).await;
                let _ = reader_done.send(());
            })
        };

        // 任一侧结束 = 这条连接结束。对端关连接时 reader 先返回，而 writer 还阻塞
        // 在空队列上 —— 必须主动中止它，否则永远等不到重连。
        tokio::select! {
            _ = writer_ended => reader.abort(),
            _ = reader_ended => writer.abort(),
        }
        let _ = writer.await;
        let _ = reader.await;

        if queue.closed.load(Ordering::Relaxed) {
            // 用户关停：writer 已把队列排空（含关闭帧），不再重连。
            break;
        }

        // 对端用 1008(Policy)/1007(Invalid) 关闭 = 鉴权/协议拒绝，重试无意义。
        let code = stats.close_code.load(Ordering::Relaxed) as u16;
        if is_fatal_close(code) {
            emit_degraded(
                sink.as_ref(),
                &cfg,
                "peer_rejected",
                0,
                &UplinkError::Unauthorized { status: code },
            );
            stats.reconnect_exhausted.store(true, Ordering::Relaxed);
            break;
        }

        match reconnect(&cfg, &queue, sink.as_ref()).await {
            Reconnected::Yes(next) => {
                stats.reconnects.fetch_add(1, Ordering::Relaxed);
                ws = *next;
            }
            Reconnected::Exhausted => {
                stats.reconnect_exhausted.store(true, Ordering::Relaxed);
                break;
            }
            Reconnected::ShuttingDown => break,
        }
    }
    stats.closed.store(true, Ordering::Relaxed);
}

/// 对端以"鉴权/协议拒绝"关闭：重连没有意义，直接降级。
fn is_fatal_close(code: u16) -> bool {
    matches!(code, 1007 | 1008)
}

/// 按 [`RECONNECT_BACKOFF`] 退避重连。
///
/// token 被拒（401/403）**不重试**：对一个刚被拒的 token 等 1s/2s/4s 再问三遍
/// 没有意义，还会把"配置错了"伪装成"网络抖了"。
async fn reconnect(cfg: &UplinkConfig, queue: &OutQueue, sink: &dyn EventSink) -> Reconnected {
    let backoff = cfg.backoff();
    for (attempt, delay) in backoff.iter().enumerate() {
        // 退避睡眠要能被关停打断，否则 stop 最长要等 4s。
        tokio::select! {
            _ = tokio::time::sleep(*delay) => {}
            _ = queue.close_notify.notified() => return Reconnected::ShuttingDown,
        }
        if queue.closed.load(Ordering::Relaxed) {
            return Reconnected::ShuttingDown;
        }

        match handshake(cfg).await {
            Ok(ws) => return Reconnected::Yes(Box::new(ws)),
            Err(e @ UplinkError::Unauthorized { .. }) => {
                emit_degraded(sink, cfg, "unauthorized", attempt + 1, &e);
                return Reconnected::Exhausted;
            }
            Err(_) => continue,
        }
    }
    emit_degraded(
        sink,
        cfg,
        "reconnect_exhausted",
        backoff.len(),
        &UplinkError::Connect(format!("{} 次重连均失败", backoff.len())),
    );
    Reconnected::Exhausted
}

/// 降级事件：内容无关（R14），只有 path / 原因种类 / 尝试次数 / 可读原因。
fn emit_degraded(
    sink: &dyn EventSink,
    cfg: &UplinkConfig,
    reason: &'static str,
    attempts: usize,
    err: &UplinkError,
) {
    sink.emit(
        EVT_CAPTURE_DEGRADED,
        serde_json::json!({
            "type": "capture_degraded",
            "path": cfg.path.as_str(),
            "reason": reason,
            "attempts": attempts,
            "last_error": err.to_string(),
        }),
    );
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
                    // 已出队却没送出去 —— 这一帧是真的丢了（区别于 R13 的
                    // eviction），单独计数：它会在线上留下可见的序号缺口。
                    stats.send_failures.fetch_add(1, Ordering::Relaxed);
                    break;
                }
            }
            Outbound::Close => {
                // 队列 FIFO 保证走到这里时前面的帧都已送出：这就是 flush。
                let _ = sink.close().await;
                break;
            }
        }
    }
    // 刻意**不**在这里置 `closed`：writer 结束只说明"这条连接结束了"，
    // 之后是重连还是收工由 supervisor 决定。
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

    // ---- item 4：断线重连 -------------------------------------------------

    /// 可重连的假 sidecar：按 `drop_after` **逐条**接受连接，第 i 条读到
    /// `drop_after[i]` 帧后主动断开（模拟 sidecar 重启 / 网络断）。
    ///
    /// 逐条给计数而不是全局一个：第一条通常只收 1~3 帧就断，第二条要收的是
    /// "断连期间积压 + 续用序号"的那一段，两者数量本来就不同。
    async fn spawn_reconnecting_server(
        drop_after: Vec<usize>,
    ) -> (u16, Arc<StdMutex<Vec<Vec<u16>>>>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let log: Arc<StdMutex<Vec<Vec<u16>>>> = Arc::new(StdMutex::new(Vec::new()));
        let out = log.clone();
        tokio::spawn(async move {
            for want in drop_after {
                let Ok((stream, _)) = listener.accept().await else {
                    break;
                };
                let Ok(mut ws) = accept_async(stream).await else {
                    break;
                };
                let mut seqs = Vec::new();
                while seqs.len() < want {
                    match ws.next().await {
                        Some(Ok(Message::Binary(b))) if b.len() >= 3 => {
                            seqs.push(u16::from_le_bytes([b[1], b[2]]));
                        }
                        Some(Ok(_)) => {}
                        _ => break,
                    }
                }
                lock(&out).push(seqs);
                let _ = ws.close(None).await; // 无关闭码 → 不是"被拒"，应重连
                let _ = ws.next().await; // 等对端确认，别把 socket 立刻丢掉
            }
        });
        (port, log)
    }

    async fn wait_until(mut cond: impl FnMut() -> bool, timeout: Duration) -> bool {
        let deadline = tokio::time::Instant::now() + timeout;
        while tokio::time::Instant::now() < deadline {
            if cond() {
                return true;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        cond()
    }

    #[tokio::test]
    async fn reconnect_keeps_the_seq_space_and_does_not_reset_it() {
        // DoD(item 4)：断线重连后 seq **续用不重置** —— 新连接的第一帧接着旧序号，
        // 而不是从 0 重来（R10 的序号空间是每路一个、跨连接单调）。
        let (port, log) = spawn_reconnecting_server(vec![3, 3]).await;
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::default());
        let cfg = UplinkConfig::new(port, "t", PathLabel::Loopback)
            .with_backoff(vec![Duration::from_millis(50)]);
        let up = AudioUplink::connect(cfg, sink).await.unwrap();

        for i in 0..3u64 {
            up.push_frame(&frame_with(i * 30, 0.1)).unwrap();
        }
        assert!(
            wait_until(|| !lock(&log).is_empty(), Duration::from_secs(5)).await,
            "第一条连接应收到 3 帧后断开"
        );
        for i in 3..6u64 {
            up.push_frame(&frame_with(i * 30, 0.1)).unwrap();
        }
        assert!(
            wait_until(|| lock(&log).len() >= 2, Duration::from_secs(5)).await,
            "应在退避后重连上第二条连接"
        );

        let seen = lock(&log).clone();
        assert_eq!(seen[0], vec![0, 1, 2], "首连的序号空间");
        assert_eq!(
            seen[1],
            vec![3, 4, 5],
            "重连后序号必须续用，不能重置为 0/1/2"
        );
        let snap = up.stats();
        assert_eq!(snap.reconnects, 1);
        assert!(!snap.reconnect_exhausted);
        assert_eq!(snap.dropped, 0, "重连不应产生 R13 丢帧");
        up.shutdown().await;
    }

    #[tokio::test]
    async fn reconnect_backoff_exhausts_and_emits_a_degradation_event() {
        // DoD(item 4)：退避耗尽 → 降级事件，且不再无限重试。
        let (port, _log) = spawn_reconnecting_server(vec![1]).await;
        let recorder = Arc::new(RecordingSink::default());
        let sink: Arc<dyn EventSink> = recorder.clone();
        // 三条 60ms 退避 ≈ 180ms，测试不必真等 7 秒。
        let cfg = UplinkConfig::new(port, "t", PathLabel::Loopback).with_backoff(vec![
            Duration::from_millis(60),
            Duration::from_millis(60),
            Duration::from_millis(60),
        ]);
        let up = AudioUplink::connect(cfg, sink).await.unwrap();
        up.push_frame(&frame_with(0, 0.1)).unwrap();

        assert!(
            wait_until(
                || recorder.names().iter().any(|n| n == EVT_CAPTURE_DEGRADED),
                Duration::from_secs(10)
            )
            .await,
            "退避耗尽必须下发降级事件，而不是静默停止"
        );
        let payload = recorder
            .payloads()
            .into_iter()
            .find(|p| p["type"] == "capture_degraded")
            .unwrap();
        assert_eq!(payload["path"], json!("loopback"));
        assert_eq!(payload["reason"], json!("reconnect_exhausted"));
        assert_eq!(payload["attempts"], json!(3));
        // R14：降级载荷内容无关 —— 只有种类与计数，没有音频/转写。
        assert!(payload.get("text").is_none());

        assert!(wait_until(|| up.stats().closed, Duration::from_secs(5)).await);
        let snap = up.stats();
        assert!(snap.reconnect_exhausted);
        assert_eq!(snap.reconnects, 0, "一次都没接上，不该记成功重连");
    }

    #[tokio::test]
    async fn a_policy_close_from_the_peer_is_fatal_and_is_not_retried() {
        // 1008 = 鉴权/策略拒绝。对一个刚被拒的连接重试三次没有意义。
        let port = spawn_server(|mut ws| async move {
            let _ = drain_frames(&mut ws, 1).await;
            ws.close(Some(tokio_tungstenite::tungstenite::protocol::CloseFrame {
                code: CloseCode::Policy,
                reason: "unauthorized".into(),
            }))
            .await
            .unwrap();
            tokio::time::sleep(Duration::from_millis(300)).await;
        })
        .await;

        let recorder = Arc::new(RecordingSink::default());
        let sink: Arc<dyn EventSink> = recorder.clone();
        let cfg = UplinkConfig::new(port, "t", PathLabel::Loopback)
            .with_backoff(vec![Duration::from_millis(30)]);
        let up = AudioUplink::connect(cfg, sink).await.unwrap();
        up.push_frame(&frame_with(0, 0.1)).unwrap();

        assert!(
            wait_until(
                || recorder.names().iter().any(|n| n == EVT_CAPTURE_DEGRADED),
                Duration::from_secs(5)
            )
            .await,
            "被拒必须降级"
        );
        let payload = recorder
            .payloads()
            .into_iter()
            .find(|p| p["type"] == "capture_degraded")
            .unwrap();
        assert_eq!(payload["reason"], json!("peer_rejected"));
        assert_eq!(payload["attempts"], json!(0), "被拒不该退避重试");
        let snap = up.stats();
        assert_eq!(snap.close_code, Some(1008));
        assert_eq!(snap.reconnects, 0);
        assert!(snap.reconnect_exhausted);
        up.shutdown().await;
    }

    #[tokio::test]
    async fn frames_queued_while_disconnected_are_flushed_after_reconnect() {
        // R13 的有界队列在断连期间照常收帧，重连后按序补发。
        let (port, log) = spawn_reconnecting_server(vec![1, 3]).await;
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::default());
        let cfg = UplinkConfig::new(port, "t", PathLabel::Loopback)
            .with_backoff(vec![Duration::from_millis(120)]);
        let up = AudioUplink::connect(cfg, sink).await.unwrap();

        up.push_frame(&frame_with(0, 0.1)).unwrap();
        assert!(
            wait_until(|| !lock(&log).is_empty(), Duration::from_secs(5)).await,
            "首连应收到 1 帧后断开"
        );
        // 断连期间（supervisor 正在退避）继续推 —— 这些帧必须进队列而不是被丢。
        for i in 1..4u64 {
            up.push_frame(&frame_with(i * 30, 0.1)).unwrap();
        }
        assert!(
            wait_until(|| lock(&log).len() >= 2, Duration::from_secs(5)).await,
            "应重连并补发积压帧"
        );
        let seen = lock(&log).clone();
        assert_eq!(seen[1], vec![1, 2, 3], "断连期间的帧应在重连后按序补发");
        assert_eq!(up.stats().dropped, 0, "队列远未满，不该触发 R13 丢帧");
        up.shutdown().await;
    }

    #[tokio::test]
    async fn shutdown_flushes_pending_frames_then_sends_a_close_frame() {
        // DoD(item 3)：flush 不是可选项 —— 停止时先补发未送出的帧，再发关闭帧。
        // 也钉住一个曾经的缺陷：`close()` 若走 `push()` 会被 closed 守卫吞掉，
        // 于是 writer 只丢 socket、从不发关闭帧（对端看到 1006 而非 1000）。
        let (tx, rx) = tokio::sync::oneshot::channel();
        let port = {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let port = listener.local_addr().unwrap().port();
            tokio::spawn(async move {
                let (stream, _) = listener.accept().await.unwrap();
                let mut ws = accept_async(stream).await.unwrap();
                let mut seqs = Vec::new();
                let mut saw_close = false;
                while let Some(msg) = ws.next().await {
                    match msg {
                        Ok(Message::Binary(b)) if b.len() >= 3 => {
                            seqs.push(u16::from_le_bytes([b[1], b[2]]));
                        }
                        Ok(Message::Close(_)) => {
                            saw_close = true;
                            break;
                        }
                        Ok(_) => {}
                        Err(_) => break,
                    }
                }
                let _ = tx.send((seqs, saw_close));
            });
            port
        };

        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::default());
        let up = AudioUplink::connect(UplinkConfig::new(port, "t", PathLabel::Loopback), sink)
            .await
            .unwrap();
        for i in 0..3u64 {
            up.push_frame(&frame_with(i * 30, 0.1)).unwrap();
        }
        up.shutdown().await;

        let (seqs, saw_close) = tokio::time::timeout(Duration::from_secs(5), rx)
            .await
            .expect("the server must observe the close")
            .expect("server task must send its report");
        assert_eq!(seqs, vec![0, 1, 2], "三帧必须全部送达（flush）");
        assert!(
            saw_close,
            "关闭帧必须真的发出（否则对端看到的是异常断开 1006）"
        );
    }
}
