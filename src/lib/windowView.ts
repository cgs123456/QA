/**
 * 当前 webview 是主窗还是提词窗（taskP7）。
 *
 * 提词窗是**独立** `WebviewWindow`，但复用同一个前端入口：Rust 侧建窗时用
 * `initialization_script` 注入 `window.__INTERVIEW_COPILOT_VIEW__`
 * （见 `src-tauri/src/stealth/overlay.rs::init_script`），这里按它分叉。
 *
 * 为什么不用 URL query（`index.html?view=teleprompter`）：`WebviewUrl::App` 收的是
 * `PathBuf`，把 query 塞进路径在不同平台上行为不一致。注入脚本在页面脚本之前
 * 执行，且不依赖任何构建配置。
 *
 * 手机伴侣屏（S8）**不经过这里**：它是手机浏览器里的一份独立页面，由 Rust 侧
 * `companion::phone_page::PAGE` 直接下发，跟这个前端包没有关系 —— 所以这里
 * 没有 `companion` 视图。
 */

/** Rust `stealth::overlay::VIEW_GLOBAL` 的字面对应。改一处必须改两处。 */
export const VIEW_GLOBAL = "__INTERVIEW_COPILOT_VIEW__";
/** Rust `stealth::overlay::VIEW_VALUE` 的字面对应。 */
export const VIEW_TELEPROMPTER = "teleprompter";

export type WindowView = "main" | "teleprompter";

/**
 * 读视图标记。**认不出就是主窗**：提词窗只是附加，
 * 认错成提词窗会让用户的主界面变成一块空的透明面板 —— 失败方向要选安全的那边。
 */
export function readWindowView(
  source: Record<string, unknown> | undefined | null,
): WindowView {
  if (source == null) return "main";
  const v = source[VIEW_GLOBAL];
  if (v === VIEW_TELEPROMPTER) return "teleprompter";
  return "main";
}
