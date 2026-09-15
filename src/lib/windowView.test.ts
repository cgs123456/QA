import { describe, expect, it } from "vitest";
import { VIEW_GLOBAL, VIEW_TELEPROMPTER, readWindowView } from "./windowView";

describe("readWindowView", () => {
  it("没有注入标记时是主窗", () => {
    expect(readWindowView({})).toBe("main");
    expect(readWindowView(undefined)).toBe("main");
    expect(readWindowView(null)).toBe("main");
  });

  it("认得提词窗的注入值", () => {
    expect(readWindowView({ [VIEW_GLOBAL]: VIEW_TELEPROMPTER })).toBe("teleprompter");
  });

  it("值不对就退回主窗，而不是抛错", () => {
    // 认错方向会让主界面变成空透明面板，所以失败一律退回主窗。
    expect(readWindowView({ [VIEW_GLOBAL]: "other" })).toBe("main");
    expect(readWindowView({ [VIEW_GLOBAL]: 1 })).toBe("main");
    expect(readWindowView({ [VIEW_GLOBAL]: null })).toBe("main");
  });

  it("两个常量是与 Rust 侧对齐的契约", () => {
    expect(VIEW_GLOBAL).toBe("__INTERVIEW_COPILOT_VIEW__");
    expect(VIEW_TELEPROMPTER).toBe("teleprompter");
  });
});
