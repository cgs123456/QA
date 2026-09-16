/**
 * 陪练统计聚合单测（S5）。纯函数，不需要 jsdom。
 *
 * 重点在**口径**而不是算术：什么算进"不会"、同数时的排序是否确定 ——
 * 这两件事错一个，统计条就会给出误导性的复习方向。
 */

import { describe, expect, it } from "vitest";

import {
  emptySummary,
  formatSummary,
  summarize,
  type RehearsalAnswer,
} from "./rehearsal";
import type { RehearsalQuestion } from "./api";

function q(id: string, category: string): RehearsalQuestion {
  return { id, standard_question: `题${id}`, official_answer: `答${id}`, category };
}

function a(id: string, category: string, verdict: RehearsalAnswer["verdict"]) {
  return { question: q(id, category), verdict };
}

describe("summarize", () => {
  it("空序列 → 全 0，且不崩", () => {
    expect(summarize([])).toEqual(emptySummary());
  });

  it("按档位计数", () => {
    const s = summarize([
      a("1", "甲", "correct"),
      a("2", "甲", "partial"),
      a("3", "乙", "unknown"),
      a("4", "乙", "unknown"),
      a("5", "丙", "correct"),
    ]);
    expect(s.answered).toBe(5);
    expect(s.byVerdict).toEqual({ correct: 2, partial: 1, unknown: 2 });
  });

  it("「高频不会」只统计 unknown —— partial 不算不会", () => {
    // partial 是"答到一半"，要补细节；unknown 是"不会"，要重学。
    // 两者混在一起，这个指标就既不指向重学也不指向补细节。
    const s = summarize([
      a("1", "甲", "partial"),
      a("2", "甲", "partial"),
      a("3", "甲", "partial"),
      a("4", "乙", "unknown"),
    ]);
    expect(s.unknownByCategory).toEqual([{ category: "乙", count: 1 }]);
  });

  it("不会的类目按计数降序，且类目一个不丢", () => {
    const s = summarize([
      a("1", "甲", "unknown"),
      a("2", "乙", "unknown"),
      a("3", "乙", "unknown"),
      a("4", "丙", "unknown"),
      a("5", "乙", "unknown"),
    ]);
    expect(s.unknownByCategory.map((c) => c.count)).toEqual([3, 1, 1]);
    expect(s.unknownByCategory[0]).toEqual({ category: "乙", count: 3 });
    expect(new Set(s.unknownByCategory.map((c) => c.category))).toEqual(
      new Set(["甲", "乙", "丙"]),
    );
  });

  it("同计数时次序确定（多次调用完全一致）", () => {
    // 不钉死具体 collation：中文 `localeCompare` 走**拼音序**（丙 < 甲 < 乙），
    // 与码点序不同，且 collation 依赖 ICU 版本 —— 钉死会让测试绑死在某个 Node 上。
    // 真正要保证的是"同输入同输出"：不确定的话每次 render 顺序会跳，
    // 用户会以为统计在变。
    const answers = [
      a("1", "丙", "unknown"),
      a("2", "甲", "unknown"),
      a("3", "乙", "unknown"),
    ];
    const first = summarize(answers).unknownByCategory;
    expect(summarize(answers).unknownByCategory).toEqual(first);
    expect(first).toHaveLength(3);
  });

  it("类目为空 → 归入 (未分类)，不丢", () => {
    const s = summarize([a("1", "", "unknown")]);
    expect(s.unknownByCategory).toEqual([{ category: "(未分类)", count: 1 }]);
  });

  it("correct 不进入 unknownByCategory", () => {
    const s = summarize([a("1", "甲", "correct"), a("2", "甲", "correct")]);
    expect(s.unknownByCategory).toEqual([]);
  });
});

describe("formatSummary", () => {
  it("固定按 答对/部分/不会 的顺序渲染", () => {
    const s = summarize([
      a("1", "甲", "unknown"),
      a("2", "甲", "correct"),
      a("3", "甲", "partial"),
    ]);
    expect(formatSummary(s)).toBe("3 题 · 答对 1 / 部分 1 / 不会 1 · 高频不会：甲(1)");
  });

  it("没有不会的题时不追加那一段（不留一个空冒号）", () => {
    const s = summarize([a("1", "甲", "correct")]);
    expect(formatSummary(s)).toBe("1 题 · 答对 1 / 部分 0 / 不会 0");
  });
});
