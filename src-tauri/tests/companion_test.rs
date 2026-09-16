//! S8 手机伴侣测试：`src-tauri/src/companion/`。
//!
//! 分三层，各管一段：
//! 1. **纯函数**（token / URL / QR / 选局域网 IP / 帧格式）—— 不需要网络；
//! 2. **真实连接**（`tokio_tungstenite::connect_async` 连真端口）—— 鉴权、广播、
//!    吊销、断连、同 token 重连；
//! 3. **源码扫描** —— 那些"运行期测不出来"的约束（token 不进日志、不绑 0.0.0.0、
//!    手机页无上行能力、命令已注册）。
//!
//! 跑法（`NO_PROXY` 必带，否则本机端口会被系统代理拦成 502）：
//! ```bash
//! export PATH="/c/Users/Administrator/.cargo/bin:$PATH" NO_PROXY="127.0.0.1,localhost"
//! cargo test --test companion_test
//! ```

use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use interview_copilot_lib::companion::{
    build_page_url, build_ws_url, cards_frame, generate_token, is_lan_ipv4, parse_token,
    pick_lan_ip, render_qr_svg, token_ok, welcome_frame, CompanionService, CompanionState,
    LiveCard, COMPANION_PORT, PROTOCOL_VERSION,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::timeout;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{connect_async, MaybeTlsStream, WebSocketStream};

type Client = WebSocketStream<MaybeTlsStream<TcpStream>>;

const WAIT: Duration = Duration::from_millis(1500);

fn card(id: &str, question: &str) -> LiveCard {
    LiveCard {
        id: id.to_string(),
        question: question.to_string(),
        answer: "三天内".to_string(),
        source: "字段直查".to_string(),
        kind: "direct".to_string(),
        at_ms: 1_700_000_000_000,
        reused: false,
    }
}

/// 起一个服务（绑 127.0.0.1 随机端口，token 固定），返回 (服务, 端口, token)。
async fn spawn(token: &str) -> (CompanionService, u16, String) {
    let service = CompanionService::new();
    let start = service
        .start_on_with_token("127.0.0.1:0".parse().unwrap(), token)
        .await
        .expect("启动伴侣服务");
    let port = start.info.port;
    (service, port, token.to_string())
}

async fn connect(port: u16, token: &str) -> Client {
    let url = build_ws_url("127.0.0.1", port, token);
    let (ws, _) = connect_async(url.as_str()).await.expect("WS 握手应当成功");
    ws
}

/// 等一帧文本；超时/关闭都返回 None。
async fn next_text(ws: &mut Client) -> Option<String> {
    match timeout(WAIT, ws.next()).await {
        Ok(Some(Ok(Message::Text(t)))) => Some(t.to_string()),
        _ => None,
    }
}

/// 一直读到连接关闭，返回期间收到的所有文本帧（用于断言吊销时说了什么）。
async fn drain_text(ws: &mut Client) -> Vec<String> {
    let mut out = Vec::new();
    let deadline = Instant::now() + WAIT;
    while Instant::now() < deadline {
        match timeout(Duration::from_millis(200), ws.next()).await {
            Ok(Some(Ok(Message::Text(t)))) => out.push(t.to_string()),
            Ok(Some(Ok(Message::Close(_)))) | Ok(None) | Ok(Some(Err(_))) => break,
            Err(_) => continue,
            // Ping/Pong/Binary：心跳与二进制帧不参与断言。
            _ => continue,
        }
    }
    out
}

/// 发一个裸 HTTP 请求（绕开任何 HTTP 客户端，也就绕开了系统代理）。
///
/// 端口已经关掉时返回 `None` —— 这本身就是「端口真的放掉了」的证据。
/// Windows 在并发压测时会偶发地给一条刚建好的回环连接甩 RST（与业务无关），
/// 所以写失败重试一次；第二次还失败才当作「端口不在了」。
async fn raw_get(port: u16, target: &str) -> Option<(u16, String)> {
    raw_get_at(SocketAddr::from(([127, 0, 0, 1], port)), target).await
}

/// 同上，但可以指定目标地址（真实局域网用例要连网卡地址，不是回环）。
async fn raw_get_at(addr: SocketAddr, target: &str) -> Option<(u16, String)> {
    for attempt in 0..2 {
        match raw_get_once(addr, target).await {
            Some(result) => return Some(result),
            None if attempt == 0 => continue,
            None => return None,
        }
    }
    None
}

async fn raw_get_once(addr: SocketAddr, target: &str) -> Option<(u16, String)> {
    let mut stream = TcpStream::connect(addr).await.ok()?;
    let request = format!("GET {target} HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).await.is_err() {
        return None;
    }
    let mut buf = Vec::new();
    // 服务端发完就关连接，Windows 上偶尔甩一个 RST 过来；读到的字节已经够断言了。
    let _ = stream.read_to_end(&mut buf).await;
    let text = String::from_utf8_lossy(&buf).to_string();
    let (head, body) = text.split_once("\r\n\r\n").unwrap_or((text.as_str(), ""));
    let status = head
        .split_whitespace()
        .nth(1)
        .and_then(|code| code.parse::<u16>().ok())
        .unwrap_or(0);
    Some((status, body.to_string()))
}

// ------------------------------------------------------------------ 纯函数

#[test]
fn token_is_url_safe_base64_with_256_bits_of_entropy() {
    let seen: std::collections::HashSet<String> = (0..500).map(|_| generate_token()).collect();
    assert_eq!(seen.len(), 500, "token 必须互不重复");
    for token in &seen {
        assert_eq!(token.len(), 43, "32 字节 → 43 字符 base64url");
        assert!(
            token
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_'),
            "token 必须是 URL-safe（才敢直接进 query）：{token}"
        );
    }
}

#[test]
fn token_ok_rejects_everything_but_an_exact_match() {
    let token = generate_token();
    assert!(token_ok(Some(&token), &token));
    assert!(!token_ok(Some(""), &token));
    assert!(!token_ok(None, &token));
    assert!(!token_ok(Some(&token[..42]), &token), "截断一个字符就不算");
    assert!(
        !token_ok(Some("anything"), ""),
        "服务未启动时 expected 为空 —— 谁都不该放进来"
    );
}

#[test]
fn parse_token_reads_only_the_token_key() {
    assert_eq!(parse_token(Some("token=abc")).as_deref(), Some("abc"));
    assert_eq!(
        parse_token(Some("x=1&token=abc&y=2")).as_deref(),
        Some("abc")
    );
    assert_eq!(parse_token(Some("token=")), None, "空值视为没带");
    assert_eq!(parse_token(Some("tok=abc")), None);
    assert_eq!(parse_token(None), None);
}

#[test]
fn urls_carry_the_token_and_nothing_else() {
    let page = build_page_url("192.168.1.5", COMPANION_PORT, "T0KEN");
    let ws = build_ws_url("192.168.1.5", COMPANION_PORT, "T0KEN");
    assert_eq!(page, "http://192.168.1.5:54322/?token=T0KEN");
    assert_eq!(ws, "ws://192.168.1.5:54322/ws?token=T0KEN");
    assert!(page.starts_with("http://"), "扫码的是 http（浏览器才认）");
    assert!(!page.contains("//0.0.0.0"));
}

fn ip(s: &str) -> IpAddr {
    s.parse::<Ipv4Addr>().unwrap().into()
}

#[test]
fn only_private_ipv4_counts_as_lan() {
    assert!(is_lan_ipv4(ip("192.168.1.5")));
    assert!(is_lan_ipv4(ip("10.0.0.7")));
    assert!(is_lan_ipv4(ip("172.16.0.1")));
    assert!(is_lan_ipv4(ip("172.31.255.255")));

    assert!(!is_lan_ipv4(ip("127.0.0.1")), "回环不是局域网出口");
    assert!(!is_lan_ipv4(ip("169.254.7.7")), "link-local 上不了网");
    assert!(!is_lan_ipv4(ip("8.8.8.8")), "公网地址绝不能绑");
    assert!(!is_lan_ipv4(ip("172.32.0.1")), "172.32 已出私有段");
    assert!(!is_lan_ipv4("::1".parse().unwrap()), "IPv6 本期不支持");
}

#[test]
fn pick_lan_ip_skips_loopback_and_public_and_returns_none_when_there_is_no_lan() {
    let mixed = vec![
        ("lo".to_string(), ip("127.0.0.1")),
        ("eth0".to_string(), ip("8.8.8.8")),
        ("wlan0".to_string(), ip("192.168.1.5")),
    ];
    assert_eq!(pick_lan_ip(&mixed), Some(ip("192.168.1.5")));

    let no_lan = vec![
        ("lo".to_string(), ip("127.0.0.1")),
        ("eth0".to_string(), ip("203.0.113.9")),
    ];
    assert_eq!(pick_lan_ip(&no_lan), None, "没有局域网就拒绝启动");
    assert_eq!(pick_lan_ip(&[]), None);
}

#[test]
fn qr_svg_is_deterministic_and_changes_with_the_data() {
    let a = build_page_url("192.168.1.5", COMPANION_PORT, "AAA");
    let b = build_page_url("192.168.1.5", COMPANION_PORT, "BBB");
    let svg_a = render_qr_svg(&a).expect("生成二维码");
    assert_eq!(
        svg_a,
        render_qr_svg(&a).unwrap(),
        "同样的数据必须出同样的图"
    );
    assert_ne!(svg_a, render_qr_svg(&b).unwrap());
    assert!(svg_a.contains("<svg"), "前端要能直接塞进 DOM");
    assert!(svg_a.contains("viewBox"));
    assert!(
        svg_a.len() > 500 && svg_a.contains("path") || svg_a.contains("rect"),
        "二维码里得真的有模块：{svg_a}"
    );
}

#[test]
fn frames_match_the_wire_contract() {
    let welcome: serde_json::Value = serde_json::from_str(&welcome_frame()).unwrap();
    assert_eq!(welcome["type"], "welcome");
    assert_eq!(welcome["protocolVersion"], PROTOCOL_VERSION);

    let frame = cards_frame(&[card("c1", "发货周期？")]).unwrap();
    let value: serde_json::Value = serde_json::from_str(&frame).unwrap();
    assert_eq!(value["type"], "cards");
    assert_eq!(value["cards"][0]["id"], "c1");
    // 契约：前端字段是 atMs（camelCase），写成 at_ms 手机端就收不到时间。
    assert!(value["cards"][0].get("atMs").is_some());
    assert!(value["cards"][0].get("at_ms").is_none());
}

// ------------------------------------------------------------------ 真实连接

#[tokio::test]
async fn http_page_is_served_only_with_a_valid_token() {
    let (service, port, token) = spawn("page-token").await;

    let (status, body) = raw_get(port, &format!("/?token={token}"))
        .await
        .expect("端口应当开着");
    assert_eq!(status, 200, "扫码用的是带 token 的页面地址");
    assert!(body.contains("InterviewCopilot"));

    assert_eq!(
        raw_get(port, "/").await.expect("端口应当开着").0,
        401,
        "没 token 不给页面"
    );
    assert_eq!(
        raw_get(port, "/?token=wrong")
            .await
            .expect("端口应当开着")
            .0,
        401
    );
    assert_eq!(
        raw_get(port, &format!("/nope?token={token}"))
            .await
            .expect("端口应当开着")
            .0,
        404
    );

    service.stop().await;
    assert!(
        raw_get(port, &format!("/?token={token}")).await.is_none(),
        "停止后端口必须关掉（不是只拒答，是根本连不上）"
    );
}

#[tokio::test]
async fn ws_handshake_rejects_missing_and_wrong_tokens() {
    let (service, port, _) = spawn("secret").await;

    let url = format!("ws://127.0.0.1:{port}/ws");
    assert!(
        connect_async(url.as_str()).await.is_err(),
        "不带 token 的握手必须失败"
    );

    let wrong = build_ws_url("127.0.0.1", port, "not-the-token");
    assert!(
        connect_async(wrong.as_str()).await.is_err(),
        "错 token 必须失败"
    );

    // 服务停掉之后，连对的 token 也进不来。
    service.stop().await;
    let right = build_ws_url("127.0.0.1", port, "secret");
    assert!(connect_async(right.as_str()).await.is_err());
}

#[tokio::test]
async fn ws_client_gets_welcome_then_cards() {
    let (service, port, token) = spawn("good").await;
    let mut ws = connect(port, &token).await;

    let welcome = next_text(&mut ws).await.expect("连上先收到 welcome");
    let value: serde_json::Value = serde_json::from_str(&welcome).unwrap();
    assert_eq!(value["type"], "welcome");
    assert_eq!(value["protocolVersion"], 1);

    let info = service.info().await;
    assert_eq!(info.state, CompanionState::Connected);
    assert_eq!(info.clients.len(), 1);

    service.broadcast_cards(vec![card("c1", "发货周期？"), card("c2", "退货政策")]);
    let frame = next_text(&mut ws).await.expect("广播要到达手机");
    let value: serde_json::Value = serde_json::from_str(&frame).unwrap();
    assert_eq!(value["type"], "cards");
    assert_eq!(value["cards"].as_array().unwrap().len(), 2);
    assert_eq!(value["cards"][0]["question"], "发货周期？");

    service.stop().await;
}

#[tokio::test]
async fn broadcast_reaches_the_client_and_a_late_joiner_gets_a_snapshot() {
    let (service, port, token) = spawn("multi").await;
    let mut first = connect(port, &token).await;
    let _ = next_text(&mut first).await;

    service.broadcast_cards(vec![card("c1", "问题一")]);
    let frame = next_text(&mut first).await.expect("已连接的那台要收到");
    assert!(frame.contains("问题一"));

    // 后连进来的手机不该看着空白等下一次提问 —— 补一帧最新快照。
    let mut late = connect(port, &token).await;
    let _ = next_text(&mut late).await;
    let snapshot = next_text(&mut late).await.expect("新连接要收到当前画面");
    let value: serde_json::Value = serde_json::from_str(&snapshot).unwrap();
    assert_eq!(value["type"], "cards");
    assert_eq!(value["cards"][0]["question"], "问题一");

    service.stop().await;
}

#[tokio::test]
async fn uplink_frames_are_ignored_the_second_screen_is_read_only() {
    let (service, port, token) = spawn("readonly").await;
    let mut ws = connect(port, &token).await;
    let _ = next_text(&mut ws).await;

    // 手机端就算发东西上来，服务端也只当没看见（不解析、不执行、不断开）。
    ws.send(Message::text("{\"type\":\"steal\"}"))
        .await
        .unwrap();
    service.broadcast_cards(vec![card("c1", "仍然收得到吗")]);
    let frame = next_text(&mut ws).await.expect("上行帧不该影响下行");
    assert!(frame.contains("仍然收得到吗"));

    service.stop().await;
}

#[tokio::test]
async fn revoking_a_device_is_not_the_same_as_stopping_the_service() {
    let (service, port, token) = spawn("revoke").await;
    let mut victim = connect(port, &token).await;
    let _ = next_text(&mut victim).await;

    let victim_id = service.info().await.clients[0].id;
    assert!(service.revoke_client(victim_id).await);

    let said = drain_text(&mut victim).await;
    assert!(
        said.iter().any(|f| f.contains("revoked_by_host")),
        "吊销前要先说一句为什么：{said:?}"
    );

    // 关键区别：吊销只踢这一台，**服务还在监听**，重新扫码还能连上。
    let after = service.info().await;
    assert!(after.clients.is_empty());
    assert_eq!(after.state, CompanionState::Listening, "吊销 ≠ 停止服务");

    let mut back = connect(port, &token).await;
    assert!(next_text(&mut back).await.is_some(), "同一个二维码还能再用");

    assert!(
        !service.revoke_client(999).await,
        "吊销不存在的 id 返回 false"
    );
    service.stop().await;
}

#[tokio::test]
async fn stop_closes_everyone_and_frees_the_port() {
    let (service, port, token) = spawn("stop").await;
    let mut ws = connect(port, &token).await;
    let _ = next_text(&mut ws).await;

    service.stop().await;
    let said = drain_text(&mut ws).await;
    assert!(said.iter().any(|f| f.contains("server_stopped")));

    let info = service.info().await;
    assert_eq!(info.state, CompanionState::Stopped);
    assert!(info.clients.is_empty());

    // 端口真的放掉了：同一个端口能立刻重新绑上。
    let start = service
        .start_on_with_token(SocketAddr::from(([127, 0, 0, 1], port)), token.as_str())
        .await
        .expect("停止后端口必须可复用");
    assert_eq!(start.info.port, port);
    assert_ne!(start.qr_svg, "", "新会话要出新二维码");
    service.stop().await;
}

#[tokio::test]
async fn a_dropped_client_leaves_no_ghost_in_the_list() {
    let (service, port, token) = spawn("drop").await;
    let mut ws = connect(port, &token).await;
    let _ = next_text(&mut ws).await;
    assert_eq!(service.info().await.clients.len(), 1);

    let _ = ws.close(None).await;
    // 等服务端那条 accept 任务跑到清理代码。
    for _ in 0..20 {
        if service.info().await.clients.is_empty() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let info = service.info().await;
    assert!(info.clients.is_empty(), "手机关页面后不该留幽灵连接");
    assert_eq!(info.state, CompanionState::Listening, "回到等人来连的状态");
    service.stop().await;
}

#[tokio::test]
async fn reconnecting_with_the_same_token_replaces_the_old_connection() {
    let (service, port, token) = spawn("lww").await;
    let mut old = connect(port, &token).await;
    let _ = next_text(&mut old).await;

    let mut new = connect(port, &token).await;
    let _ = next_text(&mut new).await;

    let said = drain_text(&mut old).await;
    assert!(
        said.iter()
            .any(|f| f.contains("replaced_by_new_connection")),
        "旧连接要被顶掉并说明理由：{said:?}"
    );
    assert_eq!(
        service.info().await.clients.len(),
        1,
        "同一 token 只允许一个活连接（主屏看得到数量）"
    );
    service.stop().await;
}

#[tokio::test]
async fn starting_twice_keeps_one_session() {
    let (service, port, token) = spawn("idem").await;
    let first = service
        .start_on_with_token(SocketAddr::from(([127, 0, 0, 1], port)), token.as_str())
        .await
        .expect("重复启动应当幂等");
    // 同一个 token → 同一个 URL → 同一张图：说明没有偷偷换会话。
    let again = service.info().await;
    assert_eq!(again.port, port);
    assert_eq!(first.info.state, CompanionState::Listening);
    service.stop().await;
}

#[tokio::test]
async fn rotating_the_token_renders_the_old_one_useless() {
    let (service, port, token) = spawn("rotate").await;
    let mut ws = connect(port, &token).await;
    let _ = next_text(&mut ws).await;

    let rotated = service.rotate_token().await.expect("换 token");
    let said = drain_text(&mut ws).await;
    assert!(said.iter().any(|f| f.contains("token_rotated")));

    let old_url = build_ws_url("127.0.0.1", port, &token);
    assert!(
        connect_async(old_url.as_str()).await.is_err(),
        "换过 token 之后旧 token 进不来"
    );
    assert_ne!(
        rotated.qr_svg,
        render_qr_svg(&build_page_url("127.0.0.1", port, &token)).unwrap()
    );
    service.stop().await;
}

// ---------------------------------------------------------- 真实局域网路径
//
// 上面那些连接用例全跑在 127.0.0.1 上——那是**测试缝**（`start_on_with_token`）。
// 手机走的是另一条路：绑网卡地址、跨设备连进来。这一段专门钉它：
// 回环上能跑，不等于手机能连（绑错地址、被防火墙拦、端口被占都是这条路才出的问题）。
// 没有局域网地址的机器（部分 CI 只有回环）会打印跳过并当成通过，不是失败。

/// 本机真实的局域网 IPv4；挑不到就 `None`。
fn real_lan_ip() -> Option<IpAddr> {
    local_ip_address::list_afinet_netifas()
        .ok()
        .and_then(|ifas| pick_lan_ip(&ifas))
}

#[tokio::test]
async fn start_binds_a_private_lan_ip_and_refuses_when_there_is_none() {
    let service = CompanionService::new();

    if real_lan_ip().is_none() {
        // 没有局域网就 fail-closed：不许退回回环，更不许退回通配地址。
        let err = service
            .start()
            .await
            .expect_err("没有局域网地址时必须拒绝启动");
        assert!(err.contains("局域网"), "错误信息要说清原因：{err}");
        assert_eq!(service.info().await.state, CompanionState::Stopped);
        return;
    }

    let start = match service.start().await {
        Ok(start) => start,
        // 固定端口被别人占着时属环境问题，不假装失败（另有用例专门钉这个）。
        Err(e) if e.contains("54322") => {
            eprintln!("跳过：54322 已被占用（{e}）");
            return;
        }
        Err(e) => panic!("有局域网地址却启动失败：{e}"),
    };

    let lan: IpAddr = start
        .info
        .lan_ip
        .as_ref()
        .expect("启动后必须报告绑到了哪个地址")
        .parse()
        .unwrap();
    assert!(
        is_lan_ipv4(lan),
        "start() 只能绑私有 IPv4 —— 实际绑了 {lan}"
    );
    assert_eq!(start.info.port, COMPANION_PORT, "生产路径用固定端口");
    assert_eq!(start.info.state, CompanionState::Listening);
    assert!(
        start.qr_svg.contains("<svg"),
        "要给出能直接塞进 DOM 的二维码"
    );

    service.stop().await;
}

#[tokio::test]
async fn the_lan_interface_serves_the_page_and_the_card_stream() {
    let Some(lan) = real_lan_ip() else {
        eprintln!("跳过：本机没有局域网 IPv4 地址");
        return;
    };
    let service = CompanionService::new();
    // 地址用真网卡（端口随机，免得跟真机上的 54322 打架）；
    // token 用已知值——测试得能连进去，生产路径的 token 是 CSPRNG 且**不暴露给前端**。
    let token = generate_token();
    let start = service
        .start_on_with_token(SocketAddr::new(lan, 0), &token)
        .await
        .expect("绑局域网地址应当成功");
    let addr = SocketAddr::new(lan, start.info.port);

    let (status, body) = raw_get_at(addr, &format!("/?token={token}"))
        .await
        .unwrap_or_else(|| panic!("局域网地址 {addr} 上应当取得到页面（取不到 → 真机也连不上）"));
    assert_eq!(status, 200, "扫码打开的就是这个地址");
    assert!(body.contains("InterviewCopilot"));
    assert_eq!(
        raw_get_at(addr, "/").await.expect("端口应当开着").0,
        401,
        "从局域网上进来的请求一样要验 token"
    );

    // 手机就是这么连的：ws://<局域网 IP>:<port>/ws?token=…
    let (mut ws, _) =
        connect_async(build_ws_url(&lan.to_string(), start.info.port, &token).as_str())
            .await
            .expect("从局域网地址握手应当成功");
    let welcome = next_text(&mut ws).await.expect("连上先收到 welcome");
    assert!(welcome.contains("welcome"));

    service.broadcast_cards(vec![card("lan1", "局域网里也收得到吗")]);
    let frame = next_text(&mut ws).await.expect("卡片要到达手机");
    assert!(frame.contains("局域网里也收得到吗"));

    service.stop().await;
    assert!(
        raw_get_at(addr, "/").await.is_none(),
        "停止后局域网端口也要关掉（不是只拒答）"
    );
}

#[tokio::test]
async fn a_busy_production_port_fails_loudly_instead_of_silently_moving() {
    let Some(lan) = real_lan_ip() else {
        eprintln!("跳过：本机没有局域网 IPv4 地址");
        return;
    };
    // 先把固定端口占住（模拟 54322 上已经有别的服务）。
    let holder = match std::net::TcpListener::bind(SocketAddr::new(lan, COMPANION_PORT)) {
        Ok(listener) => listener,
        Err(_) => {
            eprintln!("跳过：54322 已被其它进程占用，无法制造占用场景");
            return;
        }
    };

    let service = CompanionService::new();
    let err = service
        .start()
        .await
        .expect_err("端口被占用时必须报错，不能偷偷换端口继续跑");
    assert!(err.contains("54322"), "错误信息里要带上端口：{err}");
    assert_eq!(
        service.info().await.state,
        CompanionState::Stopped,
        "启动失败不能留下半开状态"
    );
    drop(holder);
}

// ------------------------------------------------------------------ 源码扫描

fn read_source(path: &str) -> String {
    std::fs::read_to_string(path).unwrap_or_else(|e| panic!("读 {path} 失败：{e}"))
}

/// 去掉注释行后的源码（这些断言针对的是**代码**，文档里提到某个词不算违规）。
fn code_lines(source: &str) -> Vec<&str> {
    source
        .lines()
        .filter(|line| {
            let t = line.trim_start();
            !(t.starts_with("//") || t.starts_with("/*") || t.starts_with('*'))
        })
        .collect()
}

#[test]
fn no_token_is_ever_written_to_a_log() {
    for path in [
        "src/companion/mod.rs",
        "src/companion/server.rs",
        "src/companion/phone_page.rs",
    ] {
        let source = read_source(path);
        for line in code_lines(&source) {
            let trimmed = line.trim_start();
            let is_log = trimmed.starts_with("eprintln!")
                || trimmed.starts_with("println!")
                || trimmed.starts_with("log::")
                || trimmed.starts_with("debug!");
            if is_log {
                assert!(
                    !line.to_ascii_lowercase().contains("token"),
                    "{path} 把 token 写进了日志：{line}"
                );
            }
        }
    }
}

#[test]
fn nothing_binds_the_wildcard_address() {
    for path in ["src/companion/mod.rs", "src/companion/server.rs"] {
        let source = read_source(path);
        for line in code_lines(&source) {
            assert!(
                !line.contains("0.0.0.0"),
                "{path} 绑了通配地址：伴侣服务只许绑局域网 IP —— {line}"
            );
        }
    }
}

#[test]
fn the_phone_page_has_no_uplink_capability() {
    let source = read_source("src/companion/phone_page.rs");
    let page = code_lines(&source).join("\n");
    for forbidden in [".send(", "fetch(", "XMLHttpRequest", "navigator.sendBeacon"] {
        assert!(
            !page.contains(forbidden),
            "手机页出现了 {forbidden}：二屏必须是只读镜像"
        );
    }
    assert!(page.contains("onmessage"), "总得收消息吧");
}

#[test]
fn all_companion_commands_are_registered() {
    let lib = read_source("src/lib.rs");
    let handler = lib
        .split_once("invoke_handler(")
        .map(|(_, rest)| rest)
        .expect("lib.rs 里应该有 invoke_handler");
    for command in [
        "start_companion",
        "stop_companion",
        "get_companion_status",
        "revoke_companion_client",
        "rotate_companion_token",
        "broadcast_to_companion",
    ] {
        assert!(
            handler.contains(command),
            "{command} 没有注册到 invoke_handler"
        );
    }
}

#[test]
fn the_service_stops_on_app_exit() {
    let lib = read_source("src/lib.rs");
    let exit = lib
        .split_once("RunEvent::Exit")
        .map(|(_, rest)| rest)
        .expect("lib.rs 里应该有退出处理");
    assert!(
        exit.contains("CompanionService") && exit.contains("stop"),
        "退出时没有停伴侣服务：端口会留在那儿，连接也不会断"
    );
}
