import { describe, expect, it } from "vitest";
import { parseSSEFrames, sourceLabel } from "./sse";

describe("parseSSEFrames", () => {
  it("parses complete frames", () => {
    const { events, rest } = parseSSEFrames(
      'data: {"type":"retrieval"}\n\ndata: {"type":"done"}\n\n',
    );
    expect(events).toEqual([{ type: "retrieval" }, { type: "done" }]);
    expect(rest).toBe("");
  });

  it("buffers split frames across chunks", () => {
    const first = parseSSEFrames('data: {"type":"retriev');
    expect(first.events).toEqual([]);
    const second = parseSSEFrames(`${first.rest}al"}\n\n`);
    expect(second.events).toEqual([{ type: "retrieval" }]);
    expect(second.rest).toBe("");
  });

  it("ignores non-JSON frames without breaking the stream", () => {
    const { events, rest } = parseSSEFrames(
      ": heartbeat\n\ndata: {bad json}\n\ndata: [1]\n\n",
    );
    expect(events).toEqual([[1]]);
    expect(rest).toBe("");
  });
});

describe("sourceLabel", () => {
  it("maps routes to Chinese labels", () => {
    expect(sourceLabel({ type: "field" })).toBe("字段直查");
    expect(sourceLabel({ type: "qa", routes: ["jieba"] })).toBe("全文检索");
    expect(sourceLabel({ type: "qa", routes: ["simple"] })).toBe("全文检索");
    expect(sourceLabel({ type: "qa", routes: ["vec"] })).toBe("语义检索");
    expect(
      sourceLabel({ type: "qa", routes: ["jieba", "simple", "vec"] }),
    ).toBe("全文检索+语义检索");
    expect(sourceLabel({ type: "qa", routes: [] })).toBe("混合检索");
  });
});
