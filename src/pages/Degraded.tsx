import { useState } from "react";

export type DegradedProps = {
  reason: string;
  detail: string;
  onRetry: () => void;
  retryMessage: string | null;
  onViewLog: () => Promise<string>;
};

/**
 * 降级页完整版（R7）：错误原因 + 查看日志入口 + 重试按钮，禁止白屏。
 * 日志来自 sidecar stderr 落盘（Rust 侧采集，content-free：无 token/问答原文）。
 */
export function Degraded({ reason, detail, onRetry, retryMessage, onViewLog }: DegradedProps) {
  const [logText, setLogText] = useState<string | null>(null);
  const [logError, setLogError] = useState<string | null>(null);
  const [loadingLog, setLoadingLog] = useState(false);

  async function viewLog() {
    if (logText != null) {
      setLogText(null);
      return;
    }
    setLoadingLog(true);
    setLogError(null);
    try {
      setLogText(await onViewLog());
    } catch (e) {
      setLogError(String(e));
    } finally {
      setLoadingLog(false);
    }
  }

  return (
    <main className="container">
      <h1>Sidecar 不可用</h1>
      <p data-testid="degraded-reason">原因：{reason}</p>
      <p data-testid="degraded-detail">{detail}</p>
      <div className="row">
        <button type="button" onClick={onRetry}>
          重试启动
        </button>
        <button type="button" onClick={viewLog} disabled={loadingLog}>
          {logText != null ? "隐藏日志" : "查看日志"}
        </button>
      </div>
      {retryMessage != null && <p data-testid="degraded-retry">{retryMessage}</p>}
      {logError != null && <p>日志读取失败：{logError}</p>}
      {logText != null && <pre data-testid="degraded-log">{logText}</pre>}
    </main>
  );
}
