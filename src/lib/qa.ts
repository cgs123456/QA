/**
 * 两段式问答（PRD §3.4「SSE 两段式问答」）：`POST /qa/ask` → `GET /qa/stream`。
 *
 * 抽成独立模块的原因：1a 的「手动查找」与 1b 的「实时提词」走的是**同一条链路**，
 * 协议消费循环只能有一份 —— 复制一份就等于给协议变更留两个改点，
 * 而这类副本通常会有一份先腐坏。
 */

import { apiPost, sidecarFetch } from "./api";
import { parseSSEFrames } from "./sse";

export type QASource = {
  type: string;
  key: string;
  score: number;
  routes?: string[];
  payload: Record<string, unknown>;
};

export type QAKind = "direct" | "llm" | "fail_closed" | "error";

export type QAResult = {
  kind: QAKind;
  text: string;
  sources: QASource[];
  llm_calls: number;
};

/** 流内事件（供调用方增量渲染，如流式生成时逐块上屏）。 */
export type AskEvent =
  | { type: "retrieval"; action: string; sources: QASource[] }
  | { type: "generation"; chunk: string }
  | { type: "done"; result: QAResult };

export type AskOptions = {
  storeId?: string;
  provider?: string;
  /** 调用方取消信号（取消是正常路径，不产生错误态）。 */
  signal?: AbortSignal;
  onEvent?: (event: AskEvent) => void;
  /** 流式读取超时（LLM 生成可能较慢，默认 120s）。 */
  streamTimeoutMs?: number;
};

export const STREAM_TIMEOUT_MS = 120_000;

/**
 * 发起一次问答并读到 `done`，返回最终结果。
 *
 * 流结束却未收到 `done` 事件时**抛错**：按契约 sidecar 总会发 `done`，
 * 缺它就是传输被截断。静默当成成功会让半截答案看起来是完整答案 ——
 * 对提词场景尤其危险（用户会把被截断的句子当成完整回答念出去）。
 */
export async function askQuestion(question: string, opts: AskOptions = {}): Promise<QAResult> {
  const { storeId, provider, signal, onEvent, streamTimeoutMs = STREAM_TIMEOUT_MS } = opts;

  const { task_id } = await apiPost<{ task_id: string }>("/qa/ask", {
    question,
    ...(storeId != null ? { store_id: storeId } : {}),
    ...(provider != null ? { provider } : {}),
  });

  const res = await sidecarFetch(
    `/qa/stream?task_id=${encodeURIComponent(task_id)}`,
    { signal },
    streamTimeoutMs,
  );
  if (!res.ok || res.body == null) {
    throw new Error(`stream HTTP ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: QAResult | null = null;

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSSEFrames(buffer);
    buffer = parsed.rest;
    for (const raw of parsed.events) {
      const event = raw as Record<string, unknown>;
      if (event["type"] === "retrieval") {
        onEvent?.({
          type: "retrieval",
          action: event["action"] as string,
          sources: (event["sources"] as QASource[]) ?? [],
        });
      } else if (event["type"] === "generation") {
        onEvent?.({ type: "generation", chunk: event["chunk"] as string });
      } else if (event["type"] === "done") {
        result = event["result"] as QAResult;
        onEvent?.({ type: "done", result });
      }
    }
  }

  if (result == null) {
    throw new Error("SSE 流未收到 done 事件（传输被截断）");
  }
  return result;
}
