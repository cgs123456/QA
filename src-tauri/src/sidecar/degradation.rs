//! Degraded-mode contract (R7).
//!
//! When the sidecar can neither start healthy nor recover within the restart
//! budget (or speaks the wrong protocol), Rust emits
//! [`DEGRADED_EVENT`] (`sidecar://degraded`) with a [`DegradedInfo`] payload
//! and the frontend renders the degraded page instead of a blank screen.
//!
//! [`handle_handshake_result`] is the single mapping from handshake outcome
//! to degraded emission, so the version-mismatch unit test exercises the
//! exact production path (via [`DegradedSink`]).

use serde::{Deserialize, Serialize};

use super::protocol::{HandshakeError, SidecarHandshake};

/// Tauri event name for degraded mode.
pub const DEGRADED_EVENT: &str = "sidecar://degraded";
/// `protocol_version` mismatch between shell and sidecar.
pub const REASON_VERSION_MISMATCH: &str = "version_mismatch";
/// Consecutive failures exhausted `MAX_RESTARTS` (see manager.rs).
pub const REASON_RESTART_EXHAUSTED: &str = "restart_exhausted";

/// Degraded payload. Content-free: reason/detail never carry the token (R8).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DegradedInfo {
    pub reason: String,
    pub detail: String,
}

/// Abstraction over the Tauri event bus so the degraded mapping is unit
/// testable without a running app (production impl lives in manager.rs).
pub trait DegradedSink {
    fn emit_degraded(&self, reason: &str, detail: &str);
}

/// Map a handshake outcome to degraded mode.
///
/// A version mismatch emits the degraded event and returns the reason;
/// any other outcome returns `None` and emits nothing.
pub fn handle_handshake_result(
    sink: &impl DegradedSink,
    result: &Result<SidecarHandshake, HandshakeError>,
) -> Option<String> {
    match result {
        Err(HandshakeError::VersionMismatch { expected, got }) => {
            let detail = format!("protocol_version mismatch: expected {expected}, got {got}");
            sink.emit_degraded(REASON_VERSION_MISMATCH, &detail);
            Some(REASON_VERSION_MISMATCH.to_string())
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    #[derive(Default)]
    struct FakeSink {
        events: Mutex<Vec<(String, String)>>,
    }

    impl DegradedSink for FakeSink {
        fn emit_degraded(&self, reason: &str, detail: &str) {
            self.events
                .lock()
                .unwrap()
                .push((reason.to_string(), detail.to_string()));
        }
    }

    fn result_of(line: &str) -> Result<SidecarHandshake, HandshakeError> {
        super::super::protocol::parse_handshake(line)
    }

    #[test]
    fn version_mismatch_triggers_degraded_event() {
        let sink = FakeSink::default();
        let line = r#"{"protocol_version":"9.9","port":1,"auth_token":"x","capabilities":[],"models_loaded":[]}"#;
        let reason = handle_handshake_result(&sink, &result_of(line));
        assert_eq!(reason.as_deref(), Some(REASON_VERSION_MISMATCH));
        let events = sink.events.lock().unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].0, REASON_VERSION_MISMATCH);
        assert!(events[0].1.contains("9.9"));
    }

    #[test]
    fn valid_handshake_emits_nothing() {
        let sink = FakeSink::default();
        let line = r#"{"protocol_version":"1.0","port":54321,"auth_token":"abc","capabilities":["qa"],"models_loaded":[]}"#;
        let reason = handle_handshake_result(&sink, &result_of(line));
        assert_eq!(reason, None);
        assert!(sink.events.lock().unwrap().is_empty());
    }

    #[test]
    fn invalid_json_emits_nothing() {
        let sink = FakeSink::default();
        let reason = handle_handshake_result(&sink, &result_of("not-json{{"));
        assert_eq!(reason, None);
        assert!(sink.events.lock().unwrap().is_empty());
    }
}
