import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import "./App.css";
import { SIDECAR_DEGRADED_EVENT } from "./lib/api";
import { Degraded } from "./pages/Degraded";
import { Knowledge } from "./pages/Knowledge";
import { LiveQA } from "./pages/LiveQA";
import { Search } from "./pages/Search";
import { Settings } from "./pages/Settings";

type SidecarStatus = {
  connected: boolean;
  port: number | null;
};

type DegradedInfo = {
  reason: string;
  detail: string;
};

function App() {
  // 默认停在「手动查找」：1a 的既有行为不动。实时提词是新增入口，不是新默认。
  const [tab, setTab] = useState<"live" | "search" | "knowledge" | "settings">("search");
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
      ) : tab === "knowledge" ? (
        <Knowledge />
      ) : (
        <Settings />
      )}
    </main>
  );
}

export default App;
