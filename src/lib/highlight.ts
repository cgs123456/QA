/**
 * 高亮渲染（F2.3 用户面，纯函数，可单测）：后端 `hl_*` 字段 → 安全 HTML。
 *
 * 安全模型：后端只插字面 `<mark>`/`</mark>` token；本模块先整体转义
 * （含 token 本体），再把转义后的 token 换成**开发者常量** markers。
 * 因此：① 知识库原文里的 `<script>`/`<img onerror>` 永远 inert；
 * ② markers 来自代码常量而非用户输入（XSS 审计点）。
 * 已知边界：原文若字面含有 `<mark>` 会被一并套框（纯展示伪影，
 * 文本本身不错位）；中文无分词截断问题（后端给的是整列文本，非摘要）。
 */

export type HighlightMarkers = { open: string; close: string };

/** 后端打标原文（合约：`simple_highlight` 默认标记串，改动需同步测试）。 */
export const BACKEND_OPEN = "<mark>";
export const BACKEND_CLOSE = "</mark>";

export const DEFAULT_MARKERS: HighlightMarkers = { open: "<mark>", close: "</mark>" };

/**
 * simple 路是字级标记（满屏小框视觉噪音大），给独立样式以示区分；
 * 其余路（含 jieba、无路由信息）走默认。
 */
export const ROUTE_MARKERS: Record<string, HighlightMarkers> = {
  simple: { open: '<mark class="hl-simple">', close: "</mark>" },
};

export function markersForRoutes(routes?: string[]): HighlightMarkers {
  if (routes?.includes("simple")) return ROUTE_MARKERS.simple;
  return DEFAULT_MARKERS;
}

export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** 带标文本 → 可直接喂 `dangerouslySetInnerHTML` 的对象（见模块头安全模型）。 */
export function renderHighlight(
  hl: string,
  markers: HighlightMarkers = DEFAULT_MARKERS,
): { __html: string } {
  const escaped = escapeHtml(hl ?? "");
  const opened = escaped.split("&lt;mark&gt;").join(markers.open);
  return { __html: opened.split("&lt;/mark&gt;").join(markers.close) };
}
