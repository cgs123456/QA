/**
 * 实时提词会话（PRD §3.3「实时触发策略」的落地状态机）。
 *
 * 与 `trigger.ts` 的分工：`trigger.ts` 只回答三个原子问题 ——
 * 「这一句算不算问题」「算不算重复」「现在能不能刷新 UI」；
 * 本模块把它们与**检索结果**缝在一起，回答「一条 `asr_final` 进来之后到底该做什么」。
 *
 * 同样是纯模块：无 React、无 Tauri、无 DOM，时间全部由调用方注入。
 * 这样「提词行为」本身可以在 node 环境下逐条断言，不必起壳、不必造事件。
 */

import type { QAResult } from "./qa";
import { sourceLabel } from "./sse";
import { DedupWindow, LOCK_MS, RefreshThrottle, questionVerdict, type QuestionReason } from "./trigger";

/** PRD 展示策略：保留最近 3 条问答，最新在最上。 */
export const MAX_CARDS = 3;

/**
 * WS 下行 `asr_final` 的载荷（Rust 侧原样转发，见 `audio/uplink.rs::TauriSink`）。
 * 字段全部可选：事件来自外部，不能假设它一定完整。
 */
export type TranscriptEvent = {
  type?: string;
  segment_id?: string;
  text?: string;
  path?: string;
  ts_ms?: number;
  duration_ms?: number;
  provider?: string;
  degraded?: string[];
};

export type LiveCardKind = "direct" | "llm" | "fail_closed" | "error";

export type LiveCard = {
  id: string;
  question: string;
  answer: string;
  /** 来源标签：字段直查 / 全文检索 / 语义检索 / LLM 生成；fail_closed 与 error 为空串。 */
  source: string;
  kind: LiveCardKind;
  atMs: number;
  /** 来自去重窗口的复用结果（未重新检索）。 */
  reused: boolean;
};

/** 一条 `asr_final` 的处置结论。调用方据此决定「什么都不做 / 上屏 / 去检索」。 */
export type LiveAction =
  | { kind: "skip"; reason: QuestionReason }
  | { kind: "render"; card: LiveCard }
  | { kind: "retrieve"; question: string; normalized: string; interrupt: boolean }
  | { kind: "queued"; question: string; normalized: string };

// ---------------------------------------------------------------- 展示策略

/** 提词框卡片列表：最新在最上，只留 `MAX_CARDS` 条。 */
export function pushCard(cards: readonly LiveCard[], card: LiveCard): LiveCard[] {
  return [card, ...cards].slice(0, MAX_CARDS);
}

/**
 * 结果 → 来源标签（PRD 展示策略的四个标签）。
 *
 * `fail_closed` 与 `error` 返回空串：前者由 UI 固定显示「知识库未命中」
 * （PRD：不显示任何 LLM 编造内容），后者不是「来源」而是故障。
 */
export function liveSourceLabel(result: QAResult): string {
  if (result.kind === "direct") {
    const top = result.sources[0];
    return top != null ? sourceLabel(top) : "字段直查";
  }
  if (result.kind === "llm") return "LLM 生成";
  return "";
}

// ---------------------------------------------------------------- 平台矩阵

export type CapturePlatform = "windows" | "macos" | "linux" | "unknown";
export type CapturePath = "loopback" | "mic";

/** 从 userAgent 判平台（Tauri webview 的 UA 含系统标识）。 */
export function detectPlatform(userAgent: string): CapturePlatform {
  const ua = userAgent.toLowerCase();
  if (ua.includes("windows")) return "windows";
  if (ua.includes("mac os") || ua.includes("macintosh")) return "macos";
  if (ua.includes("linux")) return "linux";
  return "unknown";
}

/**
 * 平台采集路径矩阵，与 `src-tauri/src/audio/mod.rs` 头注释一致：
 * Windows = 回环 + 麦克风，Linux = 监听 + 麦克风，macOS = **仅麦克风**。
 */
export function capturePaths(platform: CapturePlatform): CapturePath[] {
  switch (platform) {
    case "windows":
    case "linux":
      return ["loopback", "mic"];
    default:
      return ["mic"];
  }
}

/**
 * macOS 无系统回环 → UI 必须明示「仅麦克风」（PRD §3.2 平台矩阵）。
 * 其余平台无需提示，返回 null。
 */
export function captureNotice(platform: CapturePlatform): string | null {
  return platform === "macos" ? "仅麦克风（macOS 无系统回环采集）" : null;
}

// ---------------------------------------------------------------- 会话状态机

type Pending = { question: string; normalized: string; card: LiveCard | null };

export type LiveSessionOptions = {
  lockMs?: number;
  dedup?: { n?: number; windowMs?: number; threshold?: number };
  /**
   * 手动触发是否绕过去重（默认 `true`）。
   *
   * 落地裁定，需产品确认：PRD 的去重小节只说「同一 session 内……复用上次结果」，
   * 未按触发来源区分。但手动触发是用户显式的「现在给我答案」——
   * 若复用缓存，按键会看起来失灵；而用户按它，往往正是因为上一条答案不满足预期。
   * 故默认强制重新检索，代价是可能重复一次检索（~5–100ms）。
   */
  manualBypassesDedup?: boolean;
};

/**
 * 提词会话。调用方负责：把 `ingestFinal` / `manual` 返回的 `retrieve` 真的发出去，
 * 完成后调 `settle` 把结果交回来；并周期性调 `drain` 处理锁定期到期的排队项。
 */
export class LiveQASession {
  readonly manualBypassesDedup: boolean;
  private readonly dedup: DedupWindow<LiveCard>;
  private readonly throttle: RefreshThrottle<Pending>;
  private latest: { question: string; normalized: string } | null = null;
  private rendered: LiveCard[] = [];
  private seq = 0;

  constructor(opts: LiveSessionOptions = {}) {
    this.manualBypassesDedup = opts.manualBypassesDedup ?? true;
    this.dedup = new DedupWindow<LiveCard>(opts.dedup ?? {});
    this.throttle = new RefreshThrottle<Pending>(opts.lockMs ?? LOCK_MS);
  }

  get cards(): readonly LiveCard[] {
    return this.rendered;
  }

  /** 待执行项数（0 或 1）。 */
  get pending(): number {
    return this.throttle.pending;
  }

  /** 最近一段转写原文（手动触发用它当查询）。 */
  get lastTranscript(): string | null {
    return this.latest?.question ?? null;
  }

  /**
   * 一条 `asr_final` 到达。
   *
   * 无论是否判为问题都更新「最近一段转写」：手动触发取的是最近转写，
   * 而人说的话经常是陈述句 —— 那正是手动触发存在的意义。
   */
  ingestFinal(event: TranscriptEvent, nowMs: number): LiveAction {
    const text = (event.text ?? "").trim();
    const verdict = questionVerdict(text);
    this.latest = { question: text, normalized: verdict.normalized };

    if (!verdict.isQuestion) return { kind: "skip", reason: verdict.reason };

    const known = this.dedup.find(verdict.normalized, nowMs);
    if (known != null) {
      // 复用：沿用上次答案与来源，但问题文本用**本次**的 —— 卡片记录的是
      // 「刚刚被问到的这句话」，去重已保证两者是同一问题。
      const card = this.buildCard(text, known.payload.answer, known.payload.source, known.payload.kind, nowMs, true);
      return this.offer(card, text, verdict.normalized, nowMs);
    }
    return this.offer(null, text, verdict.normalized, nowMs);
  }

  /** 手动触发（F1.6 全局快捷键）：打断锁定期，取最近一段转写作为查询。 */
  manual(nowMs: number): LiveAction | null {
    const latest = this.latest;
    if (latest == null || latest.normalized.length === 0) return null;

    this.throttle.interrupt(nowMs);

    if (!this.manualBypassesDedup) {
      const known = this.dedup.find(latest.normalized, nowMs);
      if (known != null) {
        const card = this.buildCard(
          latest.question,
          known.payload.answer,
          known.payload.source,
          known.payload.kind,
          nowMs,
          true,
        );
        this.commit(card);
        return { kind: "render", card };
      }
    }
    return { kind: "retrieve", question: latest.question, normalized: latest.normalized, interrupt: true };
  }

  /** 锁定期到期后取出排队项（**最后一条**，不是逐条补刷）。 */
  drain(nowMs: number): LiveAction | null {
    const item = this.throttle.poll(nowMs);
    if (item == null) return null;
    if (item.card != null) {
      const card = { ...item.card, atMs: nowMs };
      this.commit(card);
      return { kind: "render", card };
    }
    return { kind: "retrieve", question: item.question, normalized: item.normalized, interrupt: false };
  }

  /**
   * 检索完成 → 生成卡片、记入去重、进入新的锁定期。
   *
   * `error` 结果**不记入去重**：否则一次网络故障会把该问题缓存 60 秒，
   * 期间用户再问只会拿到同一个错误。
   */
  settle(question: string, normalized: string, result: QAResult, nowMs: number): LiveCard {
    const card = this.buildCard(question, result.text, liveSourceLabel(result), result.kind, nowMs, false);
    if (result.kind !== "error") this.dedup.record(normalized, nowMs, card);
    this.commit(card);
    return card;
  }

  reset(): void {
    this.dedup.reset();
    this.throttle.reset();
    this.latest = null;
    this.rendered = [];
    this.seq = 0;
  }

  private offer(card: LiveCard | null, question: string, normalized: string, nowMs: number): LiveAction {
    const outcome = this.throttle.offer({ question, normalized, card }, nowMs);
    if (outcome === "execute") {
      if (card == null) return { kind: "retrieve", question, normalized, interrupt: false };
      // 复用即上屏：会话是 `cards` 的唯一所有者，调用方只需重读 `session.cards`。
      this.commit(card);
      return { kind: "render", card };
    }
    return { kind: "queued", question, normalized };
  }

  /** 上屏 + 进入锁定期（PRD：提词框更新后进入 3 秒锁定期）。 */
  private commit(card: LiveCard): void {
    this.rendered = pushCard(this.rendered, card);
    this.throttle.markRefreshed(card.atMs);
  }

  private buildCard(
    question: string,
    answer: string,
    source: string,
    kind: LiveCardKind,
    atMs: number,
    reused: boolean,
  ): LiveCard {
    this.seq += 1;
    return { id: `card_${this.seq}`, question, answer, source, kind, atMs, reused };
  }
}
