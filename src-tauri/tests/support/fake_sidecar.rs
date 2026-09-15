//! 假 sidecar：真的 WS 服务端，只做两件事 —— 完成握手、把收到的字节记下来。
//!
//! # 为什么不用真 sidecar
//!
//! 真 sidecar 是 #19 全链 E2E 的事（那里要的是真 ASR、真检索）。本模块服务的是
//! **接线**测试：两路 worker、统一上行队列、`segment_start → 帧 → segment_end`
//! 的线上顺序、R10 的共享序号空间。这些断言需要逐字节地看线上内容，
//! 而真 sidecar 只回统计、不回原始帧。所以这里要的是"确定的观察面"，
//! 不是"更多的生产代码"。
//!
//! 它**不是** mock 语义：握手是真的 `tokio_tungstenite::accept_async`，
//! 客户端走的是生产的 `AudioUplink::connect`，收到的是生产 `encode_ws_frame`
//! 打出来的字节。只多了一层记录。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use interview_copilot_lib::audio::frame::{
    FRAME_SAMPLES, HEADER_BYTES, TYPE_AUDIO, TYPE_EVENT, WIRE_BYTES,
};
use serde_json::Value;
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::Message;

/// 线上收到的一条消息。
#[derive(Debug, Clone)]
pub enum Recorded {
    Audio {
        seq: u16,
        ts_ms: u32,
        /// 前 8 个样本（用来区分两路样本，不存整帧省内存）。
        head: [f32; 8],
    },
    Event {
        seq: u16,
        ts_ms: u32,
        body: Value,
    },
}

impl Recorded {
    pub fn seq(&self) -> u16 {
        match self {
            Recorded::Audio { seq, .. } | Recorded::Event { seq, .. } => *seq,
        }
    }

    pub fn ts_ms(&self) -> u32 {
        match self {
            Recorded::Audio { ts_ms, .. } | Recorded::Event { ts_ms, .. } => *ts_ms,
        }
    }

    pub fn is_audio(&self) -> bool {
        matches!(self, Recorded::Audio { .. })
    }

    /// 事件名（音频帧返回 None）。
    pub fn event_name(&self) -> Option<&str> {
        match self {
            Recorded::Event { body, .. } => body.get("event").and_then(|v| v.as_str()),
            Recorded::Audio { .. } => None,
        }
    }
}

/// 一条连接的全部记录。
#[derive(Debug, Default)]
pub struct Conn {
    pub records: Mutex<Vec<Recorded>>,
    pub closed: AtomicBool,
}

impl Conn {
    pub fn snapshot(&self) -> Vec<Recorded> {
        self.records
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    pub fn len(&self) -> usize {
        self.records.lock().unwrap_or_else(|e| e.into_inner()).len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// 本连接承载的 path —— 取自第一条 `segment_start` 的 `path` 字段。
    pub fn path(&self) -> Option<String> {
        self.snapshot().iter().find_map(|r| match r {
            Recorded::Event { body, .. } if r.event_name() == Some("segment_start") => body
                .get("path")
                .and_then(|v| v.as_str())
                .map(str::to_string),
            _ => None,
        })
    }

    /// 事件名出现次数。
    pub fn count_event(&self, name: &str) -> usize {
        self.snapshot()
            .iter()
            .filter(|r| r.event_name() == Some(name))
            .count()
    }

    /// 音频帧数。
    pub fn audio_count(&self) -> usize {
        self.snapshot().iter().filter(|r| r.is_audio()).count()
    }

    /// 等到至少 `n` 条记录到达（或超时）。返回是否达成。
    ///
    /// **必须 await 而不是阻塞**：`#[tokio::test]` 默认是单线程运行时，
    /// 用 `std::thread::sleep` 轮询会把执行器整个卡住 —— 假 sidecar 的读任务
    /// 拿不到时间片，于是"等不到数据"这个结论完全是自找的（曾经如此）。
    pub async fn wait_for(&self, n: usize, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if self.len() >= n {
                return true;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        self.len() >= n
    }

    /// 等到至少出现 `n` 个 `segment_end`（段全部收完）。
    pub async fn wait_for_segments(&self, n: usize, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if self.count_event("segment_end") >= n {
                return true;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        self.count_event("segment_end") >= n
    }
}

/// 假 sidecar：接受 `expect` 条连接并记录。
pub struct FakeSidecar {
    pub port: u16,
    conns: Arc<Mutex<Vec<Arc<Conn>>>>,
    stop: Arc<AtomicBool>,
}

impl FakeSidecar {
    /// 起一个监听在随机端口上的假 sidecar。
    ///
    /// 接受循环**不设上限**：服务会按 `PathLabel::ALL` 逐路连接，某一路上行
    /// 建连失败时还可能重试；固定接受 N 条会让第 N+1 次连接被拒，从而把
    /// 「单路失败不致命」这类测试变成在测接受循环的长度。需要等待时用
    /// [`FakeSidecar::wait_for_connections`]。
    pub async fn start(_expect: usize) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind fake sidecar");
        let port = listener.local_addr().expect("local_addr").port();
        let conns: Arc<Mutex<Vec<Arc<Conn>>>> = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(AtomicBool::new(false));

        let conns_task = conns.clone();
        let stop_task = stop.clone();
        tokio::spawn(async move {
            loop {
                if stop_task.load(Ordering::SeqCst) {
                    break;
                }
                let Ok((stream, _)) = listener.accept().await else {
                    break;
                };
                let conn = Arc::new(Conn::default());
                conns_task
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .push(conn.clone());
                tokio::spawn(serve_conn(stream, conn));
            }
        });

        Self { port, conns, stop }
    }

    pub fn connections(&self) -> Vec<Arc<Conn>> {
        self.conns.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    /// 等到 `n` 条连接建立。
    pub async fn wait_for_connections(&self, n: usize, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if self.connections().len() >= n {
                return true;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        self.connections().len() >= n
    }

    /// 按 path 取连接。
    pub fn conn_for(&self, path: &str) -> Option<Arc<Conn>> {
        self.connections()
            .into_iter()
            .find(|c| c.path().as_deref() == Some(path))
    }

    /// 等到两条连接都报出了 path（即都收到了 `segment_start`）。
    pub async fn wait_for_paths(&self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            let conns = self.connections();
            if conns.len() >= 2 && conns.iter().all(|c| c.path().is_some()) {
                return true;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        false
    }

    /// 等到某一路的连接出现并报出 path。
    ///
    /// 注意 path 是从第一条 `segment_start` 读出来的，所以"连接建立了"与
    /// "这路能认出来"是两个时刻 —— 单路测试里必须等后者。
    pub async fn wait_for_path(&self, path: &str, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if self.conn_for(path).is_some() {
                return true;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        self.conn_for(path).is_some()
    }
}

impl Drop for FakeSidecar {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
    }
}

async fn serve_conn(stream: tokio::net::TcpStream, conn: Arc<Conn>) {
    use futures_util::{SinkExt, StreamExt};
    let Ok(mut ws) = tokio_tungstenite::accept_async(stream).await else {
        return;
    };
    while let Some(msg) = ws.next().await {
        match msg {
            Ok(Message::Binary(bytes)) => {
                if let Some(rec) = decode(&bytes) {
                    conn.records
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .push(rec);
                }
            }
            Ok(Message::Text(txt)) => {
                // 生产不发文本帧；收到即说明契约变了，记下来让断言去发现。
                conn.records
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .push(Recorded::Event {
                        seq: 0,
                        ts_ms: 0,
                        body: serde_json::json!({"event": "__unexpected_text__", "raw": txt.to_string()}),
                    });
            }
            Ok(Message::Ping(p)) => {
                let _ = ws.send(Message::Pong(p)).await;
            }
            Ok(Message::Close(_)) | Err(_) => break,
            _ => {}
        }
    }
    conn.closed.store(true, Ordering::SeqCst);
}

/// 解出 `[type][seq u16le][ts u32le][payload]`。
fn decode(bytes: &[u8]) -> Option<Recorded> {
    if bytes.len() < HEADER_BYTES {
        return None;
    }
    let ty = bytes[0];
    let seq = u16::from_le_bytes([bytes[1], bytes[2]]);
    let ts_ms = u32::from_le_bytes([bytes[3], bytes[4], bytes[5], bytes[6]]);
    match ty {
        TYPE_AUDIO => {
            if bytes.len() != WIRE_BYTES {
                return None;
            }
            let mut head = [0f32; 8];
            for (i, slot) in head.iter_mut().enumerate() {
                let o = HEADER_BYTES + i * 4;
                *slot = f32::from_le_bytes([bytes[o], bytes[o + 1], bytes[o + 2], bytes[o + 3]]);
            }
            debug_assert_eq!(FRAME_SAMPLES * 4 + HEADER_BYTES, WIRE_BYTES);
            Some(Recorded::Audio { seq, ts_ms, head })
        }
        TYPE_EVENT => {
            let body: Value = serde_json::from_slice(&bytes[HEADER_BYTES..]).ok()?;
            Some(Recorded::Event { seq, ts_ms, body })
        }
        _ => None,
    }
}
