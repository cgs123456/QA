import type { LiveCard } from "../lib/liveqa";

/**
 * 提词器（F1.5）：最近 3 条问答，最新在最上。
 *
 * 纯展示组件 —— 不含任何判定/去重/锁定逻辑，那些都在 `lib/liveqa.ts`。
 * 这样「提词框长什么样」与「什么时候刷新提词框」互不纠缠。
 *
 * Fail-Closed 的显示是 PRD 硬要求：显示「知识库未命中」，
 * **不显示任何 LLM 编造内容**（与 F5.6 一致）。
 */
export function Teleprompter({
  cards,
  placeholder = "等待问题…",
}: {
  cards: readonly LiveCard[];
  placeholder?: string;
}) {
  if (cards.length === 0) {
    return (
      <p className="teleprompter-empty" data-testid="teleprompter-empty">
        {placeholder}
      </p>
    );
  }

  return (
    <ol className="teleprompter" data-testid="teleprompter">
      {cards.map((card) => (
        <li
          className={`teleprompter-card teleprompter-card--${card.kind}`}
          data-testid="teleprompter-card"
          data-kind={card.kind}
          key={card.id}
        >
          <p className="teleprompter-question" data-testid="teleprompter-question">
            {card.question}
          </p>

          {card.kind === "fail_closed" ? (
            <p className="teleprompter-answer teleprompter-answer--miss" data-testid="teleprompter-miss">
              知识库未命中
            </p>
          ) : (
            <p className="teleprompter-answer" data-testid="teleprompter-answer">
              {card.answer}
            </p>
          )}

          <p className="teleprompter-meta">
            {card.source !== "" && (
              <span className="teleprompter-tag" data-testid="teleprompter-source">
                {card.source}
              </span>
            )}
            {card.kind === "error" && (
              <span className="teleprompter-tag teleprompter-tag--error" data-testid="teleprompter-error">
                检索失败
              </span>
            )}
            {card.reused && (
              <span className="teleprompter-tag teleprompter-tag--reused" data-testid="teleprompter-reused">
                复用
              </span>
            )}
          </p>
        </li>
      ))}
    </ol>
  );
}
