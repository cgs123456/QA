//! 手机伴侣（S8）：本地 WebSocket 二屏 + QR 扫码。
//!
//! # 它是什么
//!
//! 主屏（本机）起一个**只监听局域网 IP** 的 TCP 端口：
//! - `GET /?token=…`  → 返回手机端的只读页面（[`phone_page::PAGE`]，一个自包含 HTML）；
//! - `GET /ws?token=…`（WebSocket 升级）→ 推送主屏的提词卡片。
//!
//! 主屏把 `http://<lan_ip>:54322/?token=<token>` 渲染成 QR 码，手机扫码即连。
//!
//! # 六条硬约束（违背任一即为 bug，都有测试钉住）
//!
//! 1. **只绑局域网地址**：`start()` 只接受 [`pick_lan_ip`] 选出的 RFC1918 私有 IPv4，
//!    不绑 `0.0.0.0`、不绑回环、不绑公网地址。选不出局域网地址就**拒绝启动**（fail-closed）。
//! 2. **Token 进 QR，不进日志**：token 只出现在 QR 的 URL 里和内存里，
//!    不写 `eprintln!`/`println!`/日志文件，也**不下发给前端**（前端只拿到 QR 的 SVG）。
//! 3. **无云中继、无公网暴露**：没有 TURN/STUN/信令/任何外网服务，只有 LAN 内直连。
//! 4. **连接随时可吊销**：`stop()`（全断+关端口）、`revoke_client(id)`（单设备）、
//!    `rotate_token()`（换 token，旧连接全部失效）。
//! 5. **二屏只读镜像**：手机端只收 `cards` 帧，**没有任何上行业务帧**——
//!    上行帧被服务端直接丢弃。载荷复用 `teleprompter://cards` 的 [`LiveCard`]，
//!    手机端**不跑第二份 `useLiveQA`**（P7 已钉：同一问题不许发两次检索）。
//! 6. **默认关闭**：只有用户主动点「开启伴侣」才监听，应用退出必停。
//!
//! # 分层
//!
//! - 本文件：纯函数（token/URL/QR/选 IP/帧序列化）+ 服务状态机 + Tauri 命令；
//! - [`server`]：TCP 接受循环、HTTP 应答、WebSocket 鉴权与心跳；
//! - [`phone_page`]：手机端页面（静态字符串，无外部资源、无 CDN）。
//!
//! 威胁模型与协议规格见 `docs/companion-security.md`。

pub mod phone_page;
mod server;

use std::collections::HashMap;
use std::net::{IpAddr, SocketAddr};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use base64::Engine;
use qrcode::render::svg;
use qrcode::QrCode;
use rand::RngCore;
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};
use tokio::net::TcpListener;
use tokio::sync::{broadcast, mpsc, oneshot, RwLock};
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::frame::CloseFrame;
use tokio_tungstenite::tungstenite::Message;

/// 伴侣服务固定端口（QR 与防火墙放行都要它稳定）。
pub const COMPANION_PORT: u16 = 54322;

/// 线路协议版本。改帧格式必须同时改：本常量 / `phone_page::PAGE` / `docs/companion-security.md` §2。
pub const PROTOCOL_VERSION: u32 = 1;

/// WebSocket 路径（页面走 `/`）。
pub const WS_PATH: &str = "/ws";

/// token 的 query 参数名。
pub const TOKEN_QUERY_KEY: &str = "token";

/// token 熵：32 字节 → URL-safe base64 43 字符。
const TOKEN_BYTES: usize = 32;

/// 每张卡的 WS 关闭码（4001 = 应用层自定义：已被吊销）。
pub const REVOKED_CLOSE_CODE: u16 = 4001;

/// 服务端心跳间隔（浏览器 WS 会自动回 pong，用来发现死连接）。
pub(crate) const HEARTBEAT: Duration = Duration::from_secs(15);

/// 卡片广播通道容量（主屏推得比手机吃得快时，允许丢中间帧）。
const CARDS_CHANNEL_CAP: usize = 64;

/// QR 码最小边长（像素）。
const QR_MIN_PX: u32 = 240;

// ---------------------------------------------------------------- 纯函数层

/// 生成会话 token：32 字节 CSPRNG → URL-safe base64（无 padding）。
pub fn generate_token() -> String {
    let mut bytes = [0u8; TOKEN_BYTES];
    rand::thread_rng().fill_bytes(&mut bytes);
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

/// 常量时间比较（不做长度早退以外的短路，避免按字节计时）。
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

/// 握手 token 是否有效。
///
/// `expected` 为空（服务未启动）时恒为 false——服务没起就不该有人连得上。
pub fn token_ok(provided: Option<&str>, expected: &str) -> bool {
    if expected.is_empty() {
        return false;
    }
    match provided {
        None => false,
        Some(p) => constant_time_eq(p.as_bytes(), expected.as_bytes()),
    }
}

/// 从 query 串里取 `token=`（空值视为没有）。
pub fn parse_token(query: Option<&str>) -> Option<String> {
    let query = query?;
    for pair in query.split('&') {
        if pair.is_empty() {
            continue;
        }
        let (key, value) = match pair.split_once('=') {
            Some(kv) => kv,
            None => continue,
        };
        if key == TOKEN_QUERY_KEY {
            return if value.is_empty() {
                None
            } else {
                Some(value.to_string())
            };
        }
    }
    None
}

/// QR 里编码的**页面**地址（http，手机浏览器扫码直接打开）。
pub fn build_page_url(host: &str, port: u16, token: &str) -> String {
    format!("http://{host}:{port}/?{TOKEN_QUERY_KEY}={token}")
}

/// 页面里 JS 自己拼的 WS 地址（与 [`build_page_url`] 同源同 token）。
pub fn build_ws_url(host: &str, port: u16, token: &str) -> String {
    format!("ws://{host}:{port}{WS_PATH}?{TOKEN_QUERY_KEY}={token}")
}

/// 私有 IPv4（RFC1918）且非回环、非 link-local。
pub fn is_lan_ipv4(addr: IpAddr) -> bool {
    match addr {
        IpAddr::V4(v4) => {
            if v4.is_loopback() || v4.is_link_local() {
                return false;
            }
            let o = v4.octets();
            o[0] == 10 || (o[0] == 172 && (16..=31).contains(&o[1])) || (o[0] == 192 && o[1] == 168)
        }
        IpAddr::V6(_) => false,
    }
}

/// 从网卡列表里挑一个局域网 IPv4；挑不出就 `None`（调用方必须拒绝启动）。
pub fn pick_lan_ip(interfaces: &[(String, IpAddr)]) -> Option<IpAddr> {
    interfaces
        .iter()
        .map(|(_, addr)| *addr)
        .find(|addr| is_lan_ipv4(*addr))
}

/// 渲染 QR 码 SVG（**入参就是要编码的数据**；token 只在这里和 URL 里出现一次）。
pub fn render_qr_svg(data: &str) -> Result<String, String> {
    let code = QrCode::new(data.as_bytes()).map_err(|e| format!("生成二维码失败：{e}"))?;
    Ok(code
        .render::<svg::Color>()
        .min_dimensions(QR_MIN_PX, QR_MIN_PX)
        .dark_color(svg::Color("#000000"))
        .light_color(svg::Color("#ffffff"))
        .build())
}

/// 下行帧：`welcome`。
pub fn welcome_frame() -> String {
    serde_json::json!({
        "type": "welcome",
        "protocolVersion": PROTOCOL_VERSION,
    })
    .to_string()
}

/// 下行帧：`cards`（与 `teleprompter://cards` 载荷同源）。
pub fn cards_frame(cards: &[LiveCard]) -> Result<String, String> {
    serde_json::to_string(&serde_json::json!({ "type": "cards", "cards": cards }))
        .map_err(|e| format!("序列化卡片失败：{e}"))
}

/// 下行帧：`revoked`（服务端主动断开前先说一句为什么）。
pub fn revoked_frame(reason: &str) -> String {
    serde_json::json!({ "type": "revoked", "reason": reason }).to_string()
}

/// 带关闭码的 Close 帧。
pub(crate) fn close_frame(code: u16, reason: &str) -> Message {
    Message::Close(Some(CloseFrame {
        code: CloseCode::from(code),
        reason: reason.to_string().into(),
    }))
}

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

// ---------------------------------------------------------------- 数据类型

/// 一张提词卡片（与前端 `lib/liveqa.ts::LiveCard` 同构）。
///
/// `rename_all = "camelCase"` 是契约的一部分：前端字段是 `atMs` 不是 `at_ms`。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LiveCard {
    pub id: String,
    pub question: String,
    pub answer: String,
    pub source: String,
    pub kind: String,
    pub at_ms: i64,
    pub reused: bool,
}

/// 伴侣服务状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum CompanionState {
    /// 未启动（默认状态）
    Stopped,
    /// 监听中，尚无手机连接
    Listening,
    /// 至少一个手机已连接
    Connected,
}

/// 已连接的一台手机（**不含 token**——前端不需要，也不该看到）。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CompanionClient {
    pub id: u64,
    /// 连接建立时刻（ms），给主屏显示「连了多久」。
    pub since_ms: i64,
}

/// 主屏可见的服务状态。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CompanionInfo {
    pub state: CompanionState,
    /// 局域网 IP（仅用于展示「请用手机连同一个 Wi-Fi」）。
    pub lan_ip: Option<String>,
    pub port: u16,
    pub clients: Vec<CompanionClient>,
}

/// 启动/换 token 的返回：前端只拿到 QR 的 SVG 与状态，**拿不到 token 明文**。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CompanionStart {
    pub info: CompanionInfo,
    pub qr_svg: String,
}

// ---------------------------------------------------------------- 服务

/// 一个 WebSocket 连接的写端句柄。
pub(crate) struct ClientHandle {
    pub(crate) tx: mpsc::UnboundedSender<Message>,
    /// 该连接握手时用的 token（换 token 后旧连接要能被顶掉/清掉）。
    pub(crate) token: String,
    pub(crate) since_ms: i64,
}

/// 服务内部状态（锁粒度：一次操作一把锁，绝不在持锁时 await 网络 IO）。
pub(crate) struct Inner {
    pub(crate) token: Option<String>,
    pub(crate) lan_ip: Option<IpAddr>,
    pub(crate) port: u16,
    pub(crate) clients: HashMap<u64, ClientHandle>,
    /// 最新一次广播的卡片：新连上的手机立刻看到当前画面，而不是空白。
    pub(crate) latest: Vec<LiveCard>,
    next_id: u64,
}

impl Inner {
    fn new() -> Self {
        Self {
            token: None,
            lan_ip: None,
            port: COMPANION_PORT,
            clients: HashMap::new(),
            latest: Vec::new(),
            next_id: 0,
        }
    }

    /// 取走全部连接句柄（调用方负责发关闭帧），并清空表。
    fn take_clients(&mut self) -> Vec<ClientHandle> {
        self.clients.drain().map(|(_, h)| h).collect()
    }

    fn next_client_id(&mut self) -> u64 {
        self.next_id += 1;
        self.next_id
    }
}

/// 伴侣服务（Tauri 单例，`.manage(Arc::new(CompanionService::new()))`）。
pub struct CompanionService {
    inner: Arc<RwLock<Inner>>,
    cards_tx: broadcast::Sender<Vec<LiveCard>>,
    shutdown: RwLock<Option<oneshot::Sender<()>>>,
}

impl CompanionService {
    pub fn new() -> Self {
        let (cards_tx, _) = broadcast::channel(CARDS_CHANNEL_CAP);
        Self {
            inner: Arc::new(RwLock::new(Inner::new())),
            cards_tx,
            shutdown: RwLock::new(None),
        }
    }

    /// 启动：挑局域网 IP → 生成 token → 绑定 `lan_ip:54322` → 起接受循环。
    ///
    /// **只走这条路才是合规的**：它不可能绑到 `0.0.0.0` / 回环 / 公网地址上。
    pub async fn start(&self) -> Result<CompanionStart, String> {
        if self.is_running().await {
            return self.start_result().await;
        }
        let lan = local_ip_address::list_afinet_netifas()
            .ok()
            .and_then(|ifas| pick_lan_ip(&ifas))
            .ok_or_else(|| {
                "未发现局域网 IPv4 地址：请连上 Wi-Fi / 有线网后再开启伴侣屏".to_string()
            })?;
        self.start_on(SocketAddr::new(lan, COMPANION_PORT)).await
    }

    /// 在指定地址上启动（token 由 CSPRNG 生成）。
    ///
    /// **只给测试用**（集成测试要绑 127.0.0.1 才跑得起来）。生产路径一律 [`Self::start`]。
    /// 它不做局域网校验——所以别在命令层调用它。
    pub async fn start_on(&self, addr: SocketAddr) -> Result<CompanionStart, String> {
        self.start_on_with_token(addr, &generate_token()).await
    }

    /// 在指定地址、用指定 token 启动。
    ///
    /// **只给测试用**：集成测试要绑 127.0.0.1 且要**知道** token 才能连。
    /// 生产路径走 [`Self::start`]（自选局域网 IP + CSPRNG token）；命令层不许调它。
    pub async fn start_on_with_token(
        &self,
        addr: SocketAddr,
        token: &str,
    ) -> Result<CompanionStart, String> {
        if self.is_running().await {
            return self.start_result().await;
        }
        let token = token.to_string();
        let listener = TcpListener::bind(addr)
            .await
            .map_err(|e| format!("绑定 {addr} 失败：{e}（端口可能被其它程序占用）"))?;
        let bound = listener
            .local_addr()
            .map_err(|e| format!("读取监听地址失败：{e}"))?;

        {
            let mut inner = self.inner.write().await;
            inner.token = Some(token);
            inner.lan_ip = Some(addr.ip());
            inner.port = bound.port();
            inner.clients.clear();
            inner.latest.clear();
        }

        let (shutdown_tx, shutdown_rx) = oneshot::channel();
        *self.shutdown.write().await = Some(shutdown_tx);
        tokio::spawn(server::serve(
            listener,
            Arc::clone(&self.inner),
            self.cards_tx.clone(),
            shutdown_rx,
        ));

        self.start_result().await
    }

    /// 停止：关监听 + 全断 + 清 token。幂等。
    pub async fn stop(&self) {
        if let Some(tx) = self.shutdown.write().await.take() {
            let _ = tx.send(());
        }
        self.close_all("server_stopped").await;
        let mut inner = self.inner.write().await;
        inner.token = None;
        inner.lan_ip = None;
        inner.clients.clear();
        inner.latest.clear();
    }

    /// 吊销单台设备（按连接 id，不给 token 也能吊销）。
    pub async fn revoke_client(&self, client_id: u64) -> bool {
        let handle = { self.inner.write().await.clients.remove(&client_id) };
        match handle {
            None => false,
            Some(h) => {
                let _ = h.tx.send(Message::text(revoked_frame("revoked_by_host")));
                let _ =
                    h.tx.send(close_frame(REVOKED_CLOSE_CODE, "revoked_by_host"));
                true
            }
        }
    }

    /// 换 token：所有现有连接立刻失效（它们的 token 不再匹配），返回新 QR。
    pub async fn rotate_token(&self) -> Result<CompanionStart, String> {
        if !self.is_running().await {
            return Err("伴侣服务未启动，无法刷新二维码".to_string());
        }
        self.close_all("token_rotated").await;
        let token = generate_token();
        self.inner.write().await.token = Some(token);
        self.start_result().await
    }

    /// 主屏卡片更新 → 所有手机。**没有手机连接时是廉价的 no-op**。
    pub fn broadcast_cards(&self, cards: Vec<LiveCard>) {
        // 同步上下文（Tauri 命令）：只能 try_write。抢不到锁就只广播不记快照——
        // 快照只是给后连进来的手机补一帧，丢了不影响实时流。
        if let Ok(mut inner) = self.inner.try_write() {
            inner.latest = cards.clone();
        }
        let _ = self.cards_tx.send(cards);
    }

    /// 当前状态（给主屏显示）。
    pub async fn info(&self) -> CompanionInfo {
        let inner = self.inner.read().await;
        let mut clients: Vec<CompanionClient> = inner
            .clients
            .iter()
            .map(|(id, h)| CompanionClient {
                id: *id,
                since_ms: h.since_ms,
            })
            .collect();
        clients.sort_by_key(|c| c.id);
        let state = if inner.token.is_none() {
            CompanionState::Stopped
        } else if clients.is_empty() {
            CompanionState::Listening
        } else {
            CompanionState::Connected
        };
        CompanionInfo {
            state,
            lan_ip: inner.lan_ip.map(|ip| ip.to_string()),
            port: inner.port,
            clients,
        }
    }

    async fn is_running(&self) -> bool {
        self.inner.read().await.token.is_some()
    }

    async fn close_all(&self, reason: &str) {
        let handles = { self.inner.write().await.take_clients() };
        for h in handles {
            let _ = h.tx.send(Message::text(revoked_frame(reason)));
            let _ = h.tx.send(close_frame(REVOKED_CLOSE_CODE, reason));
        }
    }

    /// 组装启动结果：QR 编码的是**页面 URL**（http），手机扫码直接用浏览器打开。
    async fn start_result(&self) -> Result<CompanionStart, String> {
        let (token, host, port) = {
            let inner = self.inner.read().await;
            match (inner.token.clone(), inner.lan_ip) {
                (Some(token), Some(host)) => (token, host.to_string(), inner.port),
                _ => return Err("伴侣服务未启动".to_string()),
            }
        };
        let qr_svg = render_qr_svg(&build_page_url(&host, port, &token))?;
        Ok(CompanionStart {
            info: self.info().await,
            qr_svg,
        })
    }
}

impl Default for CompanionService {
    fn default() -> Self {
        Self::new()
    }
}

// ---------------------------------------------------------------- Tauri 命令

/// 启动伴侣服务，返回 QR（SVG）与状态。
#[tauri::command]
pub async fn start_companion(app: AppHandle) -> Result<CompanionStart, String> {
    app.state::<Arc<CompanionService>>().start().await
}

/// 停止伴侣服务：关端口 + 全断 + 清 token。
#[tauri::command]
pub async fn stop_companion(app: AppHandle) -> Result<CompanionInfo, String> {
    let service = app.state::<Arc<CompanionService>>();
    service.stop().await;
    Ok(service.info().await)
}

/// 当前状态（主屏轮询用）。
#[tauri::command]
pub async fn get_companion_status(app: AppHandle) -> Result<CompanionInfo, String> {
    Ok(app.state::<Arc<CompanionService>>().info().await)
}

/// 吊销单台设备。
#[tauri::command]
pub async fn revoke_companion_client(app: AppHandle, client_id: u64) -> Result<bool, String> {
    Ok(app
        .state::<Arc<CompanionService>>()
        .revoke_client(client_id)
        .await)
}

/// 刷新二维码（换 token，旧连接全部失效）。
#[tauri::command]
pub async fn rotate_companion_token(app: AppHandle) -> Result<CompanionStart, String> {
    app.state::<Arc<CompanionService>>().rotate_token().await
}

/// 主屏把卡片推给所有手机。**只读镜像**：手机端没有对应的上行能力。
#[tauri::command]
pub async fn broadcast_to_companion(app: AppHandle, cards: Vec<LiveCard>) -> Result<(), String> {
    app.state::<Arc<CompanionService>>().broadcast_cards(cards);
    Ok(())
}
