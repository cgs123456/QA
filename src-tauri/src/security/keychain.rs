//! OS keychain access for LLM API keys (PRD F6.3, R8).
//!
//! Keys live in the OS keychain (service `interview-copilot`, account =
//! provider name) and travel to the sidecar only via the authenticated
//! in-memory push (see `super::push`). They are NEVER logged or persisted
//! by this app — all log lines below mention the provider name at most.

const SERVICE: &str = "interview-copilot";

fn open_entry(provider: &str) -> Result<keyring::Entry, String> {
    match keyring::Entry::new(SERVICE, provider) {
        Ok(entry) => Ok(entry),
        #[cfg(target_os = "linux")]
        Err(_) => {
            // Linux without a keychain backend → Phase 2 encrypted-file
            // fallback (currently a placeholder that reports unimplemented).
            let _ = super::fallback::get_api_key_fallback(provider);
            Err("keyring unavailable on Linux and encrypted-file fallback"
                .to_string()
                + " is not implemented (Phase 2)")
        }
        #[cfg(not(target_os = "linux"))]
        Err(e) => Err(format!("keyring unavailable: {e}")),
    }
}

/// Read the stored key for `provider`. `Ok(None)` = no entry stored.
pub fn get_api_key(provider: &str) -> Result<Option<String>, String> {
    let entry = open_entry(provider)?;
    match entry.get_password() {
        Ok(password) => Ok(Some(password)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(format!("keyring read failed for '{provider}': {e}")),
    }
}

/// Store (or overwrite) the key for `provider` (future settings UI).
#[allow(dead_code)]
pub fn set_api_key(provider: &str, api_key: &str) -> Result<(), String> {
    let entry = open_entry(provider)?;
    entry
        .set_password(api_key)
        .map_err(|e| format!("keyring write failed for '{provider}': {e}"))
}
