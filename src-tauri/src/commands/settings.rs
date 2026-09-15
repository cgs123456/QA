//! Tauri IPC commands (settings).

use tauri::{AppHandle, Manager};
use tauri_plugin_autostart::AutoLaunchManager;

use crate::autostart::{self, AutostartState};
use crate::security::keychain;

/// Save an LLM API key (never displayed back).
///
/// 首选 OS keychain；Linux 无后端时落加密文件（F6.3 Phase 2，绝不明文）。
/// 返回值不区分落点（前端显示自己的文案，此处只求诚实不撒谎）。
#[tauri::command]
pub async fn save_api_key(provider: String, api_key: String) -> Result<String, String> {
    let provider = provider.trim().to_string();
    if provider.is_empty() {
        return Err("provider 必填".to_string());
    }
    if api_key.trim().is_empty() {
        return Err("api_key 必填".to_string());
    }
    // R8: key 只进 keychain/加密文件，返回值与日志均不含 key。
    keychain::set_api_key(&provider, &api_key)?;
    Ok(format!("saved ({provider})"))
}

/// 开机自启开关（taskP7）。
///
/// 用 `try_state` 而不是 `ManagerExt::autolaunch()`：后者内部是 `state()`，
/// 插件没就绪时会 **panic**。读不到就返回「不可用」，不把插件的失败升级成崩溃。
#[tauri::command]
pub async fn get_autostart(app: AppHandle) -> AutostartState {
    let read = match app.try_state::<AutoLaunchManager>() {
        Some(m) => m.is_enabled().map_err(|e| e.to_string()),
        None => Err(autostart::UNAVAILABLE.to_string()),
    };
    autostart::view(read, autostart::support())
}

/// 写入开机自启开关。
///
/// **失败不致命**：写注册表/LaunchAgent 可能被系统策略或杀软拦下，
/// 这时把原因原样回给 UI，应用其余功能照常（与快捷键「注册失败不阻断启动」同款纪律）。
#[tauri::command]
pub async fn set_autostart(app: AppHandle, enabled: bool) -> AutostartState {
    let write = match app.try_state::<AutoLaunchManager>() {
        Some(m) => if enabled { m.enable() } else { m.disable() }.map_err(|e| e.to_string()),
        None => Err(autostart::UNAVAILABLE.to_string()),
    };
    // 写成功 → 真值就是调用方给的值（不回头再读一次，避免把「刚写成功但读不到」
    // 又画成 unavailable）；写失败 → 走 view 的 Err 分支，available=false。
    match write {
        Ok(()) => autostart::view(Ok(enabled), autostart::support()),
        Err(e) => autostart::view(Err(e), autostart::support()),
    }
}
