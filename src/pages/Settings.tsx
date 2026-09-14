import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { apiGet, apiPost } from "../lib/api";

const PROVIDERS = ["ollama", "openai", "custom"] as const;
const PROVIDER_STORAGE_KEY = "interview-copilot.llm-provider";

type DownloadFile = { downloaded: number; total: number | null; done: boolean };
type DownloadStatus = {
  download_id: string;
  status: "queued" | "downloading" | "done" | "error";
  files: Record<string, DownloadFile>;
  error: string | null;
};

function formatBytes(n: number | null): string {
  if (n == null || n < 0) return "?";
  return `${(n / 1048576).toFixed(1)} MiB`;
}

export function Settings() {
  const [provider, setProvider] = useState<string>(
    () => window.localStorage.getItem(PROVIDER_STORAGE_KEY) ?? "ollama",
  );
  const [apiKey, setApiKey] = useState("");
  const [keyMessage, setKeyMessage] = useState<string | null>(null);
  const [savingKey, setSavingKey] = useState(false);
  const [downloadId, setDownloadId] = useState<string | null>(null);
  const [download, setDownload] = useState<DownloadStatus | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const timerRef = useRef<number | undefined>(undefined);

  useEffect(() => {
    window.localStorage.setItem(PROVIDER_STORAGE_KEY, provider);
  }, [provider]);

  useEffect(() => () => window.clearInterval(timerRef.current), []);

  async function saveKey(e: React.FormEvent) {
    e.preventDefault();
    const key = apiKey.trim();
    if (!key) {
      setKeyMessage("API Key 不能为空");
      return;
    }
    setSavingKey(true);
    try {
      // Key 经 Tauri invoke 直写 OS keychain；前端不保留、不回显（R8）。
      await invoke<string>("save_api_key", { provider, apiKey: key });
      setApiKey("");
      setKeyMessage(`已保存到系统 keychain（${provider}）`);
    } catch (err) {
      setKeyMessage(`保存失败：${String(err)}`);
    } finally {
      setSavingKey(false);
    }
  }

  async function pollDownload(id: string) {
    window.clearInterval(timerRef.current);
    const tick = async () => {
      try {
        const status = await apiGet<DownloadStatus>(`/model/download/${id}`);
        setDownload(status);
        if (status.status === "done" || status.status === "error") {
          window.clearInterval(timerRef.current);
        }
      } catch (err) {
        setDownloadError(String(err));
        window.clearInterval(timerRef.current);
      }
    };
    await tick();
    timerRef.current = window.setInterval(tick, 1000);
  }

  async function startDownload() {
    setDownload(null);
    setDownloadError(null);
    try {
      const { download_id } = await apiPost<{ download_id: string }>(
        "/model/download",
        {},
      );
      setDownloadId(download_id);
      await pollDownload(download_id);
    } catch (err) {
      setDownloadError(String(err));
    }
  }

  return (
    <div>
      <h2>设置</h2>

      <div>
        <h3>LLM Provider</h3>
        <label>
          Provider：
          <select
            data-testid="provider-select"
            value={provider}
            onChange={(e) => setProvider(e.currentTarget.value)}
          >
            {PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        {provider === "ollama" && <p>本地 Ollama，无需 API Key。</p>}
      </div>

      <div>
        <h3>API Key（写入系统 keychain）</h3>
        <form className="row" onSubmit={saveKey}>
          <input
            data-testid="apikey-input"
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.currentTarget.value)}
            placeholder={`${provider} 的 API Key`}
            autoComplete="off"
          />
          <button data-testid="apikey-save" type="submit" disabled={savingKey}>
            {savingKey ? "保存中…" : "保存"}
          </button>
        </form>
        {keyMessage != null && <p data-testid="apikey-message">{keyMessage}</p>}
      </div>

      <div>
        <h3>模型下载（bge-small-zh-v1.5）</h3>
        <button data-testid="model-download" type="button" onClick={startDownload}>
          下载/校验模型
        </button>
        {downloadError != null && <p>下载失败：{downloadError}</p>}
        {download != null && (
          <div>
            <p data-testid="download-status">
              状态：{download.status}
              {downloadId != null ? `（${downloadId}）` : ""}
            </p>
            <ul>
              {Object.entries(download.files).map(([name, f]) => (
                <li key={name}>
                  {name}：{formatBytes(f.downloaded)} / {formatBytes(f.total)}
                  {f.done ? " ✓" : ""}
                </li>
              ))}
            </ul>
            {download.status === "error" && <p>错误：{download.error}</p>}
          </div>
        )}
      </div>
    </div>
  );
}
