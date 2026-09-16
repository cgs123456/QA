// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Knowledge } from "./Knowledge";
import { ApiError } from "../lib/api";
import type { ExcelPreview } from "../lib/importFlow";

afterEach(() => cleanup());

const previewMutate = vi.fn();
const commitMutate = vi.fn();
const previewReset = vi.fn();
const commitReset = vi.fn();
// 由各用例在渲染前写入，模拟服务端返回。
const previewState: {
  data?: ExcelPreview;
  error?: unknown;
  isPending?: boolean;
} = {};
const commitState: { data?: { store_id: string; stats: Record<string, number> } } = {};

// 注意：mock 返回的引用必须稳定——Knowledge 用 useEffect 同步
// list.data 到 zustand，每 render 新数组会触发无限循环。
const STABLE_LIST = { data: [] as never[], isLoading: false, isError: false };
const STABLE_MUT = () => ({ mutate: vi.fn(), isPending: false });
const STABLE_COMPILE = {
  mutate: vi.fn(),
  isPending: false,
  isError: false,
  data: undefined,
};

vi.mock("../hooks/useStore", () => ({
  useStore: () => ({
    list: STABLE_LIST,
    create: STABLE_MUT(),
    switchStore: STABLE_MUT(),
    remove: STABLE_MUT(),
    compile: STABLE_COMPILE,
  }),
}));

vi.mock("../hooks/useImport", () => ({
  useImportPreview: () => ({
    data: previewState.data,
    error: previewState.error ?? null,
    isPending: previewState.isPending ?? false,
    isError: previewState.error != null,
    mutate: previewMutate,
    reset: previewReset,
  }),
  useImportCommit: () => ({
    data: commitState.data,
    error: null,
    isPending: false,
    isError: false,
    mutate: commitMutate,
    reset: commitReset,
  }),
}));

// S7 模板系统：Knowledge 页会拉模板列表，但本文件测的是 P1 审核流。
// 不 mock 的话 useQuery 会因为没有 QueryClientProvider 直接抛错（模板那一路与本用例无关）。
vi.mock("../hooks/useTemplates", () => ({
  useTemplates: () => ({ templates: [], isLoading: false, isError: false }),
  useCreateFromTemplate: () => ({
    mutateAsync: vi.fn().mockResolvedValue(undefined),
    mutate: vi.fn(),
    isPending: false,
  }),
  useCreateTemplate: () => ({
    mutateAsync: vi.fn().mockResolvedValue(undefined),
    mutate: vi.fn(),
    isPending: false,
  }),
}));

const EXCEL_DATA: ExcelPreview = {
  format: "excel",
  filename: "qa.xlsx",
  sheets: [
    {
      sheet: "QA",
      header_row: 1,
      n_rows: 2,
      n_cols: 2,
      headers: ["问题", "答案"],
      columns: [
        {
          index: 0,
          header: "问题",
          samples: ["发货多久？"],
          candidates: [{ role: "standard_question", confidence: 0.85, reason: "同义词" }],
        },
        {
          index: 1,
          header: "答案",
          samples: ["三天"],
          candidates: [{ role: "official_answer", confidence: 0.85, reason: "同义词" }],
        },
      ],
      suggested_mapping: { "0": "standard_question", "1": "official_answer" },
      warnings: [],
    },
  ],
};

const COMMIT_RESULT = { store_id: "s1", stats: { qa_inserted: 2 } };

beforeEach(() => {
  vi.clearAllMocks();
  delete previewState.data;
  delete previewState.error;
  delete previewState.isPending;
  delete commitState.data;
  // mock 写入 previewState/commitState 再调 onSuccess——模拟 react-query
  // 的缓存更新（组件每次 render 重读 mock 返回值）。
  previewMutate.mockImplementation((_vars, opts?: { onSuccess?: (d: ExcelPreview) => void }) => {
    previewState.data = EXCEL_DATA;
    opts?.onSuccess?.(EXCEL_DATA);
  });
  commitMutate.mockImplementation((_vars, opts?: { onSuccess?: (d: unknown) => void }) => {
    commitState.data = COMMIT_RESULT;
    opts?.onSuccess?.(COMMIT_RESULT);
  });
});

function text(id: string): string {
  return screen.getByTestId(id).textContent ?? "";
}

describe("Knowledge 审核流：上传→映射→审核→入库", () => {
  it("excel full path calls preview then commit with confirmed mapping", async () => {
    const user = userEvent.setup();
    render(<Knowledge />);
    const bytes = new Uint8Array([1, 2, 3, 4]);
    await user.upload(
      screen.getByTestId("audit-file"),
      new File([bytes], "qa.xlsx", {
        type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      }),
    );
    // preview 调的是 excel + base64。
    expect(previewMutate).toHaveBeenCalledOnce();
    const req = previewMutate.mock.calls[0][0] as {
      format: string;
      content_b64: string;
      filename: string;
    };
    expect(req.format).toBe("excel");
    expect(req.filename).toBe("qa.xlsx");
    expect(atob(req.content_b64)).toBe(String.fromCharCode(1, 2, 3, 4));
    // 提案渲染（上传链路经 File.arrayBuffer 异步，用 findBy 等待）。
    await screen.findByTestId("mapping-sheet-0");
    expect(text("mapping-sheet-0")).toContain("QA");
    // 进入审核 → 语义摘要（fixture 未映射实体列，应为 empty 态）。
    await user.click(screen.getByTestId("audit-goto-review"));
    expect(screen.queryByTestId("review-entities-empty")).not.toBeNull();
    expect(text("review-qa-note")).toContain("至多 2 行");
    // 确认入库 → commit 带确认后映射。
    await user.click(screen.getByTestId("review-confirm"));
    expect(commitMutate).toHaveBeenCalledOnce();
    const commitReq = commitMutate.mock.calls[0][0] as {
      format: string;
      mapping: Record<string, Record<string, string>>;
    };
    expect(commitReq.format).toBe("excel");
    expect(commitReq.mapping).toEqual({ QA: { "0": "standard_question", "1": "official_answer" } });
    expect(text("audit-done")).toContain("s1");
  });

  it("cancel leaves no residue (mapping gone, file cleared)", async () => {
    const user = userEvent.setup();
    render(<Knowledge />);
    await user.upload(screen.getByTestId("audit-file"), new File(["zz"], "qa.xlsx"));
    await screen.findByTestId("mapping-sheet-0");
    await user.click(screen.getByTestId("audit-cancel"));
    expect(screen.queryByTestId("mapping-sheet-0")).toBeNull();
    expect(screen.queryByTestId("audit-goto-review")).toBeNull();
    expect(previewReset).toHaveBeenCalled();
    expect((screen.getByTestId("audit-file") as HTMLInputElement).files?.length ?? 0).toBe(0);
  });

  it("rejects unsupported file extension without preview call", async () => {
    // user-event 会按 accept 属性静默过滤不匹配文件（与浏览器文件框行为一致），
    // 故此处用 fireEvent 直设 files，测组件自身的扩展名守卫（拖拽/改 accept 的兜底）。
    render(<Knowledge />);
    fireEvent.change(screen.getByTestId("audit-file"), {
      target: { files: [new File(["x"], "note.md")] },
    });
    expect(previewMutate).not.toHaveBeenCalled();
    expect(await screen.findByTestId("audit-file-error")).not.toBeNull();
    expect(text("audit-file-error")).toContain("不支持的文件类型");
  });
});

describe("Knowledge 审核流：扫描件提示", () => {
  it("422 unsupported renders explicit notice (not silent)", async () => {
    const user = userEvent.setup();
    previewMutate.mockImplementation(() => {
      previewState.error = new ApiError("http", "preview 失败", 422, {
        detail: { error: "unsupported", reason: "scanned", message: "扫描件，需 OCR", pages: [0] },
      });
    });
    render(<Knowledge />);
    await user.upload(screen.getByTestId("audit-file"), new File(["zz"], "scan.pdf"));
    // mock 在 mutate 时才写入 error（模拟服务端 422 到达），等它渲染出来。
    await screen.findByTestId("audit-unsupported");
    expect(text("audit-unsupported")).toContain("扫描件");
    expect(text("audit-unsupported")).toContain("扫描件，需 OCR");
    expect(commitMutate).not.toHaveBeenCalled();
  });
});
