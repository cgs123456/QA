// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { ReviewPanel } from "./ReviewPanel";
import type { ReviewSummary } from "../lib/importFlow";

afterEach(() => cleanup());

function text(id: string): string {
  return screen.getByTestId(id).textContent ?? "";
}

function disabled(id: string): boolean {
  return (screen.getByTestId(id) as HTMLButtonElement).disabled;
}

const SUMMARY: ReviewSummary = {
  kind: "excel",
  filename: "qa.xlsx",
  exactCounts: [{ label: "数据行数（全表）", value: 128 }],
  entities: [
    { value: "公司信息", count: 2 },
    { value: "总部", count: 1, isDefault: true },
  ],
  fieldNames: [{ value: "客服电话", count: 3 }],
  qaNote: "问答列已映射（1 表），至多 120 行成条目",
  sampleBasis: "分布基于预览样例（每列前 3 行），非全量统计",
  warnings: ["《QA》公式按缓存值读取"],
  perSheet: [{ sheet: "QA", rows: 120 }],
};

describe("ReviewPanel 渲染", () => {
  it("shows counts, buckets, qa note, basis, warnings, target", () => {
    render(
      <ReviewPanel
        summary={SUMMARY}
        targetLabel="测试库"
        onConfirm={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(text("review-title")).toContain("qa.xlsx");
    expect(text("review-title")).toContain("测试库");
    expect(text("review-counts")).toContain("128");
    expect(text("review-entities")).toContain("公司信息");
    expect(text("review-entities")).toContain("整表默认值");
    expect(text("review-fields")).toContain("客服电话");
    expect(text("review-qa-note")).toContain("至多 120 行");
    // 口径声明必须可见。
    expect(text("review-basis")).toContain("非全量统计");
    expect(text("review-warnings")).toContain("公式按缓存值读取");
  });

  it("shows empty states when nothing mapped", () => {
    render(
      <ReviewPanel
        summary={{ ...SUMMARY, entities: [], fieldNames: [] }}
        targetLabel="库"
        onConfirm={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(screen.queryByTestId("review-entities-empty")).not.toBeNull();
    expect(screen.queryByTestId("review-fields-empty")).not.toBeNull();
  });
});

describe("ReviewPanel 确认/取消路径", () => {
  it("confirm and cancel fire callbacks", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<ReviewPanel summary={SUMMARY} targetLabel="库" onConfirm={onConfirm} onCancel={onCancel} />);
    await user.click(screen.getByTestId("review-confirm"));
    expect(onConfirm).toHaveBeenCalledOnce();
    await user.click(screen.getByTestId("review-cancel"));
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("problems block confirm", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <ReviewPanel
        summary={SUMMARY}
        targetLabel="库"
        problems={["角色 standard_question 重复映射到多列"]}
        onConfirm={onConfirm}
        onCancel={() => {}}
      />,
    );
    expect(text("review-problems")).toContain("重复映射");
    expect(disabled("review-confirm")).toBe(true);
    await user.click(screen.getByTestId("review-confirm"));
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("pending disables both buttons", () => {
    render(
      <ReviewPanel summary={SUMMARY} targetLabel="库" isPending onConfirm={() => {}} onCancel={() => {}} />,
    );
    expect(disabled("review-confirm")).toBe(true);
    expect(disabled("review-cancel")).toBe(true);
    expect(text("review-confirm")).toContain("入库中");
  });

  it("pdf entity override input reports changes", async () => {
    const user = userEvent.setup();
    function Harness() {
      const [v, setV] = useState("");
      return (
        <>
          <ReviewPanel
            summary={SUMMARY}
            targetLabel="库"
            showEntityOverride
            entityOverride={v}
            onEntityOverrideChange={setV}
            onConfirm={() => {}}
            onCancel={() => {}}
          />
          <output data-testid="entity-dump">{v}</output>
        </>
      );
    }
    render(<Harness />);
    await user.type(screen.getByTestId("review-entity-override"), "总部");
    expect(screen.getByTestId("entity-dump").textContent).toBe("总部");
  });
});
