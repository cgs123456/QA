import { describe, expect, it, vi, afterEach } from "vitest";
import { ApiError, classifyFetchError, sidecarFetch } from "./api";

vi.mock("@tauri-apps/api/core", () => ({
  invoke: async () => ({ port: 1, token: "t" }),
}));

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("classifyFetchError", () => {
  it("passes ApiError through", () => {
    const err = new ApiError("unauthorized", "x", 401);
    expect(classifyFetchError(err)).toBe(err);
  });

  it("maps AbortError to timeout", () => {
    expect(classifyFetchError(new DOMException("aborted", "AbortError"))).toMatchObject({
      kind: "timeout",
    });
  });

  it("maps TypeError to network", () => {
    expect(classifyFetchError(new TypeError("fetch failed"))).toMatchObject({
      kind: "network",
    });
  });

  it("maps unknown to network", () => {
    expect(classifyFetchError("boom")).toMatchObject({ kind: "network" });
  });
});

describe("ApiError", () => {
  it("carries kind/status/message", () => {
    const err = new ApiError("http", "GET /x失败：HTTP 500", 500);
    expect(err.kind).toBe("http");
    expect(err.status).toBe(500);
    expect(err.message).toContain("500");
  });

  it("carries parsed JSON payload when present (R17 422 详情用)", async () => {
    const { apiPost } = await import("./api");
    vi.stubGlobal(
      "fetch",
      async () =>
        new Response(
          JSON.stringify({ detail: { error: "unsupported", reason: "scanned" } }),
          { status: 422, headers: { "Content-Type": "application/json" } },
        ),
    );
    const caught = await apiPost("/knowledge/import/preview", {}).then(
      () => null,
      (e: unknown) => e,
    );
    expect(caught).toBeInstanceOf(ApiError);
    const err = caught as ApiError;
    expect(err.status).toBe(422);
    expect(err.payload).toEqual({ detail: { error: "unsupported", reason: "scanned" } });
  });
});

describe("sidecarFetch abort", () => {
  it("rethrows raw AbortError on caller cancel (not timeout)", async () => {
    // 永不响应的 fetch（但遵守 signal 中止语义）+ 50ms 后取消。
    vi.stubGlobal(
      "fetch",
      (_url: unknown, init?: RequestInit) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener(
            "abort",
            () => reject(new DOMException("aborted", "AbortError")),
            { once: true },
          );
        }),
    );
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 50);
    await expect(
      sidecarFetch("/health", { signal: controller.signal }),
    ).rejects.toSatisfy(
      (e) => e instanceof DOMException && (e as DOMException).name === "AbortError",
    );
  });
});
