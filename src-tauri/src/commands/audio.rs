//! 采集服务命令（M2-7 接线）：`capture://toggle` 消费方 + 诊断面板数据源。
//!
//! # 两个入口，一个真值源
//!
//! 启停采集有两个入口：全局快捷键（F1.6）与前端开关。两者都**不拥有**采集状态，
//! 都只是把意图交给 [`CaptureService`]；服务的状态再以 `capture://state`
//! 事件回推前端。这样「快捷键按了但 UI 没变」这类双真值源问题不会出现。
//!
//! - 快捷键：`shortcuts.rs` 照旧只发 `capture://toggle`（不改那一层），
//!   本模块 `listen` 它 → 调服务。
//! - 前端：`set_capture` / `toggle_capture` 命令。
//!
//! # 为什么命令要 sidecar 凭据
//!
//! 采集的出口是 sidecar 的 `/audio/stream`（R9 要求 Bearer header），所以开流前
//! 必须先拿到端口与 token。sidecar 没连上就**拒绝启动采集**并给出可读原因 ——
//! 采到音频却送不出去不是"部分可用"，是假运行。

use std::sync::Arc;

use tauri::{AppHandle, Emitter, Listener, Manager};

use crate::audio::service::{CaptureService, ServiceSnapshot};
use crate::audio::uplink::{EventSink, TauriSink};
use crate::sidecar::manager::SidecarState;

/// 采集状态回推事件（前端据此渲染，而不是自己猜）。
pub const EVT_CAPTURE_STATE: &str = "capture://state";

fn emit_state(app: &AppHandle, snap: &ServiceSnapshot) {
    let _ = app.emit(EVT_CAPTURE_STATE, snap);
}

fn service(app: &AppHandle) -> Arc<CaptureService> {
    app.state::<Arc<CaptureService>>().inner().clone()
}

async fn credentials(app: &AppHandle) -> Result<(u16, String), String> {
    match app.state::<SidecarState>().snapshot().await {
        Some(h) => Ok((h.port, h.auth_token)),
        None => Err("sidecar 未连接，无法启动采集".to_string()),
    }
}

async fn set_capture_impl(app: &AppHandle, enabled: bool) -> Result<ServiceSnapshot, String> {
    let svc = service(app);
    let snap = if enabled {
        let (port, token) = credentials(app).await?;
        let sink: Arc<dyn EventSink> = Arc::new(TauriSink::new(app.clone()));
        svc.start(port, &token, sink).await?
    } else {
        svc.stop().await
    };
    emit_state(app, &snap);
    Ok(snap)
}

/// 当前采集状态（诊断面板 / 页面初次渲染用）。
///
/// 服务未启动时返回空快照而不是错误：「没在采集」是合法状态。
#[tauri::command]
pub async fn get_capture_state(app: AppHandle) -> Result<ServiceSnapshot, String> {
    Ok(service(&app).snapshot())
}

/// 前端开关：显式启/停。
#[tauri::command]
pub async fn set_capture(app: AppHandle, enabled: bool) -> Result<ServiceSnapshot, String> {
    set_capture_impl(&app, enabled).await
}

/// 前端开关：翻转。
#[tauri::command]
pub async fn toggle_capture(app: AppHandle) -> Result<ServiceSnapshot, String> {
    let running = service(&app).is_running();
    set_capture_impl(&app, !running).await
}

/// 注册 `capture://toggle` 的 Rust 侧消费方。
///
/// 快捷键插件（`shortcuts.rs`）照旧只发事件；这里把它接到采集服务上。
/// 事件回调是同步的，所以启停动作丢进 Tauri 的异步运行时执行 ——
/// 不阻塞键盘钩子，也不让"按一下卡一下"变成用户体验。
pub fn spawn_toggle_listener(app: &AppHandle) {
    let handle = app.clone();
    let app_for_listen = app.clone();
    app_for_listen.listen(crate::shortcuts::EVT_CAPTURE_TOGGLE, move |_event| {
        let handle = handle.clone();
        tauri::async_runtime::spawn(async move {
            let running = service(&handle).is_running();
            if let Err(e) = set_capture_impl(&handle, !running).await {
                // 失败也要让 UI 知道（例如 sidecar 未连接），而不是静默什么都不发生。
                let _ = handle.emit(
                    EVT_CAPTURE_STATE,
                    ServiceSnapshot {
                        running: false,
                        last_error: Some(e),
                        paths: Vec::new(),
                    },
                );
            }
        });
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::audio::service::ServiceSnapshot;

    /// 状态事件名是前后端契约，写死它防止顺手改名。
    #[test]
    fn capture_state_event_name_is_the_contract() {
        assert_eq!(EVT_CAPTURE_STATE, "capture://state");
    }

    /// 快捷键事件名必须和 `shortcuts.rs` 里发的一致 —— 这里跨模块钉住，
    /// 免得两边各改各的、快捷键静默失效。
    #[test]
    fn toggle_listener_targets_the_event_shortcuts_emits() {
        assert_eq!(crate::shortcuts::EVT_CAPTURE_TOGGLE, "capture://toggle");
    }

    /// 失败快照要能被前端序列化成 `{running:false, last_error, paths:[]}`。
    #[test]
    fn failure_snapshot_serializes_with_a_readable_error() {
        let snap = ServiceSnapshot {
            running: false,
            last_error: Some("sidecar 未连接，无法启动采集".to_string()),
            paths: Vec::new(),
        };
        let v = serde_json::to_value(&snap).expect("serialize");
        assert_eq!(v["running"], serde_json::json!(false));
        assert_eq!(
            v["last_error"],
            serde_json::json!("sidecar 未连接，无法启动采集")
        );
        assert_eq!(v["paths"], serde_json::json!([]));
    }
}
