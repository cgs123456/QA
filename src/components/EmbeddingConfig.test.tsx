// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EmbeddingConfig } from "./EmbeddingConfig";
import type { EmbeddingState } from "../hooks/useEmbedding";

afterEach(() => cleanup());

function text(id: string): string {
  return screen.getByTestId(id).textContent ?? "";
}

function disabled(id: string): boolean {
  return (screen.getByTestId(id) as HTMLButtonElement).disabled;
}

const IDLE: EmbeddingState = {
  active: "local",
  dim: 512,
  table: "vec_qa_local",
  rebuilding: null,
  available: [
    {
      name: "local",
      display: "本地 bge-small-zh-v1.5（512 维，离线）",
      kind: "local",
      dim: 512,
      table: "vec_qa_local",
      needs_key: false,
      note: "默认",
      ready: true,
    },
    {
      name: "cloud",
      display: "OpenAI text-embedding-3-large（3072 维）",
      kind: "cloud",
      dim: 3072,
      table: "vec_qa_cloud",
      needs_key: true,
      note: "按 token 计费",
      ready: false,
    },
  ],
};

const REBUILDING: EmbeddingState = {
  ...IDLE,
  rebuilding: {
    rebuild_id: "r1",
    status: "running",
    from: "local",
    to: "cloud",
    total: 100,
    done: 25,
    tokens: 175,
    error: null,
  },
};

function renderIdle(onSwitch: (n: string) => void = () => {}) {
  return render(
    <EmbeddingConfig
      state={IDLE}
      loadError={null}
      switching={false}
      switchError={null}
      rebuild={null}
      message={null}
      onSwitch={onSwitch}
    />,
  );
}

describe("EmbeddingConfig 渲染", () => {
  it("shows current state and catalog with ready flags", () => {
    renderIdle();
    expect(text("emb-current")).toContain("local");
    expect(text("emb-current")).toContain("512");
    expect(text("emb-current")).toContain("vec_qa_local");
    expect(screen.queryByTestId("emb-rebuilding")).toBeNull();
    // 当前项按钮禁用并标“使用中”，cloud 项可点。
    expect(disabled("emb-select-local")).toBe(true);
    expect(disabled("emb-select-cloud")).toBe(false);
  });

  it("shows load error instead of catalog", () => {
    render(
      <EmbeddingConfig
        state={null}
        loadError="boom"
        switching={false}
        switchError={null}
        rebuild={null}
        message={null}
        onSwitch={() => {}}
      />,
    );
    expect(screen.queryByTestId("emb-current")).toBeNull();
    expect(text("emb-load-error")).toContain("boom");
  });

  it("shows rebuilding progress and degrades buttons", () => {
    render(
      <EmbeddingConfig
        state={REBUILDING}
        loadError={null}
        switching={false}
        switchError={null}
        rebuild={REBUILDING.rebuilding}
        message={null}
        onSwitch={() => {}}
      />,
    );
    // 重建中提示：检索仍走旧向量 + FTS（不断链的可视化）。
    expect(text("emb-rebuilding")).toContain("重建中");
    expect(text("emb-rebuilding")).toContain("旧向量");
    expect(text("emb-progress")).toContain("25 / 100");
    expect(text("emb-progress")).toContain("175");
    expect(disabled("emb-select-local")).toBe(true);
    expect(disabled("emb-select-cloud")).toBe(true);
  });

  it("shows switch error and message", () => {
    render(
      <EmbeddingConfig
        state={IDLE}
        loadError={null}
        switching={false}
        switchError="云端不可达"
        rebuild={null}
        message="已切换"
        onSwitch={() => {}}
      />,
    );
    expect(text("emb-error")).toContain("云端不可达");
    expect(text("emb-message")).toContain("已切换");
  });
});

describe("EmbeddingConfig 切换路径", () => {
  it("clicking switch fires onSwitch with provider name", async () => {
    const user = userEvent.setup();
    const onSwitch = vi.fn();
    renderIdle(onSwitch);
    await user.click(screen.getByTestId("emb-select-cloud"));
    expect(onSwitch).toHaveBeenCalledOnce();
    expect(onSwitch).toHaveBeenCalledWith("cloud");
  });

  it("active provider button is disabled (nothing to switch to)", () => {
    renderIdle(() => {});
    // disabled 由浏览器强制不触发点击，此处只断言禁用态本身。
    expect(disabled("emb-select-local")).toBe(true);
  });
});
