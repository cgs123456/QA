/**
 * 实时提词的 React 接线：把 Tauri 事件与 `LiveQASession`（纯状态机）连起来。
 *
 * 本文件刻意只做「接线」——所有决策都在 `lib/liveqa.ts` 里，可脱离运行时单测。
 * 这里允许出现的只有：事件订阅、`Date.now()`、异步检索、把结果写进 state。
 */

import { listen } from "@tauri-apps/api/event";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  LiveQASession,
  captureNotice,
  detectPlatform,
  type CapturePlatform,
  type LiveAction,
  type LiveCard,
  type LiveSessionOptions,
  type TranscriptEvent,
} from "../lib/liveqa";
import { askQuestion } from "../lib/qa";
import type { QuestionReason } from "../lib/trigger";

/** Rust `audio/uplink.rs` 的下行事件名。 */
export const EVT_ASR_FINAL = "asr://final";
/** F1.6 手动触发提词（Rust `shortcuts.rs`）。 */
export const EVT_TELEPROMPTER_TRIGGER = "teleprompter://trigger";
/** F1.6 开始/停止采集（Rust `shortcuts.rs`）。 */
export const EVT_CAPTURE_TOGGLE = "capture://toggle";

/** 锁定期到期的轮询间隔：只影响「排队项何时被执行」，不影响锁定时长本身。 */
const DRAIN_INTERVAL_MS = 250;

export type LiveQAState = {
  cards: readonly LiveCard[];
  lastSkip: QuestionReason | null;
  pending: number;
  busy: boolean;
  lastError: string | null;
  lastPath: string | null;
  lastTranscript: string | null;
  captureRequested: boolean;
  platform: CapturePlatform;
  notice: string | null;
};

export type UseLiveQAOptions = LiveSessionOptions & {
  storeId?: string;
  provider?: string;
};

export function useLiveQA(options: UseLiveQAOptions = {}) {
  const { storeId, provider, ...sessionOptions } = options;

  const sessionRef = useRef<LiveQASession | null>(null);
  if (sessionRef.current == null) sessionRef.current = new LiveQASession(sessionOptions);
  const session = sessionRef.current;

  const abortRef = useRef<AbortController | null>(null);

  const [state, setState] = useState<LiveQAState>(() => {
    const platform = detectPlatform(typeof navigator === "undefined" ? "" : navigator.userAgent);
    return {
      cards: session.cards,
      lastSkip: null,
      pending: 0,
      busy: false,
      lastError: null,
      lastPath: null,
      lastTranscript: null,
      captureRequested: false,
      platform,
      notice: captureNotice(platform),
    };
  });

  const sync = useCallback(() => {
    setState((s) => ({ ...s, cards: [...session.cards], pending: session.pending }));
  }, [session]);

  /**
   * 执行一次检索。
   *
   * 「最新问题优先」：新检索会取消上一次未完成的检索。理由与锁定期「取最后一条」
   * 同向——提词框关心的是**现在**被问到什么，让一个 10 秒前的慢回答后到并覆盖
   * 当前问题，比丢掉它更糟。
   */
  const runRetrieval = useCallback(
    async (action: { question: string; normalized: string }) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      setState((s) => ({ ...s, busy: true, lastError: null }));
      try {
        const result = await askQuestion(action.question, { storeId, provider, signal: controller.signal });
        if (controller.signal.aborted) return;
        session.settle(action.question, action.normalized, result, Date.now());
        sync();
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        if (controller.signal.aborted) return;
        setState((s) => ({ ...s, lastError: e instanceof Error ? e.message : String(e) }));
      } finally {
        if (abortRef.current === controller) {
          abortRef.current = null;
          setState((s) => ({ ...s, busy: false }));
        }
      }
    },
    [provider, session, storeId, sync],
  );

  const dispatch = useCallback(
    (action: LiveAction | null) => {
      if (action == null) return;
      switch (action.kind) {
        case "skip":
          setState((s) => ({ ...s, lastSkip: action.reason }));
          break;
        case "render":
        case "queued":
          sync();
          break;
        case "retrieve":
          void runRetrieval(action);
          break;
      }
    },
    [runRetrieval, sync],
  );

  useEffect(() => {
    const unlisteners: (() => void)[] = [];
    let cancelled = false;
    const add = (p: Promise<() => void>) => {
      p.then((fn) => (cancelled ? fn() : unlisteners.push(fn))).catch(() => {
        // 非 Tauri 环境（如 vitest / 浏览器直开）没有事件总线：静默降级。
      });
    };

    add(
      listen<TranscriptEvent>(EVT_ASR_FINAL, (event) => {
        const payload = event.payload ?? {};
        setState((s) => ({
          ...s,
          lastPath: payload.path ?? s.lastPath,
          lastTranscript: payload.text ?? s.lastTranscript,
        }));
        dispatch(session.ingestFinal(payload, Date.now()));
      }),
    );
    add(listen(EVT_TELEPROMPTER_TRIGGER, () => dispatch(session.manual(Date.now()))));
    add(
      listen(EVT_CAPTURE_TOGGLE, () =>
        setState((s) => ({ ...s, captureRequested: !s.captureRequested })),
      ),
    );

    return () => {
      cancelled = true;
      for (const fn of unlisteners) fn();
    };
  }, [dispatch, session]);

  // 锁定期到期后执行排队项。用轮询而非定时器重排：锁定期长度由状态机决定，
  // 这里只负责「定期问一句能不能执行了」。
  useEffect(() => {
    const timer = setInterval(() => dispatch(session.drain(Date.now())), DRAIN_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [dispatch, session]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const triggerManual = useCallback(() => dispatch(session.manual(Date.now())), [dispatch, session]);

  const reset = useCallback(() => {
    session.reset();
    sync();
    setState((s) => ({ ...s, lastSkip: null, lastError: null, lastTranscript: null }));
  }, [session, sync]);

  return { ...state, triggerManual, reset };
}
