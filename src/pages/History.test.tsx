// @vitest-environment jsdom
/**
 * 历史会话页单测（S3）：列表渲染 / 空态 / 删除确认流 / 时间线排序 / R21 无检索。
 *
 * 与 `Knowledge.test.tsx` 同款做法：**mock 掉 hook 层**，把服务端返回写进
 * 可变状态对象里。这样测的是"页面怎么把数据画出来"，而不是 TanStack Query
 * 本身（那不是本页的责任）。
 *
 * 注意 mock 返回的**引用必须稳定** —— React 的 effect 依赖会拿它做比较，
 * 每次 render 一个新数组会触发无限循环（Knowledge 那边踩过）。
 *
 * 断言一律用**原生 DOM**（`textContent` / `disabled`）：本项目没装
 * `@testing-library/jest-dom`，为几个 `toBeXxx` 糖引一个依赖不划算。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { History } from "./History";
import type { SessionTimeline, SessionsPage } from "../lib/api";

afterEach(() => cleanup());

/** 读一个带 testid 元素的文本（无 jest-dom，不写 toHaveTextContent）。 */
function textOf(testId: string, index = 0): string {
  return screen.getAllByTestId(testId)[index]?.textContent ?? "";
}

function isDisabled(testId: string, index = 0): boolean {
  const el = screen.getAllByTestId(testId)[index];
  return el instanceof HTMLButtonElement ? el.disabled : false;
}

// ------------------------------------------------------------------ mock

const askQuestion = vi.fn();

vi.mock("../lib/qa", () => ({
  askQuestion: (...args: unknown[]) => askQuestion(...args),
}));

/** 由各用例在渲染前写入，模拟服务端返回。 */
const listState: { data?: SessionsPage; isPending?: boolean; isError?: boolean; error?: unknown } =
  {};
const statusState: { data?: { session_id: string | null } } = {};
const timelineState: {
  data?: SessionTimeline;
  isPending?: boolean;
  isError?: boolean;
  error?: unknown;
} = {};
const storeState: { data?: { id: string; name: string }[] } = {};

const deleteMutate = vi.fn();

vi.mock("../hooks/useSessions", () => ({
  useSessions: () => ({
    data: listState.data,
    isPending: listState.isPending ?? false,
    isError: listState.isError ?? false,
    error: listState.error,
  }),
  useSessionStatus: () => ({ data: statusState.data ?? { session_id: null } }),
  useSessionTimeline: () => ({
    data: timelineState.data,
    isPending: timelineState.isPending ?? false,
    isError: timelineState.isError ?? false,
    error: timelineState.error,
  }),
  useDeleteSession: () => ({ mutate: deleteMutate, isPending: false }),
}));

vi.mock("../hooks/useStore", () => ({
  useStoresQuery: () => ({ data: storeState.data ?? [] }),
}));

// S4：导出是一次性 mutation，不进缓存；这里用哑实现（导出不是本文件的测试对象）。
vi.mock("../hooks/useSessionRecording", () => ({
  useExportSession: () => ({ mutate: vi.fn(), isPending: false, error: null }),
}));

// ------------------------------------------------------------------ 夹具

const PAGE: SessionsPage = {
  sessions: [
    {
      session_id: "ses_0002",
      started_ms: 1_762_000_000_000,
      last_ms: 1_762_000_062_000,
      events: 7,
      store_id: "s1",
      closed: true,
      qa_exchanges: 2,
    },
    {
      session_id: "ses_0001",
      started_ms: 1_761_000_000_000,
      last_ms: 1_761_000_030_000,
      events: 3,
      store_id: null,
      closed: false,
      qa_exchanges: 0,
    },
  ],
  total: 2,
  limit: 20,
  offset: 0,
};

/** 故意**乱序（id 1,3,2,4,5）：证明前端按时刻排序，而不是照抄服务端顺序。 */
const TIMELINE: SessionTimeline = {
  session_id: "ses_0002",
  count: 5,
  events: [
    {
      id: 1,
      event_type: "session_begin",
      stage: "capture",
      ts_ms: 1_762_000_000_000,
      store_id: "s1",
      metadata: { session_id: "ses_0002" },
    },
    {
      id: 3,
      event_type: "qa_exchange",
      stage: "qa",
      ts_ms: 1_762_000_030_000,
      store_id: "s1",
      metadata: {
        action: "direct",
        provider: "ollama",
        llm_calls: 0,
        sources: 1,
        warnings: 0,
        elapsed_ms: 12,
        top1_score: 0.9,
      },
    },
    {
      id: 2,
      event_type: "asr_final",
      stage: "loopback",
      ts_ms: 1_762_000_010_000,
      store_id: "s1",
      metadata: { segment_id: "seg_1", duration_ms: 1200, provider: "stub" },
    },
    {
      id: 5,
      event_type: "rehearsal",
      stage: "rehearsal",
      ts_ms: 1_762_000_040_000,
      store_id: "s1",
      metadata: {
        verdict: "unknown",
        category: "甲",
        qa_id: "q001",
        question_text: "发货周期是多久？",
        official_answer: "三天内",
        user_answer: "大概三天吧",
      },
    },
    {
      id: 4,
      event_type: "session_end",
      stage: null,
      ts_ms: 1_762_000_062_000,
      store_id: "s1",
      metadata: { session_id: "ses_0002", duration_ms: 62_000 },
    },
  ],
};

beforeEach(() => {
  askQuestion.mockClear();
  deleteMutate.mockClear();
  listState.data = PAGE;
  listState.isPending = false;
  listState.isError = false;
  listState.error = undefined;
  statusState.data = { session_id: null };
  timelineState.data = TIMELINE;
  timelineState.isPending = false;
  timelineState.isError = false;
  timelineState.error = undefined;
  storeState.data = [{ id: "s1", name: "主库" }];
});

// ------------------------------------------------------------------ 列表

describe("History 列表", () => {
  it("每行给出时间 / 时长·问答数 / 库名", () => {
    render(<History onOpenSettings={() => {}} />);
    const rows = screen.getAllByTestId("history-row");
    expect(rows).toHaveLength(2);

    expect(rows[0].textContent).toContain("问答 2 次");
    expect(rows[0].textContent).toContain("1 分 02 秒");
    expect(textOf("history-store", 0)).toContain("主库");
    // 没记 store_id 的那行老老实实说「（未记录）」，不编一个库名。
    expect(textOf("history-store", 1)).toContain("（未记录）");
  });

  it("时间也画出来了（不能只有统计没有时刻）", () => {
    render(<History onOpenSettings={() => {}} />);
    expect(textOf("history-open", 0)).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  });

  it("没正常结束的会话要标出来（不是数据损坏，是进程被杀）", () => {
    render(<History onOpenSettings={() => {}} />);
    expect(screen.getAllByTestId("history-unclosed")).toHaveLength(1);
    expect(textOf("history-unclosed", 0)).toContain("未正常结束");
  });

  it("读失败**报错误**，不显示成空列表（空列表会被读成「这段时间没发生过事」）", () => {
    listState.data = undefined;
    listState.isError = true;
    listState.error = new Error("HTTP 500");
    render(<History onOpenSettings={() => {}} />);
    expect(textOf("history-error")).toContain("会话列表读取失败");
    expect(screen.queryByTestId("history-empty")).toBeNull();
  });
});

// ------------------------------------------------------------------ 空态

describe("History 空态", () => {
  it("一条会话都没有时给出引导，入口能跳到设置", async () => {
    listState.data = { sessions: [], total: 0, limit: 20, offset: 0 };
    const onOpenSettings = vi.fn();
    render(<History onOpenSettings={onOpenSettings} />);

    expect(textOf("history-empty")).toContain("开启会话录制");
    await userEvent.click(screen.getByTestId("history-goto-settings"));
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
  });
});

// ------------------------------------------------------------------ 删除

describe("删除确认流", () => {
  it("点删除只弹确认框，不直接删", async () => {
    render(<History onOpenSettings={() => {}} />);
    await userEvent.click(screen.getAllByTestId("history-delete")[0]);

    expect(deleteMutate).not.toHaveBeenCalled();
    expect(textOf("confirm-text")).toContain("转写文本将被永久删除");
  });

  it("取消就是取消：关框且零调用", async () => {
    render(<History onOpenSettings={() => {}} />);
    await userEvent.click(screen.getAllByTestId("history-delete")[0]);
    await userEvent.click(screen.getByTestId("confirm-cancel"));

    expect(deleteMutate).not.toHaveBeenCalled();
    expect(screen.queryByTestId("history-confirm")).toBeNull();
  });

  it("确认才真删，且删的是那一条", async () => {
    render(<History onOpenSettings={() => {}} />);
    await userEvent.click(screen.getAllByTestId("history-delete")[0]);
    await userEvent.click(screen.getByTestId("confirm-ok"));

    expect(deleteMutate).toHaveBeenCalledTimes(1);
    expect(deleteMutate.mock.calls[0][0]).toBe("ses_0002");
  });

  it("进行中的会话：删除按钮直接禁用（后端仍是唯一真值）", () => {
    statusState.data = { session_id: "ses_0002" };
    render(<History onOpenSettings={() => {}} />);

    expect(textOf("history-running")).toContain("进行中");
    expect(isDisabled("history-delete", 0)).toBe(true);
    // 另一条不受影响：守卫只针对目标会话。
    expect(isDisabled("history-delete", 1)).toBe(false);
  });
});

// ------------------------------------------------------------------ 时间线

describe("单会话时间线", () => {
  it("按时刻排序渲染（服务端可以乱序回，前端负责排）", async () => {
    render(<History onOpenSettings={() => {}} />);
    await userEvent.click(screen.getAllByTestId("history-open")[0]);

    expect(screen.getByTestId("session-detail")).not.toBeNull();
    const times = screen.getAllByTestId("tl-time").map((el) => el.textContent ?? "");
    expect(times).toEqual([...times].sort());
  });

  it("转写与问答分卡渲染，且都明说文本未落盘（R14）", async () => {
    render(<History onOpenSettings={() => {}} />);
    await userEvent.click(screen.getAllByTestId("history-open")[0]);

    expect(textOf("tl-asr")).toContain("系统回环");
    expect(textOf("tl-qa-action")).toContain("直接命中");
    // 文本位置不能留白 —— 留白会被读成"这次没说话"。
    expect(textOf("tl-asr-text")).toContain("文本未落盘");
    expect(textOf("tl-qa-text")).toContain("问题与答案未落盘");
    // 摘要把三类分开数（含陪练）。
    expect(textOf("tl-meta")).toContain("共 5 行 · 转写 1 · 问答 1 · 陪练 1");
  });

  it("可以返回列表", async () => {
    render(<History onOpenSettings={() => {}} />);
    await userEvent.click(screen.getAllByTestId("history-open")[0]);
    await userEvent.click(screen.getByTestId("tl-back"));
    expect(screen.getByTestId("history-list")).not.toBeNull();
  });

  it("陪练行渲染题面/标准答案/用户作答（S12：账有文本）", async () => {
    render(<History onOpenSettings={() => {}} />);
    await userEvent.click(screen.getAllByTestId("history-open")[0]);

    expect(screen.getByTestId("tl-rehearsal")).toBeTruthy();
    expect(textOf("tl-rehearsal-text")).toContain("发货周期是多久？");
    expect(textOf("tl-rehearsal-text")).toContain("三天内");
    expect(textOf("tl-rehearsal-text")).toContain("大概三天吧");
  });

  it("R21：从进列表到看完时间线，**零次** askQuestion", async () => {
    render(<History onOpenSettings={() => {}} />);
    await userEvent.click(screen.getAllByTestId("history-open")[0]);
    await userEvent.click(screen.getByTestId("tl-back"));
    await userEvent.click(screen.getAllByTestId("history-delete")[1]);
    await userEvent.click(screen.getByTestId("confirm-cancel"));
    await userEvent.click(screen.getAllByTestId("history-open")[1]);

    expect(askQuestion).not.toHaveBeenCalled();
    // 时间线上也不存在任何会发起检索的控件（回放就是回放）。
    expect(screen.queryByTestId("tl-ask")).toBeNull();
  });
});
