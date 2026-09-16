// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup, render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";

import { TemplateWizard } from "./TemplateWizard";
import type { TemplateSummary } from "../lib/api";

/**
 * 模板向导（S7）的三步流程：选模板 → 起名创建 / 自定义模板 → 返回。
 *
 * 两条纪律：
 * 1. **不用 jest-dom 匹配器**（`toBeInTheDocument` / `toBeDisabled`）——
 *    `@testing-library/jest-dom` 没装，用了就是 `xxx is not a function`。
 *    断言改写成 `expect(el).toBeTruthy()` 与 `expect((el as HTMLButtonElement).disabled)`。
 * 2. **每个用例结束后 cleanup** —— 不清理的话上一个用例的 DOM 还挂着，
 *    `getByText` 会撞到多个匹配（"找到多个元素"而不是找不到）。
 */

type Props = ComponentProps<typeof TemplateWizard>;

const TEMPLATES: TemplateSummary[] = [
  {
    id: "template_interview",
    name: "面试题库模板",
    description: "求职面试准备标准题库",
    category: "interview",
    version: 1,
    is_preset: true,
    created_at: "2026-09-16T00:00:00",
    updated_at: "2026-09-16T00:00:00",
  },
  {
    id: "template_knowledge",
    name: "企业知识库模板",
    description: "企业内部知识管理标准模板",
    category: "knowledge",
    version: 1,
    is_preset: true,
    created_at: "2026-09-16T00:00:00",
    updated_at: "2026-09-16T00:00:00",
  },
];

/** 每个用例发一套新 mock —— 共享 mock 会让"被调用过"这类断言互相污染。 */
function makeProps(over: Partial<Props> = {}): Props {
  return {
    templates: TEMPLATES,
    selectedTemplateId: "",
    onTemplateSelect: vi.fn(),
    newStoreName: "",
    onStoreNameChange: vi.fn(),
    onCreateFromTemplate: vi.fn().mockResolvedValue(undefined),
    onCreateCustomTemplate: vi.fn().mockResolvedValue(undefined),
    onClose: vi.fn(),
    templateError: null,
    saving: false,
    ...over,
  };
}

function button(text: string): HTMLButtonElement {
  return screen.getByText(text) as HTMLButtonElement;
}

describe("TemplateWizard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });
  afterEach(() => cleanup());

  it("渲染模板选择步骤：标题 + 按分类列出的模板", () => {
    render(<TemplateWizard {...makeProps()} />);
    expect(screen.getByText("从模板创建知识库")).toBeTruthy();
    // 分类标题与模板名都要在（模板按 category 分组渲染）。
    expect(screen.getByText("面试题库")).toBeTruthy();
    expect(screen.getByText("企业知识库")).toBeTruthy();
    expect(screen.getByText("面试题库模板")).toBeTruthy();
    expect(screen.getByText("企业知识库模板")).toBeTruthy();
  });

  it("点击模板卡片把模板 id 传出去", () => {
    const props = makeProps();
    render(<TemplateWizard {...props} />);
    fireEvent.click(screen.getByText("面试题库模板").closest("label")!);
    expect(props.onTemplateSelect).toHaveBeenCalledWith("template_interview");
  });

  it("选中模板后出现起名输入框，名字为空时创建按钮禁用", () => {
    render(<TemplateWizard {...makeProps({ selectedTemplateId: "template_interview" })} />);
    expect(screen.getByPlaceholderText("如：我的面试题库")).toBeTruthy();
    expect(button("创建知识库").disabled).toBe(true);
  });

  it("改名字走 onStoreNameChange（受控输入框，不能丢事件）", () => {
    const props = makeProps({ selectedTemplateId: "template_interview" });
    render(<TemplateWizard {...props} />);
    fireEvent.change(screen.getByPlaceholderText("如：我的面试题库"), {
      target: { value: "我的面试题库" },
    });
    expect(props.onStoreNameChange).toHaveBeenCalledWith("我的面试题库");
  });

  it("填了名字就能创建：把 (模板 id, 知识库名) 传出去", () => {
    const props = makeProps({
      selectedTemplateId: "template_interview",
      newStoreName: "我的面试题库",
    });
    render(<TemplateWizard {...props} />);
    const create = button("创建知识库");
    expect(create.disabled).toBe(false);
    fireEvent.click(create);
    expect(props.onCreateFromTemplate).toHaveBeenCalledWith(
      "template_interview",
      "我的面试题库",
    );
  });

  it("创建中：按钮文案变「创建中…」并禁用", () => {
    render(
      <TemplateWizard
        {...makeProps({
          selectedTemplateId: "template_interview",
          newStoreName: "我的面试题库",
          saving: true,
        })}
      />,
    );
    const create = button("创建中…");
    expect(create.disabled).toBe(true);
  });

  it("取消触发 onClose", () => {
    const props = makeProps({ selectedTemplateId: "template_interview" });
    render(<TemplateWizard {...props} />);
    fireEvent.click(button("取消"));
    expect(props.onClose).toHaveBeenCalled();
  });

  it("点「+ 创建自定义模板」进入自定义步骤", () => {
    render(<TemplateWizard {...makeProps()} />);
    fireEvent.click(button("+ 创建自定义模板"));
    expect(screen.getByText("创建自定义模板")).toBeTruthy();
  });

  it("自定义模板：名称为空时保存禁用，填了才启用", () => {
    render(<TemplateWizard {...makeProps()} />);
    fireEvent.click(button("+ 创建自定义模板"));
    expect(button("保存模板").disabled).toBe(true);
    fireEvent.change(screen.getByPlaceholderText("如：我的面试模板 v2"), {
      target: { value: "我的模板" },
    });
    expect(button("保存模板").disabled).toBe(false);
  });

  it("自定义模板：问答对 JSON 非法时报错且不调用后端", async () => {
    const props = makeProps();
    render(<TemplateWizard {...props} />);
    fireEvent.click(button("+ 创建自定义模板"));
    fireEvent.change(screen.getByPlaceholderText("如：我的面试模板 v2"), {
      target: { value: "测试模板" },
    });
    fireEvent.change(screen.getByPlaceholderText(/standard_question/), {
      target: { value: "{invalid json" },
    });
    fireEvent.click(button("保存模板"));

    await waitFor(() => expect(screen.getByText("问答对 JSON 格式错误")).toBeTruthy());
    expect(props.onCreateCustomTemplate).not.toHaveBeenCalled();
  });

  it("自定义模板：合法输入调 onCreateCustomTemplate 并回到选择步骤", async () => {
    const props = makeProps();
    render(<TemplateWizard {...props} />);
    fireEvent.click(button("+ 创建自定义模板"));
    fireEvent.change(screen.getByPlaceholderText("如：我的面试模板 v2"), {
      target: { value: "测试模板" },
    });
    fireEvent.change(screen.getByPlaceholderText(/standard_question/), {
      target: { value: '[{"standard_question":"问","official_answer":"答"}]' },
    });
    fireEvent.click(button("保存模板"));

    await waitFor(() => expect(props.onCreateCustomTemplate).toHaveBeenCalled());
    const args = vi.mocked(props.onCreateCustomTemplate).mock.calls[0] as unknown[];
    expect(args[0]).toBe("测试模板");
    expect(args[3]).toEqual({
      qa_pairs: [{ standard_question: "问", official_answer: "答" }],
    });
    await waitFor(() => expect(screen.getByText("从模板创建知识库")).toBeTruthy());
  });

  it("自定义模板：点「返回」回到选择步骤", () => {
    render(<TemplateWizard {...makeProps()} />);
    fireEvent.click(button("+ 创建自定义模板"));
    fireEvent.click(button("返回"));
    expect(screen.getByText("从模板创建知识库")).toBeTruthy();
  });
});
