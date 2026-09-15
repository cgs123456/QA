//! 真实 sidecar 对端：起 `scripts/serve_audio_e2e.py`（真 uvicorn + 真 ASGI 路由）。
//!
//! 给 `audio_ws_e2e.rs`（跨语言链路）与 `inject_e2e.rs`（注入全链）共用。
//! 抽出来是因为**"怎么算起好了"这件事只能有一份定义**：就绪判据是
//! `GET /health` 回显本次的 nonce（TOCTOU 安全），改一处就得两处同步的话，
//! 迟早出现"一个测试等对了、另一个在等一个已被复用的端口"。
//!
//! 两个被它钉住的事实（都是真 ASGI 才看得见的）：
//! - **`/audio/stream` 需要 WS 实现**：uvicorn 的 `ws="auto"` 在既没装
//!   `websockets` 也没装 `wsproto` 时不会优雅降级，而是对升级请求回 **404**，
//!   整条音频路直接死掉。依赖已在 `sidecar/requirements-lock.txt` 声明。
//! - **鉴权拒绝表现为 HTTP 403，不是 WS close 1008**：Starlette 在 `accept()`
//!   之前 `close()`，到了真 ASGI 服务器就是一个普通 HTTP 响应。`TestClient`
//!   会短路成 1008，所以 Python 套件里 `code == 1008` 的断言在真客户端看来是 403。
//!
//! 需要一个装了 sidecar 依赖的 Python：`INTERVIEWCOPILOT_PYTHON`，否则用 `python`。
//! 设 `INTERVIEWCOPILOT_SKIP_E2E=1` 直接跳过。

use std::io::{BufRead, BufReader};
use std::net::TcpListener;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::Value;

/// 与 `scripts/serve_audio_e2e.py --token` 同值；`AudioUplink` 用它握手。
pub const TOKEN: &str = "e2e-token-abcdefghijklmnopqrstuvwxyz012345";
/// 启动 nonce：`/health` 回显它，用来确认这个端口是我们的进程。
pub const NONCE: &str = "audio-e2e";
const START_TIMEOUT: Duration = Duration::from_secs(30);

/// 退出时一定要杀掉 sidecar（测试 panic 也要），否则会留下占着端口的孤儿。
pub struct Sidecar {
    pub child: Child,
    pub port: u16,
}

impl Drop for Sidecar {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

pub fn repo_root() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src-tauri has a parent")
        .to_path_buf()
}

pub fn python_exe() -> String {
    std::env::var("INTERVIEWCOPILOT_PYTHON").unwrap_or_else(|_| "python".to_string())
}

pub fn free_port() -> u16 {
    let l = TcpListener::bind("127.0.0.1:0").expect("bind ephemeral");
    let port = l.local_addr().unwrap().port();
    drop(l);
    port
}

/// 环境里明确要求跳过时返回 true。
pub fn skipped() -> bool {
    std::env::var("INTERVIEWCOPILOT_SKIP_E2E").is_ok()
}

/// 起 uvicorn 并等到 `/health` 回显我们的 nonce。
///
/// 返回 `None` = **跳过**（脚本不存在或解释器起不来），调用方直接 return。
/// 起得来但一直不健康 = panic（那是真故障，不该被当成"跳过"）。
pub async fn start_sidecar(extra_args: &[&str]) -> Option<Sidecar> {
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

    // 启动期崩掉就把 stderr 打出来。
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
