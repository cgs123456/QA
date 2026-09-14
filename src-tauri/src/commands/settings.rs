//! Tauri IPC commands (settings).

use crate::security::keychain;

/// Save an LLM API key into the OS keychain (never displayed back).
#[tauri::command]
pub async fn save_api_key(provider: String, api_key: String) -> Result<String, String> {
    let provider = provider.trim().to_string();
    if provider.is_empty() {
        return Err("provider 必填".to_string());
    }
    if api_key.trim().is_empty() {
        return Err("api_key 必填".to_string());
    }
    // R8: key 只进 keychain，返回值与日志均不含 key。
    keychain::set_api_key(&provider, &api_key)?;
    Ok(format!("saved to keychain ({provider})"))
}
