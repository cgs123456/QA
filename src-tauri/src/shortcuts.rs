//! Global shortcuts (Phase 1b F1.6).
//!
//! **职责边界**：这一层只做「键盘 → 事件」。它刻意**不拥有采集状态** ——
//! 采集服务（`audio/endpoint.rs` 的段端点状态机 + 采集生命周期）尚未落地，
//! 所以快捷键只负责把按键变成事件，由前端决定「现在这个按键意味着什么」。
//! 等采集服务就位，本模块也不应该改。
//!
//! 把「加速键 → 动作」做成可单测的纯查表，是因为全局快捷键在 CI 里按不了键：
//! 能钉住的只有映射关系本身，那就把映射关系钉死。

use tauri::{AppHandle, Emitter, Runtime};
use tauri_plugin_global_shortcut::{Builder, GlobalShortcutExt, Shortcut, ShortcutState};

/// F1.6：开始/停止采集。
pub const EVT_CAPTURE_TOGGLE: &str = "capture://toggle";
/// F1.6：手动触发提词（取最近一段转写作为查询，可打断锁定期）。
pub const EVT_TELEPROMPTER_TRIGGER: &str = "teleprompter://trigger";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShortcutAction {
    ToggleCapture,
    TriggerTeleprompter,
}

impl ShortcutAction {
    /// 前端订阅的事件名。
    pub const fn event_name(self) -> &'static str {
        match self {
            ShortcutAction::ToggleCapture => EVT_CAPTURE_TOGGLE,
            ShortcutAction::TriggerTeleprompter => EVT_TELEPROMPTER_TRIGGER,
        }
    }
}

/// 默认绑定。`CmdOrCtrl` 在 macOS 上是 ⌘，其余平台是 Ctrl。
///
/// 选这两个组合的理由：都带 `Shift`，避开各平台常用快捷键；
/// 提词用 `Space`（「说话」的直觉动作），采集开关用 `R`（record）。
pub const DEFAULT_BINDINGS: [(&str, ShortcutAction); 2] = [
    ("CmdOrCtrl+Shift+R", ShortcutAction::ToggleCapture),
    ("CmdOrCtrl+Shift+Space", ShortcutAction::TriggerTeleprompter),
];

/// 加速键 → 动作（纯查表，大小写不敏感）。
pub fn action_for(accelerator: &str) -> Option<ShortcutAction> {
    DEFAULT_BINDINGS
        .iter()
        .find(|(acc, _)| acc.eq_ignore_ascii_case(accelerator))
        .map(|(_, action)| *action)
}

/// 解析全部默认绑定。任一条解析失败都返回 `Err`：这是配置错误，应当响亮。
pub fn default_bindings() -> Result<Vec<(Shortcut, ShortcutAction)>, String> {
    DEFAULT_BINDINGS
        .iter()
        .map(|(acc, action)| {
            acc.parse::<Shortcut>()
                .map(|shortcut| (shortcut, *action))
                .map_err(|_| format!("invalid accelerator in DEFAULT_BINDINGS: {acc}"))
        })
        .collect()
}

/// 构建插件。处理器在插件构建时捕获绑定表，靠 `Shortcut` 相等性派发。
///
/// 用 `Shortcut` 相等性而不是「把按下的键再转成字符串去查表」：
/// `Shortcut` 的 `Display` 会输出规范化形式（如 `Ctrl+Shift+R`），
/// 与 `DEFAULT_BINDINGS` 里写的 `CmdOrCtrl+Shift+R` 字面不同 ——
/// 字符串比对会静默失配。
pub fn plugin<R: Runtime>() -> tauri::plugin::TauriPlugin<R> {
    let bindings = default_bindings().unwrap_or_default();
    Builder::new()
        .with_handler(move |app, shortcut, event| {
            // 只处理按下：否则松开时会再发一次，采集开关会来回抖。
            if event.state() != ShortcutState::Pressed {
                return;
            }
            if let Some((_, action)) = bindings.iter().find(|(s, _)| s == shortcut) {
                let _ = app.emit(action.event_name(), ());
            }
        })
        .build()
}

/// 在 `setup` 里注册默认快捷键，返回注册失败清单（内容无关，只含动作名与错误）。
///
/// 失败**不致命**：快捷键可能被别的程序占用。一个抢不到的加速键
/// 不该让整个应用起不来 —— 返回清单由调用方记录即可。
pub fn register_defaults<R: Runtime>(app: &AppHandle<R>) -> Vec<String> {
    let bindings = match default_bindings() {
        Ok(bindings) => bindings,
        Err(e) => return vec![e],
    };
    let mut failures = Vec::new();
    for (shortcut, action) in bindings {
        if let Err(e) = app.global_shortcut().register(shortcut) {
            failures.push(format!("{:?}: {e}", action));
        }
    }
    failures
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn action_for_maps_every_default_binding() {
        assert_eq!(
            action_for("CmdOrCtrl+Shift+R"),
            Some(ShortcutAction::ToggleCapture)
        );
        assert_eq!(
            action_for("CmdOrCtrl+Shift+Space"),
            Some(ShortcutAction::TriggerTeleprompter)
        );
    }

    #[test]
    fn action_for_is_case_insensitive() {
        assert_eq!(
            action_for("cmdorctrl+shift+r"),
            Some(ShortcutAction::ToggleCapture)
        );
    }

    #[test]
    fn action_for_returns_none_for_unknown_accelerator() {
        assert_eq!(action_for("CmdOrCtrl+Q"), None);
        assert_eq!(action_for(""), None);
    }

    #[test]
    fn event_names_are_stable_contract() {
        // 前端按字面订阅这两个名字，改动即破坏契约。
        assert_eq!(
            ShortcutAction::ToggleCapture.event_name(),
            "capture://toggle"
        );
        assert_eq!(
            ShortcutAction::TriggerTeleprompter.event_name(),
            "teleprompter://trigger"
        );
    }

    #[test]
    fn default_bindings_all_parse() {
        let bindings = default_bindings().expect("default bindings must parse");
        assert_eq!(bindings.len(), DEFAULT_BINDINGS.len());
    }

    #[test]
    fn default_bindings_are_distinct() {
        // 重复绑定会让其中一个永远收不到事件（先注册者胜）。
        let bindings = default_bindings().expect("parse");
        for (i, (a, _)) in bindings.iter().enumerate() {
            for (b, _) in bindings.iter().skip(i + 1) {
                assert_ne!(a, b, "duplicate shortcut in DEFAULT_BINDINGS");
            }
        }
    }

    #[test]
    fn every_action_is_bound_exactly_once() {
        let actions: Vec<_> = DEFAULT_BINDINGS.iter().map(|(_, a)| *a).collect();
        for expected in [
            ShortcutAction::ToggleCapture,
            ShortcutAction::TriggerTeleprompter,
        ] {
            assert_eq!(
                actions.iter().filter(|a| **a == expected).count(),
                1,
                "{expected:?} must be bound exactly once"
            );
        }
    }
}
