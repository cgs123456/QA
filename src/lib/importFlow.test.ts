import { describe, expect, it } from "vitest";
import {
  arrayBufferToBase64,
  bucketize,
  buildInitialMapping,
  cleanExcelMapping,
  detectImportFormat,
  excelPreviewToSummary,
  fileToBase64,
  importErrorMessage,
  parseUnsupportedDetail,
  pdfPreviewToSummary,
  setColumnRole,
  setSheetDefault,
  validateExcelMapping,
  type ExcelPreview,
  type PdfPreview,
} from "./importFlow";
import { ApiError } from "./api";

const EXCEL_FIXTURE: ExcelPreview = {
  format: "excel",
  filename: "qa.xlsx",
  sheets: [
    {
      sheet: "QA",
      header_row: 1,
      n_rows: 120,
      n_cols: 3,
      headers: ["问题", "答案", "分类"],
      columns: [
        {
          index: 0,
          header: "问题",
          samples: ["发货周期是多久？", "支持哪些支付？", "退货期限？"],
          candidates: [{ role: "standard_question", confidence: 0.85, reason: "同义词" }],
        },
        {
          index: 1,
          header: "答案",
          samples: ["三天内发出", "微信和支付宝", "七天内可退"],
          candidates: [{ role: "official_answer", confidence: 0.85, reason: "同义词" }],
        },
        {
          index: 2,
          header: "分类",
          samples: ["售后", "售后", "售后"],
          candidates: [{ role: "category", confidence: 0.85, reason: "同义词" }],
        },
      ],
      suggested_mapping: { "0": "standard_question", "1": "official_answer", "2": "category" },
      warnings: ["公式按缓存值读取（data_only=True），无缓存的公式格视为空"],
    },
    {
      sheet: "Fields",
      header_row: 1,
      n_rows: 8,
      n_cols: 3,
      headers: ["实体", "字段名", "字段值"],
      columns: [
        {
          index: 0,
          header: "实体",
          samples: ["公司信息", "公司信息", "售后政策"],
          candidates: [{ role: "entity", confidence: 0.85, reason: "同义词" }],
        },
        {
          index: 1,
          header: "字段名",
          samples: ["客服电话", "发货周期", "退货期限"],
          candidates: [{ role: "field_name", confidence: 0.85, reason: "同义词" }],
        },
        {
          index: 2,
          header: "字段值",
          samples: ["400-123", "三天", "七天"],
          candidates: [{ role: "field_value", confidence: 0.85, reason: "同义词" }],
        },
      ],
      suggested_mapping: { "0": "entity", "1": "field_name", "2": "field_value" },
      warnings: [],
    },
  ],
};

const PDF_FIXTURE: PdfPreview = {
  format: "pdf",
  filename: "info.pdf",
  supported: true,
  n_pages: 2,
  pages: [
    { index: 0, chars: 160, supported: true },
    { index: 1, chars: 0, supported: false },
  ],
  unsupported_pages: [1],
  layout_support: "single-column-only",
  layout_note: "仅支持单栏顺序文本",
  qa_samples: [{ standard_question: "发货多久？", official_answer: "三天", category: "Info" }],
  field_samples: [
    { entity: "Info", field_name: "Phone", field_value: "400" },
    { entity: "Info", field_name: "Addr", field_value: "Beijing" },
    { entity: "售后", field_name: "退货", field_value: "七天" },
  ],
  qa_count: 4,
  field_count: 9,
  warnings: ["部分页面为图片/扫描页，已跳过并列出（不静默）：1"],
};

describe("detectImportFormat", () => {
  it("maps extensions (case-insensitive)", () => {
    expect(detectImportFormat("a.XLSX")).toBe("excel");
    expect(detectImportFormat("a.xls")).toBe("excel");
    expect(detectImportFormat("a.PDF")).toBe("pdf");
    expect(detectImportFormat("a.md")).toBeNull();
    expect(detectImportFormat("")).toBeNull();
  });
});

describe("base64", () => {
  it("arrayBuffer round-trips bytes", () => {
    const bytes = new Uint8Array([0, 1, 2, 250, 255]);
    const b64 = arrayBufferToBase64(bytes.buffer);
    expect(atob(b64)).toBe(String.fromCharCode(0, 1, 2, 250, 255));
  });

  it("fileToBase64 reads Blob content", async () => {
    const blob = new Blob(["hello"]);
    expect(await fileToBase64(blob)).toBe(btoa("hello"));
  });
});

describe("mapping edits", () => {
  it("buildInitialMapping deep-copies suggestions", () => {
    const m = buildInitialMapping(EXCEL_FIXTURE);
    expect(m).toEqual({
      QA: { "0": "standard_question", "1": "official_answer", "2": "category" },
      Fields: { "0": "entity", "1": "field_name", "2": "field_value" },
    });
    m["QA"]["0"] = "ignore";
    expect(EXCEL_FIXTURE.sheets[0].suggested_mapping["0"]).toBe("standard_question");
  });

  it("setColumnRole is immutable", () => {
    const m0 = buildInitialMapping(EXCEL_FIXTURE);
    const m1 = setColumnRole(m0, "QA", 2, "ignore");
    expect(m1["QA"]["2"]).toBe("ignore");
    expect(m0["QA"]["2"]).toBe("category");
  });

  it("setSheetDefault sets and prunes empty values", () => {
    const m0 = buildInitialMapping(EXCEL_FIXTURE);
    const m1 = setSheetDefault(m0, "QA", "entity", "公司信息");
    expect(m1["QA"].__defaults__).toEqual({ entity: "公司信息" });
    const m2 = setSheetDefault(m1, "QA", "entity", "  ");
    expect(m2["QA"].__defaults__).toBeUndefined();
    expect(m0["QA"].__defaults__).toBeUndefined();
  });

  it("cleanExcelMapping drops empties for commit payload", () => {
    const cleaned = cleanExcelMapping({
      QA: { "0": "standard_question", __defaults__: { entity: "  ", category: "售后" } },
    });
    expect(cleaned).toEqual({ QA: { "0": "standard_question", __defaults__: { category: "售后" } } });
  });
});

describe("validateExcelMapping", () => {
  it("passes for suggested mapping", () => {
    expect(validateExcelMapping(EXCEL_FIXTURE, buildInitialMapping(EXCEL_FIXTURE))).toEqual([]);
  });

  it("flags missing sheet / bad col / unknown role / duplicates", () => {
    const problems = validateExcelMapping(EXCEL_FIXTURE, {
      QA: { "0": "standard_question", "1": "standard_question", "9": "ignore", "x": "bogus" as never },
    } as never);
    expect(problems.some((p) => p.includes("尚未映射") && p.includes("Fields"))).toBe(true);
    expect(problems.some((p) => p.includes("重复映射"))).toBe(true);
    expect(problems.some((p) => p.includes("越界"))).toBe(true);
    expect(problems.some((p) => p.includes("未知") || p.includes("越界"))).toBe(true);
  });
});

describe("bucketize", () => {
  it("counts and sorts desc, drops blanks", () => {
    expect(bucketize(["b", "a", "b", "  ", "a", "b"])).toEqual([
      { value: "b", count: 3 },
      { value: "a", count: 2 },
    ]);
  });
});

describe("excelPreviewToSummary", () => {
  it("aggregates entities/fieldNames from mapped sample columns", () => {
    const s = excelPreviewToSummary(EXCEL_FIXTURE, buildInitialMapping(EXCEL_FIXTURE));
    expect(s.kind).toBe("excel");
    expect(s.exactCounts).toEqual([{ label: "数据行数（全表）", value: 128 }]);
    expect(s.entities).toEqual([
      { value: "公司信息", count: 2 },
      { value: "售后政策", count: 1 },
    ]);
    expect(s.fieldNames.map((b) => b.value).sort()).toEqual(["客服电话", "发货周期", "退货期限"].sort());
    expect(s.qaNote).toContain("至多 120 行");
    expect(s.sampleBasis).toContain("前 3 行");
    expect(s.perSheet).toEqual([
      { sheet: "QA", rows: 120 },
      { sheet: "Fields", rows: 8 },
    ]);
  });

  it("marks __defaults__ entity buckets and warns on unmapped QA", () => {
    const s = excelPreviewToSummary(EXCEL_FIXTURE, {
      QA: { "0": "ignore", "1": "ignore", "2": "ignore" },
      Fields: { "0": "ignore", "1": "field_name", "2": "field_value", __defaults__: { entity: "总部" } },
    });
    expect(s.entities).toEqual([{ value: "总部", count: 1, isDefault: true }]);
    expect(s.qaNote).toContain("未映射问答列");
  });
});

describe("pdfPreviewToSummary", () => {
  it("uses exact counts and sample buckets", () => {
    const s = pdfPreviewToSummary(PDF_FIXTURE, "总部");
    expect(s.exactCounts).toEqual([
      { label: "问答条目（解析计数）", value: 4 },
      { label: "字段条目（解析计数）", value: 9 },
      { label: "页数", value: 2 },
    ]);
    expect(s.entities).toContainEqual({ value: "Info", count: 2 });
    expect(s.entities).toContainEqual({ value: "总部", count: 1, isDefault: true });
    expect(s.fieldNames.map((b) => b.value).sort()).toEqual(["Phone", "Addr", "退货"].sort());
    expect(s.warnings.some((w) => w.includes("单栏"))).toBe(true);
  });
});

describe("unsupported parsing", () => {
  const err422 = new ApiError("http", "x", 422, {
    detail: { error: "unsupported", reason: "scanned", message: "扫描件，需 OCR", pages: [0, 2] },
  });

  it("extracts message+pages from 422", () => {
    expect(parseUnsupportedDetail(err422)).toEqual({ message: "扫描件，需 OCR", pages: [0, 2] });
    expect(importErrorMessage(err422)).toBe("扫描件，需 OCR");
  });

  it("returns null for other errors", () => {
    expect(parseUnsupportedDetail(new ApiError("http", "bad", 400))).toBeNull();
    expect(parseUnsupportedDetail(new Error("x"))).toBeNull();
    expect(
      parseUnsupportedDetail(new ApiError("http", "x", 422, { detail: { error: "other" } })),
    ).toBeNull();
    expect(importErrorMessage(new ApiError("http", "oops", 500))).toContain("oops");
  });
});
