import { useCallback, useEffect, useRef, useState } from "react";
import { apiPost, sidecarFetch } from "../lib/api";
import { parseSSEFrames } from "../lib/sse";

export type QASource = {
  type: string;
  key: string;
  score: number;
  routes?: string[];
  payload: Record<string, unknown>;
};

export type QAResult =
  | { kind: "direct"; text: string; sources: QASource[]; llm_calls: number }
  | { kind: "llm"; text: string; sources: QASource[]; llm_calls: number }
  | { kind: "fail_closed"; text: string; sources: QASource[]; llm_calls: number }
  | { kind: "error"; text: string; sources: QASource[]; llm_calls: number };

export type QAState = {
  phase: "idle" | "asking" | "streaming" | "done" | "error";
  action: string | null;
  sources: QASource[];
  text: string;
  result: QAResult | null;
  error: string | null;
};

const INITIAL: QAState = {
  phase: "idle",
  action: null,
  sources: [],
  text: "",
  result: null,
  error: null,
};

/** 两段式问答：POST /qa/ask → GET /qa/stream，chunk 逐块累加，可取消。 */
export function useQA() {
  const [state, setState] = useState<QAState>(INITIAL);
  const abortRef = useRef<AbortController | null>(null);

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  useEffect(() => cancel, [cancel]);

  const ask = useCallback(
    async (question: string, storeId?: string, provider?: string) => {
      cancel();
      const controller = new AbortController();
      abortRef.current = controller;
      setState({ ...INITIAL, phase: "asking" });
      try {
        const { task_id } = await apiPost<{ task_id: string }>("/qa/ask", {
          question,
          ...(storeId != null ? { store_id: storeId } : {}),
          ...(provider != null ? { provider } : {}),
        });
        setState((s) => ({ ...s, phase: "streaming" }));
        const res = await sidecarFetch(
          `/qa/stream?task_id=${encodeURIComponent(task_id)}`,
          { signal: controller.signal },
          120000,
        );
        if (!res.ok || res.body == null) {
          throw new Error(`stream HTTP ${res.status}`);
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let text = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parsed = parseSSEFrames(buffer);
          buffer = parsed.rest;
          for (const raw of parsed.events) {
            const event = raw as Record<string, unknown>;
            if (event["type"] === "retrieval") {
              setState((s) => ({
                ...s,
                action: event["action"] as string,
                sources: (event["sources"] as QASource[]) ?? [],
              }));
            } else if (event["type"] === "generation") {
              const chunk = event["chunk"] as string;
              text += chunk;
              setState((s) => ({ ...s, text }));
            } else if (event["type"] === "done") {
              const result = event["result"] as QAResult;
              setState((s) => ({
                ...s,
                phase: "done",
                text: result.text,
                result,
                sources: result.sources,
              }));
            }
          }
        }
        setState((s) => (s.phase === "streaming" ? { ...s, phase: "done" } : s));
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setState((s) => ({
          ...s,
          phase: "error",
          error: e instanceof Error ? e.message : String(e),
        }));
      } finally {
        if (abortRef.current === controller) abortRef.current = null;
      }
    },
    [cancel],
  );

  const reset = useCallback(() => {
    cancel();
    setState(INITIAL);
  }, [cancel]);

  return { ...state, ask, cancel, reset };
}
