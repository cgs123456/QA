//! Linux encrypted-file key fallback — PLACEHOLDER (Phase 2).
//!
//! Contract for the real implementation: AES-GCM (or OS-certificate-bound)
//! file under the user data dir, NEVER plaintext. Until then every call
//! reports unimplemented so a missing Linux keychain fails loudly instead
//! of silently downgrading to plaintext.

/// Placeholder: always errors. Keeps the `Option<String>` shape of
/// [`crate::security::keychain::get_api_key`] for the future swap-in.
pub fn get_api_key_fallback(_provider: &str) -> Result<Option<String>, String> {
    Err("encrypted-file key fallback not implemented (Phase 2)".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fallback_reports_unimplemented() {
        let err = get_api_key_fallback("openai").unwrap_err();
        assert!(err.contains("Phase 2"));
    }
}
