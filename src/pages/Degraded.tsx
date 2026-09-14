import { useState } from "react";

export type DegradedProps = {
  reason: string;
  detail: string;
  onRetry: () => void;
  retryMessage: string | null;
};

/**
 * Placeholder degraded page (full version: D10).
 * R7 contract: reason + view-logs entry + retry, never a blank screen.
 */
export function Degraded({ reason, detail, onRetry, retryMessage }: DegradedProps) {
  const [showLogHint, setShowLogHint] = useState(false);

  return (
    <main className="container">
      <h1>Sidecar 不可用（占位降级页，完整版 D10）</h1>
      <p data-testid="degraded-reason">原因：{reason}</p>
      <p data-testid="degraded-detail">{detail}</p>
      <div className="row">
        <button type="button" onClick={onRetry}>
          重试启动
        </button>
        <button type="button" onClick={() => setShowLogHint((v) => !v)}>
          查看日志
        </button>
      </div>
      {retryMessage != null && <p data-testid="degraded-retry">{retryMessage}</p>}
      {showLogHint && (
        <p>日志查看器为完整版（D10）；当前请查看终端 / WebView 控制台输出。</p>
      )}
    </main>
  );
}
