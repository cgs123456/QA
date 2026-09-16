import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import "./App.css";
import { SIDECAR_DEGRADED_EVENT } from "./lib/api";
import { Degraded } from "./pages/Degraded";
import { History } from "./pages/History";
import { Knowledge } from "./pages/Knowledge";
import { LiveQA } from "./pages/LiveQA";
import { Rehearsal } from "./pages/Rehearsal";
import { Search } from "./pages/Search";
import { Settings } from "./pages/Settings";
import type { LiveCard } from "./lib/liveqa";
import { readWindowView, VIEW_GLOBAL } from "./lib/windowView";

type SidecarStatus = {
  connected: boolean;
  port: number | null;
};

type DegradedInfo = {
  reason: string;
  detail: string;
};

function App() {
  // 先读视图模式：提词窗 / 主窗（手机伴侣屏 S8 不在这里 —— 它是 Rust 侧直接
  // 下发给手机浏览器的独立页面，跟这个前端包无关）。
  // 注意：注入的标记是 `window[VIEW_GLOBAL]`，不是 `readWindowView.VIEW_GLOBAL`
  // （后者是函数上的属性，恒为 undefined → 提词窗会被认成主窗，渲染出整个主界面）。
  const injected = (window as unknown as Record<string, unknown>)[VIEW_GLOBAL];
  const view = readWindowView(injected as Record<string, unknown> | undefined);

  // 提词窗：只渲染 Teleprompter（由 LiveQA 通过事件推送卡片），不挂载主 UI
  if (view === "teleprompter") {
    return <TeleprompterWrapper />;
  }

  // 主窗：原有逻辑
  // 默认停在「手动查找」：1a 的既有行为不动。实时提词是新增入口，不是新默认。
  const [tab, setTab] = useState<
    "live" | "search" | "history" | "rehearsal" | "knowledge" | "settings"
  >("search");
  const [status, setStatus] = useState<SidecarStatus | null>(null);
  const [degraded, setDegraded] = useState<DegradedInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retryMessage, setRetryMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const [s, d] = await Promise.all([
          invoke<SidecarStatus>("get_sidecar_status"),
          invoke<DegradedInfo | null>("get_sidecar_degraded"),
        ]);
        if (!cancelled) {
          setStatus(s);
          setDegraded(d);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    }
    poll();
    const timer = setInterval(poll, 2000);
    let unlisten: (() => void) | undefined;
    listen<DegradedInfo>(SIDECAR_DEGRADED_EVENT, (event) => {
      if (!cancelled) setDegraded(event.payload);
    })
      .then((fn) => {
        unlisten = fn;
      })
      .catch(() => {});
    return () => {
      cancelled = true;
      clearInterval(timer);
      unlisten?.();
    };
  }, []);

  async function retry() {
    try {
      const msg = await invoke<string>("retry_sidecar_start");
      setRetryMessage(msg);
    } catch (e) {
      setRetryMessage(String(e));
    }
  }

  if (degraded != null) {
    return (
      <Degraded
        reason={degraded.reason}
        detail={degraded.detail}
        onRetry={retry}
        retryMessage={retryMessage}
        onViewLog={() => invoke<string>("sidecar_log_tail", { lines: 200 })}
      />
    );
  }

  const connected = status?.connected ?? false;

  return (
    <main className="container">
      <h1>InterviewCopilot</h1>
      <p data-testid="sidecar-status">
        sidecar {connected ? "connected" : "disconnected"}
        {connected && status?.port != null ? ` (port ${status.port})` : ""}
      </p>
      {error != null && <p data-testid="sidecar-error">{error}</p>}
      <nav className="row">
        <button type="button" onClick={() => setTab("live")} disabled={tab === "live"}>
          实时提词
        </button>
        <button type="button" onClick={() => setTab("search")} disabled={tab === "search"}>
          手动查找
        </button>
        <button
          type="button"
          onClick={() => setTab("history")}
          disabled={tab === "history"}
        >
          历史会话
        </button>
        <button
          type="button"
          onClick={() => setTab("rehearsal")}
          disabled={tab === "rehearsal"}
        >
          面试陪练
        </button>
        <button
          type="button"
          onClick={() => setTab("knowledge")}
          disabled={tab === "knowledge"}
        >
          知识管理
        </button>
        <button
          type="button"
          onClick={() => setTab("settings")}
          disabled={tab === "settings"}
        >
          设置
        </button>
      </nav>
      {tab === "live" ? (
        <LiveQA />
      ) : tab === "search" ? (
        <Search />
      ) : tab === "history" ? (
        <History onOpenSettings={() => setTab("settings")} />
      ) : tab === "rehearsal" ? (
        <Rehearsal />
      ) : tab === "knowledge" ? (
        <Knowledge />
      ) : (
        <Settings />
      )}
    </main>
  );
}

// 提词窗包装器：只订阅 teleprompter://cards 事件渲染卡片。
function TeleprompterWrapper() {
  const [cards, setCards] = useState<LiveCard[]>([]);

  useEffect(() => {
    let unlisten: (() => void) | undefined;
    listen<LiveCard[]>("teleprompter://cards", (event) => {
      setCards(event.payload);
    }).then((fn) => {
      unlisten = fn;
    });
    return () => unlisten?.();
  }, []);

  return (
    <main className="container teleprompter-wrapper">
      <Teleprompter cards={cards} />
    </main>
  );
}

import { Teleprompter } from "./components/Teleprompter";

export default App;
