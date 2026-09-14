use serde::{Deserialize, Serialize};

/// Expected sidecar protocol version (PRD §3.4, R2).
pub const PROTOCOL_VERSION: &str = "1.0";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SidecarHandshake {
    pub protocol_version: String,
    pub port: u16,
    pub auth_token: String,
    pub capabilities: Vec<String>,
    pub models_loaded: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HandshakeError {
    InvalidJson(String),
    VersionMismatch { expected: String, got: String },
}

impl std::fmt::Display for HandshakeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            HandshakeError::InvalidJson(e) => write!(f, "invalid handshake JSON: {e}"),
            HandshakeError::VersionMismatch { expected, got } => write!(
                f,
                "protocol_version mismatch: expected {expected}, got {got}"
            ),
        }
    }
}

impl std::error::Error for HandshakeError {}

/// Parse one stdout handshake line and validate `protocol_version`.
pub fn parse_handshake(line: &str) -> Result<SidecarHandshake, HandshakeError> {
    let hs: SidecarHandshake =
        serde_json::from_str(line).map_err(|e| HandshakeError::InvalidJson(e.to_string()))?;
    if hs.protocol_version != PROTOCOL_VERSION {
        return Err(HandshakeError::VersionMismatch {
            expected: PROTOCOL_VERSION.to_string(),
            got: hs.protocol_version,
        });
    }
    Ok(hs)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_valid_handshake() {
        let line = r#"{"protocol_version":"1.0","port":54321,"auth_token":"abc","capabilities":["qa"],"models_loaded":[]}"#;
        let hs = parse_handshake(line).expect("valid handshake must parse");
        assert_eq!(hs.protocol_version, "1.0");
        assert_eq!(hs.port, 54321);
        assert_eq!(hs.auth_token, "abc");
        assert_eq!(hs.capabilities, vec!["qa".to_string()]);
        assert!(hs.models_loaded.is_empty());
    }

    #[test]
    fn rejects_invalid_json() {
        let err = parse_handshake("not-json{{").unwrap_err();
        assert!(matches!(err, HandshakeError::InvalidJson(_)));
    }

    #[test]
    fn rejects_version_mismatch() {
        let line = r#"{"protocol_version":"9.9","port":1,"auth_token":"x","capabilities":[],"models_loaded":[]}"#;
        let err = parse_handshake(line).unwrap_err();
        assert_eq!(
            err,
            HandshakeError::VersionMismatch {
                expected: "1.0".to_string(),
                got: "9.9".to_string(),
            }
        );
    }
}
