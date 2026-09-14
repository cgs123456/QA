//! Sidecar process lifecycle (R7).
//!
//! - Dev mode launches the interpreter from `INTERVIEWCOPILOT_PYTHON`
//!   (default `python`) with the entry from `INTERVIEWCOPILOT_SIDECAR_ENTRY`
//!   (default `<manifest>/../sidecar/src/main.py`).
//! - Stdout handshake is read with a 10 s timeout and validated via
//!   [`crate::sidecar::protocol::parse_handshake`]. A version mismatch kills
//!   the child and enters degraded mode (`sidecar://degraded`,
//!   reason `version_mismatch`) — no restart, the same binary would mismatch
//!   forever.
//! - A supervisor loop polls `/health` every 1 s, detects crashes, and
//!   restarts with exponential backoff 1 s / 2 s / 4 s (`MAX_RESTARTS = 3`).
//!   The consecutive-failure counter resets to zero whenever health recovers.
//! - App exit (`RunEvent::Exit`, wired in lib.rs) kills the whole sidecar
//!   process tree via [`kill_sidecar_tree_blocking`].
//!
//! R8: the token is stored in memory only and NEVER appears in logs,
//! events, or error strings — only port / counts / reasons are logged.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};

use super::degradation::{
    DegradedInfo, DegradedSink, DEGRADED_EVENT, REASON_RESTART_EXHAUSTED,
    REASON_VERSION_MISMATCH, handle_handshake_result,
};
use super::protocol::{parse_handshake, HandshakeError, SidecarHandshake};

/// How long to wait for the sidecar's first stdout handshake line.
pub const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
/// Health poll interval.
pub const HEALTH_POLL_INTERVAL: Duration = Duration::from_secs(1);
/// Max consecutive restarts before entering degraded mode.
pub const MAX_RESTARTS: u32 = 3;
/// Consecutive failed health polls that mark a live process as unhealthy.
pub const MAX_CONSECUTIVE_HEALTH_FAILURES: u32 = 3;
/// Backoff delays for consecutive failures 1 / 2 / 3 (saturates at 4 s).
pub fn backoff_for_consecutive_failures(n: u32) -> Duration {
    match n {
        1 => Duration::from_secs(1),
        2 => Duration::from_secs(2),
        _ => Duration::from_secs(4),
    }
}

static SUPERVISOR_RUNNING: AtomicBool = AtomicBool::new(false);

/// Typed spawn/handshake failures. Never carries the token (R8:
///
/// `InvalidHandshake` only forwards the parse error, which contains no
/// request/response bodies).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SpawnError {
    SpawnFailed(String),
    HandshakeTimeout,
    StdoutClosed,
    EmptyHandshake,
    InvalidHandshake(HandshakeError),
}

impl std::fmt::Display for SpawnError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SpawnError::SpawnFailed(e) => write!(f, "failed to spawn sidecar: {e}"),
            SpawnError::HandshakeTimeout => {
                write!(f, "timed out waiting for sidecar handshake (10s)")
            }
            SpawnError::StdoutClosed => {
                write!(f, "sidecar stdout closed before handshake")
            }
            SpawnError::EmptyHandshake => {
                write!(f, "sidecar printed empty handshake line")
            }
            SpawnError::InvalidHandshake(e) => write!(f, "sidecar handshake invalid: {e}"),
        }
    }
}

impl std::error::Error for SpawnError {}

pub fn is_version_mismatch(e: &SpawnError) -> bool {
    matches!(
        e,
        SpawnError::InvalidHandshake(HandshakeError::VersionMismatch { .. })
    )
}

#[derive(Debug, Default)]
pub struct SidecarState {
    handshake: tokio::sync::Mutex<Option<SidecarHandshake>>,
    degraded: tokio::sync::Mutex<Option<DegradedInfo>>,
    child_pid: std::sync::Mutex<Option<u32>>,
}

impl SidecarState {
    pub async fn snapshot(&self) -> Option<SidecarHandshake> {
        self.handshake.lock().await.clone()
    }

    pub async fn get_degraded(&self) -> Option<DegradedInfo> {
        self.degraded.lock().await.clone()
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct SidecarStatus {
    pub connected: bool,
    pub port: Option<u16>,
}

/// In-memory credentials for the frontend HTTP client (R8: IPC only,
///
/// never persisted).
#[derive(Debug, Clone, Serialize)]
pub struct SidecarCredentials {
    pub port: u16,
    pub token: String,
}

#[tauri::command]
pub async fn get_sidecar_status(
    state: tauri::State<'_, SidecarState>,
) -> Result<SidecarStatus, String> {
    let snap = state.snapshot().await;
    let degraded = state.get_degraded().await;
    Ok(SidecarStatus {
        connected: snap.is_some() && degraded.is_none(),
        port: snap.map(|h| h.port),
    })
}

#[tauri::command]
pub async fn get_sidecar_credentials(
    state: tauri::State<'_, SidecarState>,
) -> Result<SidecarCredentials, String> {
    let snap = state.snapshot().await;
    match snap {
        Some(h) => Ok(SidecarCredentials {
            port: h.port,
            token: h.auth_token,
        }),
        None => Err("sidecar not connected".to_string()),
    }
}

#[tauri::command]
pub async fn get_sidecar_degraded(
    state: tauri::State<'_, SidecarState>,
) -> Result<Option<DegradedInfo>, String> {
    Ok(state.get_degraded().await)
}

/// Manual retry from the degraded page. No-op when the supervisor is
/// already running.
#[tauri::command]
pub async fn retry_sidecar_start(app: AppHandle) -> Result<String, String> {
    if SUPERVISOR_RUNNING.swap(true, Ordering::SeqCst) {
        return Ok("supervisor already running".to_string());
    }
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        supervise(handle).await;
    });
    Ok("supervisor restarting".to_string())
}

/// Production [`DegradedSink`]: emits the Tauri degraded event.
struct AppSink(AppHandle);

impl DegradedSink for AppSink {
    fn emit_degraded(&self, reason: &str, detail: &str) {
        let _ = self.0.emit(
            DEGRADED_EVENT,
            DegradedInfo {
                reason: reason.to_string(),
                detail: detail.to_string(),
            },
        );
    }
}

fn python_interpreter() -> String {
    std::env::var("INTERVIEWCOPILOT_PYTHON").unwrap_or_else(|_| "python".to_string())
}

fn sidecar_entry() -> PathBuf {
    if let Ok(p) = std::env::var("INTERVIEWCOPILOT_SIDECAR_ENTRY") {
        return PathBuf::from(p);
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("sidecar")
        .join("src")
        .join("main.py")
}

fn with_state<T>(app: &AppHandle, f: impl FnOnce(&SidecarState) -> T) -> Option<T> {
    app.try_state::<SidecarState>().map(|s| f(&s))
}

async fn set_handshake(app: &AppHandle, hs: SidecarHandshake) {
    if let Some(state) = app.try_state::<SidecarState>() {
        *state.handshake.lock().await = Some(hs);
    }
}

async fn set_degraded_state(app: &AppHandle, reason: &str, detail: &str) {
    if let Some(state) = app.try_state::<SidecarState>() {
        *state.handshake.lock().await = None;
        *state.degraded.lock().await = Some(DegradedInfo {
            reason: reason.to_string(),
            detail: detail.to_string(),
        });
    }
}

async fn clear_degraded(app: &AppHandle) {
    if let Some(state) = app.try_state::<SidecarState>() {
        *state.degraded.lock().await = None;
    }
}

async fn set_pid(app: &AppHandle, pid: u32) {
    with_state(app, |s| {
        *s.child_pid.lock().unwrap() = Some(pid);
    });
}

async fn clear_pid(app: &AppHandle) {
    with_state(app, |s| {
        *s.child_pid.lock().unwrap() = None;
    });
}

async fn clear_pid_if(app: &AppHandle, pid: Option<u32>) {
    with_state(app, |s| {
        let mut guard = s.child_pid.lock().unwrap();
        if *guard == pid {
            *guard = None;
        }
    });
}

/// Spawn the sidecar and resolve its stdout handshake.
///
/// Pitfalls handled: stdout line must be flushed by the sidecar
/// (`print(..., flush=True)`); bind failure exits non-zero before any
/// handshake is printed, so it surfaces as timeout/closed stdout. On ANY
/// handshake failure the child is killed so no orphan survives.
pub async fn spawn_and_handshake(
) -> Result<(tokio::process::Child, SidecarHandshake), SpawnError> {
    let python = python_interpreter();
    let entry = sidecar_entry();
    let mut child = tokio::process::Command::new(&python)
        .arg(&entry)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::inherit())
        .spawn()
        .map_err(|e| SpawnError::SpawnFailed(format!("{python} {entry:?}: {e}")))?;

    let stdout = match child.stdout.take() {
        Some(s) => s,
        None => {
            let _ = child.kill().await;
            return Err(SpawnError::StdoutClosed);
        }
    };
    let mut lines = BufReader::new(stdout).lines();

    let first_line = tokio::time::timeout(HANDSHAKE_TIMEOUT, lines.next_line())
        .await
        .map_err(|_| SpawnError::HandshakeTimeout);
    let first_line = match first_line {
        Ok(Ok(Some(line))) => line,
        Ok(Ok(None)) | Ok(Err(_)) => {
            let _ = child.kill().await;
            return Err(SpawnError::StdoutClosed);
        }
        Err(e) => {
            let _ = child.kill().await;
            return Err(e);
        }
    };

    if first_line.trim().is_empty() {
        let _ = child.kill().await;
        return Err(SpawnError::EmptyHandshake);
    }

    match parse_handshake(first_line.trim()) {
        Ok(hs) => {
            // R8: log port/capabilities only — never the token.
            println!(
                "[sidecar] handshake parsed: port={} caps={:?}",
                hs.port, hs.capabilities
            );
            Ok((child, hs))
        }
        Err(e) => {
            let _ = child.kill().await;
            Err(SpawnError::InvalidHandshake(e))
        }
    }
}

/// Minimal `/health` probe over TCP (`/health` needs no token, R2).
async fn health_ok(port: u16) -> bool {
    let probe = async {
        let mut stream = tokio::net::TcpStream::connect(("127.0.0.1", port)).await?;
        stream
            .write_all(
                b"GET /health HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n",
            )
            .await?;
        let mut resp = Vec::new();
        let mut chunk = vec![0u8; 1024];
        loop {
            let n = stream.read(&mut chunk).await?;
            if n == 0 {
                break;
            }
            resp.extend_from_slice(&chunk[..n]);
            if resp.len() > 4096 || resp.windows(4).any(|w| w == b"\r\n\r\n") {
                break;
            }
        }
        Ok::<bool, std::io::Error>(
            resp.starts_with(b"HTTP/1.0 200") || resp.starts_with(b"HTTP/1.1 200"),
        )
    };
    matches!(
        tokio::time::timeout(Duration::from_millis(800), probe).await,
        Ok(Ok(true))
    )
}

enum MonitorOutcome {
    Exited { healthy_seen: bool },
    Unhealthy { healthy_seen: bool },
}

/// Watch one child: 1 s health polls + exit detection.
///
/// `failures` (the supervisor's consecutive-failure counter) resets to
/// zero on every successful health poll — the required "清零 on recover".
async fn monitor_child(
    app: &AppHandle,
    mut child: tokio::process::Child,
    port: u16,
    failures: &mut u32,
) -> MonitorOutcome {
    if let Some(pid) = child.id() {
        set_pid(app, pid).await;
    }
    let mut healthy_seen = false;
    let mut consec_fail = 0u32;
    loop {
        tokio::time::sleep(HEALTH_POLL_INTERVAL).await;
        match child.try_wait() {
            Ok(Some(status)) => {
                println!("[sidecar] process exited: {status}");
                clear_pid_if(app, child.id()).await;
                return MonitorOutcome::Exited { healthy_seen };
            }
            Ok(None) => {}
            Err(e) => {
                println!("[sidecar] process wait error: {e}");
                clear_pid(app).await;
                return MonitorOutcome::Exited { healthy_seen };
            }
        }
        if health_ok(port).await {
            if !healthy_seen {
                println!("[sidecar] health recovered");
            }
            healthy_seen = true;
            consec_fail = 0;
            *failures = 0;
        } else {
            consec_fail += 1;
            if consec_fail >= MAX_CONSECUTIVE_HEALTH_FAILURES {
                println!("[sidecar] health check failed {consec_fail}x; killing sidecar");
                let _ = child.kill().await;
                let _ = child.wait().await;
                clear_pid(app).await;
                return MonitorOutcome::Unhealthy { healthy_seen };
            }
        }
    }
}

/// Supervisor: spawn → monitor → exponential-backoff restart (≤3) → degraded.
pub async fn supervise(app: AppHandle) {
    SUPERVISOR_RUNNING.store(true, Ordering::SeqCst);
    let mut failures: u32 = 0;
    loop {
        match spawn_and_handshake().await {
            Ok((child, hs)) => {
                let port = hs.port;
                set_handshake(&app, hs).await;
                clear_degraded(&app).await;
                let outcome = monitor_child(&app, child, port, &mut failures).await;
                let healthy_seen = match outcome {
                    MonitorOutcome::Exited { healthy_seen } => healthy_seen,
                    MonitorOutcome::Unhealthy { healthy_seen } => healthy_seen,
                };
                failures = if healthy_seen { 1 } else { failures + 1 };
                if failures > MAX_RESTARTS {
                    let detail =
                        format!("sidecar failed {failures} times in a row; giving up");
                    println!("[sidecar] {detail}");
                    set_degraded_state(&app, REASON_RESTART_EXHAUSTED, &detail).await;
                    if let Some(state) = app.try_state::<SidecarState>() {
                        let info = state.get_degraded().await;
                        if let Some(info) = info {
                            let _ = app.emit(DEGRADED_EVENT, info);
                        }
                    }
                    break;
                }
                let wait = backoff_for_consecutive_failures(failures);
                println!(
                    "[sidecar] restart {failures}/{MAX_RESTARTS} in {}s",
                    wait.as_secs()
                );
                tokio::time::sleep(wait).await;
            }
            Err(e) => {
                if let SpawnError::InvalidHandshake(ref he) = e {
                    // Tested path: emits sidecar://degraded(version_mismatch).
                    let mapped =
                        handle_handshake_result(&AppSink(app.clone()), &Err(he.clone()));
                    if mapped.as_deref() == Some(REASON_VERSION_MISMATCH) {
                        set_degraded_state(&app, REASON_VERSION_MISMATCH, &e.to_string())
                            .await;
                        break;
                    }
                }
                failures += 1;
                if failures > MAX_RESTARTS {
                    let detail = format!("sidecar spawn failed {failures}x: {e}");
                    println!("[sidecar] {detail}");
                    set_degraded_state(&app, REASON_RESTART_EXHAUSTED, &detail).await;
                    if let Some(state) = app.try_state::<SidecarState>() {
                        let info = state.get_degraded().await;
                        if let Some(info) = info {
                            let _ = app.emit(DEGRADED_EVENT, info);
                        }
                    }
                    break;
                }
                let wait = backoff_for_consecutive_failures(failures);
                println!(
                    "[sidecar] spawn failed ({e}); restart {failures}/{MAX_RESTARTS} in {}s",
                    wait.as_secs()
                );
                tokio::time::sleep(wait).await;
            }
        }
    }
    SUPERVISOR_RUNNING.store(false, Ordering::SeqCst);
}

/// Kill the sidecar process tree. Called from `RunEvent::Exit` (lib.rs) in
/// a synchronous context — reads the recorded pid via a std mutex and
/// shells out to `taskkill /T /F` (Windows) so no python process survives
/// app exit.
pub fn kill_sidecar_tree_blocking(app: &AppHandle) {
    let pid = app
        .try_state::<SidecarState>()
        .and_then(|s| s.child_pid.lock().unwrap().take());
    match pid {
        Some(p) => {
            println!("[sidecar] exit cleanup: killing process tree pid={p}");
            kill_tree(p);
        }
        None => println!("[sidecar] exit cleanup: no child pid recorded"),
    }
}

#[cfg(windows)]
fn kill_tree(pid: u32) {
    let _ = std::process::Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .output();
}

#[cfg(not(windows))]
fn kill_tree(pid: u32) {
    let _ = std::process::Command::new("kill")
        .args(["-9", &pid.to_string()])
        .output();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_schedule_is_1s_2s_4s_saturating() {
        assert_eq!(backoff_for_consecutive_failures(1), Duration::from_secs(1));
        assert_eq!(backoff_for_consecutive_failures(2), Duration::from_secs(2));
        assert_eq!(backoff_for_consecutive_failures(3), Duration::from_secs(4));
        assert_eq!(backoff_for_consecutive_failures(4), Duration::from_secs(4));
        assert_eq!(backoff_for_consecutive_failures(99), Duration::from_secs(4));
    }

    #[test]
    fn detects_version_mismatch_errors() {
        let mismatch = SpawnError::InvalidHandshake(HandshakeError::VersionMismatch {
            expected: "1.0".to_string(),
            got: "9.9".to_string(),
        });
        assert!(is_version_mismatch(&mismatch));
        assert!(!is_version_mismatch(&SpawnError::SpawnFailed(
            "x".to_string()
        )));
        assert!(!is_version_mismatch(&SpawnError::HandshakeTimeout));
    }
}
