/**
 * 会话账的渲染逻辑（S3）：**判断都在这里（可脱离运行时单测），组件只画**。
 *
 * 与 `lib/capture.ts` / `lib/liveqa.ts` 同一个分工约定：组件里不再散落
 * `if (x > 0)` 之类的格式化判断，因为"用户看到什么"必须能被单测钉住 ——
 * 这里每一条都决定了用户以为发生了什么，判断错了界面会理直气壮地骗人。
 *
 * # R21（UI 面）：这个文件不认识检索
 *
 * 时间线是**回放**，不是问答。回放期间触发任何检索都会产生两个后果：
 * ① 回放动作反过来污染会话账（自证循环）；② 用户在看历史时被悄悄扣了一次
 * 真实检索。所以本模块**不 import 任何检索/问答函数**，只把账行翻译成渲染事实。
 * 这个"不 import"本身由 `sessions.test.ts` 的**源码级断言**守住
 * （grep 到 `askQuestion` / `useQA` 就红）—— 靠自觉不够，靠测试。
 *
 * # R14 / S12：文本口径
 *
 * - `asr_final` / `asr_error` / `qa_exchange`：R14 content-free，只记数字与枚举，
 *   时间线显示「未落盘」。
 * - `rehearsal`（S12）：metadata 含 question_text/official_answer/user_answer，
 *   **账有文本，日志无文本**（R14 约束的是应用层日志，不是落盘内容）。
 */

import type { SessionEvent, SessionSummary } from "./api";

/** R22 删除确认文案（任务书给定，逐字使用，不改写）。 */
export const SESSION_DELETE_CONFIRM_TEXT = "转写文本将被永久删除";

/** 清除全部会话的二次确认文案。删的是**全部**，语气要比单条更重。 */
export const SESSION_PURGE_CONFIRM_TEXT = "全部会话的账行将被永久删除，且不可恢复";

// ------------------------------------------------------------------ 录制开关与保留策略

/** 保留策略：null = 永久（不删）。与服务端 `RETENTION_CHOICES` 一致。 */
export type RetentionDays = 7 | 30 | null;

export const RETENTION_FOREVER: RetentionDays = null;

export function retentionLabel(days: RetentionDays): string {
  if (days === 7) return "保留 7 天";
  if (days === 30) return "保留 30 天";
  return "永久保留";
}

/**
 * 保留策略的一句话说明。
 *
 * 永久之外还要补一句"到期按最后一次活动算" —— 用户以为的"30 天"通常指
 * "最近 30 天还动过的"，不写出来就会有人以为是按会话开始时间算。
 */
export function retentionHint(days: RetentionDays): string {
  if (days === null) return "不自动删除（只在设置页手动清除）";
  return `${retentionLabel(days)}；到期 = 最后一次活动早于 ${days} 天前，sidecar 启动时清理一次`;
}

/** 数据量的一句话（设置页显示）。零值也要说出来，藏起来看不出在监控什么。 */
export function usageLine(u: {
  sessions: number;
  events: number;
  payload_bytes: number;
}): string {
  return `${u.sessions} 次会话 · ${u.events} 行账 · 载荷 ${formatBytes(u.payload_bytes)}`;
}

export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** 账行类型 → 中文标签。未知类型原样透出（宁可难看，不要悄悄吞掉）。 */
export const EVENT_TYPE_LABELS: Record<string, string> = {
  session_begin: "会话开始",
  session_end: "会话结束",
  asr_final: "转写完成",
  asr_error: "转写失败",
  qa_exchange: "一次问答",
};

export function eventTypeLabel(eventType: string): string {
  return EVENT_TYPE_LABELS[eventType] ?? eventType;
}

/** 路名（`loopback` / `mic` / `qa`）→ 中文；空值回落到「—」。 */
export function stageLabel(stage: string | null | undefined): string {
  if (stage == null || stage === "") return "—";
  if (stage === "loopback") return "系统回环";
  if (stage === "mic") return "麦克风";
  if (stage === "qa") return "问答";
  return stage;
}

// ------------------------------------------------------------------ 时间与时长

function pad(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/** `2026-09-16 11:22`（本地时区）；`null`/0 → 「—」（不拿 0 当时刻）。 */
export function formatDateTime(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms <= 0) return "—";
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** `11:22:03`；同上，无效值 → 「—」。 */
export function formatClock(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms <= 0) return "—";
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/**
 * 时长：`12 秒` / `1 分 02 秒` / `1 小时 03 分`。
 *
 * 零值也要有说法（"0 秒"），不能显示空串 —— 空串会被读成"没统计到"，
 * 而 0 秒是一次真实发生的、确实很短的会话。
 */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h} 小时 ${pad(m)} 分`;
  if (m > 0) return `${m} 分 ${pad(s)} 秒`;
  return `${s} 秒`;
}

/** 会话时长 = 最后一行 − 首行（服务端给的是两个时刻，不是时长）。 */
export function sessionDurationMs(s: SessionSummary): number {
  const span = s.last_ms - s.started_ms;
  return Number.isFinite(span) && span > 0 ? span : 0;
}

/**
 * 问答次数。`qa_exchanges` 由列表端点**可选**下发（S1 目前只给总账行数），
 * 没下发就返回 `null` —— **不做"用总事件数冒充问答数"的近似**，
 * 那种近似会让"这次会话问了 5 个问题"变成"这次会话有 5 行账"。
 */
export function sessionQaCount(s: SessionSummary): number | null {
  const n = s.qa_exchanges;
  return typeof n === "number" && Number.isFinite(n) ? n : null;
}

/** 列表行的一句话统计（问答数有就说，没有就退回到总事件数并标明口径）。 */
export function sessionStatsLine(s: SessionSummary): string {
  const qa = sessionQaCount(s);
  const dur = formatDuration(sessionDurationMs(s));
  if (qa != null) return `${dur} · 问答 ${qa} 次 · 账 ${s.events} 行`;
  return `${dur} · 账 ${s.events} 行（未分解）`;
}

/**
 * `store_id` → 库名。
 *
 * 找不到就**显示 id 本身**，不显示"未知库"也不留白：库被删了但账还在是正常
 * 情况（账按 v002 的 `store_id` 列记"当时是哪个库"），显示 id 至少可追溯；
 * 显示"未知"会把"库被删了"和"没记下来"混成一件事。
 */
export function storeNameById(
  stores: { id: string; name: string }[] | undefined | null,
  id: string | null | undefined,
): string {
  if (id == null || id === "") return "（未记录）";
  const hit = stores?.find((s) => s.id === id);
  return hit?.name ?? id;
}

// ------------------------------------------------------------------ 删除守卫

export type DeleteGuard = { ok: true } | { ok: false; reason: string };

/**
 * 删除失败时给用户的那句话：**优先用服务端给的**。
 *
 * 409（会话进行中）与 404（本来就没有）是两种不同的情况，服务端各有一句
 * `message`。糊成一句"删除失败"会让人以为要重试，而 409 重试一百次也没用。
 */
export function deleteErrorMessage(e: unknown): string {
  const err = e as { payload?: unknown; message?: unknown } | null;
  const payload = err?.payload as { message?: unknown } | undefined;
  if (typeof payload?.message === "string" && payload.message !== "") return payload.message;
  if (typeof err?.message === "string" && err.message !== "") return err.message;
  return String(e);
}

/**
 * 能不能删：正在进行的会话不让删（服务端会回 409）。
 *
 * 在前端先拦一次不是重复实现后端规则 —— 后端那条是**正确性**保护
 * （删了会把"这次会话有多少段"毁掉），前端这条是**别让用户白点一次并看到报错**。
 * 后端仍是唯一真值：撞上 409 时前端照实显示服务端的话。
 */
export function canDelete(s: SessionSummary, activeSessionId: string | null): DeleteGuard {
  if (activeSessionId != null && s.session_id === activeSessionId) {
    return { ok: false, reason: "会话进行中，先停止采集再删除" };
  }
  return { ok: true };
}

// ------------------------------------------------------------------ 时间线

/** `asr_final` 的账行 → 渲染事实（全数字，无文本；R14）。 */
export type AsrFinalFacts = {
  segmentId: string;
  durationMs: number;
  provider: string;
  degradedLevels: number;
  droppedOldest: number;
};

export function asrFinalFacts(m: Record<string, unknown>): AsrFinalFacts {
  const num = (k: string) => {
    const v = m[k];
    return typeof v === "number" && Number.isFinite(v) ? v : 0;
  };
  return {
    segmentId: String(m["segment_id"] ?? "—"),
    durationMs: num("duration_ms"),
    provider: String(m["provider"] ?? "—"),
    degradedLevels: num("degraded_levels"),
    droppedOldest: num("dropped_oldest"),
  };
}

/** `qa_exchange` 的账行 → 渲染事实（无问题、无答案；R14）。 */
export type QaExchangeFacts = {
  action: string;
  provider: string;
  llmCalls: number;
  sources: number;
  warnings: number;
  elapsedMs: number;
  top1Score: number;
};

export const QA_ACTION_LABELS: Record<string, string> = {
  direct: "直接命中",
  llm: "LLM 生成",
  fail_closed: "未命中（拒答）",
  error: "生成失败",
};

export function qaActionLabel(action: string): string {
  return QA_ACTION_LABELS[action] ?? action;
}

export function qaExchangeFacts(m: Record<string, unknown>): QaExchangeFacts {
  const num = (k: string) => {
    const v = m[k];
    return typeof v === "number" && Number.isFinite(v) ? v : 0;
  };
  return {
    action: String(m["action"] ?? "—"),
    provider: String(m["provider"] ?? "—"),
    llmCalls: num("llm_calls"),
    sources: num("sources"),
    warnings: num("warnings"),
    elapsedMs: num("elapsed_ms"),
    top1Score: num("top1_score"),
  };
}

/** `rehearsal` 的账行 → 渲染事实（含题面/标准答案/用户作答；S12：账有文本）。 */
export type RehearsalFacts = {
  verdict: string;
  category: string | null;
  qaId: string;
  questionText: string | null;
  officialAnswer: string | null;
  userAnswer: string | null;
};

export function rehearsalFacts(m: Record<string, unknown>): RehearsalFacts {
  return {
    verdict: String(m["verdict"] ?? "—"),
    category: m["category"] != null ? String(m["category"]) : null,
    qaId: String(m["qa_id"] ?? "—"),
    questionText: m["question_text"] != null ? String(m["question_text"]) : null,
    officialAnswer: m["official_answer"] != null ? String(m["official_answer"]) : null,
    userAnswer: m["user_answer"] != null ? String(m["user_answer"]) : null,
  };
}

/** 时间线的一行（判别联合：不同类渲染不同卡片，调用方不必再判空）。 */
export type TimelineRow = {
  id: number;
  tsMs: number | null;
  typeLabel: string;
  stage: string;
  kind: "boundary" | "asr" | "qa" | "rehearsal" | "unknown";
  /** `kind === "asr"` 时才有。 */
  asr: AsrFinalFacts | null;
  /** `kind === "qa"` 时才有。 */
  qa: QaExchangeFacts | null;
  /** `kind === "rehearsal"` 时才有。 */
  rehearsal: RehearsalFacts | null;
  /** 脏 metadata：如实暴露，不让整条时间线挂掉。 */
  unparsable: string | null;
};

/**
 * 时间线排序：**时刻升序，同毫秒按行 id**。
 *
 * 服务端已经按 id 升序回（id 是真实发生序，ts_ms 会并列），这里再排一次
 * 是为了让"排序"这件事在前端也有明确说法 —— 组件要按 ts 分组展示，
 * 一旦顺序被上游改动，这里的单测会先红，而不是用户先看到错序的时间线。
 * `ts_ms` 缺失（v002 之前的回填行）排最后，但仍按 id 相对有序。
 */
export function timelineOrder(events: SessionEvent[]): SessionEvent[] {
  return [...events].sort((a, b) => {
    const ta = a.ts_ms;
    const tb = b.ts_ms;
    if (ta == null && tb == null) return a.id - b.id;
    if (ta == null) return 1;
    if (tb == null) return -1;
    if (ta !== tb) return ta - tb;
    return a.id - b.id;
  });
}

export function toTimelineRow(e: SessionEvent): TimelineRow {
  const base = {
    id: e.id,
    tsMs: e.ts_ms,
    typeLabel: eventTypeLabel(e.event_type),
    stage: stageLabel(e.stage),
  };
  const unparsableRaw = e.metadata?.["_unparsable"];
  const unparsable =
    typeof unparsableRaw === "string" ? unparsableRaw : unparsableRaw != null ? String(unparsableRaw) : null;

  if (e.event_type === "asr_final" || e.event_type === "asr_error") {
    return { ...base, kind: "asr", asr: asrFinalFacts(e.metadata ?? {}), qa: null, rehearsal: null, unparsable };
  }
  if (e.event_type === "qa_exchange") {
    return { ...base, kind: "qa", asr: null, qa: qaExchangeFacts(e.metadata ?? {}), rehearsal: null, unparsable };
  }
  if (e.event_type === "rehearsal") {
    return { ...base, kind: "rehearsal", asr: null, qa: null, rehearsal: rehearsalFacts(e.metadata ?? {}), unparsable };
  }
  if (e.event_type === "session_begin" || e.event_type === "session_end") {
    return { ...base, kind: "boundary", asr: null, qa: null, rehearsal: null, unparsable };
  }
  return { ...base, kind: "unknown", asr: null, qa: null, rehearsal: null, unparsable };
}

export function toTimeline(events: SessionEvent[]): TimelineRow[] {
  return timelineOrder(events).map(toTimelineRow);
}

/** 时间线的一句话摘要（页面顶部用；零值也要说出来，藏起来看不出在监控什么）。 */
export function timelineSummary(rows: TimelineRow[]): string {
  const asr = rows.filter((r) => r.kind === "asr").length;
  const qa = rows.filter((r) => r.kind === "qa").length;
  const rehearsal = rows.filter((r) => r.kind === "rehearsal").length;
  return `共 ${rows.length} 行 · 转写 ${asr} · 问答 ${qa} · 陪练 ${rehearsal}`;
}
