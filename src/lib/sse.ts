/**
 * SSE 帧解析（纯函数，可单测）：`data: {...}\n\n` 帧流 → 事件数组 + 余 buffer。
 * 调用方把 fetch body chunk 拼进 buffer 后反复调用，断帧自动留待下次。
 */
export type SSEParseResult = {
  events: unknown[];
  rest: string;
};

export function parseSSEFrames(buffer: string): SSEParseResult {
  const events: unknown[] = [];
  let rest = buffer;
  for (;;) {
    const sep = rest.indexOf("\n\n");
    if (sep < 0) break;
    const frame = rest.slice(0, sep);
    rest = rest.slice(sep + 2);
    for (const line of frame.split("\n")) {
      const trimmed = line.trimEnd();
      if (!trimmed.startsWith("data:")) continue;
      const payload = trimmed.slice(5).trim();
      if (!payload) continue;
      try {
        events.push(JSON.parse(payload));
      } catch {
        // 非 JSON 帧（如心跳注释）忽略，不中断流。
      }
    }
  }
  return { events, rest };
}

/** 来源标签：field→字段直查；jieba/simple→全文检索；vec→语义检索。 */
export function sourceLabel(source: { type: string; routes?: string[] }): string {
  if (source.type === "field") return "字段直查";
  const labels: string[] = [];
  const routes = source.routes ?? [];
  if (routes.includes("jieba") || routes.includes("simple")) {
    labels.push("全文检索");
  }
  if (routes.includes("vec")) labels.push("语义检索");
  return labels.length > 0 ? labels.join("+") : "混合检索";
}
