/**
 * 手机伴侣（S8）前端接线：主屏侧的启停/吊销/状态查询 + 卡片广播。
 *
 * 与提词窗（`teleprompter://cards`，同进程事件总线）的区别：
 * - 提词窗：Tauri 事件，主窗 → 本机另一个窗口。
 * - 伴侣屏：跨设备，走 Rust 侧的局域网 WebSocket（`CompanionService`）。
 *
 * 两条纪律：
 * 1. **前端拿不到 token**。启动只回 QR 的 SVG，token 只存在于 QR 编码的 URL 里，
 *    这样它不可能被日志/截图/埋点带出去（见 `docs/companion-security.md` §3）。
 * 2. **二屏只读**。这里只有「推卡片」一个方向，没有任何从手机读数据的入口。
 */
import { invoke } from "@tauri-apps/api/core";

import type { LiveCard } from "./liveqa";

/** 伴侣服务状态（`CompanionState`，serde camelCase）。 */
export type CompanionState = "stopped" | "listening" | "connected";

/** 已连接的一台手机（**不含 token**）。 */
export type CompanionClient = {
  id: number;
  sinceMs: number;
};

/** 主屏可见的服务状态。 */
export type CompanionInfo = {
  state: CompanionState;
  lanIp: string | null;
  port: number;
  clients: CompanionClient[];
};

/** 启动 / 刷新二维码的返回：状态 + QR 的 SVG 源码。 */
export type CompanionStart = {
  info: CompanionInfo;
  qrSvg: string;
};

/** 开启伴侣屏：绑局域网 IP + 生成 token，返回二维码。 */
export function startCompanion(): Promise<CompanionStart> {
  return invoke<CompanionStart>("start_companion");
}

/** 停止：关端口 + 断开全部手机 + 清 token。 */
export function stopCompanion(): Promise<CompanionInfo> {
  return invoke<CompanionInfo>("stop_companion");
}

/** 当前状态（主屏轮询用）。 */
export function getCompanionStatus(): Promise<CompanionInfo> {
  return invoke<CompanionInfo>("get_companion_status");
}

/** 吊销单台设备。 */
export function revokeCompanionClient(clientId: number): Promise<boolean> {
  return invoke<boolean>("revoke_companion_client", { clientId });
}

/** 刷新二维码（换 token，已连的手机立刻失效）。 */
export function rotateCompanionToken(): Promise<CompanionStart> {
  return invoke<CompanionStart>("rotate_companion_token");
}

/**
 * 把卡片推给所有手机。**没有手机连接时是廉价的 no-op**（Rust 侧广播通道无人订阅）。
 * 载荷与 `teleprompter://cards` 完全一致——二屏是同一份数据的镜像，不是第二路检索。
 */
export async function broadcastToCompanion(cards: readonly LiveCard[]): Promise<void> {
  await invoke("broadcast_to_companion", { cards: cards as LiveCard[] });
}
