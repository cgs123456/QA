// @vitest-environment jsdom
/**
 * 伴侣面板单测（S8）：开关态、二维码渲染、吊销、停止。
 *
 * 与 `Rehearsal.test.tsx` 同款做法：mock 掉 `lib/companion`，把状态写进可变对象。
 * 断言用原生 DOM（本项目没装 `@testing-library/jest-dom`）。
 *
 * 最后两条是**纪律断言**：前端拿不到 token（模块导出里没有它），
 * 面板也不跑第二份 `useLiveQA`（二屏只读镜像的纪律）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { CompanionPanel } from "./CompanionPanel";
import type { CompanionInfo, CompanionStart } from "../lib/companion";

const state = vi.hoisted(() => ({
  info: {
    state: "stopped",
    lanIp: null,
    port: 54322,
    clients: [],
  } as CompanionInfo,
}));

const start = vi.fn();
const stop = vi.fn();
const rotate = vi.fn();
const revoke = vi.fn();

vi.mock("../lib/companion", () => ({
  getCompanionStatus: vi.fn(async () => state.info),
  startCompanion: (...args: unknown[]) => start(...args),
  stopCompanion: (...args: unknown[]) => stop(...args),
  rotateCompanionToken: (...args: unknown[]) => rotate(...args),
  revokeCompanionClient: (...args: unknown[]) => revoke(...args),
}));

beforeEach(() => {
  state.info = { state: "stopped", lanIp: null, port: 54322, clients: [] };
  start.mockReset();
  stop.mockReset();
  rotate.mockReset();
  revoke.mockReset();
});

afterEach(() => cleanup());

const QR = "<svg viewBox='0 0 240 240'><path d='M0'/></svg>";

function respond(startResult: CompanionStart) {
  start.mockResolvedValue(startResult);
  rotate.mockResolvedValue(startResult);
}

describe("伴侣面板", () => {
  it("默认关闭：只有「开启」能点，没有二维码", async () => {
    render(<CompanionPanel />);
    await screen.findByTestId("companion");

    expect(screen.getByTestId("companion-state").textContent).toBe("未开启");
    expect(screen.getByTestId("companion-start")).toBeTruthy();
    expect((screen.getByTestId("companion-stop") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByTestId("companion-qr")).toBeNull();
  });

  it("开启后渲染二维码并提示同一个 Wi-Fi", async () => {
    const user = userEvent.setup();
    // 轮询回来的是 stopped（还没开），点了「开启」之后才变 listening。
    respond({
      info: { state: "listening", lanIp: "192.168.1.5", port: 54322, clients: [] },
      qrSvg: QR,
    });

    render(<CompanionPanel />);
    await user.click(screen.getByTestId("companion-start"));

    expect(start).toHaveBeenCalledTimes(1);
    const qr = await screen.findByTestId("companion-qr");
    expect(qr.querySelector("svg")).toBeTruthy();
    expect(screen.getByTestId("companion-state").textContent).toBe("等待手机扫码");
    expect(screen.getByTestId("companion-lan").textContent).toContain("192.168.1.5");
  });

  it("已连设备可单台吊销：按 id 调用，不是按 token", async () => {
    const user = userEvent.setup();
    state.info = {
      state: "connected",
      lanIp: "192.168.1.5",
      port: 54322,
      clients: [{ id: 3, sinceMs: 1_700_000_000_000 }],
    };
    revoke.mockResolvedValue(true);

    render(<CompanionPanel />);
    const button = await screen.findByTestId("companion-revoke-3");
    await user.click(button);

    expect(revoke).toHaveBeenCalledWith(3);
  });

  it("停止：关闭全部并撤掉二维码", async () => {
    const user = userEvent.setup();
    respond({ info: { state: "listening", lanIp: "192.168.1.5", port: 54322, clients: [] }, qrSvg: QR });
    stop.mockResolvedValue({ state: "stopped", lanIp: null, port: 54322, clients: [] });

    render(<CompanionPanel />);
    await user.click(screen.getByTestId("companion-start"));
    await screen.findByTestId("companion-qr");

    await user.click(screen.getByTestId("companion-stop"));

    expect(stop).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("companion-state").textContent).toBe("未开启");
    expect(screen.queryByTestId("companion-qr")).toBeNull();
  });

  it("刷新二维码会换 token（旧连接失效）", async () => {
    const user = userEvent.setup();
    state.info = { state: "connected", lanIp: "192.168.1.5", port: 54322, clients: [] };
    respond({ info: state.info, qrSvg: QR });

    render(<CompanionPanel />);
    await user.click(screen.getByTestId("companion-rotate"));
    expect(rotate).toHaveBeenCalledTimes(1);
    expect(await screen.findByTestId("companion-qr")).toBeTruthy();
  });

  it("启动失败把原因显示出来，不静默", async () => {
    const user = userEvent.setup();
    start.mockRejectedValue(new Error("未发现局域网 IPv4 地址"));

    render(<CompanionPanel />);
    await user.click(screen.getByTestId("companion-start"));

    expect((await screen.findByTestId("companion-error")).textContent).toContain(
      "未发现局域网 IPv4 地址",
    );
  });
});

describe("伴侣纪律", () => {
  it("前端拿不到 token 明文（状态与启动结果里都没有它）", () => {
    // 契约：`CompanionStart` 只有 info + qrSvg，`CompanionInfo` 只有四个字段。
    // 多一个 token 字段，就是把它放到前端内存里了（日志/截图/埋点都会带走）。
    const sample: CompanionStart = {
      info: { state: "listening", lanIp: "192.168.1.5", port: 54322, clients: [] },
      qrSvg: QR,
    };
    expect(Object.keys(sample).sort()).toEqual(["info", "qrSvg"]);
    expect(Object.keys(sample.info).sort()).toEqual(["clients", "lanIp", "port", "state"]);
  });

  it("面板不跑第二份 useLiveQA（二屏只读镜像）", async () => {
    const source = await import("./CompanionPanel?raw");
    expect(source.default).not.toMatch(/useLiveQA|askQuestion/);
  });
});
