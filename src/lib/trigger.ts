/**
 * 实时触发策略（PRD §3.3「实时触发策略」）——纯函数 + 时间注入状态机。
 *
 * 本模块**不依赖 React / Tauri / DOM**。理由见 PRD 原话：「对实时辅助类产品，
 * 触发策略就是产品本身」——它是产品核心逻辑，必须能脱离运行时单测。
 *
 * **时间一律由调用方注入**（`nowMs`），模块内不读 `Date.now()`：
 * 否则「3 秒锁定期」「60 秒去重窗口」只能靠 sleep 测，测试既慢又不稳。
 *
 * 本文件所有阈值均为 PRD 初值，Phase 1a 用 100 题评测集标定后写回 PRD 本节。
 */

// ---------------------------------------------------------------- 参数（PRD 初值）

/** 归一化后短于该长度即跳过（太短，不可能是完整问题）。 */
export const MIN_QUESTION_CHARS = 4;
/** 去重窗口保留的最近查询条数。 */
export const DEDUP_N = 10;
/** 去重窗口时长（ms）。超窗后同一问题可重新检索。 */
export const DEDUP_WINDOW_MS = 60_000;
/** 3-gram Jaccard 相似度阈值：≥ 视为重复，复用上次结果。 */
export const DEDUP_THRESHOLD = 0.85;
/** 提词框刷新后的锁定期（ms）。 */
export const LOCK_MS = 3_000;
/** 指纹的 n-gram 长度。 */
export const NGRAM = 3;

/** 句尾疑问标记（标点 `?` 另行在原文上判定，见 `questionVerdict`）。 */
export const TRAILING_QUESTION_MARKS = ["吗", "呢", "吧", "么"] as const;

/** 开头疑问词。 */
export const QUESTION_PREFIXES = [
  "怎么",
  "如何",
  "为什么",
  "什么",
  "哪",
  "多少",
  "是否",
  "能不能",
  "可不可以",
  "有没有",
  "几点",
  "多久",
] as const;

/** 陈述句开头。 */
export const DECLARATIVE_PREFIXES = [
  "我",
  "我们",
  "好的",
  "嗯",
  "对",
  "是",
  "那",
  "然后",
] as const;

// ---------------------------------------------------------------- 归一化

/** 归一化时保留的字符：字母与数字（其余标点/空白/符号全部丢弃）。 */
const NON_WORD = /[^\p{L}\p{N}]+/gu;

/**
 * 归一化：NFKC（全角→半角）→ 去标点（含空白）→ 转小写。
 *
 * PRD 原文为「去标点、转小写、去首尾空白」。这里把**全部空白**一并去掉
 * （而不仅是首尾）：中文不需要空格，且 3-gram 指纹对空白极敏感
 * ——「发货周期 是多久」与「发货周期是多久」应判为同一问题。
 */
export function normalize(text: string): string {
  return text.normalize("NFKC").replace(NON_WORD, "").toLowerCase();
}

/** 轻度归一化：只做 NFKC 与首尾去空白，**保留标点**（用于句尾 `?` 判定）。 */
export function normalizeLight(text: string): string {
  return text.normalize("NFKC").trim();
}

// ---------------------------------------------------------------- 问题判定

export type QuestionReason =
  | "too_short"
  | "trailing_mark"
  | "question_prefix"
  | "declarative"
  | "fallthrough";

export type QuestionVerdict = {
  isQuestion: boolean;
  reason: QuestionReason;
  normalized: string;
};

/**
 * 问题判定（PRD §3.3 规则表，**按表顺序**短路）。
 *
 * 顺序不可交换，两处特别说明：
 *
 * 1. **句尾 `?` 必须在原文上判定。** PRD 先要求「归一化（去标点）」再要求
 *    「末尾含 `吗/呢/吧/?/么`」——归一化会把 `?` 去掉，两条自相矛盾。
 *    实现取「中文语气词在归一化文本上判、`?` 在轻度归一化文本上判」，
 *    即两边都不丢信号。
 * 2. **陈述句规则里的「无疑问词」指全文不含疑问词**，而非「开头不是疑问词」。
 *    否则「我该怎么办」会被判成陈述句跳过——那正是最该检索的一类问题。
 *
 * 另记：「然后呢」按表顺序先命中「长度 < 4」而被跳过，尽管它以 `呢` 结尾。
 * 这是 PRD 规则顺序的直接结果，不是实现偏差。
 */
export function questionVerdict(raw: string): QuestionVerdict {
  const normalized = normalize(raw);

  if (normalized.length < MIN_QUESTION_CHARS) {
    return { isQuestion: false, reason: "too_short", normalized };
  }

  const trailingPunct = /[?？]$/.test(normalizeLight(raw));
  if (trailingPunct || TRAILING_QUESTION_MARKS.some((m) => normalized.endsWith(m))) {
    return { isQuestion: true, reason: "trailing_mark", normalized };
  }

  if (QUESTION_PREFIXES.some((p) => normalized.startsWith(p))) {
    return { isQuestion: true, reason: "question_prefix", normalized };
  }

  const hasQuestionWord = QUESTION_PREFIXES.some((p) => normalized.includes(p));
  if (!hasQuestionWord && DECLARATIVE_PREFIXES.some((p) => normalized.startsWith(p))) {
    return { isQuestion: false, reason: "declarative", normalized };
  }

  // 其他 → 是问题：宁可多检索，不可漏。
  return { isQuestion: true, reason: "fallthrough", normalized };
}

export function isQuestion(raw: string): boolean {
  return questionVerdict(raw).isQuestion;
}

// ---------------------------------------------------------------- 指纹与相似度

/**
 * 指纹：归一化文本的字符 3-gram 集合。
 *
 * 归一化后不足 3 字符时退化为「整串一个 gram」。若直接返回空集，
 * 两个不同短串的空集 Jaccard 会是 1（全等），把不相干的问题误判为重复。
 */
export function trigrams(normalized: string): Set<string> {
  const grams = new Set<string>();
  if (normalized.length === 0) return grams;
  if (normalized.length < NGRAM) {
    grams.add(normalized);
    return grams;
  }
  for (let i = 0; i + NGRAM <= normalized.length; i += 1) {
    grams.add(normalized.slice(i, i + NGRAM));
  }
  return grams;
}

/** Jaccard 相似度。两个空集视为全等（1）；一空一非空为 0。 */
export function jaccard(a: ReadonlySet<string>, b: ReadonlySet<string>): number {
  if (a.size === 0 && b.size === 0) return 1;
  let intersection = 0;
  for (const gram of a) {
    if (b.has(gram)) intersection += 1;
  }
  const union = a.size + b.size - intersection;
  return union === 0 ? 1 : intersection / union;
}

/** 两段原始文本的 3-gram Jaccard 相似度（内部各自归一化）。 */
export function similarity(x: string, y: string): number {
  return jaccard(trigrams(normalize(x)), trigrams(normalize(y)));
}

// ---------------------------------------------------------------- 去重窗口

export type DedupEntry<T> = {
  normalized: string;
  grams: Set<string>;
  atMs: number;
  payload: T;
};

export type DedupOptions = {
  n?: number;
  windowMs?: number;
  threshold?: number;
};

/**
 * 去重窗口：最近 N 条查询的指纹，60 秒滑动窗。
 *
 * `find` 与 `record` 分开，是因为**载荷要等检索完成才知道**：
 * 命中时要复用上次的结果，所以得先查、后记，不能边查边记。
 */
export class DedupWindow<T = unknown> {
  readonly n: number;
  readonly windowMs: number;
  readonly threshold: number;
  private entries: DedupEntry<T>[] = [];

  constructor(opts: DedupOptions = {}) {
    this.n = opts.n ?? DEDUP_N;
    this.windowMs = opts.windowMs ?? DEDUP_WINDOW_MS;
    this.threshold = opts.threshold ?? DEDUP_THRESHOLD;
  }

  /** 查找相似历史条目；无命中返回 null（**不写入**，写入请用 `record`）。 */
  find(normalized: string, nowMs: number): DedupEntry<T> | null {
    this.prune(nowMs);
    const grams = trigrams(normalized);
    for (const entry of this.entries) {
      if (jaccard(grams, entry.grams) >= this.threshold) return entry;
    }
    return null;
  }

  /**
   * 记入一条查询及其结果。
   *
   * 重复查询**不刷新**已存在条目的时间戳（PRD：「超窗后同一问题可重新检索」
   * —— 若刷新，反复问同一句会让它永不过期，与该条不符）。
   */
  record(normalized: string, nowMs: number, payload: T): void {
    this.prune(nowMs);
    this.entries.push({ normalized, grams: trigrams(normalized), atMs: nowMs, payload });
    while (this.entries.length > this.n) this.entries.shift();
  }

  /** 清掉超出窗口的条目。 */
  private prune(nowMs: number): void {
    const cutoff = nowMs - this.windowMs;
    this.entries = this.entries.filter((entry) => entry.atMs >= cutoff);
  }

  get size(): number {
    return this.entries.length;
  }

  reset(): void {
    this.entries = [];
  }
}

// ---------------------------------------------------------------- 刷新节流（锁定期）

/**
 * 锁定期：提词框刷新后 3 秒内不刷新 UI，新查询入队；到期只执行**最后一条**。
 *
 * 「取最后一条」在此实现为**单槽队列**（`offer` 直接覆盖）：
 * 与「入队后取队尾」完全等价，但内存有界——锁定期内可以说很多句，
 * 没有必要把中间那些注定被丢弃的查询留着。
 */
export class RefreshThrottle<T = unknown> {
  readonly lockMs: number;
  private lockedUntilMs = 0;
  private slot: T | null = null;

  constructor(lockMs: number = LOCK_MS) {
    this.lockMs = lockMs;
  }

  isLocked(nowMs: number): boolean {
    return nowMs < this.lockedUntilMs;
  }

  /** UI 已刷新 → 进入锁定期。 */
  markRefreshed(nowMs: number): void {
    this.lockedUntilMs = nowMs + this.lockMs;
  }

  /** 新查询：未锁定 → 立即执行；锁定中 → 入队（覆盖上一条）。 */
  offer(item: T, nowMs: number): "execute" | "queued" {
    if (!this.isLocked(nowMs)) return "execute";
    this.slot = item;
    return "queued";
  }

  /** 锁定期已过且有待执行项 → 取走最后一条；否则 null。 */
  poll(nowMs: number): T | null {
    if (this.isLocked(nowMs)) return null;
    const item = this.slot;
    this.slot = null;
    return item;
  }

  /**
   * 手动触发：立即打断锁定期并清空队列。
   * 手动项由调用方直接执行（优先级高于自动），故此处不返回任何待办。
   */
  interrupt(nowMs: number): void {
    this.lockedUntilMs = nowMs;
    this.slot = null;
  }

  /** 待执行项数（0 或 1）。 */
  get pending(): number {
    return this.slot === null ? 0 : 1;
  }

  reset(): void {
    this.lockedUntilMs = 0;
    this.slot = null;
  }
}
