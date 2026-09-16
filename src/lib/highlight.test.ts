import { describe, expect, it } from "vitest";
import {
  BACKEND_CLOSE,
  BACKEND_OPEN,
  DEFAULT_MARKERS,
  ROUTE_MARKERS,
  escapeHtml,
  markersForRoutes,
  renderHighlight,
} from "./highlight";

describe("escapeHtml", () => {
  it("escapes tag-significant chars (& first)", () => {
    expect(escapeHtml(`<img src=x onerror="y">&`)).toBe(
      "&lt;img src=x onerror=&quot;y&quot;&gt;&amp;",
    );
  });
});

describe("markersForRoutes", () => {
  it("picks simple markers for the simple route, default otherwise", () => {
    expect(markersForRoutes(["simple"])).toEqual(ROUTE_MARKERS.simple);
    expect(markersForRoutes(["jieba"])).toEqual(DEFAULT_MARKERS);
    expect(markersForRoutes([])).toEqual(DEFAULT_MARKERS);
    expect(markersForRoutes(undefined)).toEqual(DEFAULT_MARKERS);
    expect(markersForRoutes(["jieba", "simple"])).toEqual(ROUTE_MARKERS.simple);
  });
});

describe("renderHighlight", () => {
  it("wraps backend tokens with default markers", () => {
    expect(renderHighlight(`${BACKEND_OPEN}发货${BACKEND_CLOSE}周期`)).toEqual({
      __html: "<mark>发货</mark>周期",
    });
  });

  it("supports custom markers", () => {
    expect(
      renderHighlight(`${BACKEND_OPEN}发货${BACKEND_CLOSE}`, {
        open: "[",
        close: "]",
      }),
    ).toEqual({ __html: "[发货]" });
  });

  it("keeps answer text inert (XSS)", () => {
    const evil = `<img src=x onerror="alert(1)">`;
    const { __html } = renderHighlight(`${BACKEND_OPEN}${evil}${BACKEND_CLOSE}`);
    // 转义后原文里的尖括号全部实体化，只有开发者常量是真标签。
    expect(__html).not.toContain("<img");
    expect(__html).toContain("&lt;img");
    expect(__html).toContain("<mark>");
  });

  it("leaves unmarked text escaped but tag-free", () => {
    expect(renderHighlight("三天内发出<a>").__html).toBe("三天内发出&lt;a&gt;");
  });

  it("tolerates empty input", () => {
    expect(renderHighlight("").__html).toBe("");
  });

  it("backend contract constants match the sidecar", () => {
    expect(BACKEND_OPEN).toBe("<mark>");
    expect(BACKEND_CLOSE).toBe("</mark>");
  });
});
