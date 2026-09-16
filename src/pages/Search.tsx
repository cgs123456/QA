import { useState } from "react";
import { useQA, type QASource } from "../hooks/useQA";
import { markersForRoutes, renderHighlight } from "../lib/highlight";
import { sourceLabel } from "../lib/sse";

function preview(source: QASource): string {
  const payload = source.payload as Record<string, unknown>;
  if (source.type === "field") {
    return `${String(payload["field_name"] ?? "")}：${String(payload["field_value"] ?? "")}`;
  }
  return String(payload["standard_question"] ?? "");
}

/** 命中预览：有后端片段则渲染高亮（F2.3），否则回落 60 字纯文本。 */
function HitPreview({ source }: { source: QASource }) {
  const hl = source.type === "qa" ? source.hl_question : undefined;
  if (hl == null || hl === "") {
    return <span>{preview(source).slice(0, 60)}</span>;
  }
  return (
    <span
      data-testid="hit-hl"
      dangerouslySetInnerHTML={renderHighlight(hl, markersForRoutes(source.routes))}
    />
  );
}

function AnswerCard({
  phase,
  action,
  text,
  resultKind,
  provider,
  degraded,
  error,
}: {
  phase: string;
  action: string | null;
  text: string;
  resultKind: string | null;
  provider: string | null;
  degraded: string[];
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
        {/* P8：实际出力 provider 标注（复用 asr_final degraded 模式）；无标注=单 provider。 */}
        {resultKind === "llm" && provider != null && provider !== "" && (
          <p data-testid="answer-provider">
            实际出力：{provider}
            {degraded.length > 0 && `（降级：${degraded.join("、")}）`}
          </p>
        )}
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
                <HitPreview source={s} />{" "}
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
          provider={result?.provider ?? null}
          degraded={result?.degraded ?? []}
          error={error}
        />
      </div>
    </div>
  );
}
