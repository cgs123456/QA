/**
 * 触发策略单测（PRD §3.3「实时触发策略」）。
 *
 * 三组：**问题判定 / 去重窗口 / 刷新节流**。PRD 规则表逐条覆盖，
 * 每条规则至少一例，且刻意包含「规则顺序导致的边界行为」——
 * 那些不是 bug，是 PRD 表顺序的直接结果，必须被测试钉住，
 * 否则将来有人调换顺序会静默改变产品行为。
 *
 * 时间全部由测试注入（`nowMs` 是普通数字），无 sleep、无 fake timer。
 */

import { describe, expect, it } from "vitest";

import {
  DEDUP_N,
  DEDUP_THRESHOLD,
  DEDUP_WINDOW_MS,
  LOCK_MS,
  MIN_QUESTION_CHARS,
  NGRAM,
  DedupWindow,
  RefreshThrottle,
  isQuestion,
  jaccard,
  normalize,
  normalizeLight,
  questionVerdict,
  similarity,
  trigrams,
} from "./trigger";

// ------------------------------------------------------------------ 1. PRD 参数初值

describe("PRD 参数初值（防止被私自改动）", () => {
  it("与 PRD §3.3 文字一一对应", () => {
    // PRD：「最近 N=10 条查询的指纹」
    expect(DEDUP_N).toBe(10);
    // PRD：「去重窗口 60 秒」
    expect(DEDUP_WINDOW_MS).toBe(60_000);
    // PRD：「Jaccard 相似度 ≥ 0.85」
    expect(DEDUP_THRESHOLD).toBe(0.85);
    // PRD：「3 秒锁定期」
    expect(LOCK_MS).toBe(3_000);
    // PRD：「字符 3-gram 集合」
    expect(NGRAM).toBe(3);
    // PRD：「归一化后长度 < 4 字符」
    expect(MIN_QUESTION_CHARS).toBe(4);
  });

  it("阈值未标定前不得改动（PRD 明示为初值）", () => {
    // 这条断言的意义：PRD 说这三个阈值要等 100 题评测集标定后写回。
    // 在标定发生前，任何「手感上太严/太松」的调整都属于未经证据的改动。
    expect([DEDUP_THRESHOLD, LOCK_MS, DEDUP_WINDOW_MS]).toEqual([0.85, 3_000, 60_000]);
  });
});

// ------------------------------------------------------------------ 2. 归一化

describe("归一化", () => {
  it("去标点、去全部空白、转小写", () => {
    expect(normalize("Hello, World!")).toBe("helloworld");
    expect(normalize("发货周期 是多久")).toBe("发货周期是多久");
    expect(normalize("  发货周期是多久  ")).toBe("发货周期是多久");
  });

  it("NFKC：全角转半角", () => {
    expect(normalize("ＡＢＣ１２３")).toBe("abc123");
  });

  it("轻量归一化：只去首尾空白，NFKC 会把全角标点转半角", () => {
    // NFKC 把全角 `？`(U+FF1F) 映射为半角 `?`(U+003F)，故保留的是半角形态。
    // 句尾判定用 /[?？]$/ 同时兜住两种形态，NFKC 只是让它更早统一。
    expect(normalizeLight("  发货周期是多久？  ")).toBe("发货周期是多久?");
    expect(normalizeLight("a b")).toBe("a b"); // 内部空白保留
    expect(normalize("发货周期是多久？")).toBe("发货周期是多久");
  });

  it("纯标点归一化后为空", () => {
    expect(normalize("？？！")).toBe("");
    expect(normalize("")).toBe("");
  });
});

// ------------------------------------------------------------------ 3. 问题判定（PRD 规则表逐条）

describe("问题判定 —— PRD 规则表逐条覆盖", () => {
  describe("规则 1：归一化后长度 < 4 字符 → 跳过", () => {
    it("3 字及以下一律跳过", () => {
      for (const text of ["嗯", "好的", "怎么样", "为什么", "你好吗", "然后呢"]) {
        const v = questionVerdict(text);
        expect(v.isQuestion, `${text} 应被跳过`).toBe(false);
        expect(v.reason).toBe("too_short");
      }
    });

    it("空串与纯标点跳过", () => {
      expect(questionVerdict("").reason).toBe("too_short");
      expect(questionVerdict("？？？").reason).toBe("too_short");
    });

    it("恰好 4 字不跳过（边界含等号）", () => {
      const v = questionVerdict("这个东西");
      expect(v.reason).not.toBe("too_short");
    });

    it("「然后呢」因长度优先被跳过 —— PRD 表顺序的直接结果", () => {
      // 它以 `呢` 结尾，本可命中规则 2；但规则 1 在前，先被长度挡下。
      // 这是 PRD 规则顺序的必然结果，不是实现偏差（已在 trigger.ts 注释记录）。
      const v = questionVerdict("然后呢");
      expect(v.reason).toBe("too_short");
      expect(v.isQuestion).toBe(false);
    });
  });

  describe("规则 2：末尾含 吗/呢/吧/?/么 → 是问题", () => {
    it("中文语气词结尾", () => {
      for (const text of ["你吃饭了吗", "这个怎么弄呢", "我们先这样吧", "你听到了么"]) {
        const v = questionVerdict(text);
        expect(v.isQuestion, `${text} 应判为问题`).toBe(true);
        expect(v.reason).toBe("trailing_mark");
      }
    });

    it("半角与全角问号结尾", () => {
      expect(questionVerdict("你的发货周期是多久?").reason).toBe("trailing_mark");
      expect(questionVerdict("这个方案可行？").reason).toBe("trailing_mark");
    });

    it("`?` 必须在轻度归一化文本上判定（否则被去标点规则吃掉）", () => {
      // 「我能用这个方案?」归一化后是「我能用这个方案」——以「我」开头、
      // 全文不含疑问词，若只看归一化文本会被规则 4 判为陈述句跳过。
      // 规则 2 把 `?` 当疑问信号，故此处必须是问题。
      const v = questionVerdict("我能用这个方案?");
      expect(v.reason).toBe("trailing_mark");
      expect(v.isQuestion).toBe(true);
    });

    it("已知代价：陈述句以「吧」结尾会被判为问题", () => {
      // PRD 把「吧」列为句尾疑问标记，而规则 2 在规则 4（陈述句）之前。
      // 于是「那我们现在开始吧」被判为问题 —— 这是过度触发（宁可多检索）。
      // 记录为已知行为，留待 100 题评测集标定时决定是否收紧。
      const v = questionVerdict("那我们现在开始吧");
      expect(v.reason).toBe("trailing_mark");
      expect(v.isQuestion).toBe(true);
    });
  });

  describe("规则 3：开头匹配疑问词 → 是问题", () => {
    it("PRD 疑问词表逐词命中", () => {
      const cases: [string, string][] = [
        ["怎么配置模型", "怎么"],
        ["如何导入知识库", "如何"],
        ["为什么要用本地模型", "为什么"],
        ["什么是 Fail-Closed", "什么"],
        ["哪个方案更好一点", "哪"],
        ["多少天能到货", "多少"],
        ["是否支持导出数据", "是否"],
        ["能不能换一个模型", "能不能"],
        ["可不可以离线运行", "可不可以"],
        ["有没有更快的方案", "有没有"],
        ["几点开始面试", "几点"],
        ["多久能出结果", "多久"],
      ];
      for (const [text, word] of cases) {
        const v = questionVerdict(text);
        expect(v.isQuestion, `${text}（${word}）应判为问题`).toBe(true);
        expect(v.reason, `${text} 应由疑问词开头命中`).toBe("question_prefix");
      }
    });
  });

  describe("规则 4：以陈述句开头且无疑问词 → 跳过", () => {
    it("PRD 陈述句开头表逐词命中", () => {
      const cases: [string, string][] = [
        ["我这边之前用的是那个方案", "我"],
        ["我们下周一再聊这件事", "我们"],
        ["好的我明白了你的意思", "好的"],
        ["嗯这个我记下了", "嗯"],
        ["对就按这个来办", "对"],
        ["是的我确认过了", "是"],
        ["那我们先按旧流程走", "那"],
        ["然后我们就开始了", "然后"],
      ];
      for (const [text, word] of cases) {
        const v = questionVerdict(text);
        expect(v.isQuestion, `${text}（${word}）应被跳过`).toBe(false);
        expect(v.reason, `${text} 应判为陈述句`).toBe("declarative");
      }
    });

    it("「无疑问词」指全文不含疑问词 —— 否则「我该怎么办」会被误杀", () => {
      // 这是本实现最容易被写错的一处：若把「无疑问词」理解成
      // 「开头不是疑问词」，则「我该怎么办」以「我」开头 → 被判陈述句跳过。
      // 而它恰恰是最该检索的一类问题。
      const v = questionVerdict("我该怎么办");
      expect(v.isQuestion).toBe(true);
      expect(v.reason).toBe("fallthrough");
    });

    it("陈述句开头 + 句尾语气词 → 规则 2 先命中，仍判为问题", () => {
      const v = questionVerdict("我们这样做对吧");
      expect(v.reason).toBe("trailing_mark");
      expect(v.isQuestion).toBe(true);
    });
  });

  describe("规则 5：其他 → 是问题（宁可多检索，不可漏）", () => {
    it("无法归类的完整句子一律检索", () => {
      for (const text of ["发货周期是多久", "这个方案的风险点", "团队规模大概多少"]) {
        const v = questionVerdict(text);
        expect(v.isQuestion, `${text} 应判为问题`).toBe(true);
        expect(v.reason).toBe("fallthrough");
      }
    });

    it("兜底不是「跳过」——失败方向必须是多检索", () => {
      // 反向断言：若哪天有人把兜底改成 false，本测试必须红。
      const v = questionVerdict("一段既没有疑问词也不以陈述句开头的普通文本");
      expect(v.reason).toBe("fallthrough");
      expect(v.isQuestion).toBe(true);
    });
  });

  describe("返回值与便捷函数", () => {
    it("verdict 带回归一化文本，供去重指纹直接使用", () => {
      const v = questionVerdict("  你的发货周期是多久？  ");
      expect(v.normalized).toBe("你的发货周期是多久");
    });

    it("isQuestion 与 questionVerdict().isQuestion 一致", () => {
      for (const text of ["你吃饭了吗", "我这边确认过了", "发货周期是多久", "嗯"]) {
        expect(isQuestion(text)).toBe(questionVerdict(text).isQuestion);
      }
    });
  });

  describe("可独立测试性（坑：判定函数不得依赖 Tauri 事件）", () => {
    it("纯字符串进、纯对象出，无运行时依赖", () => {
      // 本测试文件运行在 vitest `environment: "node"` 下，没有 window、
      // 没有 Tauri 全局对象。整个模块能在这里跑通，即证明它不依赖运行时。
      expect(typeof window).toBe("undefined");
      const v = questionVerdict("发货周期是多久");
      expect(Object.keys(v).sort()).toEqual(["isQuestion", "normalized", "reason"]);
    });
  });
});

// ------------------------------------------------------------------ 4. 指纹与相似度

describe("指纹与相似度", () => {
  it("3-gram 集合", () => {
    expect([...trigrams("abcd")].sort()).toEqual(["abc", "bcd"]);
    expect([...trigrams("abc")]).toEqual(["abc"]);
  });

  it("不足 3 字符退化为整串单 gram，绝不返回空集", () => {
    // 若短串返回空集，两个不相干的短串 Jaccard 都会是 1（全等），
    // 从而被去重窗口误判为同一问题。
    expect([...trigrams("ab")]).toEqual(["ab"]);
    expect(jaccard(trigrams("ab"), trigrams("cd"))).toBe(0);
  });

  it("空串才是空集", () => {
    expect(trigrams("").size).toBe(0);
  });

  it("Jaccard 边界：全等 1、无交集 0、双空集 1", () => {
    expect(jaccard(trigrams("发货周期是多久"), trigrams("发货周期是多久"))).toBe(1);
    expect(jaccard(trigrams("发货周期"), trigrams("退货政策"))).toBe(0);
    expect(jaccard(new Set(), new Set())).toBe(1);
    expect(jaccard(new Set(), trigrams("abc"))).toBe(0);
  });

  it("similarity 内部各自归一化：标点与空白差异不影响判定", () => {
    expect(similarity("你的发货周期是多久？", "你的发货周期是多久")).toBe(1);
    expect(similarity("发货周期 是多久", "发货周期是多久")).toBe(1);
  });

  it("不同问题相似度低", () => {
    expect(similarity("发货周期是多久", "退货政策是什么")).toBeLessThan(0.2);
  });

  it("已知严格性：3-gram 对中文的容错很窄（0.85 几乎要求逐字重复）", () => {
    // 「发货周期是多久」vs「发货周期是多久呢」→ 5∩6 = 5/6 ≈ 0.833 < 0.85。
    // 即 ASR 多吐一个语气词就会漏过去重，多跑一次检索。
    // 代价是不对称的：漏去重只是多检索一次（~5–100ms），
    // 误去重则是把上一个问题的答案冒充成本问题的答案 —— 后者严重得多。
    // 故此处保留严格侧，交由 100 题评测集标定时决定。
    const s = similarity("发货周期是多久", "发货周期是多久呢");
    expect(s).toBeGreaterThan(0.8);
    expect(s).toBeLessThan(DEDUP_THRESHOLD);
  });
});

// ------------------------------------------------------------------ 5. 去重窗口

describe("去重窗口 —— PRD「去重窗口」小节", () => {
  const Q = "你的发货周期是多久";

  it("首次查询无命中，记录后可命中并复用载荷", () => {
    const w = new DedupWindow<{ answer: string }>();
    expect(w.find(normalize(Q), 0)).toBeNull();

    w.record(normalize(Q), 0, { answer: "3–5 个工作日" });
    const hit = w.find(normalize(Q), 1_000);
    expect(hit).not.toBeNull();
    expect(hit?.payload.answer).toBe("3–5 个工作日");
  });

  it("DoD：连说两句相似问题 → 第二句复用上次结果，不重新检索", () => {
    // 真机场景：同一句话被 ASR 识别两次，差异只在标点/空白。
    const w = new DedupWindow<{ answer: string }>();

    const first = questionVerdict("你的发货周期是多久？");
    expect(w.find(first.normalized, 0)).toBeNull();
    w.record(first.normalized, 0, { answer: "3–5 个工作日" });

    const second = questionVerdict("你的发货周期 是多久");
    const hit = w.find(second.normalized, 2_000);
    expect(hit?.payload.answer).toBe("3–5 个工作日");
  });

  it("不同问题不得误命中（宁可漏去重，不可错去重）", () => {
    const w = new DedupWindow<{ answer: string }>();
    w.record(normalize(Q), 0, { answer: "3–5 个工作日" });
    expect(w.find(normalize("退货政策是什么"), 1_000)).toBeNull();
  });

  it("窗口 60 秒：边界内含、超边界不含", () => {
    const w = new DedupWindow<{ answer: string }>();
    w.record(normalize(Q), 0, { answer: "a" });

    expect(w.find(normalize(Q), DEDUP_WINDOW_MS)).not.toBeNull(); // 恰好 60s → 仍在窗内
    expect(w.find(normalize(Q), DEDUP_WINDOW_MS + 1)).toBeNull(); // 60s + 1ms → 过期
  });

  it("超窗后同一问题可重新检索（重复查询不刷新时间戳）", () => {
    const w = new DedupWindow<{ answer: string }>();
    w.record(normalize(Q), 0, { answer: "旧答案" });

    // 第 59 秒又问一次：命中，且**不**延长原条目寿命
    expect(w.find(normalize(Q), 59_000)?.payload.answer).toBe("旧答案");
    // 第 61 秒：原条目已过期（若重复查询会刷新时间戳，这里就会仍然命中）
    expect(w.find(normalize(Q), 61_000)).toBeNull();
  });

  it("N=10：超过 10 条后最旧的被挤出", () => {
    const w = new DedupWindow<string>();
    const keys = Array.from({ length: DEDUP_N + 1 }, (_, i) => String.fromCharCode(97 + i).repeat(10));

    keys.forEach((k, i) => w.record(k, i, `payload-${i}`));
    expect(w.size).toBe(DEDUP_N);

    expect(w.find(keys[0], DEDUP_N)).toBeNull(); // 最旧（第 1 条）已挤出
    expect(w.find(keys[DEDUP_N], DEDUP_N)?.payload).toBe(`payload-${DEDUP_N}`); // 最新仍在
  });

  it("find 不写入（查与记必须分开：载荷要等检索完成才知道）", () => {
    const w = new DedupWindow<{ answer: string }>();
    w.find(normalize(Q), 0);
    expect(w.size).toBe(0);
  });

  it("reset 清空", () => {
    const w = new DedupWindow<string>();
    w.record(normalize(Q), 0, "a");
    w.reset();
    expect(w.size).toBe(0);
    expect(w.find(normalize(Q), 0)).toBeNull();
  });

  it("构造参数可覆盖（便于标定与测试）", () => {
    const w = new DedupWindow<string>({ n: 1, windowMs: 100, threshold: 0.5 });
    w.record("aaaaaaaaaa", 0, "a");
    w.record("bbbbbbbbbb", 1, "b");
    expect(w.size).toBe(1); // n=1 只留最新
    expect(w.find("aaaaaaaaaa", 1)).toBeNull();
    expect(w.find("bbbbbbbbbb", 101)).not.toBeNull(); // 边界含等号：atMs=1 >= cutoff=1
    expect(w.find("bbbbbbbbbb", 102)).toBeNull(); // windowMs=100 已过期
  });
});

// ------------------------------------------------------------------ 6. 刷新节流（锁定期）

describe("刷新节流 —— PRD「刷新节流（锁定期）」小节", () => {
  it("未锁定 → 立即执行", () => {
    const t = new RefreshThrottle<string>();
    expect(t.isLocked(0)).toBe(false);
    expect(t.offer("q1", 0)).toBe("execute");
  });

  it("刷新后 3 秒内锁定：新查询入队但不刷新 UI", () => {
    const t = new RefreshThrottle<string>();
    t.markRefreshed(0);

    expect(t.isLocked(1)).toBe(true);
    expect(t.isLocked(2_999)).toBe(true);
    expect(t.offer("q2", 1_000)).toBe("queued");
    expect(t.poll(2_000)).toBeNull(); // 锁定期内不刷新
  });

  it("锁定期边界：[t, t+3s) —— 恰好 3s 时解锁", () => {
    const t = new RefreshThrottle<string>();
    t.markRefreshed(0);
    expect(t.isLocked(LOCK_MS - 1)).toBe(true);
    expect(t.isLocked(LOCK_MS)).toBe(false);
  });

  it("DoD：到期取「最后一条」而非逐条补刷", () => {
    const t = new RefreshThrottle<string>();
    t.markRefreshed(0);
    expect(t.offer("旧问题", 500)).toBe("queued");
    expect(t.offer("中间问题", 1_500)).toBe("queued");
    expect(t.offer("最新问题", 2_500)).toBe("queued");

    // 逐条补刷会先返回「旧问题」——那正是 PRD 要避免的（旧问题已过时）
    expect(t.poll(LOCK_MS)).toBe("最新问题");
    expect(t.poll(LOCK_MS + 1)).toBeNull(); // 取走后槽位清空
    expect(t.pending).toBe(0);
  });

  it("单槽队列内存有界：锁定期内说再多句也只留最后一条", () => {
    const t = new RefreshThrottle<number>();
    t.markRefreshed(0);
    for (let i = 0; i < 50; i += 1) t.offer(i, 1_000 + i);
    expect(t.pending).toBe(1);
    expect(t.poll(LOCK_MS)).toBe(49);
  });

  it("未锁定且无待执行项 → poll 返回 null", () => {
    const t = new RefreshThrottle<string>();
    expect(t.poll(0)).toBeNull();
    expect(t.poll(999_999)).toBeNull();
  });

  it("DoD：手动触发立即打断锁定期", () => {
    const t = new RefreshThrottle<string>();
    t.markRefreshed(0);
    t.offer("自动队列里的问题", 1_000);

    t.interrupt(1_200);
    expect(t.isLocked(1_200)).toBe(false); // 锁定被打断
    expect(t.pending).toBe(0); // 队列被清空
    expect(t.poll(1_200)).toBeNull(); // 手动项由调用方直接执行，不从这里取

    // 打断后自动查询恢复「立即执行」
    expect(t.offer("打断后的新问题", 1_201)).toBe("execute");
  });

  it("手动触发后重新计时：再次刷新仍会进入新的锁定期", () => {
    const t = new RefreshThrottle<string>();
    t.markRefreshed(0);
    t.interrupt(1_000);
    t.markRefreshed(1_000);
    expect(t.isLocked(1_001)).toBe(true);
    expect(t.isLocked(4_000)).toBe(false);
  });

  it("reset 清空锁定与队列", () => {
    const t = new RefreshThrottle<string>();
    t.markRefreshed(0);
    t.offer("q", 1_000);
    t.reset();
    expect(t.isLocked(1_000)).toBe(false);
    expect(t.pending).toBe(0);
  });
});

// ------------------------------------------------------------------ 7. 端到端：判定 → 去重 → 锁定

describe("组合流程：asr_final 文本 → 判定 → 去重 → 锁定", () => {
  type Card = { question: string; answer: string; source: string };

  it("同一问题连说两次：第二次复用结果，且不占用刷新机会", () => {
    const dedup = new DedupWindow<Card>();
    const throttle = new RefreshThrottle<Card>();
    const rendered: Card[] = [];
    let retrievals = 0;

    const onFinal = (text: string, nowMs: number): void => {
      const verdict = questionVerdict(text);
      if (!verdict.isQuestion) return;

      const known = dedup.find(verdict.normalized, nowMs);
      const card: Card = known
        ? known.payload
        : (() => {
            retrievals += 1;
            return { question: verdict.normalized, answer: "3–5 个工作日", source: "字段直查" };
          })();

      if (!known) dedup.record(verdict.normalized, nowMs, card);

      if (throttle.offer(card, nowMs) === "execute") {
        rendered.push(card);
        throttle.markRefreshed(nowMs);
      }
    };

    onFinal("你的发货周期是多久？", 0);
    expect(rendered).toHaveLength(1);
    expect(retrievals).toBe(1);

    // 第二句：相似问题，且在锁定期内
    onFinal("你的发货周期是多久", 1_000);
    expect(retrievals).toBe(1); // 未重新检索
    expect(rendered).toHaveLength(1); // 未刷新 UI（锁定期内入队）

    // 锁定期结束后取最后一条 —— 即复用来的那张卡
    const last = throttle.poll(LOCK_MS);
    expect(last?.answer).toBe("3–5 个工作日");
  });

  it("非问题不进链路：跳过的不产生检索、不产生卡片", () => {
    const dedup = new DedupWindow<Card>();
    let retrievals = 0;

    for (const text of ["嗯", "我这边确认过了", "好的"]) {
      const verdict = questionVerdict(text);
      if (!verdict.isQuestion) continue;
      retrievals += 1;
      dedup.record(verdict.normalized, 0, { question: text, answer: "", source: "" });
    }
    expect(retrievals).toBe(0);
    expect(dedup.size).toBe(0);
  });
});
