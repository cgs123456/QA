import { useCallback, useEffect, useRef, useState } from "react";
import { askQuestion, type QAResult, type QASource } from "../lib/qa";

// 类型从 lib/qa 透出：调用方（Search 等）沿用 `from "../hooks/useQA"` 的既有写法。
export type { QAResult, QASource } from "../lib/qa";

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
        const result = await askQuestion(question, {
          storeId,
          provider,
          signal: controller.signal,
          onEvent: (event) => {
            if (event.type === "retrieval") {
              setState((s) => ({
                ...s,
                phase: "streaming",
                action: event.action,
                sources: event.sources,
              }));
            } else if (event.type === "generation") {
              setState((s) => ({ ...s, phase: "streaming", text: s.text + event.chunk }));
            }
          },
        });
        setState((s) => ({
          ...s,
          phase: "done",
          text: result.text,
          result,
          sources: result.sources,
        }));
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
