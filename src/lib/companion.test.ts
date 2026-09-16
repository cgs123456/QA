/**
 * 伴侣前端接线单测（S8）。
 *
 * 测的是「命令名 + 参数形状」这条契约：Rust 侧改了命令名或字段，这页会红。
 * 真正的连线行为由 `tests/companion_test.rs` 用真实 WS 客户端覆盖。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { LiveCard } from "./liveqa";

const invoke = vi.fn();

vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invoke(...args),
}));

import {
  broadcastToCompanion,
  getCompanionStatus,
  revokeCompanionClient,
  rotateCompanionToken,
  startCompanion,
  stopCompanion,
} from "./companion";

beforeEach(() => invoke.mockReset());

const card: LiveCard = {
  id: "c1",
  question: "发货周期是多久？",
  answer: "三天内",
  source: "字段直查",
  kind: "direct",
  atMs: 1_700_000_000_000,
  reused: false,
};

describe("伴侣命令接线", () => {
  it("启动/停止/状态/吊销/刷新 走的是各自那条命令", async () => {
    invoke.mockResolvedValue({ info: { state: "listening" }, qrSvg: "<svg/>" });

    await startCompanion();
    await stopCompanion();
    await getCompanionStatus();
    await rotateCompanionToken();

    expect(invoke.mock.calls.map((c) => c[0])).toEqual([
      "start_companion",
      "stop_companion",
      "get_companion_status",
      "rotate_companion_token",
    ]);
  });

  it("吊销按 clientId 传参（不下发 token）", async () => {
    invoke.mockResolvedValue(true);
    await expect(revokeCompanionClient(7)).resolves.toBe(true);
    expect(invoke).toHaveBeenCalledWith("revoke_companion_client", { clientId: 7 });
  });

  it("广播的载荷就是 teleprompter 那份卡片（camelCase atMs）", async () => {
    invoke.mockResolvedValue(undefined);
    await broadcastToCompanion([card]);

    expect(invoke).toHaveBeenCalledWith("broadcast_to_companion", { cards: [card] });
    const payload = invoke.mock.calls[0][1] as { cards: LiveCard[] };
    expect(payload.cards[0].atMs).toBe(1_700_000_000_000);
  });
});
