pub mod audio;
pub mod autostart;
pub mod commands;
pub mod companion;
pub mod security;
pub mod shortcuts;
pub mod sidecar;
pub mod stealth;
pub mod tray;
pub mod updater;

use sidecar::manager::{
    get_sidecar_credentials, get_sidecar_degraded, get_sidecar_status, kill_sidecar_tree_blocking,
    retry_sidecar_start, sidecar_log_tail, supervise, SidecarState,
};
use tauri::Manager;
use tauri::RunEvent;
use tauri_plugin_autostart::MacosLauncher;

use crate::audio::service::CaptureService;
use crate::commands::audio::{get_capture_state, set_capture, toggle_capture};
use crate::commands::settings::{get_autostart, save_api_key, set_autostart};
use crate::commands::window::{destroy_overlay, get_overlay_status, set_overlay, toggle_overlay};
use crate::companion::{
    broadcast_to_companion, get_companion_status, revoke_companion_client, rotate_companion_token,
    start_companion, stop_companion, CompanionService,
};
use crate::stealth::overlay;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        // F1.6 全局快捷键。处理器在插件构建时拿到绑定表，只发事件、不碰采集状态。
        .plugin(shortcuts::plugin())
        // taskP7：开机自启。插件 setup 只解析 current_exe，**不写注册表**——
        // 真正写入发生在用户拨 Settings 开关时，失败会回给 UI 而不是拦住启动。
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            None,
        ))
        .manage(SidecarState::default())
        // M2-7：采集服务单例。快捷键与前端开关都通过它启停，状态由它持有。
        .manage(std::sync::Arc::new(CaptureService::new()))
        // S8：伴侣服务单例（本地 WS 二屏）。
        .manage(std::sync::Arc::new(CompanionService::new()))
        // taskP7：托盘是否真的建起来了。主窗「关闭」要不要收进托盘取决于它。
        .manage(tray::TrayState::default())
        .setup(|app| {
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                supervise(handle).await;
            });
            // M2-7：把 `capture://toggle`（快捷键发的事件）接到采集服务上。
            commands::audio::spawn_toggle_listener(app.handle());
            // 注册失败不阻断启动：加速键可能被别的程序占用。
            // 用 eprintln 而非日志插件：当前没有日志通道，且这是启动期一次性信息。
            let failures = shortcuts::register_defaults(app.handle());
            if !failures.is_empty() {
                eprintln!("[shortcuts] failed to register: {}", failures.join(", "));
            }
            // taskP7 托盘。建不起来同样不阻断启动，但**必须如实记下**：
            // 没有托盘时主窗的「关闭」要退回真退出，否则程序关不掉（见 tray::close_plan）。
            match tray::setup(app.handle()) {
                Ok(()) => {
                    if let Some(state) = app.try_state::<tray::TrayState>() {
                        state.mark_ready();
                    }
                }
                Err(e) => eprintln!("[tray] {e}（主窗关闭将直接退出）"),
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            match event {
                // taskP7：最小化到托盘。托盘不可用时放行，让窗口正常关闭。
                tauri::WindowEvent::CloseRequested { api, .. } => {
                    if window.label() == tray::MAIN_WINDOW_LABEL
                        && tray::handle_close_request(window)
                    {
                        api.prevent_close();
                    }
                }
                // taskP7：提词窗没了（用户关的、或它自己崩了）。**主应用不受影响**：
                // 窗口是独立 WebviewWindow，这里只记一笔；重建入口就是
                // `set_overlay(visible=true)` / 托盘那一项，走的是同一个 `Plan::Create`。
                tauri::WindowEvent::Destroyed if window.label() == overlay::OVERLAY_LABEL => {
                    eprintln!("[stealth] 提词窗已销毁（重建入口：托盘「显示/隐藏提词窗」）");
                }
                _ => {}
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_sidecar_status,
            get_sidecar_credentials,
            get_sidecar_degraded,
            retry_sidecar_start,
            sidecar_log_tail,
            save_api_key,
            get_capture_state,
            set_capture,
            toggle_capture,
            get_overlay_status,
            set_overlay,
            toggle_overlay,
            destroy_overlay,
            get_autostart,
            set_autostart,
            start_companion,
            stop_companion,
            get_companion_status,
            revoke_companion_client,
            rotate_companion_token,
            broadcast_to_companion
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                // R7: never leave a stray python process behind.
                kill_sidecar_tree_blocking(app_handle);
                // M2-7: 进程退出前先停采集，别让 uplink 线程拖着 socket 一起走。
                if let Some(svc) = app_handle.try_state::<std::sync::Arc<CaptureService>>() {
                    svc.inner().request_stop();
                }
                // S8: 退出前停止伴侣服务，切断所有连接。
                if let Some(svc) = app_handle.try_state::<std::sync::Arc<CompanionService>>() {
                    tauri::async_runtime::block_on(async { svc.stop().await });
                }
            }
        });
}
