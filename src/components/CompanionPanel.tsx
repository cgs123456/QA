import { useCallback, useEffect, useState } from "react";

import {
  getCompanionStatus,
  revokeCompanionClient,
  rotateCompanionToken,
  startCompanion,
  stopCompanion,
  type CompanionInfo,
  type CompanionStart,
} from "../lib/companion";

/** 状态轮询间隔：手机什么时候连上，服务端比主屏先知道。 */
const POLL_MS = 2000;

const STATE_TEXT: Record<CompanionInfo["state"], string> = {
  stopped: "未开启",
  listening: "等待手机扫码",
  connected: "手机已连接",
};

/**
 * 伴侣面板（S8）：主屏侧的开关 + 二维码 + 已连设备列表。
 *
 * 三条纪律：
 * 1. **前端不碰 token** —— 只能拿到 QR 的 SVG，所以这里没有「复制连接地址」这种按钮。
 * 2. **默认关闭** —— 不点「开启伴侣屏」就不监听任何端口。
 * 3. **随时可吊销** —— 单台「断开」/ 全部「停止」/「刷新二维码」换 token 三档。
 *
 * 这个面板**不订阅卡片、不碰实时会话**：推卡片是实时会话那一路的事
 * （本文件刻意不 import 它，有测试钉住），这里只管连接本身。
 */
export function CompanionPanel() {
  const [info, setInfo] = useState<CompanionInfo | null>(null);
  const [qrSvg, setQrSvg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setInfo(await getCompanionStatus());
    } catch {
      // 轮询失败不弹错：伴侣是可选能力，主界面不该因为它闪红。
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  async function run(job: () => Promise<CompanionStart | CompanionInfo | boolean>) {
    setBusy(true);
    setError(null);
    try {
      const result = await job();
      if (typeof result === "object" && result !== null && "qrSvg" in result) {
        setQrSvg(result.qrSvg);
        setInfo(result.info);
      } else if (typeof result === "object" && result !== null) {
        setInfo(result);
        setQrSvg(null);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const state = info?.state ?? "stopped";

  return (
    <section data-testid="companion" className="companion-panel">
      <h3>手机伴侣（局域网）</h3>

      <div className="row">
        <button
          type="button"
          data-testid="companion-start"
          disabled={busy || state !== "stopped"}
          onClick={() => void run(startCompanion)}
        >
          开启伴侣屏
        </button>
        <button
          type="button"
          data-testid="companion-stop"
          disabled={busy || state === "stopped"}
          onClick={() => void run(stopCompanion)}
        >
          停止
        </button>
        <button
          type="button"
          data-testid="companion-rotate"
          disabled={busy || state === "stopped"}
          onClick={() => void run(rotateCompanionToken)}
        >
          刷新二维码
        </button>
      </div>

      <p data-testid="companion-state">{STATE_TEXT[state]}</p>
      {info?.lanIp != null && state !== "stopped" && (
        <p data-testid="companion-lan">
          手机请连接同一个 Wi-Fi，扫码访问 {info.lanIp}:{info.port}
        </p>
      )}
      {error != null && <p data-testid="companion-error">伴侣服务出错：{error}</p>}

      {qrSvg != null && state !== "stopped" && (
        // SVG 由 Rust `qrcode` 现场生成（无外部输入），不是用户提供的 HTML。
        <div data-testid="companion-qr" dangerouslySetInnerHTML={{ __html: qrSvg }} />
      )}

      {info != null && info.clients.length > 0 && (
        <ul data-testid="companion-clients">
          {info.clients.map((client) => (
            <li key={client.id}>
              <span data-testid={`companion-client-${client.id}`}>设备 #{client.id}</span>{" "}
              <button
                type="button"
                data-testid={`companion-revoke-${client.id}`}
                disabled={busy}
                onClick={() => void run(() => revokeCompanionClient(client.id))}
              >
                断开
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
