/**
 * 会话账渲染逻辑单测（S3）。
 *
 * 这里测的是**用户会读到什么**：时长、时间、库名、删除确认、时间线顺序。
 * 判断错了界面会理直气壮地骗人 —— 比如把"总账行数"说成"问答次数"。
 */

import { describe, expect, it } from "vitest";

// Vite 的 `?raw`：把源码当字符串读进来做**源码级**断言（R21）。
// 用 `?raw` 而不是 `node:fs` —— 这是浏览器项目，tsconfig 里没有 node 类型。
// 扩展名必须写全：`./sessions?raw` 在 Linux 上报 ENOENT（Vite 不会替 `?raw` 补扩展名，
// Windows 上碰巧能解析）。CI 是 ubuntu，所以这条会真红。
import sessionsSrc from "./sessions.ts?raw";
import sessionDetailSrc from "../components/SessionDetail.tsx?raw";

import type { SessionEvent, SessionSummary } from "./api";
import {
  canDelete,
  deleteErrorMessage,
  eventTypeLabel,
  formatClock,
  formatDateTime,
  formatDuration,
  qaActionLabel,
  qaExchangeFacts,
  formatBytes,
  rehearsalFacts,
  retentionHint,
  retentionLabel,
  sessionDurationMs,
  sessionQaCount,
  sessionStatsLine,
  stageLabel,
  storeNameById,
  timelineOrder,
  timelineSummary,
  toTimelineRow,
  usageLine,
  SESSION_DELETE_CONFIRM_TEXT,
  SESSION_PURGE_CONFIRM_TEXT,
} from "./sessions";

// ------------------------------------------------------------------ 夹具

function session(over: Partial<SessionSummary> = {}): SessionSummary {
  return {
    session_id: "ses_0001",
    started_ms: 1_762_000_000_000,
    last_ms: 1_762_000_062_000,
    events: 7,
    store_id: "s1",
    closed: true,
    ...over,
  };
}

function event(over: Partial<SessionEvent> = {}): SessionEvent {
  return {
    id: 1,
    event_type: "asr_final",
    stage: "loopback",
    ts_ms: 1_762_000_010_000,
    store_id: "s1",
    metadata: { segment_id: "seg_1", duration_ms: 1200, provider: "stub" },
    ...over,
  };
}

// ------------------------------------------------------------------ 时间与时长

describe("formatDuration", () => {
  it("秒 / 分秒 / 时分 三档各有一种说法", () => {
    expect(formatDuration(12_000)).toBe("12 秒");
    expect(formatDuration(62_000)).toBe("1 分 02 秒");
    expect(formatDuration(3_780_000)).toBe("1 小时 03 分");
  });

  it("0 秒也要说出来 —— 空串会被读成「没统计到」", () => {
    expect(formatDuration(0)).toBe("0 秒");
  });

  it("负数与 NaN 是「—」，不显示 0 秒假装测到了", () => {
    expect(formatDuration(-5)).toBe("—");
    expect(formatDuration(Number.NaN)).toBe("—");
  });
});

describe("formatDateTime / formatClock", () => {
  it("无效时刻是「—」，不拿 0 当 1970 年", () => {
    expect(formatDateTime(0)).toBe("—");
    expect(formatDateTime(null)).toBe("—");
    expect(formatClock(undefined)).toBe("—");
  });

  it("有效时刻给出可读的本地时间", () => {
    expect(formatDateTime(1_762_000_000_000)).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
    expect(formatClock(1_762_000_000_000)).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });
});

// ------------------------------------------------------------------ 列表行

describe("会话列表的一行", () => {
  it("时长取末行减首行（服务端给的是两个时刻，不是时长）", () => {
    expect(sessionDurationMs(session())).toBe(62_000);
    // last < started 是不该发生的，钳到 0 而不是显示负数。
    expect(sessionDurationMs(session({ last_ms: 1, started_ms: 999 }))).toBe(0);
  });

  it("有 qa_exchanges 就说问答数，没有就明说「未分解」—— 不拿总行数冒充", () => {
    expect(sessionQaCount(session({ qa_exchanges: 3 }))).toBe(3);
    expect(sessionQaCount(session())).toBeNull();

    expect(sessionStatsLine(session({ qa_exchanges: 3 }))).toContain("问答 3 次");
    const line = sessionStatsLine(session());
    expect(line).toContain("账 7 行");
    expect(line).toContain("未分解");
    expect(line).not.toContain("问答 7 次");
  });

  it("库名：命中就给名，库被删了就回落 id（可追溯），没记就是「（未记录）」", () => {
    const stores = [{ id: "s1", name: "主库" }];
    expect(storeNameById(stores, "s1")).toBe("主库");
    expect(storeNameById(stores, "s-deleted")).toBe("s-deleted");
    expect(storeNameById(stores, null)).toBe("（未记录）");
    expect(storeNameById(undefined, "s1")).toBe("s1");
  });
});

// ------------------------------------------------------------------ 删除

describe("删除守卫与文案", () => {
  it("R22 文案逐字使用（不改写、不翻译）", () => {
    expect(SESSION_DELETE_CONFIRM_TEXT).toBe("转写文本将被永久删除");
  });

  it("进行中的会话先拦一次，理由要说清该怎么解", () => {
    const s = session();
    expect(canDelete(s, null).ok).toBe(true);
    const guarded = canDelete(s, "ses_0001");
    expect(guarded.ok).toBe(false);
    if (!guarded.ok) expect(guarded.reason).toContain("停止采集");
  });

  it("删除失败优先显示服务端的话（409 与 404 说法不同）", () => {
    expect(
      deleteErrorMessage({ payload: { message: "会话进行中，先 /session/end 再删" } }),
    ).toBe("会话进行中，先 /session/end 再删");
    expect(deleteErrorMessage(new Error("HTTP 404"))).toBe("HTTP 404");
  });
});

// ------------------------------------------------------------------ 标签

describe("标签", () => {
  it("已知类型/路名给中文，未知原样透出（不悄悄吞掉）", () => {
    expect(eventTypeLabel("asr_final")).toBe("转写完成");
    expect(eventTypeLabel("brand_new")).toBe("brand_new");
    expect(stageLabel("mic")).toBe("麦克风");
    expect(stageLabel(null)).toBe("—");
    expect(qaActionLabel("fail_closed")).toBe("未命中（拒答）");
    expect(qaActionLabel("weird")).toBe("weird");
  });
});

// ------------------------------------------------------------------ 录制开关与保留策略

describe("录制开关与保留策略（S4）", () => {
  it("三档保留策略各有说法，永久要说清「不自动删除」", () => {
    expect(retentionLabel(7)).toBe("保留 7 天");
    expect(retentionLabel(30)).toBe("保留 30 天");
    expect(retentionLabel(null)).toBe("永久保留");
    expect(retentionHint(null)).toContain("不自动删除");
  });

  it("有限期必须补一句「按最后一次活动算」—— 用户以为的 30 天是这个意思", () => {
    expect(retentionHint(30)).toContain("最后一次活动");
    expect(retentionHint(30)).toContain("30 天");
    expect(retentionHint(7)).toContain("7 天");
  });

  it("数据量一句话：会话数 / 行数 / 载荷都要在，零值也别说成读不到", () => {
    expect(usageLine({ sessions: 3, events: 41, payload_bytes: 2_048 })).toBe(
      "3 次会话 · 41 行账 · 载荷 2.0 KB",
    );
    const empty = usageLine({ sessions: 0, events: 0, payload_bytes: 0 });
    expect(empty).toContain("0 次会话");
    expect(empty).toContain("0 行账");
  });

  it("字节三档：B / KB / MB", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2_048)).toBe("2.0 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
    expect(formatBytes(-1)).toBe("—");
  });

  it("清除全部的文案比删单条更重（范围 + 不可恢复）", () => {
    expect(SESSION_PURGE_CONFIRM_TEXT).toContain("全部");
    expect(SESSION_PURGE_CONFIRM_TEXT).toContain("不可恢复");
    expect(SESSION_PURGE_CONFIRM_TEXT).not.toBe(SESSION_DELETE_CONFIRM_TEXT);
  });
});

// ------------------------------------------------------------------ 时间线

describe("时间线排序", () => {
  it("时刻升序；同毫秒按行 id 升序（服务端按发生序回，这里只保证不被打乱）", () => {
    const rows = timelineOrder([
      event({ id: 3, ts_ms: 200 }),
      event({ id: 1, ts_ms: 300 }),
      event({ id: 2, ts_ms: 300 }),
      event({ id: 9, ts_ms: 100 }),
    ]);
    expect(rows.map((r) => r.id)).toEqual([9, 3, 1, 2]);
  });

  it("ts_ms 缺失（v002 之前的回填行）排最后，但内部仍按 id 有序", () => {
    const rows = timelineOrder([
      event({ id: 5, ts_ms: null }),
      event({ id: 2, ts_ms: 100 }),
      event({ id: 3, ts_ms: null }),
    ]);
    expect(rows.map((r) => r.id)).toEqual([2, 3, 5]);
  });

  it("不原地改参数（排序结果要能被反复渲染而不漂移）", () => {
    const input = [event({ id: 2, ts_ms: 200 }), event({ id: 1, ts_ms: 100 })];
    const out = timelineOrder(input);
    expect(input.map((r) => r.id)).toEqual([2, 1]);
    expect(out.map((r) => r.id)).toEqual([1, 2]);
  });
});

describe("时间线的一行", () => {
  it("asr_final 解析出段/时长/provider/降级（没有文本字段）", () => {
    const row = toTimelineRow(
      event({
        metadata: {
          segment_id: "seg_3",
          duration_ms: 2400,
          provider: "faster-whisper",
          degraded_levels: 1,
          dropped_oldest: 0,
        },
      }),
    );
    expect(row.kind).toBe("asr");
    expect(row.asr).toEqual({
      segmentId: "seg_3",
      durationMs: 2400,
      provider: "faster-whisper",
      degradedLevels: 1,
      droppedOldest: 0,
    });
    expect(row.qa).toBeNull();
  });

  it("qa_exchange 解析出动作/provider/计数（没有 problem 与 answer 字段）", () => {
    const row = toTimelineRow(
      event({
        id: 7,
        event_type: "qa_exchange",
        stage: "qa",
        metadata: {
          action: "direct",
          provider: "ollama",
          llm_calls: 0,
          sources: 2,
          warnings: 1,
          elapsed_ms: 42,
          top1_score: 0.9,
        },
      }),
    );
    expect(row.kind).toBe("qa");
    expect(row.qa).toEqual({
      action: "direct",
      provider: "ollama",
      llmCalls: 0,
      sources: 2,
      warnings: 1,
      elapsedMs: 42,
      top1Score: 0.9,
    });
    expect(row.asr).toBeNull();
  });

  it("rehearsal 解析出档位/类目/语料/题面/标准答案/用户作答", () => {
    const row = toTimelineRow(
      event({
        id: 8,
        event_type: "rehearsal",
        stage: "rehearsal",
        metadata: {
          verdict: "unknown",
          category: "甲",
          qa_id: "q001",
          question_text: "发货周期是多久？",
          official_answer: "三天内",
          user_answer: "大概三天吧",
        },
      }),
    );
    expect(row.kind).toBe("rehearsal");
    expect(row.rehearsal).toEqual({
      verdict: "unknown",
      category: "甲",
      qaId: "q001",
      questionText: "发货周期是多久？",
      officialAnswer: "三天内",
      userAnswer: "大概三天吧",
    });
    expect(row.asr).toBeNull();
    expect(row.qa).toBeNull();
  });

  it("rehearsal 缺字段不炸：缺失的字符串按 null 处理", () => {
    const facts = rehearsalFacts({});
    expect(facts.verdict).toBe("—");
    expect(facts.category).toBeNull();
    expect(facts.qaId).toBe("—");
    expect(facts.questionText).toBeNull();
    expect(facts.officialAnswer).toBeNull();
    expect(facts.userAnswer).toBeNull();
  });

  it("边界行与未知类型各归各的 kind", () => {
    expect(toTimelineRow(event({ event_type: "session_begin", stage: "capture" })).kind).toBe(
      "boundary",
    );
    const unknown = toTimelineRow(event({ event_type: "whatever" }));
    expect(unknown.kind).toBe("unknown");
    expect(unknown.typeLabel).toBe("whatever");
  });

  it("脏 metadata 不让整条时间线挂掉：如实标出来", () => {
    const row = toTimelineRow(
      event({ event_type: "asr_final", metadata: { _unparsable: "{oops" } }),
    );
    expect(row.unparsable).toBe("{oops");
    expect(row.asr?.segmentId).toBe("—");
  });

  it("缺字段不炸：缺失的数字按 0、缺失的字符串按「—」", () => {
    const facts = qaExchangeFacts({});
    expect(facts.llmCalls).toBe(0);
    expect(facts.action).toBe("—");
  });

  it("摘要把转写与问答分开数（零值也要出现）", () => {
    const rows = [
      toTimelineRow(event({ id: 1 })),
      toTimelineRow(event({ id: 2, event_type: "qa_exchange", stage: "qa" })),
      toTimelineRow(event({ id: 3, event_type: "rehearsal", stage: "rehearsal" })),
    ];
    const summary = timelineSummary(rows);
    expect(summary).toContain("转写 1");
    expect(summary).toContain("问答 1");
    expect(summary).toContain("陪练 1");
    expect(timelineSummary([])).toContain("共 0 行");
  });
});

// ------------------------------------------------------------------ R21（源码级）

describe("R21（UI 面）：时间线不认识检索", () => {
  // 靠"记得别这么做"守不住：这里直接查源码。
  // 只查 "R21" 出现**之前**的部分 —— 文档里"提到"这些名字不算引用它们。
  const cases: [string, string][] = [
    ["lib/sessions.ts", sessionsSrc],
    ["components/SessionDetail.tsx", sessionDetailSrc],
  ];

  it.each(cases)("%s 不引用任何检索/问答入口", (_name, src) => {
    const body = src.split("R21")[0];
    for (const banned of ["askQuestion", "useQA(", "/qa/ask"]) {
      expect(body).not.toContain(banned);
    }
  });

  it("两个文件里也确实没有 import 检索/问答模块", () => {
    for (const src of [sessionsSrc, sessionDetailSrc]) {
      expect(src).not.toMatch(/from\s+["'][^"']*\bqa["']/);
    }
  });
});
