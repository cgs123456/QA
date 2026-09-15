//! 窗口级增强（taskP7）：置顶透明提词窗 + 「本窗口不进屏幕捕获」。
//!
//! # 边界（PRD §1.6）
//!
//! 本模块只做一件事：**声明本应用自己窗口的显示属性**。
//!
//! - 做：无边框 / 透明 / 置顶 / 跳过任务栏的独立提词窗；
//!   Windows `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`；
//!   macOS `sharingType = .none`（经 tauri `content_protected`）。
//! - **不做**：进程伪装、进程/模块名混淆、反调试、反检测、
//!   监考/录屏/会议软件的绕过或对抗、注入别的进程、挂钩系统调用。
//!
//! 这条线的划法：上面「做」的每一项都是**操作系统公开的窗口属性**，
//! 效果仅限于「本应用自己的窗口」；下面「不做」的每一项都是
//! **对别人的程序或系统动手**。完整禁止清单与理由见 `docs/stealth-boundary.md`。
//!
//! # 为什么决策要拎成纯函数
//!
//! 和 `shortcuts.rs` 同一个理由：CI 里开不出真窗口，「窗口该不该建、
//! 建完该显还是该隐」这个决策本身却是能钉住的，那就把决策钉死。
//! [`overlay::plan`] / [`overlay::spec`] / [`overlay::capture_exclusion_support`]
//! 全是纯的；碰 Tauri 的只有 `overlay` 里最外层那几个薄壳 `pub fn`。

pub mod overlay;
