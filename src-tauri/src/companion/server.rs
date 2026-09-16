//! 伴侣服务器：TCP 接受循环 + HTTP 页面应答 + WebSocket 只读通道。
//!
//! # 为什么要自己读一遍 HTTP 头
//!
//! 手机扫码打开的是 `http://…/?token=…`（浏览器只认 http），页面里的 JS 再连
//! `ws://…/ws?token=…`。同一个端口要同时服务两种请求，而 `tokio_tungstenite`
//! 的 `accept_async` 一旦拿去读非 WS 请求就会把流吃掉。所以这里先**手工读请求头**
//! （到 `\r\n\r\n` 为止），判断是不是 Upgrade，不是就自己应答；是就把读到的头**原样喂回**
//! 给 `accept_async`（[`Prefixed`]）。
//!
//! # 上行帧
//!
//! 手机端是只读镜像：收到的任何上行帧都直接丢弃，不解析、不执行。

use std::io;
use std::net::SocketAddr;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};

use futures_util::{SinkExt, StreamExt};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{broadcast, mpsc, oneshot, RwLock};
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::WebSocketStream;

use super::{
    cards_frame, close_frame, now_ms, parse_token, phone_page, revoked_frame, token_ok,
    welcome_frame, ClientHandle, Inner, LiveCard, HEARTBEAT, REVOKED_CLOSE_CODE, WS_PATH,
};

/// 请求头大小上限（超过就判为恶意/畸形连接，直接关掉）。
const MAX_HEAD_BYTES: usize = 16 * 1024;

/// 401 应答体（不回显任何 token 信息）。
const BODY_401: &str = "<!doctype html><meta charset=utf-8><title>401</title>\
<p>连接已失效或二维码过期，请在主屏点「刷新二维码」后重新扫码。</p>";

const BODY_404: &str = "<!doctype html><meta charset=utf-8><title>404</title><p>not found</p>";

/// 接受循环。收到 shutdown 信号即退出并释放端口。
pub(super) async fn serve(
    listener: TcpListener,
    inner: Arc<RwLock<Inner>>,
    cards_tx: broadcast::Sender<Vec<LiveCard>>,
    mut shutdown: oneshot::Receiver<()>,
) {
    loop {
        tokio::select! {
            accepted = listener.accept() => {
                match accepted {
                    Ok((stream, peer)) => {
                        tokio::spawn(handle(stream, peer, Arc::clone(&inner), cards_tx.clone()));
                    }
                    // 监听出错（句柄被关/资源耗尽）：退一小步再试，避免打满 CPU。
                    Err(e) => {
                        eprintln!("[companion] accept 失败：{e}");
                        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                    }
                }
            }
            _ = &mut shutdown => break,
        }
    }
}

async fn handle(
    mut stream: TcpStream,
    peer: SocketAddr,
    inner: Arc<RwLock<Inner>>,
    cards_tx: broadcast::Sender<Vec<LiveCard>>,
) {
    let head = match read_head(&mut stream).await {
        Ok(h) => h,
        Err(e) => {
            eprintln!("[companion] {peer} 读请求头失败：{e}");
            return;
        }
    };
    let request = RequestHead::parse(&head);

    if request.is_websocket_upgrade() {
        let provided = parse_token(request.query());
        let expected = {
            let guard = inner.read().await;
            guard.token.clone().unwrap_or_default()
        };
        if !token_ok(provided.as_deref(), &expected) {
            let _ = write_response(&mut stream, 401, "Unauthorized", BODY_401).await;
            return;
        }
        let ws = match tokio_tungstenite::accept_async(Prefixed::new(head, stream)).await {
            Ok(ws) => ws,
            Err(e) => {
                eprintln!("[companion] {peer} WebSocket 握手失败：{e}");
                return;
            }
        };
        // 上面已校验过，unwrap 安全。
        serve_ws(ws, provided.unwrap_or_default(), inner, cards_tx).await;
    } else {
        serve_http(stream, &request, &inner).await;
    }
}

/// 一个 WS 连接的生命周期：注册 → welcome → 补一帧快照 → 推流 + 心跳 → 清理。
async fn serve_ws<S>(
    mut ws: WebSocketStream<S>,
    token: String,
    inner: Arc<RwLock<Inner>>,
    cards_tx: broadcast::Sender<Vec<LiveCard>>,
) where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let (tx, mut rx) = mpsc::unbounded_channel::<Message>();

    // 同一 token 再来一次握手：**顶掉旧连接**（Last-Writer-Wins）。
    // 不拒绝是为了让「手机刷新页面」能用；代价是克隆设备会挤掉真机——
    // 这个状态在主屏的客户端列表里看得见（数量不会变成 2）。
    let (id, replaced) = {
        let mut guard = inner.write().await;
        let mut replaced: Vec<mpsc::UnboundedSender<Message>> = Vec::new();
        guard.clients.retain(|_, h| match h.token == token {
            true => {
                replaced.push(h.tx.clone());
                false
            }
            false => true,
        });
        let id = guard.next_client_id();
        guard.clients.insert(
            id,
            ClientHandle {
                tx: tx.clone(),
                token,
                since_ms: now_ms(),
            },
        );
        (id, replaced)
    };
    for old in replaced {
        let _ = old.send(Message::text(revoked_frame("replaced_by_new_connection")));
        let _ = old.send(close_frame(
            REVOKED_CLOSE_CODE,
            "replaced_by_new_connection",
        ));
    }

    let _ = tx.send(Message::text(welcome_frame()));
    let latest = { inner.read().await.latest.clone() };
    if !latest.is_empty() {
        if let Ok(frame) = cards_frame(&latest) {
            let _ = tx.send(Message::text(frame));
        }
    }

    let mut cards_rx = cards_tx.subscribe();
    let mut heartbeat = tokio::time::interval(HEARTBEAT);
    heartbeat.tick().await; // 第一次 tick 立即完成，丢掉

    loop {
        tokio::select! {
            Some(msg) = rx.recv() => {
                let closing = matches!(msg, Message::Close(_));
                if ws.send(msg).await.is_err() { break; }
                // 发完 Close 就退：否则这条任务要等到手机端也关闭才回收。
                if closing { break; }
            }
            Ok(cards) = cards_rx.recv() => {
                match cards_frame(&cards) {
                    Ok(frame) => { if ws.send(Message::text(frame)).await.is_err() { break; } }
                    Err(e) => eprintln!("[companion] 卡片序列化失败：{e}"),
                }
            }
            _ = heartbeat.tick() => {
                if ws.send(Message::Ping(Default::default())).await.is_err() { break; }
            }
            incoming = ws.next() => {
                match incoming {
                    // 只读通道：上行帧一律丢弃（连 Close 都不需要，流断开即 None）。
                    Some(Ok(_)) => {}
                    None | Some(Err(_)) => break,
                }
            }
        }
    }

    // 断连清理：这是「手机关页面 / 锁屏 / 换网络」的唯一回收点。
    inner.write().await.clients.remove(&id);
}

/// 普通 HTTP：只服务一个页面，且**必须带有效 token**（否则 401）。
async fn serve_http(mut stream: TcpStream, request: &RequestHead, inner: &Arc<RwLock<Inner>>) {
    let valid = {
        let guard = inner.read().await;
        token_ok(
            parse_token(request.query()).as_deref(),
            &guard.token.clone().unwrap_or_default(),
        )
    };
    let (status, reason, body) = if !valid {
        (401, "Unauthorized", BODY_401)
    } else if request.method != "GET" {
        (405, "Method Not Allowed", BODY_404)
    } else if request.path == "/" || request.path == "/index.html" {
        (200, "OK", phone_page::PAGE)
    } else {
        (404, "Not Found", BODY_404)
    };
    // 发完就撒手：让 TcpStream 正常 Drop（FIN），不要主动 shutdown——
    // 在 Windows 上那会让对端收到 RST，还没读完的响应体会丢掉。
    let _ = write_response(&mut stream, status, reason, body).await;
}

async fn write_response(
    stream: &mut TcpStream,
    status: u16,
    reason: &str,
    body: &str,
) -> io::Result<()> {
    let head = format!(
        "HTTP/1.1 {status} {reason}\r\n\
         Content-Type: text/html; charset=utf-8\r\n\
         Content-Length: {}\r\n\
         Cache-Control: no-store\r\n\
         X-Content-Type-Options: nosniff\r\n\
         Connection: close\r\n\r\n",
        body.len()
    );
    stream.write_all(head.as_bytes()).await?;
    stream.write_all(body.as_bytes()).await?;
    stream.flush().await
}

/// 读到 `\r\n\r\n` 为止（含终止符）。
async fn read_head(stream: &mut TcpStream) -> io::Result<Vec<u8>> {
    let mut buf: Vec<u8> = Vec::with_capacity(512);
    let mut chunk = [0u8; 512];
    loop {
        if let Some(end) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
            return Ok(buf[..end + 4].to_vec());
        }
        if buf.len() > MAX_HEAD_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "请求头超过 16KB",
            ));
        }
        let n = stream.read(&mut chunk).await?;
        if n == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "连接在读请求头前关闭",
            ));
        }
        buf.extend_from_slice(&chunk[..n]);
    }
}

/// 只解析我们关心的那点东西：方法、路径、query、以及有没有 Upgrade。
pub(super) struct RequestHead {
    method: String,
    path: String,
    raw_query: Option<String>,
    upgrade: bool,
}

impl RequestHead {
    fn parse(head: &[u8]) -> Self {
        let text = String::from_utf8_lossy(head);
        let mut lines = text.split("\r\n");
        let mut method = String::new();
        let mut path = String::new();
        let mut raw_query: Option<String> = None;

        if let Some(first) = lines.next() {
            let mut parts = first.split_whitespace();
            method = parts.next().unwrap_or("").to_ascii_uppercase();
            let target = parts.next().unwrap_or("");
            let (p, q) = match target.split_once('?') {
                Some((p, q)) => (p.to_string(), Some(q.to_string())),
                None => (target.to_string(), None),
            };
            path = p;
            raw_query = q;
        }

        let mut upgrade = false;
        for line in lines {
            if let Some((key, value)) = line.split_once(':') {
                if key.eq_ignore_ascii_case("upgrade")
                    && value.to_ascii_lowercase().contains("websocket")
                {
                    upgrade = true;
                }
            }
        }
        Self {
            method,
            path,
            raw_query,
            upgrade,
        }
    }

    fn query(&self) -> Option<&str> {
        self.raw_query.as_deref()
    }

    fn is_websocket_upgrade(&self) -> bool {
        self.upgrade && self.path == WS_PATH
    }
}

/// 把已经读走的请求头拼回流前面，交给 `accept_async` 接着读。
struct Prefixed<S> {
    head: Vec<u8>,
    pos: usize,
    rest: S,
}

impl<S> Prefixed<S> {
    fn new(head: Vec<u8>, rest: S) -> Self {
        Self { head, pos: 0, rest }
    }
}

impl<S: AsyncRead + Unpin> AsyncRead for Prefixed<S> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut tokio::io::ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        if this.pos < this.head.len() {
            let remaining = &this.head[this.pos..];
            let n = remaining.len().min(buf.remaining());
            buf.put_slice(&remaining[..n]);
            this.pos += n;
            return Poll::Ready(Ok(()));
        }
        Pin::new(&mut this.rest).poll_read(cx, buf)
    }
}

impl<S: AsyncWrite + Unpin> AsyncWrite for Prefixed<S> {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.get_mut().rest).poll_write(cx, buf)
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().rest).poll_flush(cx)
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().rest).poll_shutdown(cx)
    }
}
