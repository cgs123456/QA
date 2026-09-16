/**
 * 面试陪练页（S5 / PRD F9.1-F9.3 最小版）。规格：`docs/rehearsal-spec.md`。
 *
 * 流程：选类目/题数 → 抽题 → 逐题作答（键入或**口述**）→ 展开标准答案 → 自评三档
 * → 统计条。
 *
 * 三条纪律（都由单测钉住）：
 * 1. **全程不触发检索**：本页不 import `askQuestion`。练习是"回想 + 自评"，
 *    不是"查资料"；一旦接了检索，练的就不是记忆而是检索。
 * 2. **作答文本不出发**：只把档位/类目/语料 id 发给服务端（R14）。
 * 3. **没记上就说没记上**：录制开关关着时 `recorded:false`，UI 明示，
 *    不假装统计进了账。
 *
 * 不做（任务书明确排除）：LLM 评分、跨会话长期统计。
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  beginSession,
  drawRehearsalQuestions,
  endSession,
  fetchRehearsalCategories,
  postRehearsalVerdict,
  type RehearsalCategory,
  type RehearsalQuestion,
  type RehearsalVerdict,
} from "../lib/api";
import {
  VERDICT_LABELS,
  VERDICT_ORDER,
  formatSummary,
  summarize,
  type RehearsalAnswer,
} from "../lib/rehearsal";
import { useRehearsalVoice } from "../hooks/useRehearsalVoice";

const N_CHOICES = [5, 10, 20];

type Phase = "idle" | "running" | "done";

export function Rehearsal() {
  const [categories, setCategories] = useState<RehearsalCategory[]>([]);
  const [category, setCategory] = useState("");
  const [n, setN] = useState(10);
  const [phase, setPhase] = useState<Phase>("idle");
  const [questions, setQuestions] = useState<RehearsalQuestion[]>([]);
  const [index, setIndex] = useState(0);
  const [answers, setAnswers] = useState<RehearsalAnswer[]>([]);
  const [draft, setDraft] = useState("");
  const [revealed, setRevealed] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  /** null = 还没自评过；false = 记了但没进账（录制关着）；true = 已入账。 */
  const [lastRecorded, setLastRecorded] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchRehearsalCategories()
      .then((r) => {
        if (!cancelled) setCategories(r.categories);
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoadError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const summary = useMemo(() => summarize(answers), [answers]);
  const current = questions[index] ?? null;
  const total = questions.length;

  const voice = useRehearsalVoice({
    onTranscript: (text) =>
      setDraft((d) => (d.trim() ? `${d.trim()} ${text}` : text)),
  });

  const start = useCallback(async () => {
    setActionError(null);
    setLastRecorded(null);
    try {
      // 开会话（source=rehearsal）。录制开关关着时服务端回 recording:false，
      // 那**不是错误** —— 练习照常，只是不记账，见 §3.3。
      try {
        await beginSession("rehearsal");
      } catch {
        /* 会话边界失败不影响练习：自评时服务端会回 recorded:false */
      }
      const draw = await drawRehearsalQuestions({
        category: category || undefined,
        n,
      });
      if (draw.questions.length === 0) {
        setActionError("这个范围内没有题，换个类目或先导入知识库");
        return;
      }
      setQuestions(draw.questions);
      setIndex(0);
      setAnswers([]);
      setDraft("");
      setRevealed(false);
      setPhase("running");
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  }, [category, n]);

  const judge = useCallback(
    async (verdict: RehearsalVerdict) => {
      if (!current) return;
      setActionError(null);
      // 先落 UI（自评是用户说了算，不该等服务端往返），再尽力记账。
      setAnswers((a) => [...a, { question: current, verdict }]);
      const userAnswer = draft.trim();
      setDraft("");
      setRevealed(false);
      if (voice.on) await voice.toggle();
      try {
        const r = await postRehearsalVerdict({
          qa_id: current.id,
          category: current.category,
          verdict,
          question_text: current.standard_question,
          official_answer: current.official_answer,
          user_answer: userAnswer,
        });
        setLastRecorded(r.recorded);
      } catch {
        // 记账失败不阻断练习（与 sidecar 侧 best-effort 同一条纪律）。
        setLastRecorded(false);
      }
      if (index + 1 >= total) {
        setPhase("done");
        try {
          await endSession();
        } catch {
          /* 同上：边界失败不阻断 */
        }
      } else {
        setIndex((i) => i + 1);
      }
    },
    [current, index, total, voice],
  );

  if (loadError != null) {
    return (
      <section>
        <h2>面试陪练</h2>
        <p data-testid="rehearsal-error">{loadError}</p>
      </section>
    );
  }

  return (
    <section>
      <h2>面试陪练</h2>

      <div className="row">
        <label>
          类目
          <select
            data-testid="rehearsal-category"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            disabled={phase === "running"}
          >
            <option value="">全部</option>
            {categories.map((c) => (
              <option key={c.category} value={c.category}>
                {c.category}（{c.count}）
              </option>
            ))}
          </select>
        </label>
        <label>
          题数
          <select
            data-testid="rehearsal-n"
            value={n}
            onChange={(e) => setN(Number(e.target.value))}
            disabled={phase === "running"}
          >
            {N_CHOICES.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <button type="button" data-testid="rehearsal-start" onClick={start}>
          {phase === "idle" ? "开始练习" : "重新抽题"}
        </button>
      </div>

      {categories.length === 0 && phase === "idle" && (
        <p data-testid="rehearsal-empty">
          当前知识库还没有题目 —— 先去「知识管理」导入一份，再来练习。
        </p>
      )}

      {actionError != null && (
        <p data-testid="rehearsal-action-error">{actionError}</p>
      )}

      {phase === "running" && current && (
        <div>
          <p data-testid="rehearsal-progress">
            第 {index + 1} / {total} 题 · {current.category}
          </p>
          <p data-testid="rehearsal-question">{current.standard_question}</p>

          <label>
            你的作答
            <textarea
              data-testid="rehearsal-draft"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={3}
            />
          </label>
          <div className="row">
            <button
              type="button"
              data-testid="rehearsal-voice"
              onClick={() => void voice.toggle()}
              disabled={voice.busy}
            >
              {voice.on ? "停止口述" : "口述作答"}
            </button>
            <button
              type="button"
              data-testid="rehearsal-reveal"
              onClick={() => setRevealed(true)}
            >
              看标准答案
            </button>
          </div>
          {voice.error != null && (
            <p data-testid="rehearsal-voice-error">
              口述不可用（{voice.error}）—— 可以直接键入作答。
            </p>
          )}

          {revealed && (
            <div data-testid="rehearsal-answer">
              <p>标准问法：{current.standard_question}</p>
              <p>标准答案：{current.official_answer}</p>
            </div>
          )}

          <div className="row">
            {VERDICT_ORDER.map((v) => (
              <button
                key={v}
                type="button"
                data-testid={`rehearsal-judge-${v}`}
                onClick={() => void judge(v)}
              >
                {VERDICT_LABELS[v]}
              </button>
            ))}
          </div>
        </div>
      )}

      {phase !== "idle" && (
        <p data-testid="rehearsal-summary">{formatSummary(summary)}</p>
      )}
      {lastRecorded === false && phase !== "idle" && (
        <p data-testid="rehearsal-not-recorded">
          本次未记账（会话录制已关闭）—— 去「设置 · 会话录制」开启后才会留下记录。
        </p>
      )}

      {phase === "done" && (
        <div className="row">
          <button type="button" data-testid="rehearsal-again" onClick={start}>
            再来一组
          </button>
        </div>
      )}
    </section>
  );
}
