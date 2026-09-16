/**
 * 面试陪练（S5）的**纯计算**部分：作答序列 → 统计条。
 *
 * 抽题与记账在服务端（`routers/rehearsal.py`：可测的种子随机 + 唯一的落行入口）；
 * 这里只管"把用户点出来的三档算成一句话"。放成纯函数是为了能直接单测 ——
 * 页面测试要 mock 网络、要渲染，而这段逻辑一个输入一个输出，不该被那些噪声盖住。
 *
 * 规格：`docs/rehearsal-spec.md` §4。**不做**跨会话长期统计（留 Phase 4）。
 */

import type { RehearsalQuestion, RehearsalVerdict } from "./api";

/** 一次作答：题 + 用户自评。 */
export type RehearsalAnswer = {
  question: RehearsalQuestion;
  verdict: RehearsalVerdict;
};

export type RehearsalSummary = {
  /** 已自评的题数（不是抽到的题数）。 */
  answered: number;
  byVerdict: Record<RehearsalVerdict, number>;
  /** 不会的题按类目计数，**降序**（同数按类目名升序，保证渲染稳定）。 */
  unknownByCategory: { category: string; count: number }[];
};

export const VERDICT_LABELS: Record<RehearsalVerdict, string> = {
  correct: "答对",
  partial: "部分",
  unknown: "不会",
};

/** 三档的固定顺序：UI 按钮、统计条都按它排，避免各处各排一套。 */
export const VERDICT_ORDER: RehearsalVerdict[] = ["correct", "partial", "unknown"];

export function emptySummary(): RehearsalSummary {
  return { answered: 0, byVerdict: { correct: 0, partial: 0, unknown: 0 },
           unknownByCategory: [] };
}

/**
 * 作答序列 → 统计条。
 *
 * **"高频不会的类目"只统计 `unknown`**：`partial` 是"答到一半"，
 * 把它并进"不会"会让这个指标变成"没答全的类目"，指向的复习动作完全不同
 * （一个要重学、一个要补细节）。
 */
export function summarize(answers: RehearsalAnswer[]): RehearsalSummary {
  const byVerdict: Record<RehearsalVerdict, number> = {
    correct: 0, partial: 0, unknown: 0,
  };
  const unknown: Record<string, number> = {};
  for (const a of answers) {
    byVerdict[a.verdict] += 1;
    if (a.verdict === "unknown") {
      const c = a.question.category || "(未分类)";
      unknown[c] = (unknown[c] ?? 0) + 1;
    }
  }
  const unknownByCategory = Object.entries(unknown)
    .map(([category, count]) => ({ category, count }))
    // 计数降序；同数时按 `localeCompare`（对中文是**拼音序**：丙 < 甲 < 乙，
    // 不是码点序）—— 只要确定即可，用户不会在意甲乙丙还是丙甲乙，
    // 但在意"每次 render 顺序不一样"。故测试只断言**确定性与计数降序**，
    // 不钉死具体 collation（那会绑死 ICU 版本）。
    .sort((a, b) => b.count - a.count || a.category.localeCompare(b.category));
  return { answered: answers.length, byVerdict, unknownByCategory };
}

/** 统计条的一句话（页面与测试共用，避免两处各写一套文案）。 */
export function formatSummary(s: RehearsalSummary): string {
  const parts = VERDICT_ORDER.map(
    (v) => `${VERDICT_LABELS[v]} ${s.byVerdict[v]}`,
  ).join(" / ");
  const top = s.unknownByCategory
    .map((c) => `${c.category}(${c.count})`)
    .join(" ");
  return `${s.answered} 题 · ${parts}${top ? ` · 高频不会：${top}` : ""}`;
}
