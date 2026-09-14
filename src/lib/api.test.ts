import { describe, expect, it } from "vitest";
import { ApiError, classifyFetchError } from "./api";

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
});
