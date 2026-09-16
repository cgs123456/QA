// @vitest-environment jsdom
/**
 * 陪练页单测（S5）：抽题渲染 / 自评推进 / 统计条 / 未记账明示 / **无检索**。
 *
 * 与 `History.test.tsx` 同款做法：mock 掉 **lib 与 hook 层**，把服务端返回写进
 * 可变对象。测的是"页面怎么把数据画出来"，不是 fetch 本身。
 *
 * 断言一律用**原生 DOM**（`textContent` / `disabled`）：本项目没装
 * `@testing-library/jest-dom`。
 *
 * 最后一条用例是纪律断言：**练习全程不触发检索**（`askQuestion` 零调用）。
 * 一旦有人给这页接了检索，练的就不是记忆而是检索 —— 那要红。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Rehearsal } from "./Rehearsal";
import type { RehearsalQuestion } from "../lib/api";

afterEach(() => cleanup());

function textOf(testId: string): string {
  return screen.getByTestId(testId).textContent ?? "";
}

function has(testId: string): boolean {
  return screen.queryByTestId(testId) != null;
}

// ------------------------------------------------------------------ mock

const QUESTIONS: RehearsalQuestion[] = [
  { id: "q1", standard_question: "发货周期是多久？", official_answer: "三天内", category: "甲" },
  { id: "q2", standard_question: "客服电话是多少？", official_answer: "400", category: "乙" },
  { id: "q3", standard_question: "退货期限是几天？", official_answer: "七天", category: "乙" },
];

const fetchCategories = vi.fn();
const drawQuestions = vi.fn();
const postVerdict = vi.fn();
const beginSession = vi.fn();
const endSession = vi.fn();
/** 纪律断言用：练习全程不许有人调它。 */
const askQuestion = vi.fn();
const voiceToggle = vi.fn();

vi.mock("../lib/api", () => ({
  fetchRehearsalCategories: () => fetchCategories(),
  drawRehearsalQuestions: (b: unknown) => drawQuestions(b),
  postRehearsalVerdict: (b: unknown) => postVerdict(b),
  beginSession: (s: unknown) => beginSession(s),
  endSession: () => endSession(),
}));

vi.mock("../lib/qa", () => ({
  askQuestion: (...args: unknown[]) => askQuestion(...args),
}));

vi.mock("../hooks/useRehearsalVoice", () => ({
  useRehearsalVoice: () => ({
    on: false,
    busy: false,
    error: null,
    toggle: () => voiceToggle(),
  }),
}));

beforeEach(() => {
  vi.clearAllMocks();
  fetchCategories.mockResolvedValue({
    store_id: "st1",
    categories: [{ category: "甲", count: 1 }, { category: "乙", count: 2 }],
    total: 3,
  });
  drawQuestions.mockResolvedValue({
    store_id: "st1",
    questions: QUESTIONS,
    drawn: 3,
    pool: 3,
    seed: 7,
  });
  postVerdict.mockResolvedValue({ recorded: true, session_id: "ses_0001" });
  beginSession.mockResolvedValue({ session_id: "ses_0001", already: false, recording: true });
  endSession.mockResolvedValue({ session_id: "ses_0001", closed: true });
});

// ------------------------------------------------------------------ 用例

describe("Rehearsal", () => {
  it("类目下拉来自服务端，不硬编码", async () => {
    render(<Rehearsal />);
    // 必须**限定在类目下拉里**找 option：页面上还有一个题数下拉，
    // 不限定会把它的 5/10/20 一起捞进来（第一次就是这么红的）。
    const select = await screen.findByTestId("rehearsal-category");
    const options = within(select as HTMLElement).getAllByRole("option");
    // 首项固定是「全部」，其余是服务端给的类目（带计数）
    expect(options.map((o) => o.textContent)).toEqual(["全部", "甲（1）", "乙（2）"]);
  });

  it("库为空 → 给出导入引导，而不是一个点不动的开始", async () => {
    fetchCategories.mockResolvedValue({ store_id: "st1", categories: [], total: 0 });
    render(<Rehearsal />);
    expect(await screen.findByTestId("rehearsal-empty")).toBeTruthy();
  });

  it("开始练习 → 抽题并显示第一题与进度", async () => {
    render(<Rehearsal />);
    await userEvent.click(screen.getByTestId("rehearsal-start"));
    expect(await screen.findByTestId("rehearsal-question")).toBeTruthy();
    expect(textOf("rehearsal-question")).toBe("发货周期是多久？");
    expect(textOf("rehearsal-progress")).toBe("第 1 / 3 题 · 甲");
    // 会话以 source=rehearsal 开
    expect(beginSession).toHaveBeenCalledWith("rehearsal");
  });

  it("看标准答案 → 展开 standard_question 与 official_answer", async () => {
    render(<Rehearsal />);
    await userEvent.click(screen.getByTestId("rehearsal-start"));
    await userEvent.click(await screen.findByTestId("rehearsal-reveal"));
    const text = textOf("rehearsal-answer");
    expect(text).toContain("三天内");
  });

  it("自评推进到下一题，并更新统计条", async () => {
    render(<Rehearsal />);
    await userEvent.click(screen.getByTestId("rehearsal-start"));
    await userEvent.click(await screen.findByTestId("rehearsal-judge-correct"));
    expect(textOf("rehearsal-progress")).toBe("第 2 / 3 题 · 乙");
    expect(textOf("rehearsal-summary")).toContain("1 题 · 答对 1");
    // 发档位/类目/语料 id + 题面/标准答案/用户作答（账有文本，日志无文本——R14）
    expect(postVerdict).toHaveBeenCalledWith({
      qa_id: "q1",
      category: "甲",
      verdict: "correct",
      question_text: "发货周期是多久？",
      official_answer: "三天内",
      user_answer: "",
    });
  });

  it("答完最后一题 → 结束态 + 收会话", async () => {
    render(<Rehearsal />);
    await userEvent.click(screen.getByTestId("rehearsal-start"));
    for (const v of ["correct", "unknown", "unknown"]) {
      await userEvent.click(await screen.findByTestId(`rehearsal-judge-${v}`));
    }
    expect(has("rehearsal-again")).toBe(true);
    expect(textOf("rehearsal-summary")).toContain("3 题 · 答对 1 / 部分 0 / 不会 2");
    expect(textOf("rehearsal-summary")).toContain("高频不会：乙(2)");
    expect(endSession).toHaveBeenCalled();
  });

  it("记不上账就明示「本次未记账」，不假装记上了", async () => {
    postVerdict.mockResolvedValue({ recorded: false, session_id: null });
    render(<Rehearsal />);
    await userEvent.click(screen.getByTestId("rehearsal-start"));
    await userEvent.click(await screen.findByTestId("rehearsal-judge-unknown"));
    expect(await screen.findByTestId("rehearsal-not-recorded")).toBeTruthy();
  });

  it("记账请求失败也不阻断练习（自评已经落了 UI）", async () => {
    postVerdict.mockRejectedValue(new Error("sidecar 断了"));
    render(<Rehearsal />);
    await userEvent.click(screen.getByTestId("rehearsal-start"));
    await userEvent.click(await screen.findByTestId("rehearsal-judge-correct"));
    expect(textOf("rehearsal-progress")).toBe("第 2 / 3 题 · 乙");
    expect(has("rehearsal-not-recorded")).toBe(true);
  });

  it("抽题失败就地报错，不吞", async () => {
    drawQuestions.mockRejectedValue(new Error("该范围内没有题"));
    render(<Rehearsal />);
    await userEvent.click(screen.getByTestId("rehearsal-start"));
    expect(await screen.findByTestId("rehearsal-action-error")).toBeTruthy();
  });

  it("练习全程不触发检索（askQuestion 零调用）", async () => {
    render(<Rehearsal />);
    await userEvent.click(screen.getByTestId("rehearsal-start"));
    await userEvent.click(await screen.findByTestId("rehearsal-reveal"));
    for (const v of ["correct", "partial", "unknown"]) {
      await userEvent.click(await screen.findByTestId(`rehearsal-judge-${v}`));
    }
    expect(askQuestion).not.toHaveBeenCalled();
  });
});
