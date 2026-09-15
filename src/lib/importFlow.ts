/**
 * P1 导入审核流（R17 用户面）的纯逻辑层：类型、映射编辑、客户端校验、
 * 语义分布摘要、错误解析。刻意不依赖 React/DOM，可在 node 下单测。
 *
 * 与后端契约（`POST /knowledge/import/preview|commit`）同源：
 * - Excel preview：逐 sheet `{headers, columns[{index,header,samples,
 *   candidates[{role,confidence,reason}]}], suggested_mapping, warnings,
 *   n_rows, n_cols}`；commit mapping 为 `{sheet: {col: role,
 *   __defaults__?: {entity?, category?}}}`。
 * - PDF preview：`{qa_count, field_count, qa_samples, field_samples,
 *   unsupported_pages, pages, warnings}`；commit mapping 仅允许
 *   `{"entity": ...}` 或省略。
 *
 * 口径诚实性（R17 坑）：preview 样例是**有界子集**（后端每列前 3 行，
 * P1 测试锁定，不加量），全量只在 commit 发生。分布摘要一律标注
 * `sampleBasis`，行数（`n_rows`/`qa_count`）是精确计数。UI 必须同时展示
 * 两者，不得把样例分布当成全量分布。
 */

import { ApiError } from "./api";

export type ImportFormat = "excel" | "pdf";

export type ColumnRole =
  | "entity"
  | "field_name"
  | "field_value"
  | "standard_question"
  | "official_answer"
  | "category"
  | "usage_status"
  | "followup_logic"
  | "id"
  | "ignore";

/** 后端体积上限镜像（preview/commit 同值；超限前端先拦，免拼巨型 JSON）。 */
export const IMPORT_MAX_BYTES = 10 * 1024 * 1024;

export const ROLE_OPTIONS: { value: ColumnRole; label: string }[] = [
  { value: "ignore", label: "忽略" },
  { value: "entity", label: "实体 entity" },
  { value: "field_name", label: "字段名 field_name" },
  { value: "field_value", label: "字段值 field_value" },
  { value: "standard_question", label: "标准问题 standard_question" },
  { value: "official_answer", label: "标准答案 official_answer" },
  { value: "category", label: "分类 category" },
  { value: "usage_status", label: "使用状态 usage_status" },
  { value: "followup_logic", label: "后续逻辑 followup_logic" },
  { value: "id", label: "编号 id" },
];

export type ColumnCandidate = { role: string; confidence: number; reason: string };
export type SheetColumn = {
  index: number;
  header: string;
  samples: string[];
  candidates: ColumnCandidate[];
};
export type SheetProposal = {
  sheet: string;
  header_row: number | null;
  n_rows: number;
  n_cols: number;
  headers: string[];
  columns: SheetColumn[];
  suggested_mapping: Record<string, string>;
  warnings: string[];
};
export type ExcelPreview = { format: "excel"; filename: string; sheets: SheetProposal[] };

export type QaSample = {
  standard_question: string;
  official_answer: string;
  category?: string | null;
};
export type FieldSample = { entity: string; field_name: string; field_value: string };
export type PdfPreview = {
  format: "pdf";
  filename: string;
  supported: boolean;
  n_pages: number;
  pages: { index: number; chars: number; supported: boolean }[];
  unsupported_pages: number[];
  layout_support: string;
  layout_note: string;
  qa_samples: QaSample[];
  field_samples: FieldSample[];
  qa_count: number;
  field_count: number;
  warnings: string[];
};

export type ImportPreview = ExcelPreview | PdfPreview;

export type SheetDefaults = { entity?: string; category?: string };

/**
 * Excel commit 映射：`{表: {列: 角色, __defaults__?: {entity?, category?}}}`。
 * 索引签名覆盖数字列键，`__defaults__` 走具名可选属性（两者类型都兼容
 * `string | SheetDefaults`，读取时按 key 收窄）。
 */
export type SheetMapping = {
  [col: string]: string | SheetDefaults | undefined;
  __defaults__?: SheetDefaults;
};

export type ExcelCommitMapping = Record<string, SheetMapping>;

export const DEFAULTS_KEY = "__defaults__";
const MUTABLE_DEFAULT_KEYS = ["entity", "category"] as const;

/** 发送给后端的 commit mapping 必须是纯 JSON（去 undefined）。 */
export function cleanExcelMapping(mapping: ExcelCommitMapping): ExcelCommitMapping {
  const out: ExcelCommitMapping = {};
  for (const [sheet, spec] of Object.entries(mapping)) {
    const sheetOut: SheetMapping = {};
    for (const [k, v] of Object.entries(spec)) {
      if (k === DEFAULTS_KEY || typeof v !== "string") continue;
      sheetOut[k] = v;
    }
    const d = spec.__defaults__;
    const defaults: SheetDefaults = {};
    if (d?.entity?.trim()) defaults.entity = d.entity.trim();
    if (d?.category?.trim()) defaults.category = d.category.trim();
    if (Object.keys(defaults).length > 0) sheetOut.__defaults__ = defaults;
    out[sheet] = sheetOut;
  }
  return out;
}

// ---------- 文件 ----------

export function detectImportFormat(filename: string): ImportFormat | null {
  const lower = (filename || "").toLowerCase();
  if (lower.endsWith(".xlsx") || lower.endsWith(".xls")) return "excel";
  if (lower.endsWith(".pdf")) return "pdf";
  return null;
}

/** ArrayBuffer → base64（分块拼串，避免大文件栈溢出；浏览器/Node 通用）。 */
export function arrayBufferToBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  const CHUNK = 0x8000;
  let binary = "";
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

export async function fileToBase64(file: Blob): Promise<string> {
  return arrayBufferToBase64(await file.arrayBuffer());
}

// ---------- 映射编辑（不可变更新） ----------

export function buildInitialMapping(preview: ExcelPreview): ExcelCommitMapping {
  const out: ExcelCommitMapping = {};
  for (const s of preview.sheets) {
    out[s.sheet] = { ...s.suggested_mapping };
  }
  return out;
}

export function setColumnRole(
  mapping: ExcelCommitMapping,
  sheet: string,
  col: number,
  role: ColumnRole,
): ExcelCommitMapping {
  return { ...mapping, [sheet]: { ...(mapping[sheet] ?? {}), [String(col)]: role } };
}

export function setSheetDefault(
  mapping: ExcelCommitMapping,
  sheet: string,
  key: (typeof MUTABLE_DEFAULT_KEYS)[number],
  value: string,
): ExcelCommitMapping {
  const spec: SheetMapping = { ...(mapping[sheet] ?? {}) };
  const prev: SheetDefaults = { ...(spec.__defaults__ ?? {}) };
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
  return { ...mapping, [sheet]: spec };
}

/** 客户端镜像校验（服务端是最终裁决，此处只做即时反馈）。返回问题列表，为空即通过。 */
export function validateExcelMapping(preview: ExcelPreview, mapping: ExcelCommitMapping): string[] {
  const problems: string[] = [];
  const knownRoles = new Set(ROLE_OPTIONS.map((o) => o.value));
  for (const s of preview.sheets) {
    const spec = mapping[s.sheet];
    if (spec == null) {
      problems.push(`工作表《${s.sheet}》尚未映射`);
      continue;
    }
    const seen = new Map<string, string>();
    for (const [k, v] of Object.entries(spec)) {
      if (k === DEFAULTS_KEY) {
        const d: unknown = spec.__defaults__;
        if (typeof d !== "object" || d == null) {
          problems.push(`工作表《${s.sheet}》默认值须为对象`);
          continue;
        }
        for (const [dk, dv] of Object.entries(d)) {
          if (!(MUTABLE_DEFAULT_KEYS as readonly string[]).includes(dk)) {
            problems.push(`工作表《${s.sheet}》不支持默认值 ${dk}`);
          } else if (typeof dv !== "string" || !dv.trim() || dv.trim().length > 64) {
            problems.push(`工作表《${s.sheet}》默认值 ${dk} 非法（1–64 字符）`);
          }
        }
        continue;
      }
      const col = Number(k);
      if (!Number.isInteger(col) || col < 0 || col >= s.n_cols) {
        problems.push(`工作表《${s.sheet}》列 ${k} 越界`);
        continue;
      }
      if (typeof v !== "string" || !knownRoles.has(v as ColumnRole)) {
        problems.push(`工作表《${s.sheet}》列 ${k} 角色未知`);
        continue;
      }
      if (v !== "ignore") {
        const prev = seen.get(v);
        if (prev !== undefined) {
          problems.push(`工作表《${s.sheet}》角色 ${v} 重复映射到多列`);
        } else {
          seen.set(v, k);
        }
      }
    }
  }
  return problems;
}

// ---------- 语义分布摘要（计数级，不全文） ----------

export type Bucket = { value: string; count: number; isDefault?: boolean };

export function bucketize(values: string[]): Bucket[] {
  const counts = new Map<string, number>();
  for (const v of values) {
    const key = (v || "").trim();
    if (!key) continue;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([value, count]) => ({ value, count }))
    .sort((a, b) => b.count - a.count || a.value.localeCompare(b.value, "zh"));
}

function columnSamples(sheet: SheetProposal, colKey: string): string[] {
  const col = sheet.columns.find((c) => String(c.index) === colKey);
  return col?.samples ?? [];
}

export type ReviewSummary = {
  kind: ImportFormat;
  filename: string;
  /** 精确口径（后端计数，非样例）。 */
  exactCounts: { label: string; value: number }[];
  /** 样例口径分布（频次=样例内出现次数，非全量）。 */
  entities: Bucket[];
  fieldNames: Bucket[];
  /** 问答列映射情况（Excel）/精确条目数（PDF）。 */
  qaNote: string;
  /** 口径声明（UI 必须展示）。 */
  sampleBasis: string;
  warnings: string[];
  perSheet: { sheet: string; rows: number }[];
};

export function excelPreviewToSummary(
  preview: ExcelPreview,
  mapping: ExcelCommitMapping,
): ReviewSummary {
  const entities: string[] = [];
  const fieldNames: string[] = [];
  const perSheet = preview.sheets.map((s) => ({ sheet: s.sheet, rows: s.n_rows }));
  let qaSheets = 0;
  let qaMaxRows = 0;
  const warnings = preview.sheets.flatMap((s) =>
    s.warnings.map((w) => `《${s.sheet}》${w}`),
  );
  const defaultEntities: string[] = [];
  for (const s of preview.sheets) {
    const spec = mapping[s.sheet] ?? {};
    const roleOf = (role: string): string | undefined =>
      Object.entries(spec).find(([k, v]) => k !== DEFAULTS_KEY && v === role)?.[0];
    const entityCol = roleOf("entity");
    if (entityCol !== undefined) {
      entities.push(...columnSamples(s, entityCol));
    } else {
      const d = spec.__defaults__?.entity?.trim();
      if (d) defaultEntities.push(d);
    }
    const nameCol = roleOf("field_name");
    if (nameCol !== undefined) fieldNames.push(...columnSamples(s, nameCol));
    if (roleOf("standard_question") !== undefined && roleOf("official_answer") !== undefined) {
      qaSheets += 1;
      qaMaxRows += s.n_rows;
    }
  }
  const totalRows = perSheet.reduce((a, b) => a + b.rows, 0);
  return {
    kind: "excel",
    filename: preview.filename,
    exactCounts: [{ label: "数据行数（全表）", value: totalRows }],
    entities: [
      ...bucketize(entities),
      ...bucketize(defaultEntities).map((b) => ({ ...b, isDefault: true })),
    ],
    fieldNames: bucketize(fieldNames),
    qaNote:
      qaSheets > 0
        ? `问答列已映射（${qaSheets} 表），至多 ${qaMaxRows} 行成条目（空行跳过，以入库 stats 为准）`
        : "未映射问答列（仅入库字段；如需问答请回上一步补映射）",
    sampleBasis: "分布基于预览样例（每列前 3 行），非全量统计；全量以 commit 入库结果为准",
    warnings,
    perSheet,
  };
}

export function pdfPreviewToSummary(
  preview: PdfPreview,
  entityOverride?: string,
): ReviewSummary {
  const entities = preview.field_samples.map((f) => f.entity);
  const defaultEntities = entityOverride?.trim() ? [entityOverride.trim()] : [];
  const warnings = [...preview.warnings];
  if (preview.layout_note) warnings.push(preview.layout_note);
  return {
    kind: "pdf",
    filename: preview.filename,
    exactCounts: [
      { label: "问答条目（解析计数）", value: preview.qa_count },
      { label: "字段条目（解析计数）", value: preview.field_count },
      { label: "页数", value: preview.n_pages },
    ],
    entities: [
      ...bucketize(entities),
      ...bucketize(defaultEntities).map((b) => ({ ...b, isDefault: true })),
    ],
    fieldNames: bucketize(preview.field_samples.map((f) => f.field_name)),
    qaNote: `问答 ${preview.qa_count} 条、字段 ${preview.field_count} 条（解析计数，commit 前校验可能再过滤）`,
    sampleBasis: "分布基于预览样例（每类前 3 条），非全量统计；全量以 commit 入库结果为准",
    warnings,
    perSheet: [],
  };
}

// ---------- 错误解析 ----------

export type UnsupportedDetail = { message: string; pages: number[] };

/** 422 unsupported 提取（扫描件明确提示态用）；非 422/非目标结构返回 null。 */
export function parseUnsupportedDetail(err: unknown): UnsupportedDetail | null {
  if (!(err instanceof ApiError) || err.status !== 422) return null;
  const payload = err.payload as { detail?: unknown } | undefined;
  const d = payload?.detail as { error?: unknown; message?: unknown; pages?: unknown } | undefined;
  if (d?.error !== "unsupported") return null;
  return {
    message: typeof d.message === "string" && d.message ? d.message : "文件不受支持",
    pages: Array.isArray(d.pages) ? d.pages.filter((p): p is number => typeof p === "number") : [],
  };
}

export function importErrorMessage(err: unknown): string {
  const u = parseUnsupportedDetail(err);
  if (u) return u.message;
  if (err instanceof ApiError) return err.message;
  return String(err);
}
