//! Sidecar process lifecycle (skeleton: spawn + stdout handshake only).
//!
//! - Dev mode launches the interpreter from `INTERVIEWCOPILOT_PYTHON`
//!   (default `python`) with the entry from `INTERVIEWCOPILOT_SIDECAR_ENTRY`
//!   (default `<manifest>/../sidecar/src/main.py`).
//! - Reads stdout line-by-line with a 10 s timeout, parses the handshake JSON
//!   via [`crate::sidecar::protocol::parse_handshake`], stores port/token.
//! - Full lifecycle (health poll, restart ≤3, process-tree kill — R7) is a
//!   later task; here the child handle is intentionally detached after a
//!   successful handshake so the sidecar keeps running.

use std::path::PathBuf;
use std::time::Duration;

use serde::Serialize;
use tokio::io::{AsyncBufReadExt, BufReader};

use super::protocol::{parse_handshake, SidecarHandshake};

/// How long to wait for the sidecar's first stdout handshake line.
pub const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Debug, Default)]
pub struct SidecarState {
    inner: tokio::sync::Mutex<Option<SidecarHandshake>>,
}

impl SidecarState {
    pub async fn set(&self, hs: SidecarHandshake) {
        *self.inner.lock().await = Some(hs);
    }

    pub async fn snapshot(&self) -> Option<SidecarHandshake> {
        self.inner.lock().await.clone()
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct SidecarStatus {
    pub connected: bool,
    pub port: Option<u16>,
}

#[tauri::command]
pub async fn get_sidecar_status(
    state: tauri::State<'_, SidecarState>,
) -> Result<SidecarStatus, String> {
    let snap = state.snapshot().await;
    Ok(SidecarStatus {
        connected: snap.is_some(),
        port: snap.map(|h| h.port),
    })
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

/// Spawn the sidecar and resolve its stdout handshake.
///
/// Pitfalls handled: stdout line must be flushed by the sidecar
/// (`print(..., flush=True)`); bind failure must exit non-zero before any
/// handshake is printed, so a timeout here surfaces it as an error.
pub async fn spawn_and_handshake() -> Result<SidecarHandshake, String> {
    let python = python_interpreter();
    let entry = sidecar_entry();
    let mut child = tokio::process::Command::new(&python)
        .arg(&entry)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::inherit())
        .spawn()
        .map_err(|e| format!("failed to spawn sidecar ({python} {entry:?}): {e}"))?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "sidecar stdout not piped".to_string())?;
    let mut lines = BufReader::new(stdout).lines();

    let first_line = tokio::time::timeout(HANDSHAKE_TIMEOUT, lines.next_line())
        .await
        .map_err(|_| "timed out waiting for sidecar handshake (10s)".to_string())?
        .map_err(|e| format!("failed reading sidecar stdout: {e}"))?
        .ok_or_else(|| "sidecar stdout closed before handshake".to_string())?;

    let trimmed = first_line.trim();
    if trimmed.is_empty() {
        let _ = child.kill().await;
        return Err("sidecar printed empty handshake line".to_string());
    }

    let hs =
        parse_handshake(trimmed).map_err(|e| format!("sidecar handshake invalid: {e}"))?;
    println!("[sidecar] handshake parsed: port={} caps={:?}", hs.port, hs.capabilities);

    // Skeleton scope: detach child (dropping the handle does not kill it);
    // later tasks track the handle for health/restart/kill (R7).
    std::mem::forget(child);
    Ok(hs)
}
