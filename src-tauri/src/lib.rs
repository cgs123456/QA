pub mod audio;
pub mod commands;
pub mod security;
pub mod shortcuts;
pub mod sidecar;
pub mod tray;
pub mod updater;

use sidecar::manager::{
    get_sidecar_credentials, get_sidecar_degraded, get_sidecar_status, kill_sidecar_tree_blocking,
    retry_sidecar_start, sidecar_log_tail, supervise, SidecarState,
};
use tauri::RunEvent;

use crate::commands::settings::save_api_key;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        // F1.6 全局快捷键。处理器在插件构建时拿到绑定表，只发事件、不碰采集状态。
        .plugin(shortcuts::plugin())
        .manage(SidecarState::default())
        .setup(|app| {
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                supervise(handle).await;
            });
            // 注册失败不阻断启动：加速键可能被别的程序占用。
            // 用 eprintln 而非日志插件：当前没有日志通道，且这是启动期一次性信息。
            let failures = shortcuts::register_defaults(app.handle());
            if !failures.is_empty() {
                eprintln!("[shortcuts] failed to register: {}", failures.join(", "));
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_sidecar_status,
            get_sidecar_credentials,
            get_sidecar_degraded,
            retry_sidecar_start,
            sidecar_log_tail,
            save_api_key
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                // R7: never leave a stray python process behind.
                kill_sidecar_tree_blocking(app_handle);
            }
        });
}
