pub mod audio;
pub mod commands;
pub mod security;
pub mod shortcuts;
pub mod sidecar;
pub mod tray;
pub mod updater;

use sidecar::manager::{get_sidecar_status, spawn_and_handshake, SidecarState};
use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(SidecarState::default())
        .setup(|app| {
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                match spawn_and_handshake().await {
                    Ok(hs) => {
                        if let Some(state) = handle.try_state::<SidecarState>() {
                            state.set(hs).await;
                        }
                    }
                    Err(e) => {
                        eprintln!("[sidecar] startup failed: {e}");
                    }
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![get_sidecar_status])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
