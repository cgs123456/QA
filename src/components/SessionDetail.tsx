/**
 * 单会话时间线（S3）。**纯展示组件** —— 数据由 `pages/History.tsx` 经
 * `useSessionTimeline` 取好后传进来，所以它不认识 TanStack Query，
 * 也不认识检索（R21 的 UI 面）。
 *
 * # 文本渲染
 *
 * - `asr_final` / `asr_error` / `qa_exchange`：R14 content-free，显示「未落盘」。
 * - `rehearsal`（S12）：metadata 里有 question_text/official_answer/user_answer，
 *   **直接渲染**（账有文本，日志无文本）。
 *
 * # R21（UI 面）
 *
 * 时间线是**回放**。回放期间不触发任何检索：本组件不 import 任何问答/检索函数，
 * `sessions.test.ts` 用源码级断言盯着这一点（比"记得别这么做"靠得住）。
 */

import type { SessionEvent } from "../lib/api";
import {
  formatClock,
  formatDuration,
  qaActionLabel,
  timelineSummary,
  toTimeline,
  type TimelineRow,
} from "../lib/sessions";

/** 文本类位置统一的说法（一句话，不重复解释三段话）。 */
export const TEXT_NOT_STORED = "文本未落盘（R14：账只记计数，不记内容）";

function AsrRow({ row }: { row: TimelineRow }) {
  const f = row.asr;
  return (
    <li data-testid="tl-asr" data-segment={f?.segmentId}>
      <span data-testid="tl-time">{formatClock(row.tsMs)}</span>{" "}
      <strong>{row.typeLabel}</strong> · {row.stage}
      {f != null && (
        <>
          {" "}
          · 段 {f.segmentId} · {formatDuration(f.durationMs)} · {f.provider}
          {f.degradedLevels > 0 && ` · 降级 ${f.degradedLevels} 级`}
          {f.droppedOldest > 0 && ` · 丢帧 ${f.droppedOldest}`}
        </>
      )}
      <div data-testid="tl-asr-text">{TEXT_NOT_STORED}</div>
    </li>
  );
}

function QaRow({ row }: { row: TimelineRow }) {
  const f = row.qa;
  return (
    <li data-testid="tl-qa">
      <span data-testid="tl-time">{formatClock(row.tsMs)}</span>{" "}
      <strong data-testid="tl-qa-action">{f != null ? qaActionLabel(f.action) : row.typeLabel}</strong>
      {f != null && (
        <>
          {" "}
          · {f.provider} · LLM {f.llmCalls} 次 · 命中 {f.sources} 条
          {f.warnings > 0 && ` · 告警 ${f.warnings}`} · {formatDuration(f.elapsedMs)}
        </>
      )}
      {/*
        来源标签本该复用 QASource 的渲染（`lib/sse.ts` 的 `sourceLabel`），
        但账里只落了命中**条数**，没有来源对象 —— 所以这里如实显示条数，
        并说清为什么没有标签。等 F11.3 裁定后才谈得上存来源。
      */}
      <div data-testid="tl-qa-text">问题与答案未落盘（R14）；来源标签需先裁定 F11.3</div>
    </li>
  );
}

function RehearsalRow({ row }: { row: TimelineRow }) {
  const f = row.rehearsal;
  return (
    <li data-testid="tl-rehearsal">
      <span data-testid="tl-time">{formatClock(row.tsMs)}</span>{" "}
      <strong>{row.typeLabel}</strong> · {row.stage}
      {f != null && (
        <>
          {" "}
          · 档位：{f.verdict}
          {f.category != null && ` · 类目：${f.category}`}
          · 语料：{f.qaId}
        </>
      )}
      {f != null && (
        <div data-testid="tl-rehearsal-text">
          <div>题面：{f.questionText ?? "（无）"}</div>
          <div>标准答案：{f.officialAnswer ?? "（无）"}</div>
          <div>用户作答：{f.userAnswer ?? "（无）"}</div>
        </div>
      )}
    </li>
  );
}

export function SessionDetail({
  sessionId,
  events,
  storeName,
  onBack,
  onExport,
  exportPending = false,
  exportError = null,
}: {
  sessionId: string;
  events: SessionEvent[];
  storeName: string;
  onBack: () => void;
  /** 单会话导出（S4）。只读动作，**不是检索** —— 不触碰问答链路。 */
  onExport?: () => void;
  exportPending?: boolean;
  exportError?: string | null;
}) {
  const rows = toTimeline(events);
  return (
    <div data-testid="session-detail">
      <button type="button" data-testid="tl-back" onClick={onBack}>
        返回列表
      </button>
      <h3 data-testid="tl-title">会话 {sessionId}</h3>
      {onExport != null && (
        <button
          type="button"
          data-testid="tl-export"
          disabled={exportPending}
          onClick={onExport}
        >
          {exportPending ? "导出中…" : "导出 JSON"}
        </button>
      )}
      {exportError != null && <p data-testid="tl-export-error">导出失败：{exportError}</p>}
      <p data-testid="tl-meta">
        知识库：{storeName} · {timelineSummary(rows)}
      </p>
      {/*
        R21：这一页是回放，下面只有 `<li>`，没有任何会发起检索的控件。
        「重问一次」之类的事不做 —— 那会把回放变成一次真实检索。
      */}
      <ul data-testid="tl-rows">
        {rows.map((row) => {
          if (row.kind === "asr") return <AsrRow key={row.id} row={row} />;
          if (row.kind === "qa") return <QaRow key={row.id} row={row} />;
          if (row.kind === "rehearsal") return <RehearsalRow key={row.id} row={row} />;
          if (row.kind === "boundary") {
            return (
              <li key={row.id} data-testid="tl-boundary">
                <span data-testid="tl-time">{formatClock(row.tsMs)}</span>{" "}
                <strong>{row.typeLabel}</strong>
              </li>
            );
          }
          return (
            <li key={row.id} data-testid="tl-unknown">
              <span data-testid="tl-time">{formatClock(row.tsMs)}</span>{" "}
              <strong>{row.typeLabel}</strong>
              {row.unparsable != null && <div data-testid="tl-unparsable">账行无法解析</div>}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
