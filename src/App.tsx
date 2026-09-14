import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import "./App.css";

type SidecarStatus = {
  connected: boolean;
  port: number | null;
};

function App() {
  const [status, setStatus] = useState<SidecarStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const s = await invoke<SidecarStatus>("get_sidecar_status");
        if (!cancelled) {
          setStatus(s);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    }
    poll();
    const timer = setInterval(poll, 2000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const connected = status?.connected ?? false;

  return (
    <main className="container">
      <h1>InterviewCopilot (skeleton)</h1>
      <p data-testid="sidecar-status">
        sidecar {connected ? "connected" : "disconnected"}
        {connected && status?.port != null ? ` (port ${status.port})` : ""}
      </p>
      {error != null && <p data-testid="sidecar-error">{error}</p>}
      {status == null && error == null && <p>waiting for sidecar…</p>}
    </main>
  );
}

export default App;
