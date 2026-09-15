//! 置顶透明提词窗（taskP7）。
//!
//! **它是什么**：一个**独立**的 `WebviewWindow`（label [`OVERLAY_LABEL`]），
//! 无边框、透明、置顶、不进任务栏，用于面试时把提词内容浮在别的窗口之上。
//!
//! **它不是什么**：不做进程伪装、不做反检测、不做监考/录屏绕过。
//! 本模块只声明「本应用自己的窗口不要出现在屏幕捕获里」——
//! 这是 Windows/macOS 都提供的**正规窗口属性**，不是注入，也不是挂钩。
//! 完整禁止清单见 `docs/stealth-boundary.md`。
//!
//! # 三个必须写清的预期（否则会被当成 bug）
//!
//! 1. **`WDA_EXCLUDEFROMCAPTURE` 只影响捕获，不影响本人观看**。窗口对使用者
//!    照常可见——这正是我们要的（提词窗自己看不见就没用了）。
//! 2. **它不保证「绝对拍不到」**。硬件采集卡、部分远程桌面/虚拟机方案在系统层
//!    绕过 DWM，仍可能拍到。本模块不做「绝对隐身」的承诺。
//! 3. **`sharingType(.none)`（macOS）与 `WDA_*`（Windows）语义不同**：前者同时
//!    影响截屏与录屏且由窗口自己声明，后者只对 DWM 捕获生效。跨平台行为差异
//!    在 `docs/stealth-boundary.md` 里逐条列。
//!
//! # 为什么决策要拎成纯函数
//!
//! 和 `shortcuts.rs` 同一个理由：CI 里开不出真窗口，能钉住的只有决策本身。
//! 所以 [`plan`] / [`spec`] / [`capture_exclusion_support`] / [`init_script`]
//! 全是纯的，碰 Tauri 的只有下面那几个薄壳 `pub fn`。

use tauri::{AppHandle, Manager, Runtime, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

/// 提词窗的窗口 label。命令、托盘、前端都按这个名字找窗口——是契约的一部分。
pub const OVERLAY_LABEL: &str = "teleprompter";

/// 提词窗标题。窗口不进任务栏，这个标题主要给任务管理器与无障碍工具看。
pub const OVERLAY_TITLE: &str = "InterviewCopilot 提词";

/// 注入到提词窗的全局变量名，前端据此判断「我是提词窗」。
pub const VIEW_GLOBAL: &str = "__INTERVIEW_COPILOT_VIEW__";

/// 提词窗的取值。前端 `src/lib/windowView.ts` 里有一份对应的常量，改动即破坏契约。
pub const VIEW_VALUE: &str = "teleprompter";

/// 建窗时注入的启动脚本。
///
/// 用**注入脚本**而不是 URL query（`index.html?view=...`）或第二个 HTML 入口：
///
/// - `WebviewUrl::App` 收的是 `PathBuf`，把 query 塞进路径在不同平台上行为不一致；
/// - 第二个 HTML 入口要动 Vite 的 `rollupOptions.input`，为一个小窗口改构建配置不划算。
///
/// 脚本在页面脚本之前执行，`main.tsx` 读这个全局变量即可。用函数而不是常量，
/// 是为了让它只由 [`VIEW_GLOBAL`] / [`VIEW_VALUE`] 派生，不存在第二处字面量。
pub fn init_script() -> String {
    format!("window.{VIEW_GLOBAL}={VIEW_VALUE:?};")
}

/// 提词窗的几何与窗口属性。**纯数据**——壳层照着它建窗口，测试照着它钉契约。
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct OverlaySpec {
    pub label: &'static str,
    pub title: &'static str,
    pub width: f64,
    pub height: f64,
    pub always_on_top: bool,
    pub decorations: bool,
    pub transparent: bool,
    pub skip_taskbar: bool,
    pub resizable: bool,
    pub shadow: bool,
    /// 是否允许获得焦点。
    ///
    /// 刻意 `false`：提词窗一抢焦点就会把正在面试的那个窗口踢到后台，
    /// 那是这个功能最典型的失败方式。Windows 上对应 `WS_EX_NOACTIVATE`——
    /// 它**不影响鼠标消息**，所以后续要加拖动/滚动仍然可行。
    pub focusable: bool,
    /// 建窗时是否立刻可见。`false`：建完由 [`Intent`] 决定显隐，避免「先闪一下再隐藏」。
    pub initially_visible: bool,
}

/// 提词窗规格。尺寸给的是「一屏能放下 3 条提词」的最小值，可手动拉大。
pub const fn spec() -> OverlaySpec {
    OverlaySpec {
        label: OVERLAY_LABEL,
        title: OVERLAY_TITLE,
        width: 420.0,
        height: 320.0,
        always_on_top: true,
        decorations: false,
        transparent: true,
        skip_taskbar: true,
        resizable: true,
        shadow: false,
        focusable: false,
        initially_visible: false,
    }
}

/// 窗口当前是否存在。由壳层查 manager 得到后传进来——纯函数不碰 Tauri。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Presence {
    Absent,
    Present,
}

/// 调用方想让提词窗处于什么状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Intent {
    Show,
    Hide,
    Destroy,
}

/// 该做的动作。`AlreadyOk` 表示什么都不用做——整套接口是幂等的。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Plan {
    Create,
    Show,
    Hide,
    Destroy,
    AlreadyOk,
}

/// 决策表：存在性 + 当前可见性 + 意图 → 动作。
///
/// 两条刻意的选择：
///
/// - `Absent` + `Hide`/`Destroy` → `AlreadyOk`。**不为了「隐藏」去建一个窗口**：
///   否则托盘点两次「隐藏」会先建后隐，屏幕上闪一下。
/// - `Destroy` 之后再来 `Show` → `Create`，这条路径就是「重建」。
///   窗口崩了不需要特殊恢复逻辑，重建入口和首次创建是同一段代码。
pub const fn plan(presence: Presence, visible: bool, intent: Intent) -> Plan {
    match (presence, intent) {
        (Presence::Absent, Intent::Show) => Plan::Create,
        (Presence::Absent, Intent::Hide | Intent::Destroy) => Plan::AlreadyOk,
        (Presence::Present, Intent::Show) => {
            if visible {
                Plan::AlreadyOk
            } else {
                Plan::Show
            }
        }
        (Presence::Present, Intent::Hide) => {
            if visible {
                Plan::Hide
            } else {
                Plan::AlreadyOk
            }
        }
        (Presence::Present, Intent::Destroy) => Plan::Destroy,
    }
}

/// 「本窗口不进屏幕捕获」在各平台上的可达程度。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CaptureExclusion {
    /// 已通过平台正规 API 生效（Windows `WDA_EXCLUDEFROMCAPTURE` / macOS `sharingType = .none`）。
    Applied,
    /// 只能尽力而为：置顶 + 透明已经做到，但**不保证**不进捕获（Linux 无等价 API）。
    BestEffort,
    /// 平台不支持，或系统调用失败。
    Unsupported,
}

impl CaptureExclusion {
    /// 给前端/文档用的稳定字符串。改动即破坏契约。
    pub const fn tag(self) -> &'static str {
        match self {
            CaptureExclusion::Applied => "applied",
            CaptureExclusion::BestEffort => "best_effort",
            CaptureExclusion::Unsupported => "unsupported",
        }
    }
}

/// 当前平台的可达程度。编译期决定，无副作用。
pub const fn capture_exclusion_support() -> CaptureExclusion {
    if cfg!(windows) || cfg!(target_os = "macos") {
        CaptureExclusion::Applied
    } else {
        // Linux：X11 下没有任何「不进捕获」的正规属性；Wayland 下连置顶都不保证
        // （合成器说了算）。明示不支持，不做假承诺。
        CaptureExclusion::BestEffort
    }
}

/// 提词窗对外可见的状态快照（命令返回值）。
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct OverlayStatus {
    pub label: &'static str,
    pub present: bool,
    pub visible: bool,
    /// [`CaptureExclusion::tag`]。
    pub capture_exclusion: &'static str,
}

// ---------------------------------------------------------------------------
// 薄壳层：以下函数才碰 Tauri。
// ---------------------------------------------------------------------------

fn window<R: Runtime>(app: &AppHandle<R>) -> Result<WebviewWindow<R>, String> {
    app.get_webview_window(OVERLAY_LABEL)
        .ok_or_else(|| format!("提词窗（{OVERLAY_LABEL}）不存在"))
}

/// 查当前存在性与可见性。
pub fn presence<R: Runtime>(app: &AppHandle<R>) -> (Presence, bool) {
    match app.get_webview_window(OVERLAY_LABEL) {
        // `is_visible` 失败当作「不可见」：这是查询，不是命令，宁可保守。
        Some(w) => (Presence::Present, w.is_visible().unwrap_or(false)),
        None => (Presence::Absent, false),
    }
}

/// 状态快照。
pub fn status<R: Runtime>(app: &AppHandle<R>) -> OverlayStatus {
    let (presence, visible) = presence(app);
    OverlayStatus {
        label: OVERLAY_LABEL,
        present: presence == Presence::Present,
        visible,
        capture_exclusion: capture_exclusion_support().tag(),
    }
}

fn build_overlay<R: Runtime>(app: &AppHandle<R>) -> Result<WebviewWindow<R>, String> {
    let s = spec();
    WebviewWindowBuilder::new(app, s.label, WebviewUrl::App("index.html".into()))
        .title(s.title)
        .inner_size(s.width, s.height)
        .always_on_top(s.always_on_top)
        .decorations(s.decorations)
        .transparent(s.transparent)
        .skip_taskbar(s.skip_taskbar)
        .resizable(s.resizable)
        .shadow(s.shadow)
        .focusable(s.focusable)
        .focused(false)
        .visible(s.initially_visible)
        .initialization_script(init_script())
        // macOS：tao 内部就是 `ns_window.setSharingType(NSWindowSharingType::None)`，
        // 即任务书里写的 `sharingType(.none)`。其余平台是 no-op（Windows 的等价物
        // 由 `apply_capture_exclusion` 单独做）。
        .content_protected(true)
        .build()
        .map_err(|e| format!("创建提词窗失败：{e}"))
}

/// 执行一次意图，返回执行后的状态。
///
/// **不返回 `Plan`**：调用方（命令 / 托盘）只关心「现在是什么样」，
/// 不关心内部走了哪条分支——那是实现细节，暴露出去只会多一处要同步的契约。
pub fn apply<R: Runtime>(app: &AppHandle<R>, intent: Intent) -> Result<OverlayStatus, String> {
    let (presence, visible) = presence(app);
    match plan(presence, visible, intent) {
        Plan::AlreadyOk => {}
        Plan::Create => {
            let w = build_overlay(app)?;
            let exclusion = apply_capture_exclusion(&w);
            if exclusion != CaptureExclusion::Applied {
                // 不是致命错误：窗口照常可用，只是「不进捕获」这一条没兑现。
                // 记录而不上报，避免把「平台不支持」伪装成「操作失败」。
                eprintln!(
                    "[stealth] 提词窗捕获排除未生效（{:?}，平台结论 {}）",
                    exclusion,
                    capture_exclusion_support().tag()
                );
            }
            if intent == Intent::Show {
                w.show().map_err(|e| format!("显示提词窗失败：{e}"))?;
            }
        }
        Plan::Show => window(app)?
            .show()
            .map_err(|e| format!("显示提词窗失败：{e}"))?,
        Plan::Hide => window(app)?
            .hide()
            .map_err(|e| format!("隐藏提词窗失败：{e}"))?,
        Plan::Destroy => window(app)?
            .destroy()
            .map_err(|e| format!("销毁提词窗失败：{e}"))?,
    }
    Ok(status(app))
}

/// 把「不进捕获」这条属性打到已建好的窗口上。
///
/// 建窗时已经设过一次；这里是**重建路径**的保障：`Plan::Create` 每次都调，
/// 所以窗口崩掉重建之后属性不会丢。
#[cfg(windows)]
pub fn apply_capture_exclusion<R: Runtime>(window: &WebviewWindow<R>) -> CaptureExclusion {
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        SetWindowDisplayAffinity, WDA_EXCLUDEFROMCAPTURE,
    };

    let hwnd = match window.hwnd() {
        Ok(h) => h,
        Err(e) => {
            eprintln!("[stealth] 取 HWND 失败：{e}");
            return CaptureExclusion::Unsupported;
        }
    };
    // tauri 的 `hwnd()` 返回 `windows::Win32::Foundation::HWND(pub *mut c_void)`，
    // 而 windows-sys 的 `HWND` 直接就是 `*mut c_void`：裸指针直传即可，
    // 所以不必把 `windows` crate 也加进依赖（少一棵大树）。
    //
    // SAFETY: `hwnd` 来自本进程存活窗口的原生句柄；`SetWindowDisplayAffinity`
    // 只读该句柄，不转移所有权、不释放。失败返回 0，不 panic。
    let ok = unsafe { SetWindowDisplayAffinity(hwnd.0, WDA_EXCLUDEFROMCAPTURE) };
    if ok == 0 {
        CaptureExclusion::Unsupported
    } else {
        CaptureExclusion::Applied
    }
}

/// macOS 等价物：`sharingType = .none`。
#[cfg(target_os = "macos")]
pub fn apply_capture_exclusion<R: Runtime>(window: &WebviewWindow<R>) -> CaptureExclusion {
    match window.set_content_protected(true) {
        Ok(()) => CaptureExclusion::Applied,
        Err(e) => {
            eprintln!("[stealth] 设置内容保护失败：{e}");
            CaptureExclusion::Unsupported
        }
    }
}

/// Linux 及其它平台：没有等价 API，只能尽力而为。
#[cfg(not(any(windows, target_os = "macos")))]
pub fn apply_capture_exclusion<R: Runtime>(_window: &WebviewWindow<R>) -> CaptureExclusion {
    // 置顶 + 透明 + 跳过任务栏已经由 `spec()` 做到，但「不进捕获」做不到。
    // 这里**不**尝试任何偏门手段（合成器 hack、X11 shape 扩展、录制管道探测）——
    // 那是 PRD §1.6 明令禁止的方向，见 docs/stealth-boundary.md。
    CaptureExclusion::BestEffort
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn spec_is_transparent_always_on_top_and_offscreen_by_default() {
        let s = spec();
        assert_eq!(
            s.label, "teleprompter",
            "label 是跨模块契约，改了要同步四处"
        );
        assert!(s.always_on_top, "提词窗不置顶就没有存在意义");
        assert!(s.transparent, "透明是「浮在别人窗口上」的前提");
        assert!(s.skip_taskbar, "提词窗出现在任务栏会打断面试时的窗口切换");
        assert!(!s.decorations, "有边框就不透明了");
        assert!(!s.shadow, "阴影会在透明窗口外围留一圈灰");
        assert!(!s.initially_visible, "先建后闪一下再隐藏会被当成 bug");
        assert!(
            !s.focusable,
            "抢焦点会把面试窗口踢到后台——这是本功能最典型的失败方式"
        );
        assert!(s.resizable, "尺寸可调，用户才能按自己的屏幕摆");
        assert!(s.width > 0.0 && s.height > 0.0);
    }

    #[test]
    fn plan_covers_the_full_create_show_destroy_rebuild_cycle() {
        // 首次显示：不存在 → 建。
        assert_eq!(plan(Presence::Absent, false, Intent::Show), Plan::Create);
        // 建完已可见，再 Show：幂等。
        assert_eq!(plan(Presence::Present, true, Intent::Show), Plan::AlreadyOk);
        // 建完还不可见（Create 时 intent=Hide 的场景），Show 要真的显示。
        assert_eq!(plan(Presence::Present, false, Intent::Show), Plan::Show);
        // 隐藏。
        assert_eq!(plan(Presence::Present, true, Intent::Hide), Plan::Hide);
        // 销毁。
        assert_eq!(
            plan(Presence::Present, true, Intent::Destroy),
            Plan::Destroy
        );
        // 重建：销毁之后再 Show，走的还是 Create —— 没有单独的「恢复」分支。
        assert_eq!(plan(Presence::Absent, false, Intent::Show), Plan::Create);
    }

    #[test]
    fn hide_and_destroy_are_idempotent_when_absent() {
        // 不为了「隐藏/销毁」去建一个窗口：否则连点两次会先建后隐，屏幕上闪一下。
        assert_eq!(plan(Presence::Absent, false, Intent::Hide), Plan::AlreadyOk);
        assert_eq!(
            plan(Presence::Absent, false, Intent::Destroy),
            Plan::AlreadyOk
        );
        // 已隐藏时再 Hide 也是空操作。
        assert_eq!(
            plan(Presence::Present, false, Intent::Hide),
            Plan::AlreadyOk
        );
    }

    #[test]
    fn init_script_is_derived_from_the_two_constants() {
        let script = init_script();
        assert!(
            script.contains(VIEW_GLOBAL),
            "注入脚本必须设置 {VIEW_GLOBAL}，否则前端认不出提词窗：{script}"
        );
        assert!(
            script.contains(VIEW_VALUE),
            "注入脚本必须带上 {VIEW_VALUE} 这个取值：{script}"
        );
        assert!(script.ends_with(';'), "注入脚本是语句，要有分号：{script}");
    }

    #[test]
    fn capture_exclusion_support_matches_the_documented_matrix() {
        let tag = capture_exclusion_support().tag();
        if cfg!(windows) || cfg!(target_os = "macos") {
            assert_eq!(
                tag, "applied",
                "Windows/macOS 都有正规 API，不该退到 best_effort"
            );
        } else {
            assert_eq!(tag, "best_effort", "Linux 没有等价 API，不许声称 applied");
        }
    }

    #[test]
    fn capture_exclusion_tags_are_a_stable_contract() {
        // 前端与 docs/stealth-boundary.md 按字面引用这三个值。
        assert_eq!(CaptureExclusion::Applied.tag(), "applied");
        assert_eq!(CaptureExclusion::BestEffort.tag(), "best_effort");
        assert_eq!(CaptureExclusion::Unsupported.tag(), "unsupported");
    }
}
