//! 系统托盘（taskP7）：最小化到托盘 + 左键显隐主窗 + 右键菜单。
//!
//! # 职责边界
//!
//! 托盘**不拥有**任何状态，它只把点击翻译成三件事之一：
//!
//! 1. 显隐提词窗 → 交给 `stealth::overlay::apply`（那才是窗口的真值源）；
//! 2. 开始/停止采集 → **发 `capture://toggle` 事件**，复用 `shortcuts.rs` 的契约，
//!    不新造事件名、不直接调采集服务（快捷键与托盘必须是同一条路径，
//!    否则「按了没反应」这类问题会有两个互不相同的成因）；
//! 3. 退出 → `app.exit(0)`。
//!
//! 把「菜单 id → 动作」做成可单测的纯查表，理由与 `shortcuts.rs` 相同：
//! CI 里点不了托盘图标，能钉住的只有映射关系本身，那就把映射关系钉死。
//!
//! # 采集项为什么是灰的
//!
//! M2-8（C1b）已经把 `capture://toggle` 的消费方
//! （`commands::audio::spawn_toggle_listener`）接上了，但 taskP7 的任务书要求
//! 这一项**先灰置**。保持灰置是刻意的：托盘是常驻入口，误点一次就会在用户
//! 没准备的时候开麦。要放开只改 [`CAPTURE_WIRED`] —— 标签与 `enabled` 都由它派生，
//! 没有第二处开关。

use std::sync::atomic::{AtomicBool, Ordering};

use tauri::menu::{MenuBuilder, MenuItemBuilder};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, Runtime};

use crate::stealth::overlay;

/// 托盘图标 id。只建一个托盘，但显式给 id，避免将来多托盘时靠「第一个」猜。
pub const TRAY_ID: &str = "main-tray";

/// 托盘 tooltip。
pub const TRAY_TOOLTIP: &str = "InterviewCopilot";

/// 右键菜单项 id（契约：前端/测试按字面引用）。
pub const MENU_SHOW_OVERLAY: &str = "tray.show_overlay";
pub const MENU_TOGGLE_CAPTURE: &str = "tray.toggle_capture";
pub const MENU_QUIT: &str = "tray.quit";

/// 采集开关是否已接线。见模块头注释：taskP7 阶段刻意保持 `false`（灰置）。
pub const CAPTURE_WIRED: bool = false;

/// 主窗的 label。托盘「左键显隐主窗」按这个名字找窗口。
pub const MAIN_WINDOW_LABEL: &str = "main";

/// 托盘菜单项。`label` 固定，不随状态变 —— 动态改标签要持有 `MenuItem` 句柄并
/// `set_text`，为一行文案多存一份跨窗口状态不划算（见 `docs/manual-verification-p7.md`）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TrayMenuItem {
    pub id: &'static str,
    pub label: &'static str,
    pub enabled: bool,
}

/// 菜单项点击后要做的事。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TrayAction {
    /// 提词窗显隐。
    ToggleOverlay,
    /// 开始/停止采集。
    ToggleCapture,
    Quit,
}

impl TrayAction {
    pub const fn menu_id(self) -> &'static str {
        match self {
            TrayAction::ToggleOverlay => MENU_SHOW_OVERLAY,
            TrayAction::ToggleCapture => MENU_TOGGLE_CAPTURE,
            TrayAction::Quit => MENU_QUIT,
        }
    }

    /// 需要「发事件」的动作返回事件名，其余返回 `None`。
    ///
    /// 采集复用 `shortcuts.rs::EVT_CAPTURE_TOGGLE` 的字面值（`capture://toggle`）。
    /// 这里**不** `use` 那个常量而是重复字面量：两个常量指向同一个字符串，
    /// `menu_ids_and_event_names_are_a_stable_contract` 会断言它们相等，
    /// 所以不存在「改了一处忘了另一处」的空间；反过来，跨模块 `use` 会让
    /// 这一层的依赖方向变脏（托盘不该依赖快捷键模块）。
    pub const fn event_name(self) -> Option<&'static str> {
        match self {
            TrayAction::ToggleCapture => Some("capture://toggle"),
            TrayAction::ToggleOverlay | TrayAction::Quit => None,
        }
    }
}

/// 右键菜单（纯数据，顺序即菜单顺序）。
pub fn menu(capture_wired: bool) -> [TrayMenuItem; 3] {
    [
        TrayMenuItem {
            id: MENU_SHOW_OVERLAY,
            // 任务书写的是「显示提词」。这里用「显示/隐藏」：菜单项是常驻的，
            // 写死「显示」而行为是切换，用户会以为点了没反应。
            label: "显示/隐藏提词窗",
            enabled: true,
        },
        TrayMenuItem {
            id: MENU_TOGGLE_CAPTURE,
            label: "开始/停止采集",
            enabled: capture_wired,
        },
        TrayMenuItem {
            id: MENU_QUIT,
            label: "退出",
            enabled: true,
        },
    ]
}

/// 菜单 id → 动作（纯查表）。
pub fn action_for_menu_id(id: &str) -> Option<TrayAction> {
    match id {
        MENU_SHOW_OVERLAY => Some(TrayAction::ToggleOverlay),
        MENU_TOGGLE_CAPTURE => Some(TrayAction::ToggleCapture),
        MENU_QUIT => Some(TrayAction::Quit),
        _ => None,
    }
}

/// 菜单 id 是否可点。`capture_wired` 只影响采集那一项。
pub fn menu_item_enabled(id: &str, capture_wired: bool) -> bool {
    menu(capture_wired)
        .iter()
        .find(|item| item.id == id)
        .map(|item| item.enabled)
        .unwrap_or(false)
}

/// 左键点托盘图标：主窗可见就藏、不可见就显。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MainWindowAction {
    Show,
    Hide,
}

pub const fn main_window_action(visible: bool) -> MainWindowAction {
    if visible {
        MainWindowAction::Hide
    } else {
        MainWindowAction::Show
    }
}

/// 主窗收到「关闭」请求时该怎么办。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClosePlan {
    /// 收进托盘，不退出。
    HideToTray,
    /// 真退出。
    Quit,
}

/// 关闭主窗的决策。
///
/// `tray_ready` 为假时必须**真退出**：托盘没建起来（图标被系统策略拦了、
/// 或建图标时出错）还把关闭变成隐藏，用户就再也关不掉这个程序了 ——
/// 只能去任务管理器。宁可失去「最小化到托盘」，也不能留一个关不掉的应用。
pub const fn close_plan(tray_ready: bool) -> ClosePlan {
    if tray_ready {
        ClosePlan::HideToTray
    } else {
        ClosePlan::Quit
    }
}

/// 托盘是否真的建起来了。主窗关闭行为依赖它（见 [`close_plan`]）。
#[derive(Debug, Default)]
pub struct TrayState {
    ready: AtomicBool,
}

impl TrayState {
    pub fn mark_ready(&self) {
        self.ready.store(true, Ordering::SeqCst);
    }

    pub fn is_ready(&self) -> bool {
        self.ready.load(Ordering::SeqCst)
    }
}

// ---------------------------------------------------------------------------
// 薄壳层
// ---------------------------------------------------------------------------

fn toggle_main_window<R: Runtime>(app: &AppHandle<R>) {
    let Some(w) = app.get_webview_window(MAIN_WINDOW_LABEL) else {
        return;
    };
    let visible = w.is_visible().unwrap_or(false);
    match main_window_action(visible) {
        MainWindowAction::Show => {
            let _ = w.show();
            let _ = w.set_focus();
        }
        MainWindowAction::Hide => {
            let _ = w.hide();
        }
    }
}

/// 主窗关闭请求：按 [`close_plan`] 决定收进托盘还是真退出。
///
/// 返回 `true` 表示已拦截（调用方要 `api.prevent_close()`）。
pub fn handle_close_request<R: Runtime>(window: &tauri::Window<R>) -> bool {
    let ready = window
        .app_handle()
        .try_state::<TrayState>()
        .map(|s| s.is_ready())
        .unwrap_or(false);
    match close_plan(ready) {
        ClosePlan::HideToTray => {
            let _ = window.hide();
            true
        }
        ClosePlan::Quit => false,
    }
}

/// 建托盘。返回 `Err` 表示托盘不可用 —— 调用方**只记录、不致命**，
/// 与快捷键「注册失败不阻断启动」同款纪律。
pub fn setup<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    let items = menu(CAPTURE_WIRED);
    let mut builder = MenuBuilder::new(app);
    for item in items {
        let entry = MenuItemBuilder::with_id(item.id, item.label)
            .enabled(item.enabled)
            .build(app)
            .map_err(|e| format!("建托盘菜单项 {} 失败：{e}", item.id))?;
        builder = builder.item(&entry);
    }
    let menu = builder
        .build()
        .map_err(|e| format!("建托盘菜单失败：{e}"))?;

    let mut tray = TrayIconBuilder::with_id(TRAY_ID)
        .tooltip(TRAY_TOOLTIP)
        .menu(&menu)
        // 左键留给「显隐主窗」，所以左键不弹菜单。
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| {
            let id = event.id().as_ref();
            match action_for_menu_id(id) {
                Some(TrayAction::ToggleOverlay) => {
                    let visible = overlay::status(app).visible;
                    let intent = if visible {
                        overlay::Intent::Hide
                    } else {
                        overlay::Intent::Show
                    };
                    if let Err(e) = overlay::apply(app, intent) {
                        eprintln!("[tray] 提词窗操作失败：{e}");
                    }
                }
                Some(TrayAction::ToggleCapture) => {
                    // 灰置项理论上收不到事件；这里再判一次 enabled，
                    // 免得将来有人只改常量忘了同步菜单。
                    if !menu_item_enabled(id, CAPTURE_WIRED) {
                        return;
                    }
                    if let Some(name) = TrayAction::ToggleCapture.event_name() {
                        let _ = app.emit(name, ());
                    }
                }
                Some(TrayAction::Quit) => app.exit(0),
                None => {}
            }
        })
        .on_tray_icon_event(|tray, event| {
            // `MouseButtonState::Up` 在这个枚举里是「松开」—— tauri 该枚举的
            // doc 注释把 Up/Down 写反了（见 `docs/manual-verification-p7.md`
            // 的人工核对项）。只认松开：按下+松开都处理会切两次。
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                toggle_main_window(tray.app_handle());
            }
        });

    // 图标直接复用应用图标：不再引入一份托盘专用资源，也就不会出现
    // 「图标文件漏进打包清单」这类只在安装版上才暴露的问题。
    if let Some(icon) = app.default_window_icon().cloned() {
        tray = tray.icon(icon);
    } else {
        eprintln!("[tray] 没有默认窗口图标，托盘将没有图标（功能不受影响）");
    }

    tray.build(app).map_err(|e| format!("建托盘失败：{e}"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn menu_ids_are_a_stable_contract() {
        assert_eq!(MENU_SHOW_OVERLAY, "tray.show_overlay");
        assert_eq!(MENU_TOGGLE_CAPTURE, "tray.toggle_capture");
        assert_eq!(MENU_QUIT, "tray.quit");
    }

    #[test]
    fn every_menu_id_dispatches_to_exactly_one_action() {
        for item in menu(CAPTURE_WIRED) {
            let action = action_for_menu_id(item.id)
                .unwrap_or_else(|| panic!("菜单项 {} 没有对应动作", item.id));
            assert_eq!(action.menu_id(), item.id, "id → 动作 → id 必须回到原处");
        }
    }

    #[test]
    fn unknown_menu_id_dispatches_to_nothing() {
        // 多语言/系统注入的菜单项不该被误当成我们的动作。
        assert_eq!(action_for_menu_id(""), None);
        assert_eq!(action_for_menu_id("tray.unknown"), None);
        assert_eq!(action_for_menu_id("show_overlay"), None);
    }

    #[test]
    fn capture_item_is_the_only_disabled_one_and_only_when_unwired() {
        let unwired = menu(false);
        assert!(!menu_item_enabled(MENU_TOGGLE_CAPTURE, false));
        assert!(menu_item_enabled(MENU_SHOW_OVERLAY, false));
        assert!(menu_item_enabled(MENU_QUIT, false));
        assert_eq!(unwired.iter().filter(|i| !i.enabled).count(), 1);

        // 放开接线后三项都该可点：常量是唯一开关。
        let wired = menu(true);
        assert!(wired.iter().all(|i| i.enabled));
    }

    #[test]
    fn toggle_capture_reuses_the_shortcut_event_contract() {
        // 托盘与快捷键必须是同一条路径，否则「按了没反应」会有两个成因。
        assert_eq!(
            TrayAction::ToggleCapture.event_name(),
            Some(crate::shortcuts::EVT_CAPTURE_TOGGLE)
        );
        // 另外两项不该发事件：显隐提词窗走窗口 API，退出走 app.exit。
        assert_eq!(TrayAction::ToggleOverlay.event_name(), None);
        assert_eq!(TrayAction::Quit.event_name(), None);
    }

    #[test]
    fn left_click_toggles_main_window_both_ways() {
        assert_eq!(main_window_action(true), MainWindowAction::Hide);
        assert_eq!(main_window_action(false), MainWindowAction::Show);
    }

    #[test]
    fn close_hides_to_tray_only_when_the_tray_really_exists() {
        assert_eq!(close_plan(true), ClosePlan::HideToTray);
        // 托盘没建起来还隐藏，用户就再也关不掉这个程序了。
        assert_eq!(close_plan(false), ClosePlan::Quit);
    }

    #[test]
    fn tray_state_starts_not_ready_and_is_sticky() {
        let state = TrayState::default();
        assert!(!state.is_ready(), "建托盘之前必须当作「没有托盘」");
        state.mark_ready();
        assert!(state.is_ready());
    }
}
