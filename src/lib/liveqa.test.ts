/**
 * 实时提词会话单测：判定 → 去重 → 锁定 → 结果上屏 的完整缝合。
 *
 * `trigger.test.ts` 验的是三个原子能力；本文件验的是**它们缝在一起之后的行为**——
 * 也就是用户真正感受到的东西：什么时候卡片会跳、跳出来的是哪一条。
 */

import { describe, expect, it } from "vitest";

import {
  MAX_CARDS,
  LiveQASession,
  captureNotice,
  capturePaths,
  detectPlatform,
  liveSourceLabel,
  pushCard,
  type LiveCard,
  type TranscriptEvent,
} from "./liveqa";
import type { QAKind, QAResult, QASource } from "./qa";

// ------------------------------------------------------------------ 夹具

function result(kind: QAKind, text: string, sources: QASource[] = []): QAResult {
  return { kind, text, sources, llm_calls: 0 };
}

const FIELD: QASource = {
  type: "field",
  key: "f1",
  score: 0.9,
  payload: { field_name: "发货周期", field_value: "3–5 个工作日" },
};
const FTS: QASource = { type: "qa", key: "q1", score: 0.7, routes: ["simple"], payload: {} };
const VEC: QASource = { type: "qa", key: "q2", score: 0.6, routes: ["vec"], payload: {} };

const Q = "你的发货周期是多久";
const Q_PUNCT = "你的发货周期是多久？";

function final(text: string): TranscriptEvent {
  return { type: "asr_final", segment_id: "seg_1", text, path: "loopback", ts_ms: 0, duration_ms: 1500 };
}

function card(over: Partial<LiveCard> = {}): LiveCard {
  return {
    id: "c1",
    question: "q",
    answer: "a",
    source: "字段直查",
    kind: "direct",
    atMs: 0,
    reused: false,
    ...over,
  };
}

// ------------------------------------------------------------------ 展示策略

describe("展示策略（PRD「展示策略」小节）", () => {
  it("保留最近 3 条，最新在最上", () => {
    expect(MAX_CARDS).toBe(3);
    let cards: LiveCard[] = [];
    for (let i = 1; i <= 5; i += 1) cards = pushCard(cards, card({ id: `c${i}` }));
    expect(cards.map((c) => c.id)).toEqual(["c5", "c4", "c3"]);
  });

  it("来源标签映射到 PRD 的四个标签", () => {
    expect(liveSourceLabel(result("direct", "x", [FIELD]))).toBe("字段直查");
    expect(liveSourceLabel(result("direct", "x", [FTS]))).toBe("全文检索");
    expect(liveSourceLabel(result("direct", "x", [VEC]))).toBe("语义检索");
    expect(liveSourceLabel(result("llm", "x", [FTS]))).toBe("LLM 生成");
  });

  it("direct 无命中来源时回落到「字段直查」", () => {
    expect(liveSourceLabel(result("direct", "x", []))).toBe("字段直查");
  });

  it("fail_closed 与 error 没有来源标签（前者由 UI 固定显示未命中）", () => {
    expect(liveSourceLabel(result("fail_closed", "知识库未命中"))).toBe("");
    expect(liveSourceLabel(result("error", "boom"))).toBe("");
  });
});

// ------------------------------------------------------------------ 平台矩阵

describe("平台采集矩阵（macOS 仅麦克风）", () => {
  it("识别平台", () => {
    expect(detectPlatform("Mozilla/5.0 (Windows NT 10.0; Win64; x64)")).toBe("windows");
    expect(detectPlatform("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)")).toBe("macos");
    expect(detectPlatform("Mozilla/5.0 (X11; Linux x86_64)")).toBe("linux");
    expect(detectPlatform("")).toBe("unknown");
  });

  it("macOS 只有麦克风，Windows/Linux 有回环", () => {
    expect(capturePaths("macos")).toEqual(["mic"]);
    expect(capturePaths("windows")).toEqual(["loopback", "mic"]);
    expect(capturePaths("linux")).toEqual(["loopback", "mic"]);
  });

  it("仅 macOS 提示「仅麦克风」", () => {
    expect(captureNotice("macos")).toContain("仅麦克风");
    for (const p of ["windows", "linux", "unknown"] as const) {
      expect(captureNotice(p)).toBeNull();
    }
  });
});

// ------------------------------------------------------------------ 判定接入

describe("asr_final 接入：判定为问题的才走链路", () => {
  it("非问题 → skip，且不产生卡片", () => {
    const s = new LiveQASession();
    for (const text of ["嗯", "我这边之前用的是那个方案"]) {
      const action = s.ingestFinal(final(text), 0);
      expect(action.kind).toBe("skip");
    }
    expect(s.cards).toHaveLength(0);
  });

  it("问题 → retrieve（未锁定）", () => {
    const s = new LiveQASession();
    const action = s.ingestFinal(final(Q_PUNCT), 0);
    expect(action).toMatchObject({ kind: "retrieve", normalized: Q, interrupt: false });
  });

  it("非问题也会更新「最近一段转写」（手动触发要用）", () => {
    const s = new LiveQASession();
    s.ingestFinal(final("我这边之前用的是那个方案"), 0);
    expect(s.lastTranscript).toBe("我这边之前用的是那个方案");
  });

  it("空文本不产生任何动作", () => {
    const s = new LiveQASession();
    expect(s.ingestFinal({ type: "asr_final" }, 0).kind).toBe("skip");
    expect(s.cards).toHaveLength(0);
  });
});

// ------------------------------------------------------------------ 去重复用

describe("去重复用：不重新检索", () => {
  it("窗口内重复问题 → 复用上次答案，标记 reused", () => {
    const s = new LiveQASession();
    const first = s.ingestFinal(final(Q), 0);
    expect(first.kind).toBe("retrieve");
    if (first.kind !== "retrieve") return;
    s.settle(first.question, first.normalized, result("direct", "3–5 个工作日", [FIELD]), 0);
    expect(s.cards).toHaveLength(1);

    // 锁定期已过（3s），但去重窗口（60s）仍在 → 复用而非重检索
    const second = s.ingestFinal(final(Q_PUNCT), 3_500);
    expect(second.kind).toBe("render");
    if (second.kind !== "render") return;
    expect(second.card.reused).toBe(true);
    expect(second.card.answer).toBe("3–5 个工作日");
    expect(second.card.source).toBe("字段直查");
    expect(s.cards).toHaveLength(2);
  });

  it("不同问题不复用", () => {
    const s = new LiveQASession();
    s.ingestFinal(final(Q), 0);
    s.settle(Q, "你的发货周期是多久", result("direct", "3–5 个工作日", [FIELD]), 0);
    const action = s.ingestFinal(final("退货政策是什么"), 3_500);
    expect(action.kind).toBe("retrieve");
  });

  it("fail_closed 也会被复用（未命中是合法答案，不是故障）", () => {
    const s = new LiveQASession();
    s.ingestFinal(final(Q), 0);
    s.settle(Q, "你的发货周期是多久", result("fail_closed", "知识库未命中"), 0);
    const action = s.ingestFinal(final(Q_PUNCT), 3_500);
    expect(action.kind).toBe("render");
    if (action.kind !== "render") return;
    expect(action.card.kind).toBe("fail_closed");
    expect(action.card.source).toBe("");
  });

  it("检索失败不写入去重窗口：重问必须重新检索", () => {
    const s = new LiveQASession();
    s.ingestFinal(final(Q), 0);
    s.settle(Q, "你的发货周期是多久", result("error", "网络错误"), 0);
    expect(s.cards[0]?.kind).toBe("error");

    const again = s.ingestFinal(final(Q), 3_500);
    expect(again.kind).toBe("retrieve");
  });
});

// ------------------------------------------------------------------ 锁定期

describe("锁定期：入队不刷新，到期取最后一条", () => {
  function locked(): LiveQASession {
    const s = new LiveQASession();
    const a = s.ingestFinal(final(Q), 0);
    if (a.kind === "retrieve") s.settle(a.question, a.normalized, result("direct", "A", [FIELD]), 0);
    return s;
  }

  it("锁定期内的新问题入队而非刷新", () => {
    const s = locked();
    const action = s.ingestFinal(final("退货政策是什么"), 1_000);
    expect(action.kind).toBe("queued");
    expect(s.pending).toBe(1);
    expect(s.cards).toHaveLength(1); // UI 未刷新
  });

  it("到期前 drain 返回 null，到期后取最后一条", () => {
    const s = locked();
    s.ingestFinal(final("退货政策是什么"), 1_000);
    s.ingestFinal(final("客服电话是多少呢"), 2_000);

    expect(s.drain(2_999)).toBeNull(); // 仍锁定
    const action = s.drain(3_000);
    expect(action).toMatchObject({ kind: "retrieve", question: "客服电话是多少呢" });
    expect(s.pending).toBe(0);
  });

  it("排队的是复用结果时，drain 直接上屏而不检索", () => {
    const s = new LiveQASession();
    const a = s.ingestFinal(final(Q), 0);
    if (a.kind === "retrieve") s.settle(a.question, a.normalized, result("direct", "3–5 个工作日", [FIELD]), 0);

    // 1s 后重复问题：命中去重但仍在锁定期 → 入队
    expect(s.ingestFinal(final(Q_PUNCT), 1_000).kind).toBe("queued");
    const action = s.drain(3_000);
    expect(action?.kind).toBe("render");
    if (action?.kind !== "render") return;
    expect(action.card.reused).toBe(true);
    expect(s.cards).toHaveLength(2);
  });

  it("drain 空队列返回 null", () => {
    expect(new LiveQASession().drain(999_999)).toBeNull();
  });
});

// ------------------------------------------------------------------ 手动触发

describe("手动触发（F1.6 全局快捷键）", () => {
  it("无转写时返回 null", () => {
    expect(new LiveQASession().manual(0)).toBeNull();
  });

  it("只有空文本时返回 null", () => {
    const s = new LiveQASession();
    s.ingestFinal(final(""), 0);
    expect(s.manual(0)).toBeNull();
  });

  it("打断锁定期：即使处于锁定也立即检索", () => {
    const s = new LiveQASession();
    const a = s.ingestFinal(final(Q), 0);
    if (a.kind === "retrieve") s.settle(a.question, a.normalized, result("direct", "A", [FIELD]), 0);
    s.ingestFinal(final("退货政策是什么"), 1_000);
    expect(s.pending).toBe(1);

    const action = s.manual(1_500);
    expect(action).toMatchObject({ kind: "retrieve", interrupt: true });
    expect(s.pending).toBe(0); // 队列被清空
  });

  it("取最近一段转写，且**绕开问题判定**（陈述句也能手动提问）", () => {
    const s = new LiveQASession();
    expect(s.ingestFinal(final("我这边之前用的是那个方案"), 0).kind).toBe("skip");
    const action = s.manual(0);
    expect(action).toMatchObject({ kind: "retrieve", question: "我这边之前用的是那个方案" });
  });

  it("默认绕过去重：按键强制重新检索（否则按键看起来失灵）", () => {
    const s = new LiveQASession();
    const a = s.ingestFinal(final(Q), 0);
    if (a.kind === "retrieve") s.settle(a.question, a.normalized, result("direct", "3–5 个工作日", [FIELD]), 0);

    const action = s.manual(1_000);
    expect(action).toMatchObject({ kind: "retrieve", interrupt: true });
  });

  it("可选：手动也走去重（manualBypassesDedup=false）", () => {
    const s = new LiveQASession({ manualBypassesDedup: false });
    const a = s.ingestFinal(final(Q), 0);
    if (a.kind === "retrieve") s.settle(a.question, a.normalized, result("direct", "3–5 个工作日", [FIELD]), 0);

    const action = s.manual(1_000);
    expect(action?.kind).toBe("render");
    if (action?.kind !== "render") return;
    expect(action.card.reused).toBe(true);
  });

  it("手动检索完成后同样进入新的锁定期", () => {
    const s = new LiveQASession();
    s.ingestFinal(final(Q), 0);
    const action = s.manual(0);
    if (action?.kind !== "retrieve") throw new Error("应返回 retrieve");
    s.settle(action.question, action.normalized, result("llm", "生成中", [FTS]), 0);
    expect(s.cards[0]?.source).toBe("LLM 生成");
    expect(s.ingestFinal(final("退货政策是什么"), 1_000).kind).toBe("queued");
  });
});

// ------------------------------------------------------------------ 重置

describe("reset", () => {
  it("清空卡片、去重窗口与锁定", () => {
    const s = new LiveQASession();
    const a = s.ingestFinal(final(Q), 0);
    if (a.kind === "retrieve") s.settle(a.question, a.normalized, result("direct", "A", [FIELD]), 0);
    s.ingestFinal(final("退货政策是什么"), 1_000);

    s.reset();
    expect(s.cards).toHaveLength(0);
    expect(s.pending).toBe(0);
    expect(s.lastTranscript).toBeNull();
    expect(s.ingestFinal(final(Q), 1_100).kind).toBe("retrieve"); // 锁与去重都已清
  });
});

// ------------------------------------------------------------------ DoD 场景

describe("DoD 场景：真机说「发货周期是多久」", () => {
  it("固定答案路径：从 asr_final 到卡片上屏，只经一次判定 + 一次检索", () => {
    const s = new LiveQASession();
    const action = s.ingestFinal(final("发货周期是多久"), 0);
    expect(action.kind).toBe("retrieve");
    if (action.kind !== "retrieve") return;

    // 固定答案路径（1a 实测 ask→done 2.4ms）
    const card_ = s.settle(action.question, action.normalized, result("direct", "3–5 个工作日", [FIELD]), 30);
    expect(card_.question).toBe("发货周期是多久");
    expect(card_.answer).toBe("3–5 个工作日");
    expect(card_.source).toBe("字段直查");
    expect(card_.kind).toBe("direct");
    expect(s.cards).toHaveLength(1);
  });

  it("连说两句相似问题：第二次复用，不重新检索", () => {
    const s = new LiveQASession();
    let retrievals = 0;

    const feed = (text: string, nowMs: number): void => {
      const action = s.ingestFinal(final(text), nowMs);
      if (action.kind === "retrieve") {
        retrievals += 1;
        s.settle(action.question, action.normalized, result("direct", "3–5 个工作日", [FIELD]), nowMs);
      } else if (action.kind === "queued") {
        const drained = s.drain(nowMs + 3_000);
        if (drained?.kind === "retrieve") {
          retrievals += 1;
          s.settle(drained.question, drained.normalized, result("direct", "3–5 个工作日", [FIELD]), nowMs + 3_000);
        }
      }
    };

    feed("你的发货周期是多久？", 0);
    feed("你的发货周期是多久", 10_000);

    expect(retrievals).toBe(1); // 第二句复用
    expect(s.cards).toHaveLength(2);
    expect(s.cards[0]?.reused).toBe(true);
  });
});
