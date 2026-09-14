//! Secret push: after every sidecar handshake, POST the selected provider's
//! key (read from the OS keychain) to the authenticated
//! `POST /settings/llm-secret` endpoint. The sidecar holds it in memory
//! only. Re-push on every (re)connect: a restarted sidecar has empty memory.
//!
//! R8: only the provider NAME appears in logs — never the key, never the
//! request body. reqwest error Display contains URL/status only.

use tauri::{AppHandle, Manager};

use crate::sidecar::manager::SidecarState;

/// Provider selected for generation. Ollama is local and keyless → no push.
fn selected_provider() -> String {
    std::env::var("INTERVIEWCOPILOT_LLM_PROVIDER")
        .unwrap_or_else(|_| "ollama".to_string())
        .trim()
        .to_lowercase()
}

/// Push the current provider key to a freshly handshaked sidecar.
/// Best-effort: any failure is logged (without secrets) and swallowed —
/// generation without a key simply fails at use time with a clear error.
pub async fn push_llm_secret(app: &AppHandle) {
    let provider = selected_provider();
    if provider == "ollama" {
        return;
    }
    let api_key = match crate::security::keychain::get_api_key(&provider) {
        Ok(Some(key)) => key,
        Ok(None) => {
            println!("[secret] no stored key for provider '{provider}', skipping push");
            return;
        }
        Err(e) => {
            eprintln!("[secret] keyring read failed for '{provider}': {e}");
            return;
        }
    };
    // NOTE: port + token come from the in-memory handshake and are never
    // logged (R8); only the provider name appears in log lines.
    let (port, token) = match app.try_state::<SidecarState>() {
        Some(state) => match state.snapshot().await {
            Some(hs) => (hs.port, hs.auth_token),
            None => {
                eprintln!("[secret] sidecar not connected, skipping push");
                return;
            }
        },
        None => {
            eprintln!("[secret] sidecar state missing, skipping push");
            return;
        }
    };
    let endpoint = format!("http://127.0.0.1:{port}/settings/llm-secret");
    let client = reqwest::Client::new();
    match client
        .post(&endpoint)
        .bearer_auth(token)
        .json(&serde_json::json!({"provider": provider, "api_key": api_key}))
        .send()
        .await
    {
        Ok(resp) if resp.status().is_success() => {
            println!("[secret] llm secret pushed for '{provider}'");
        }
        Ok(resp) => {
            eprintln!(
                "[secret] secret push failed for '{provider}': http {}",
                resp.status()
            );
        }
        Err(e) => {
            eprintln!("[secret] secret push failed for '{provider}': {e}");
        }
    }
}
