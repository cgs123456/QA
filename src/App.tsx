import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import "./App.css";
import { SIDECAR_DEGRADED_EVENT } from "./lib/api";
import { Degraded } from "./pages/Degraded";
import { Knowledge } from "./pages/Knowledge";
import { Search } from "./pages/Search";

type SidecarStatus = {
  connected: boolean;
  port: number | null;
};

type DegradedInfo = {
  reason: string;
  detail: string;
};

function App() {
  const [tab, setTab] = useState<"search" | "knowledge">("search");
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
      </nav>
      {tab === "search" ? <Search /> : <Knowledge />}
    </main>
  );
}

export default App;
