/**
 * 实时提词的 React 接线：把 Tauri 事件与 `LiveQASession`（纯状态机）连起来。
 *
 * 本文件刻意只做「接线」——所有决策都在 `lib/liveqa.ts` 里，可脱离运行时单测。
 * 这里允许出现的只有：事件订阅、`Date.now()`、异步检索、把结果写进 state。
 */

import { emit, listen } from "@tauri-apps/api/event";
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
import {
  EMPTY_CAPTURE_STATE,
  EVT_CAPTURE_STATE,
  getCaptureState,
  toggleCapture,
  type CaptureState,
} from "../lib/capture";
import { askQuestion } from "../lib/qa";
import type { QuestionReason } from "../lib/trigger";

/** Rust `audio/uplink.rs` 的下行事件名。 */
export const EVT_ASR_FINAL = "asr://final";
/** F1.6 手动触发提词（Rust `shortcuts.rs`）。 */
export const EVT_TELEPROMPTER_TRIGGER = "teleprompter://trigger";
/** F1.6 开始/停止采集（Rust `shortcuts.rs`）。 */
export const EVT_CAPTURE_TOGGLE = "capture://toggle";
/**
 * taskP7：主窗 → 提词窗的单向卡片推送。
 *
 * 提词窗**不**跑自己的会话（否则同一问题会发两次检索），它只订阅这个事件渲染。
 * 载荷是 `LiveCard[]`（内容含转写与答案，属 UI 专用，**不得落日志**）。
 */
export const EVT_TELEPROMPTER_CARDS = "teleprompter://cards";

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
  /**
   * 采集服务的真实状态（来自 `capture://state` / 命令返回值）。
   *
   * 这里**不再自己翻一个布尔值**：采集的真值源在 Rust 侧（`CaptureService`），
   * 前端猜一个「我以为在采集」的状态，会在设备打不开时理直气壮地骗人。
   */
  capture: CaptureState;
  /** 上一次启停采集失败的原因（如 sidecar 未连接）。 */
  captureError: string | null;
  platform: CapturePlatform;
  notice: string | null;
};

export type UseLiveQAOptions = LiveSessionOptions & {
  storeId?: string;
  provider?: string;
  /**
   * taskP7：把算好的卡片单向推给提词窗（`EVT_TELEPROMPTER_CARDS`）。
   *
   * 只有主窗该开这个开关。提词窗自己开的话会变成「两个窗互相推」，而且它本来
   * 就不该跑会话 —— 会话的所有权必须唯一。
   */
  broadcast?: boolean;
};

export function useLiveQA(options: UseLiveQAOptions = {}) {
  const { storeId, provider, broadcast = false, ...sessionOptions } = options;

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
      capture: EMPTY_CAPTURE_STATE,
      captureError: null,
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
    // 采集状态由 Rust 侧回推。前端**不**监听 `capture://toggle` 自己去翻状态 ——
    // 那个事件是"意图"，不是"结果"：按下去可能因为 sidecar 没连上而失败。
    add(
      listen<CaptureState>(EVT_CAPTURE_STATE, (event) => {
        if (event.payload == null) return;
        setState((s) => ({
          ...s,
          capture: event.payload,
          captureError: event.payload.last_error,
        }));
      }),
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

  // 首次渲染拉一次真实状态：页面可能在采集已经跑起来之后才被打开
  // （快捷键先按、再切到这个 tab），只靠事件会漏掉这一帧。
  useEffect(() => {
    let cancelled = false;
    getCaptureState()
      .then((snap) => {
        if (!cancelled) setState((s) => ({ ...s, capture: snap }));
      })
      .catch(() => {
        // 非 Tauri 环境（vitest / 浏览器直开）：保持空状态。
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // taskP7：把算好的卡片单向推给提词窗。只在**引用真的变了**时发 ——
  // `drain` 每 250ms 轮询一次，不加这个判断就变成 4 次/秒的空推。
  const broadcastRef = useRef(state.cards);
  useEffect(() => {
    if (!broadcast || broadcastRef.current === state.cards) return;
    broadcastRef.current = state.cards;
    // 事件总线不存在（vitest / 浏览器直开）时静默降级：这是可选能力，不是主路径。
    void emit(EVT_TELEPROMPTER_CARDS, state.cards).catch(() => {});
  }, [broadcast, state.cards]);

  const triggerManual = useCallback(() => dispatch(session.manual(Date.now())), [dispatch, session]);

  /**
   * 前端开关：走命令，不用事件。
   *
   * 快捷键走事件（那是键盘钩子，只能发事件）；按钮走命令是因为它能**拿到结果**
   * 并把失败原因直接显示出来，不用等状态回推绕一圈。
   */
  const requestCaptureToggle = useCallback(async () => {
    try {
      const snap = await toggleCapture();
      setState((s) => ({ ...s, capture: snap, captureError: snap.last_error }));
    } catch (e) {
      setState((s) => ({
        ...s,
        captureError: e instanceof Error ? e.message : String(e),
      }));
    }
  }, []);

  const reset = useCallback(() => {
    session.reset();
    sync();
    setState((s) => ({ ...s, lastSkip: null, lastError: null, lastTranscript: null }));
  }, [session, sync]);

  return { ...state, triggerManual, requestCaptureToggle, reset };
}
