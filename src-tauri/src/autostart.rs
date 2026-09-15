//! 开机自启开关（taskP7）。
//!
//! 走 `tauri-plugin-autostart`：Windows 写注册表 Run 项，macOS 用 LaunchAgent，
//! Linux 写 `~/.config/autostart/*.desktop`。**不自己碰注册表**——
//! 三个平台的实现差异不值得在本仓重写一遍，而且自写注册表代码很难测。
//!
//! # 「注册失败不阻断启动」这条纪律在这里怎么落地
//!
//! 关键在于分清两件事：
//!
//! - **启动时**：我们**不注册**任何东西。插件 `setup` 只做一件事——把
//!   `current_exe()` 解析出来存进 `AutoLaunchManager`，**不写注册表**。
//!   所以「启动时自启注册失败」这个场景根本不存在：没有注册，就没有注册失败。
//! - **用户拨开关时**：这时才真的写注册表。写失败（组策略禁用 Run 项、
//!   无权限、被杀软拦截）会以 `Err` 返回给 UI 显示成一句话，**不影响应用其余功能**，
//!   也不 panic。
//!
//! 残留风险只有一个：插件 `setup` 里 `current_exe()` 失败会让插件初始化失败。
//! 那属于「进程镜像路径读不出来」的极端环境，同一时刻 sidecar 拉起与
//! 快捷键注册也都会失败——不是本模块能单独兜住的，故不为此加防御代码。
//!
//! # 为什么 `view()` 要单独拎出来
//!
//! 「读不到」和「确认是关的」是两件事。把读失败显示成「已关闭」，
//! 用户会以为自己成功关掉了自启，其实只是没读到 —— 那是**骗人**。
//! 所以 [`AutostartState::available`] 与 `enabled` 分开表达。

use serde::Serialize;
use tauri::{AppHandle, Manager, Runtime};
use tauri_plugin_autostart::AutoLaunchManager;

/// 插件没就绪时的统一说明（内容无关，不含路径）。
pub const UNAVAILABLE: &str =
    "自启插件未就绪（插件初始化失败）；开关不可用，应用其余功能不受影响。";

/// 平台是否支持自启注册。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AutostartSupport {
    Native,
    Unsupported,
}

/// `auto-launch` 只实现 Windows / macOS / Linux；其它平台（含移动端）明示不支持。
pub const fn support() -> AutostartSupport {
    if cfg!(any(windows, target_os = "macos", target_os = "linux")) {
        AutostartSupport::Native
    } else {
        AutostartSupport::Unsupported
    }
}

/// 给 Settings 面板渲染的状态。
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct AutostartState {
    /// 是否已注册开机自启。**读失败时也是 `false`**，所以必须同时看 `available`。
    pub enabled: bool,
    /// 读写通道是否可用（平台支持 + 插件就绪 + 上一次读写成功）。
    pub available: bool,
    /// 给用户看的一句话。
    pub note: String,
}

/// 读到的结果 + 平台支持 → 前端视图。纯函数。
pub fn view(read: Result<bool, String>, support: AutostartSupport) -> AutostartState {
    if support == AutostartSupport::Unsupported {
        return AutostartState {
            enabled: false,
            available: false,
            note: "当前平台不支持开机自启开关。".to_string(),
        };
    }
    match read {
        Ok(enabled) => AutostartState {
            enabled,
            available: true,
            note: if enabled {
                "已设置开机自启。".to_string()
            } else {
                "未设置开机自启。".to_string()
            },
        },
        // 读失败 ≠ 关闭。这里 available=false 就是让 UI 别把「不知道」画成「关」。
        Err(e) => AutostartState {
            enabled: false,
            available: false,
            note: format!("自启状态读取失败：{e}"),
        },
    }
}

/// 取插件管理的自启句柄。插件没就绪时返回 `Err` 而**不是 panic**
/// （`ManagerExt::autolaunch()` 内部是 `state()`，取不到会 panic —— 这里不用它）。
pub fn manager<R: Runtime>(
    app: &AppHandle<R>,
) -> Result<tauri::State<'_, AutoLaunchManager>, String> {
    app.try_state::<AutoLaunchManager>()
        .ok_or_else(|| UNAVAILABLE.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn read_failure_is_not_reported_as_disabled() {
        // 这条是本模块最要紧的断言：把「读不到」画成「已关闭」是骗人。
        let state = view(Err("registry denied".into()), AutostartSupport::Native);
        assert!(!state.enabled);
        assert!(
            !state.available,
            "读失败必须让 available=false，UI 才不会画成「已关闭」"
        );
        assert!(
            state.note.contains("registry denied"),
            "原因要能看见：{}",
            state.note
        );
    }

    #[test]
    fn enabled_and_disabled_are_both_available() {
        let on = view(Ok(true), AutostartSupport::Native);
        assert!(on.enabled && on.available);

        let off = view(Ok(false), AutostartSupport::Native);
        assert!(!off.enabled, "确认关闭 ≠ 读不到");
        assert!(off.available, "确认关闭时通道是好的，available 必须是 true");
    }

    #[test]
    fn unsupported_platform_never_claims_a_value() {
        let state = view(Ok(true), AutostartSupport::Unsupported);
        assert!(!state.available);
        assert!(!state.enabled, "平台不支持时不许把 enabled 画成 true");
        assert!(state.note.contains("不支持"));
    }

    #[test]
    fn support_matches_the_platform() {
        if cfg!(any(windows, target_os = "macos", target_os = "linux")) {
            assert_eq!(support(), AutostartSupport::Native);
        } else {
            assert_eq!(support(), AutostartSupport::Unsupported);
        }
    }
}
