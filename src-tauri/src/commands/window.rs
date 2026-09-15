//! 提词窗命令（taskP7）。
//!
//! 命令层只做两件事：把参数翻成 [`overlay::Intent`]、把结果原样返回。
//! 所有决策在 `stealth::overlay`（纯函数，可单测），所有副作用在那里的薄壳。
//!
//! 与 `commands/audio.rs` 同款纪律：**前端不拥有窗口状态**。
//! `toggle_overlay` 是「先查真值再翻转」，不是前端自己记一个布尔值再传进来 ——
//! 托盘也能改这个窗口，前端记的那份一定会和真值漂移。

use tauri::AppHandle;

use crate::stealth::overlay::{self, Intent, OverlayStatus};

/// 查提词窗状态（存在性 / 可见性 / 捕获排除是否生效）。
#[tauri::command]
pub async fn get_overlay_status(app: AppHandle) -> Result<OverlayStatus, String> {
    Ok(overlay::status(&app))
}

/// 显隐提词窗。`visible=false` 只是隐藏，窗口留着（下次显示不用重建）。
#[tauri::command]
pub async fn set_overlay(app: AppHandle, visible: bool) -> Result<OverlayStatus, String> {
    let intent = if visible { Intent::Show } else { Intent::Hide };
    overlay::apply(&app, intent)
}

/// 翻转提词窗显隐。真值取自 `overlay::status`，不取自调用方。
#[tauri::command]
pub async fn toggle_overlay(app: AppHandle) -> Result<OverlayStatus, String> {
    let visible = overlay::status(&app).visible;
    let intent = if visible { Intent::Hide } else { Intent::Show };
    overlay::apply(&app, intent)
}

/// 销毁提词窗。**重建入口就是 `set_overlay(visible=true)`** ——
/// 销毁后 `plan()` 会走 `Create`，与首次创建是同一段代码，没有单独的「恢复」分支。
#[tauri::command]
pub async fn destroy_overlay(app: AppHandle) -> Result<OverlayStatus, String> {
    overlay::apply(&app, Intent::Destroy)
}
