import { useState } from "react";
import { useQA, type QASource } from "../hooks/useQA";
import { sourceLabel } from "../lib/sse";

function preview(source: QASource): string {
  const payload = source.payload as Record<string, unknown>;
  if (source.type === "field") {
    return `${String(payload["field_name"] ?? "")}：${String(payload["field_value"] ?? "")}`;
  }
  return String(payload["standard_question"] ?? "");
}

function AnswerCard({
  phase,
  action,
  text,
  resultKind,
  error,
}: {
  phase: string;
  action: string | null;
  text: string;
  resultKind: string | null;
  error: string | null;
}) {
  if (phase === "error" || resultKind === "error") {
    return <p data-testid="answer-error">回答失败：{error ?? "未知错误"}</p>;
  }
  if (resultKind === "fail_closed") {
    return <p data-testid="answer-empty">知识库未命中</p>;
  }
  if (resultKind === "direct") {
    return (
      <div>
        <p data-testid="answer-kind">固定答案{action != null ? `（${action}）` : ""}</p>
        <p data-testid="answer-text">{text}</p>
      </div>
    );
  }
  if (phase === "streaming" || resultKind === "llm") {
    return (
      <div>
        <p data-testid="answer-kind">
          流式生成{phase === "streaming" ? "（生成中…）" : ""}
        </p>
        <p data-testid="answer-text">{text}</p>
      </div>
    );
  }
  return <p data-testid="answer-empty">输入问题后显示答案</p>;
}

export function Search() {
  const [input, setInput] = useState("");
  const { phase, action, sources, text, result, error, ask, reset } = useQA();

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const q = input.trim();
    if (q) void ask(q);
  }

  return (
    <div>
      <h2>手动查找</h2>
      <form className="row" onSubmit={submit}>
        <input
          data-testid="search-input"
          value={input}
          onChange={(e) => setInput(e.currentTarget.value)}
          placeholder="输入问题…"
        />
        <button
          data-testid="search-ask"
          type="submit"
          disabled={phase === "asking" || phase === "streaming"}
        >
          提问
        </button>
        <button type="button" onClick={reset}>
          清空
        </button>
      </form>

      {sources.length > 0 && (
        <div>
          <h3>命中列表</h3>
          <ul data-testid="hit-list">
            {sources.map((s) => (
              <li key={`${s.type}:${s.key}`}>
                <span data-testid="hit-tag">{sourceLabel(s)}</span>{" "}
                <span>{preview(s).slice(0, 60)}</span>{" "}
                <span>({s.score.toFixed(3)})</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <h3>答案</h3>
        <AnswerCard
          phase={phase}
          action={action}
          text={text}
          resultKind={result?.kind ?? null}
          error={error}
        />
      </div>
    </div>
  );
}
