//! OS keychain access for LLM API keys (PRD F6.3, R8).
//!
//! Keys live in the OS keychain (service `interview-copilot`, account =
//! provider name) and travel to the sidecar only via the authenticated
//! in-memory push (see `super::push`). They are NEVER logged or persisted
//! by this app — all log lines below mention the provider name at most.
//!
//! Linux fallback (F6.3 Phase 2, P8): when the keyring backend itself fails
//! (no libsecret/D-Bus), reads/writes fall through to the encrypted file in
//! [`super::fallback`]. Dispatch is a pure function of backend health, with
//! three arms: healthy backend wins (file never consulted); backend failure
//! goes to the file; file failure returns `Err` (plaintext is forbidden,
//! there is no fourth arm).
//! `get_with`/`set_with` expose exactly this dispatch over any
//! [`SecretStore`] so unit tests can drive all three states with a mock.

const SERVICE: &str = "interview-copilot";

/// Keyring backend seam (production = [`OsKeychain`], tests = mock).
pub trait SecretStore {
    /// `Ok(Some)` = stored value; `Ok(None)` = backend healthy, no entry;
    /// `Err` = backend failure (→ file fallback on Linux).
    fn get_password(&self, account: &str) -> Result<Option<String>, String>;
    /// `Err` = backend failure (→ file fallback on Linux).
    fn set_password(&self, account: &str, secret: &str) -> Result<(), String>;
}

/// The real OS keychain via the `keyring` crate.
pub struct OsKeychain;

impl SecretStore for OsKeychain {
    fn get_password(&self, account: &str) -> Result<Option<String>, String> {
        let entry = keyring::Entry::new(SERVICE, account)
            .map_err(|e| format!("keyring unavailable: {e}"))?;
        match entry.get_password() {
            Ok(password) => Ok(Some(password)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(e) => Err(format!("keyring read failed for '{account}': {e}")),
        }
    }

    fn set_password(&self, account: &str, secret: &str) -> Result<(), String> {
        let entry = keyring::Entry::new(SERVICE, account)
            .map_err(|e| format!("keyring unavailable: {e}"))?;
        entry
            .set_password(secret)
            .map_err(|e| format!("keyring write failed for '{account}': {e}"))
    }
}

/// Read dispatch over an injected backend (unit-test seam for the 3 states).
pub fn get_with(
    store: &dyn SecretStore,
    provider: &str,
    dir: &std::path::Path,
) -> Result<Option<String>, String> {
    match store.get_password(provider) {
        Ok(value) => Ok(value),
        Err(_) => super::fallback::get_api_key_fallback(provider, dir),
    }
}

/// Write dispatch over an injected backend (unit-test seam for the 3 states).
pub fn set_with(
    store: &dyn SecretStore,
    provider: &str,
    secret: &str,
    dir: &std::path::Path,
) -> Result<(), String> {
    match store.set_password(provider, secret) {
        Ok(()) => Ok(()),
        Err(_) => super::fallback::set_api_key_fallback(provider, secret, dir),
    }
}

/// Read the stored key for `provider`. `Ok(None)` = no entry stored.
pub fn get_api_key(provider: &str) -> Result<Option<String>, String> {
    match OsKeychain.get_password(provider) {
        Ok(value) => Ok(value),
        // Inner error detail is intentionally not logged (R8: provider names
        // at most — keyring errors can echo bus paths, never secrets, but the
        // discipline stays uniform).
        #[cfg(target_os = "linux")]
        Err(_) => {
            eprintln!("[secret] keyring backend failed for '{provider}', trying encrypted file");
            let dir = super::fallback::default_base_dir()?;
            super::fallback::get_api_key_fallback(provider, &dir)
        }
        #[cfg(not(target_os = "linux"))]
        Err(e) => Err(e),
    }
}

/// Store (or overwrite) the key for `provider` (settings UI command).
pub fn set_api_key(provider: &str, api_key: &str) -> Result<(), String> {
    match OsKeychain.set_password(provider, api_key) {
        Ok(()) => Ok(()),
        #[cfg(target_os = "linux")]
        Err(_) => {
            eprintln!("[secret] keyring backend failed for '{provider}', trying encrypted file");
            let dir = super::fallback::default_base_dir()?;
            super::fallback::set_api_key_fallback(provider, api_key, &dir)
        }
        #[cfg(not(target_os = "linux"))]
        Err(e) => Err(e),
    }
}

#[cfg(test)]
pub(crate) mod mock {
    //! Programmable keyring backend: drives the 3 dispatch states without
    //! touching the OS (works on any platform, no libsecret needed).
    use std::collections::HashMap;
    use std::sync::Mutex;

    /// `fail_backend = true` simulates "no libsecret/D-Bus".
    pub(crate) struct MockStore {
        values: Mutex<HashMap<String, String>>,
        fail_backend: bool,
    }

    impl MockStore {
        pub(crate) fn with_values(pairs: &[(&str, &str)]) -> Self {
            Self {
                values: Mutex::new(
                    pairs
                        .iter()
                        .map(|(k, v)| (k.to_string(), v.to_string()))
                        .collect(),
                ),
                fail_backend: false,
            }
        }

        pub(crate) fn failing() -> Self {
            Self {
                values: Mutex::new(HashMap::new()),
                fail_backend: true,
            }
        }

        pub(crate) fn stored(&self, account: &str) -> Option<String> {
            self.values.lock().expect("mock lock").get(account).cloned()
        }
    }

    impl super::SecretStore for MockStore {
        fn get_password(&self, account: &str) -> Result<Option<String>, String> {
            if self.fail_backend {
                return Err("mock keyring backend failure".to_string());
            }
            Ok(self.values.lock().expect("mock lock").get(account).cloned())
        }

        fn set_password(&self, account: &str, secret: &str) -> Result<(), String> {
            if self.fail_backend {
                return Err("mock keyring backend failure".to_string());
            }
            self.values
                .lock()
                .expect("mock lock")
                .insert(account.to_string(), secret.to_string());
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::mock::MockStore;
    use super::*;
    use crate::security::fallback::test_support::use_machine_id;

    fn workdir(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("ic-kc-test-{name}"));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("create test dir");
        dir
    }

    /// State 1a: backend healthy with a value → keychain wins, file untouched.
    #[test]
    fn healthy_backend_value_wins_and_ignores_file() {
        let dir = workdir("healthy-value");
        let store = MockStore::with_values(&[("openai", "sk-live")]);
        assert_eq!(
            get_with(&store, "openai", &dir).unwrap(),
            Some("sk-live".to_string())
        );
        assert!(
            !super::super::fallback::store_path(&dir).exists(),
            "healthy backend must not touch the file store"
        );
    }

    /// State 1b: backend healthy without entry → None, file never consulted
    /// (keychain is source of truth whenever it works).
    #[test]
    fn healthy_backend_no_entry_yields_none() {
        let dir = workdir("healthy-empty");
        let store = MockStore::with_values(&[]);
        // Even with a stale file present, a healthy backend decides alone.
        std::fs::write(
            super::super::fallback::store_path(&dir),
            r#"{"openai":{"magic":"IC-KF1","v":1,"salt":"00","ct":"00","tag":"00"}}"#,
        )
        .unwrap();
        assert_eq!(get_with(&store, "openai", &dir).unwrap(), None);
    }

    /// State 2 (read): backend failure → encrypted-file roundtrip
    /// (machine-id via shared env guard → runs on any OS).
    #[test]
    fn backend_failure_falls_through_to_file() {
        let (_guard, dir) = use_machine_id("kc-read", "test-machine-keychain\n");
        let store = MockStore::failing();
        set_with(&store, "openai", "sk-file", &dir).unwrap();
        // The mock itself must NOT hold the secret (it never saw a success).
        assert_eq!(store.stored("openai"), None);
        assert_eq!(
            get_with(&store, "openai", &dir).unwrap(),
            Some("sk-file".to_string())
        );
    }

    /// State 2 (write): backend failure → set lands in the file, readable back.
    #[test]
    fn backend_failure_write_lands_in_file() {
        let (_guard, dir) = use_machine_id("kc-write", "test-machine-keychain\n");
        let store = MockStore::failing();
        set_with(&store, "openai", "sk-new", &dir).unwrap();
        assert_eq!(store.stored("openai"), None);
        assert_eq!(
            get_with(&store, "openai", &dir).unwrap(),
            Some("sk-new".to_string())
        );
    }

    /// State 3: backend failure + broken file → loud Err, never plaintext.
    #[test]
    fn backend_and_file_failure_errors_loudly() {
        let (_guard, dir) = use_machine_id("kc-double-fault", "m\n");
        std::fs::write(super::super::fallback::store_path(&dir), "{corrupt").unwrap();
        let store = MockStore::failing();
        let err = get_with(&store, "openai", &dir).unwrap_err();
        assert!(err.contains("corrupt"), "unexpected: {err}");
    }

    /// Healthy-backend writes never touch the file.
    #[test]
    fn healthy_backend_write_stays_in_keychain() {
        let dir = workdir("healthy-write");
        let store = MockStore::with_values(&[]);
        set_with(&store, "openai", "sk-live", &dir).unwrap();
        assert_eq!(store.stored("openai"), Some("sk-live".to_string()));
        assert!(!super::super::fallback::store_path(&dir).exists());
    }
}
