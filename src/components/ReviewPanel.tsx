import type { ReviewSummary } from "../lib/importFlow";

/**
 * 语义范围审核（R17）：commit 前展示**将要入库**的分布摘要。
 * 只展示计数级摘要（entity/field_name 取值 × 样例内频次），不全文展示；
 * 口径声明（`sampleBasis`）必须可见 —— 样例分布不是全量分布。
 */
export function ReviewPanel({
  summary,
  targetLabel,
  problems = [],
  entityOverride = "",
  showEntityOverride = false,
  onEntityOverrideChange,
  isPending = false,
  onConfirm,
  onCancel,
}: {
  summary: ReviewSummary;
  targetLabel: string;
  problems?: string[];
  entityOverride?: string;
  showEntityOverride?: boolean;
  onEntityOverrideChange?: (v: string) => void;
  isPending?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const blocked = problems.length > 0;
  return (
    <div>
      <h4 data-testid="review-title">入库前审核（{summary.filename} → {targetLabel}）</h4>
      <ul data-testid="review-counts">
        {summary.exactCounts.map((c) => (
          <li key={c.label}>
            {c.label}：{c.value}
          </li>
        ))}
      </ul>
      {summary.perSheet.length > 0 && (
        <ul data-testid="review-sheets">
          {summary.perSheet.map((s) => (
            <li key={s.sheet}>
              {s.sheet}：{s.rows} 行
            </li>
          ))}
        </ul>
      )}
      <div>
        <h5>实体 entity 分布</h5>
        {summary.entities.length === 0 ? (
          <p data-testid="review-entities-empty">无（未映射实体列且无默认值）</p>
        ) : (
          <ul data-testid="review-entities">
            {summary.entities.map((b) => (
              <li key={`${b.value}-${b.isDefault === true ? "d" : "s"}`}>
                {b.value} ×{b.count}
                {b.isDefault === true ? "（整表默认值）" : "（样例内）"}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <h5>字段名 field_name 分布</h5>
        {summary.fieldNames.length === 0 ? (
          <p data-testid="review-fields-empty">无（未映射字段名列）</p>
        ) : (
          <ul data-testid="review-fields">
            {summary.fieldNames.map((b) => (
              <li key={b.value}>
                {b.value} ×{b.count}（样例内）
              </li>
            ))}
          </ul>
        )}
      </div>
      <p data-testid="review-qa-note">{summary.qaNote}</p>
      <p data-testid="review-basis">{summary.sampleBasis}</p>
      {showEntityOverride && (
        <label>
          覆盖默认实体（可选，1–64 字符）：
          <input
            data-testid="review-entity-override"
            value={entityOverride}
            disabled={isPending}
            onChange={(e) => onEntityOverrideChange?.(e.currentTarget.value)}
            placeholder="空=按文件名/标题域"
          />
        </label>
      )}
      {summary.warnings.length > 0 && (
        <ul data-testid="review-warnings">
          {summary.warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}
      {blocked && (
        <ul data-testid="review-problems">
          {problems.map((p, i) => (
            <li key={i}>{p}</li>
          ))}
        </ul>
      )}
      <div>
        <button
          data-testid="review-confirm"
          type="button"
          disabled={isPending || blocked}
          onClick={onConfirm}
        >
          {isPending ? "入库中…" : "确认入库"}
        </button>{" "}
        <button data-testid="review-cancel" type="button" disabled={isPending} onClick={onCancel}>
          取消
        </button>
      </div>
    </div>
  );
}
