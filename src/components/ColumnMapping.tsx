import {
  DEFAULTS_KEY,
  ROLE_OPTIONS,
  type ColumnRole,
  type ExcelCommitMapping,
  type ExcelPreview,
} from "../lib/importFlow";

/**
 * 列映射确认（R17）：preview 提案渲染 → 用户逐列改角色 →
 * 确认后交 ReviewPanel 做语义审核。本组件不发请求、不写库。
 */
export function ColumnMapping({
  preview,
  mapping,
  onMappingChange,
  disabled = false,
}: {
  preview: ExcelPreview;
  mapping: ExcelCommitMapping;
  onMappingChange: (m: ExcelCommitMapping) => void;
  disabled?: boolean;
}) {
  function setRole(sheet: string, col: number, role: ColumnRole) {
    onMappingChange({
      ...mapping,
      [sheet]: { ...(mapping[sheet] ?? {}), [String(col)]: role },
    });
  }

  function resetSheet(sheet: string, suggested: Record<string, string>) {
    const next = { ...mapping };
    const keepDefaults = next[sheet]?.__defaults__;
    next[sheet] =
      keepDefaults !== undefined ? { ...suggested, [DEFAULTS_KEY]: keepDefaults } : { ...suggested };
    onMappingChange(next);
  }

  function setDefault(sheet: string, key: "entity" | "category", value: string) {
    const spec = { ...(mapping[sheet] ?? {}) };
    const prev = { ...(spec.__defaults__ ?? {}) };
    if (value.trim()) {
      prev[key] = value;
    } else {
      delete prev[key];
    }
    if (Object.keys(prev).length > 0) {
      spec[DEFAULTS_KEY] = prev;
    } else {
      delete spec[DEFAULTS_KEY];
    }
    onMappingChange({ ...mapping, [sheet]: spec });
  }

  return (
    <div>
      {preview.sheets.map((s, si) => {
        const spec = mapping[s.sheet] ?? {};
        const suggested = s.suggested_mapping;
        const maxSamples = Math.max(0, ...s.columns.map((c) => c.samples.length));
        return (
          <div key={s.sheet} data-testid={`mapping-sheet-${si}`}>
            <h4>
              {s.sheet}（{s.n_rows} 行 × {s.n_cols} 列
              {s.header_row != null ? `，表头第 ${s.header_row} 行` : "，未识别表头"}）
            </h4>
            <p data-testid={`mapping-limit-${si}`}>
              预览为每列前 {maxSamples} 行样例（有界预览），共 {s.n_rows}{" "}
              行将在确认后全量入库（preview/commit 分离）。
            </p>
            {s.n_cols === 0 && <p>空表，无可用数据（提交时将被服务端拒绝）。</p>}
            {s.columns.map((c) => {
              const raw = spec[String(c.index)];
              const current = (typeof raw === "string" ? raw : "ignore") as ColumnRole;
              const top = c.candidates[0];
              return (
                <div key={c.index} data-testid={`mapping-col-${si}-${c.index}`}>
                  <label>
                    第 {c.index + 1} 列「{c.header}」
                    <select
                      data-testid={`mapping-select-${si}-${c.index}`}
                      value={current}
                      disabled={disabled}
                      onChange={(e) => setRole(s.sheet, c.index, e.currentTarget.value as ColumnRole)}
                    >
                      {ROLE_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                          {suggested[String(c.index)] === o.value ? "（建议）" : ""}
                        </option>
                      ))}
                    </select>
                  </label>{" "}
                  {top != null && top.role !== "ignore" && (
                    <span data-testid={`mapping-reason-${si}-${c.index}`}>提案：{top.reason}</span>
                  )}
                  <ul data-testid={`mapping-samples-${si}-${c.index}`}>
                    {c.samples.map((sample, ri) => (
                      <li key={ri}>{sample}</li>
                    ))}
                  </ul>
                </div>
              );
            })}
            <div>
              <label>
                整表默认实体：
                <input
                  data-testid={`mapping-default-${si}-entity`}
                  value={spec[DEFAULTS_KEY]?.entity ?? ""}
                  disabled={disabled}
                  onChange={(e) => setDefault(s.sheet, "entity", e.currentTarget.value)}
                  placeholder="如：公司信息（无实体列时必填）"
                />
              </label>{" "}
              <label>
                整表默认分类：
                <input
                  data-testid={`mapping-default-${si}-category`}
                  value={spec[DEFAULTS_KEY]?.category ?? ""}
                  disabled={disabled}
                  onChange={(e) => setDefault(s.sheet, "category", e.currentTarget.value)}
                  placeholder="可选"
                />
              </label>{" "}
              <button
                data-testid={`mapping-reset-${si}`}
                type="button"
                disabled={disabled}
                onClick={() => resetSheet(s.sheet, suggested)}
              >
                重置为建议
              </button>
            </div>
            {s.warnings.length > 0 && (
              <ul data-testid={`mapping-warnings-${si}`}>
                {s.warnings.map((w, wi) => (
                  <li key={wi}>{w}</li>
                ))}
              </ul>
            )}
          </div>
        );
      })}
    </div>
  );
}
