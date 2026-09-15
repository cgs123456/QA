// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { ColumnMapping } from "./ColumnMapping";
import type { ExcelCommitMapping, ExcelPreview } from "../lib/importFlow";
import { buildInitialMapping } from "../lib/importFlow";

afterEach(() => cleanup());

function text(id: string): string {
  return screen.getByTestId(id).textContent ?? "";
}

const PREVIEW: ExcelPreview = {
  format: "excel",
  filename: "qa.xlsx",
  sheets: [
    {
      sheet: "QA",
      header_row: 1,
      n_rows: 120,
      n_cols: 2,
      headers: ["问题", "答案"],
      columns: [
        {
          index: 0,
          header: "问题",
          samples: ["发货多久？", "怎么退？"],
          candidates: [{ role: "standard_question", confidence: 0.85, reason: "表头命中同义词" }],
        },
        {
          index: 1,
          header: "答案",
          samples: ["三天", "七天"],
          candidates: [{ role: "official_answer", confidence: 0.85, reason: "表头命中同义词" }],
        },
      ],
      suggested_mapping: { "0": "standard_question", "1": "official_answer" },
      warnings: ["公式按缓存值读取"],
    },
  ],
};

function Harness({
  onChangeSpy,
}: {
  onChangeSpy?: (m: ExcelCommitMapping) => void;
}) {
  const [mapping, setMapping] = useState<ExcelCommitMapping>(() => buildInitialMapping(PREVIEW));
  return (
    <>
      <ColumnMapping
        preview={PREVIEW}
        mapping={mapping}
        onMappingChange={(m) => {
          setMapping(m);
          onChangeSpy?.(m);
        }}
      />
      <output data-testid="dump">{JSON.stringify(mapping)}</output>
    </>
  );
}

function dump(): ExcelCommitMapping {
  return JSON.parse(text("dump")) as ExcelCommitMapping;
}

describe("ColumnMapping 提案渲染", () => {
  it("shows headers, samples, suggested selections, reasons, limit note, warnings", () => {
    render(<Harness />);
    expect(text("mapping-sheet-0")).toContain("QA");
    expect(text("mapping-sheet-0")).toContain("120 行");
    expect((screen.getByTestId("mapping-select-0-0") as HTMLSelectElement).value).toBe(
      "standard_question",
    );
    expect(text("mapping-reason-0-0")).toContain("表头命中同义词");
    expect(text("mapping-samples-0-0")).toContain("发货多久？");
    // 行数上限提示：样例有界 + 全量 commit 分离。
    expect(text("mapping-limit-0")).toContain("120");
    expect(text("mapping-limit-0")).toContain("全量入库");
    expect(text("mapping-warnings-0")).toContain("公式按缓存值读取");
  });

  it("empty sheet shows notice instead of columns", () => {
    const empty: ExcelPreview = {
      format: "excel",
      filename: "e.xlsx",
      sheets: [
        {
          sheet: "空",
          header_row: null,
          n_rows: 0,
          n_cols: 0,
          headers: [],
          columns: [],
          suggested_mapping: {},
          warnings: ["空表，无可用数据"],
        },
      ],
    };
    render(<ColumnMapping preview={empty} mapping={{ 空: {} }} onMappingChange={() => {}} />);
    expect(text("mapping-sheet-0")).toContain("空表");
  });
});

describe("ColumnMapping 确认路径（改列→父状态）", () => {
  it("changing a select reports the updated mapping", async () => {
    const user = userEvent.setup();
    const spy = vi.fn();
    render(<Harness onChangeSpy={spy} />);
    await user.selectOptions(screen.getByTestId("mapping-select-0-1"), "ignore");
    expect(spy).toHaveBeenCalledOnce();
    expect((spy.mock.calls[0][0] as ExcelCommitMapping)["QA"]["1"]).toBe("ignore");
    expect(dump()["QA"]["1"]).toBe("ignore");
  });

  it("reset restores suggestions", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.selectOptions(screen.getByTestId("mapping-select-0-0"), "ignore");
    expect(dump()["QA"]["0"]).toBe("ignore");
    await user.click(screen.getByTestId("mapping-reset-0"));
    expect(dump()["QA"]["0"]).toBe("standard_question");
  });

  it("defaults inputs update __defaults__", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByTestId("mapping-default-0-entity"), "公司信息");
    expect(dump()["QA"].__defaults__).toEqual({ entity: "公司信息" });
  });
});
