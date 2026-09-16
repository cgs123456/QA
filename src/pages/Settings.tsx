import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { ApiError, apiGet, apiPost, apiPut } from "../lib/api";
import { EmbeddingConfig } from "../components/EmbeddingConfig";
import { useEmbeddingProvider, useRebuildStatus, useSwitchEmbedding } from "../hooks/useEmbedding";
import {
  EVT_CAPTURE_STATE,
  diagnosticsRows,
  getCaptureState,
  pathLabel,
  pathStatusLabel,
  selfCheckSummary,
  sidecarCaptureSummary,
  type CaptureState,
  type SidecarCapture,
} from "../lib/capture";

// F6.1 全量：六 provider（密钥全部复用 save_api_key → POST /settings/llm-secret，
// secret_slot 即 provider 名，ollama 除外无需 key）。
const PROVIDERS = ["ollama", "openai", "claude", "gemini", "groq", "custom"] as const;
const PROVIDER_STORAGE_KEY = "interview-copilot.llm-provider";
// bge 仍是向量检索的权重，与 ASR 模型走同一套下载通道（POST /model/download）。
const BGE_MODEL = "bge-small-zh-v1.5";

type DownloadFile = { downloaded: number; total: number | null; done: boolean };
type DownloadStatus = {
  download_id: string;
  status: "queued" | "downloading" | "done" | "error";
  files: Record<string, DownloadFile>;
  error: string | null;
};

type AsrEntry = {
  name: string;
  display: string;
  kind: "local" | "cloud";
  model: string | null;
  note: string;
  ready: boolean;
};

type AsrCatalog = {
  selected: string;
  spec: { name: string; display: string; kind: string; model: string | null };
  ready: boolean;
  with_chain: boolean;
  available: AsrEntry[];
};

type LatencyState = {
  enabled: boolean;
  cuda: boolean;
  note: string;
};

type AudioDiagnostics = {
  last_connection: Record<string, number> | null;
  limits: { max_pending: number; max_segment_frames: number; send_timeout_s: number };
  capture: SidecarCapture;
};

type DegradeChain = { total: number; by_kind: Record<string, number>; by_provider: Record<string, number> };

type DegradeDiagnostics = {
  chains: { asr: DegradeChain; llm: DegradeChain; embedding: DegradeChain };
};

/**
 * taskP7：开机自启开关。
 *
 * `available=false` 表示**这次读数不可信**（插件没就绪 / 平台不支持 / 读写失败），
 * 而不是「已关闭」—— 所以它只用来加警告样式，**不用来禁用输入**：
 * 禁用了用户连重试都做不到。
 */
type AutostartState = {
  enabled: boolean;
  available: boolean;
  note: string;
};

function formatBytes(n: number | null): string {
  if (n == null || n < 0) return "?";
  return `${(n / 1048576).toFixed(1)} MiB`;
}

function DownloadProgress({ dl }: { dl: DownloadStatus }) {
  return (
    <div>
      <p data-testid="download-status">
        状态：{dl.status}（{dl.download_id}）
      </p>
      <ul>
        {Object.entries(dl.files).map(([name, f]) => (
          <li key={name}>
            {name}：{formatBytes(f.downloaded)} / {formatBytes(f.total)}
            {f.done ? " ✓" : ""}
          </li>
        ))}
      </ul>
      {dl.status === "error" && <p>错误：{dl.error}</p>}
    </div>
  );
}

export function Settings() {
  const [provider, setProvider] = useState<string>(
    () => window.localStorage.getItem(PROVIDER_STORAGE_KEY) ?? "ollama",
  );
  const [apiKey, setApiKey] = useState("");
  const [keyMessage, setKeyMessage] = useState<string | null>(null);
  const [savingKey, setSavingKey] = useState(false);

  // ---- ASR Provider（F6.2） ----
  const [asr, setAsr] = useState<AsrCatalog | null>(null);
  const [asrLoadError, setAsrLoadError] = useState<string | null>(null);
  const [switching, setSwitching] = useState(false);
  const [asrMessage, setAsrMessage] = useState<string | null>(null);

  // ---- 低延迟模式（task18：需 CUDA，无 GPU 时开关禁用） ----
  const [latency, setLatency] = useState<LatencyState | null>(null);
  const [latencyMessage, setLatencyMessage] = useState<string | null>(null);
  const [latencyBusy, setLatencyBusy] = useState(false);

  // ---- 向量检索（F5.3/F6.5 双 embedding：切换起后台重建，完成后原子生效） ----
  const embQuery = useEmbeddingProvider();
  const switchEmb = useSwitchEmbedding();
  const [embMessage, setEmbMessage] = useState<string | null>(null);
  const [embSwitchError, setEmbSwitchError] = useState<string | null>(null);
  const embRebuildingId = embQuery.data?.rebuilding?.rebuild_id ?? null;
  const embRebuildQuery = useRebuildStatus(embRebuildingId, embRebuildingId != null);

  // ---- 音频诊断（task19：最近一次 WS 连接计数 + 限额；设备名待 Rust 接线） ----
  const [diag, setDiag] = useState<AudioDiagnostics | null>(null);
  const [diagError, setDiagError] = useState<string | null>(null);

  // ---- 降级计数（P8：ASR/LLM/embedding 三链统一，纯计数 R14） ----
  const [degrade, setDegrade] = useState<DegradeDiagnostics | null>(null);
  const [degradeError, setDegradeError] = useState<string | null>(null);

  // ---- 采集自检（M2-7：Rust 侧真值；开流 500ms 探测的格式/权限/帧率） ----
  const [capture, setCapture] = useState<CaptureState | null>(null);
  const [captureError, setCaptureError] = useState<string | null>(null);

  // ---- 开机自启（taskP7：tauri-plugin-autostart） ----
  const [autostart, setAutostart] = useState<AutostartState | null>(null);
  const [autostartBusy, setAutostartBusy] = useState(false);

  async function refreshDiag() {
    setDiagError(null);
    setDegradeError(null);
    setCaptureError(null);
    try {
      setDiag(await apiGet<AudioDiagnostics>("/diagnostics/audio"));
    } catch (err) {
      setDiagError(String(err));
    }
    // 降级计数独立拉取：音频诊断挂了也不影响降级计数可见。
    try {
      setDegrade(await apiGet<DegradeDiagnostics>("/diagnostics/degrade"));
    } catch (err) {
      setDegradeError(String(err));
    }
    // 采集状态是本地命令，与 sidecar 端点分开 catch：sidecar 挂了也要能看采集诊断。
    try {
      setCapture(await getCaptureState());
    } catch (err) {
      setCaptureError(String(err));
    }
  }

  // ---- 模型下载（按模型键区分；断点续传由 sidecar downloader 保证） ----
  const [downloads, setDownloads] = useState<Record<string, DownloadStatus>>({});
  const [downloadErrors, setDownloadErrors] = useState<Record<string, string>>({});
  const timersRef = useRef<Record<string, number>>({});

  useEffect(() => {
    window.localStorage.setItem(PROVIDER_STORAGE_KEY, provider);
  }, [provider]);

  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      for (const t of Object.values(timers)) window.clearInterval(t);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    apiGet<AsrCatalog>("/asr/providers")
      .then((catalog) => {
        if (!cancelled) setAsr(catalog);
      })
      .catch((err) => {
        if (!cancelled) {
          setAsrLoadError(
            err instanceof ApiError && err.status === 404
              ? "当前 sidecar 不支持 ASR 切换（需更新 sidecar）"
              : String(err),
          );
        }
      });
    apiGet<LatencyState>("/asr/latency")
      .then((st) => {
        if (!cancelled) setLatency(st);
      })
      .catch(() => {
        // 旧 sidecar 无此端点：开关区隐藏（asr 目录 404 提示已覆盖需升级）。
        if (!cancelled) setLatency(null);
      });
    apiGet<AudioDiagnostics>("/diagnostics/audio")
      .then((d) => {
        if (!cancelled) setDiag(d);
      })
      .catch((err) => {
        if (!cancelled) setDiagError(String(err));
      });
    apiGet<DegradeDiagnostics>("/diagnostics/degrade")
      .then((d) => {
        if (!cancelled) setDegrade(d);
      })
      .catch((err) => {
        // 旧 sidecar 无此端点：降级计数区隐藏（404 即老版本）。
        if (!cancelled) {
          if (err instanceof ApiError && err.status === 404) setDegrade(null);
          else setDegradeError(String(err));
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // 采集自检：先拉一次快照（可能快捷键已经把采集跑起来了），再订阅回推。
  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | undefined;
    getCaptureState()
      .then((snap) => {
        if (!cancelled) setCapture(snap);
      })
      .catch((err) => {
        // 非 Tauri 环境（vitest / 浏览器直开）没有命令通道：面板隐藏而不是报错。
        if (!cancelled) setCaptureError(String(err));
      });
    listen<CaptureState>(EVT_CAPTURE_STATE, (event) => {
      if (!cancelled && event.payload != null) setCapture(event.payload);
    })
      .then((fn) => {
        if (cancelled) fn();
        else unlisten = fn;
      })
      .catch(() => {
        // 同上：没有事件总线时静默降级。
      });
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);

  // 开机自启：拉一次当前状态。命令读不到（非 Tauri 环境）就整块隐藏，
  // 而不是画一个点了没反应的开关。
  useEffect(() => {
    let cancelled = false;
    invoke<AutostartState>("get_autostart")
      .then((st) => {
        if (!cancelled) setAutostart(st);
      })
      .catch(() => {
        if (!cancelled) setAutostart(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function setAutostartEnabled(enabled: boolean) {
    setAutostartBusy(true);
    try {
      // 写失败不致命：Rust 侧把原因原样放回 note（应用其余功能照常）。
      setAutostart(await invoke<AutostartState>("set_autostart", { enabled }));
    } catch (e) {
      setAutostart((s) => (s == null ? s : { ...s, available: false, note: `设置失败：${String(e)}` }));
    } finally {
      setAutostartBusy(false);
    }
  }

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

  async function switchAsrProvider(name: string) {
    if (asr != null && name === asr.selected) return;
    setSwitching(true);
    setAsrMessage(null);
    try {
      // 切换只换 sidecar 进程内引用（权重懒加载），对下一段音频即时生效，无需重启。
      const catalog = await apiPut<AsrCatalog>("/asr/provider", { name });
      setAsr(catalog);
      const entry = catalog.available.find((e) => e.name === catalog.selected);
      setAsrMessage(
        entry != null && !entry.ready && entry.kind === "local"
          ? `已切换到 ${entry.display}，即时生效；权重未下载（见下方），未就绪前该级会降级。`
          : `已切换到 ${catalog.spec.display}，即时生效（下一段音频起用）。`,
      );
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setAsrMessage("切换失败：云端 ASR 需要先在上方保存 API Key（openai 槽位）。");
      } else {
        setAsrMessage(`切换失败：${String(err)}`);
      }
    } finally {
      setSwitching(false);
    }
  }

  /** 409 响应体里的服务端 message（`{"detail":{"error","message"}}`），取不到则回退原文。 */
  function embErrorText(err: unknown): string {
    if (err instanceof ApiError && typeof err.payload === "object" && err.payload != null) {
      const detail = (err.payload as { detail?: unknown }).detail as
        | { message?: unknown }
        | undefined;
      if (typeof detail?.message === "string" && detail.message !== "") return detail.message;
    }
    return String(err);
  }

  async function switchEmbedding(name: string) {
    if (embQuery.data != null && name === embQuery.data.active) return;
    setEmbMessage(null);
    setEmbSwitchError(null);
    try {
      // 切换只建后台重建任务（可达校验在服务端做）；完成前检索仍走旧表，不断链。
      const res = await switchEmb.mutateAsync(name);
      if (res.rebuilding == null) {
        setEmbMessage(`已是 ${res.active}，无需切换。`);
      } else {
        setEmbMessage(
          `已开始重建（${res.rebuilding.from} → ${res.rebuilding.to}），完成后自动切换；期间检索不受影响。`,
        );
      }
    } catch (err) {
      setEmbSwitchError(embErrorText(err));
    }
  }

  async function setLowLatency(enabled: boolean) {
    setLatencyBusy(true);
    setLatencyMessage(null);
    try {
      // 开关只影响下一段音频；在途段不受影响（与 provider 切换同语义）。
      const st = await apiPut<LatencyState>("/asr/latency", { enabled });
      setLatency(st);
      setLatencyMessage(
        st.enabled ? "低延迟模式已开启（下一段起出流式部分结果）。"
                   : "低延迟模式已关闭（整段路径，与之前行为一致）。",
      );
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setLatencyMessage("无法开启：本机无可用 CUDA（需 GPU，CPU 请保持关闭）。");
      } else {
        setLatencyMessage(`切换失败：${String(err)}`);
      }
    } finally {
      setLatencyBusy(false);
    }
  }

  function pollDownload(model: string, id: string) {
    const prev = timersRef.current[model];
    if (prev != null) window.clearInterval(prev);
    const tick = async () => {
      try {
        const status = await apiGet<DownloadStatus>(`/model/download/${id}`);
        setDownloads((m) => ({ ...m, [model]: status }));
        if (status.status === "done" || status.status === "error") {
          const t = timersRef.current[model];
          if (t != null) window.clearInterval(t);
        }
      } catch (err) {
        setDownloadErrors((m) => ({ ...m, [model]: String(err) }));
        const t = timersRef.current[model];
        if (t != null) window.clearInterval(t);
      }
    };
    void tick();
    timersRef.current[model] = window.setInterval(tick, 1000);
  }

  async function startDownload(model: string) {
    setDownloadErrors((m) => {
      const next = { ...m };
      delete next[model];
      return next;
    });
    try {
      // 缺省即 bge（兼容旧行为）；ASR 模型传模型键。续传由 sidecar 按 .part 续写。
      const body = model === BGE_MODEL ? {} : { model };
      const { download_id } = await apiPost<{ download_id: string }>(
        "/model/download",
        body,
      );
      pollDownload(model, download_id);
    } catch (err) {
      setDownloadErrors((m) => ({ ...m, [model]: String(err) }));
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
        <h3>ASR Provider（语音转写，即时生效）</h3>
        {asrLoadError != null && <p>ASR 目录加载失败：{asrLoadError}</p>}
        {asr == null && asrLoadError == null && <p>加载中…</p>}
        {asr != null && (
          <>
            <label>
              Provider：
              <select
                data-testid="asr-provider-select"
                value={asr.selected}
                disabled={switching}
                onChange={(e) => void switchAsrProvider(e.currentTarget.value)}
              >
                {asr.available.map((e) => (
                  <option key={e.name} value={e.name}>
                    {e.display}
                    {e.ready ? "" : "（未就绪）"}
                  </option>
                ))}
              </select>
            </label>
            {switching && <p>切换中…</p>}
            {asrMessage != null && <p data-testid="asr-message">{asrMessage}</p>}
            <ul>
              {asr.available.map((e) => (
                <li key={e.name}>
                  {e.display}：
                  {e.kind === "cloud"
                    ? "需要 API Key（上方保存 openai 槽位）"
                    : e.ready
                      ? "权重就绪 ✓"
                      : `权重未下载（模型 ${e.model}，约 200MB）`}
                  {e.note !== "" && ` —— ${e.note}`}
                  {e.kind === "local" && e.model != null && (
                    <div>
                      <button
                        data-testid={`model-download-${e.model}`}
                        type="button"
                        onClick={() => void startDownload(e.model as string)}
                      >
                        下载/校验 {e.model}
                      </button>
                      {downloadErrors[e.model] != null && (
                        <p>下载失败：{downloadErrors[e.model]}</p>
                      )}
                      {downloads[e.model] != null && (
                        <DownloadProgress dl={downloads[e.model]} />
                      )}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}
      </div>

      <div>
        <h3>低延迟模式（流式部分结果，需 CUDA）</h3>
        {latency == null && <p>加载中…（旧 sidecar 无此能力则不显示）</p>}
        {latency != null && (
          <>
            <label>
              <input
                data-testid="latency-toggle"
                type="checkbox"
                checked={latency.enabled}
                disabled={latencyBusy || (!latency.enabled && !latency.cuda)}
                onChange={(e) => void setLowLatency(e.currentTarget.checked)}
              />
              说话过程中即时出字（asr_partial）
            </label>
            {!latency.cuda && (
              <p data-testid="latency-nocuda">
                本机无可用 CUDA，开关已禁用；CPU 请保持关闭（整段路径不受影响）。
              </p>
            )}
            {latencyMessage != null && <p data-testid="latency-message">{latencyMessage}</p>}
            <p>{latency.note}</p>
          </>
        )}
      </div>

      <EmbeddingConfig
        state={embQuery.data ?? null}
        loadError={embQuery.isError ? String(embQuery.error) : null}
        switching={switchEmb.isPending}
        switchError={embSwitchError}
        rebuild={embRebuildQuery.data ?? null}
        message={embMessage}
        onSwitch={(name) => void switchEmbedding(name)}
      />

      {/* taskP7：开机自启。Rust 侧启动时不注册自启，注册只发生在这里的写入，
          所以写失败不会拦住应用启动，只会显示在下面这句话里。 */}
      {autostart != null && (
        <div>
          <h3>开机自启</h3>
          <label>
            <input
              data-testid="autostart-toggle"
              type="checkbox"
              checked={autostart.enabled}
              disabled={autostartBusy}
              onChange={(e) => void setAutostartEnabled(e.currentTarget.checked)}
            />
            开机时自动启动 InterviewCopilot
          </label>
          <p data-testid="autostart-note" className={autostart.available ? undefined : "warn"}>
            {autostart.note}
          </p>
        </div>
      )}

      <div>
        <h3>模型下载（bge-small-zh-v1.5）</h3>
        <button data-testid="model-download" type="button" onClick={() => void startDownload(BGE_MODEL)}>
          下载/校验模型
        </button>
        {downloadErrors[BGE_MODEL] != null && <p>下载失败：{downloadErrors[BGE_MODEL]}</p>}
        {downloads[BGE_MODEL] != null && (
          <DownloadProgress dl={downloads[BGE_MODEL]} />
        )}
      </div>

      <div>
        <h3>音频诊断（最近一次连接，内容无关）</h3>
        <button data-testid="diag-refresh" type="button" onClick={() => void refreshDiag()}>
          刷新
        </button>
        {diagError != null && <p>诊断加载失败：{diagError}</p>}
        {diag != null && (
          <>
            {diag.last_connection == null ? (
              <p data-testid="diag-empty">尚无音频连接（抓包/回放一次后这里会有计数）。</p>
            ) : (
              <ul data-testid="diag-counters">
                {Object.entries(diag.last_connection).map(([k, v]) => (
                  <li key={k}>
                    {k}：{v}
                  </li>
                ))}
              </ul>
            )}
            <p>
              限额：待转写 ≤{diag.limits.max_pending}／单段 ≤{diag.limits.max_segment_frames} 帧／
              下行超时 {diag.limits.send_timeout_s}s
            </p>
            <p data-testid="diag-capture">
              sidecar 侧：{sidecarCaptureSummary(diag.capture)}
            </p>
          </>
        )}
      </div>

      <div>
        <h3>降级计数（三链统一，内容无关）</h3>
        {degradeError != null && <p>降级计数加载失败：{degradeError}</p>}
        {degrade == null && degradeError == null && <p>加载中…</p>}
        {degrade != null && (
          <ul data-testid="degrade-counters">
            {(["asr", "llm", "embedding"] as const).map((chain) => (
              <li key={chain}>
                {chain}：{degrade.chains[chain].total}
                {Object.entries(degrade.chains[chain].by_kind).map(([k, v]) => ` ${k}×${v}`)}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* 采集自检（Rust 侧真值）：开流 500ms 探测的设备/格式/实际帧率 */}
      <div>
        <h3>采集自检（Rust 侧）</h3>
        <button data-testid="capture-refresh" type="button" onClick={() => void refreshDiag()}>
          刷新
        </button>
        {captureError != null && (
          <p data-testid="capture-diag-error">采集诊断不可用：{captureError}</p>
        )}
        {capture != null && (
          <>
            <p data-testid="capture-diag-state">
              {capture.running ? "采集中" : "未采集"}
              {capture.last_error != null && ` · 异常：${capture.last_error}`}
            </p>
            {capture.paths.length === 0 ? (
              <p data-testid="capture-diag-empty">尚未启动过采集（按 F1.6 或到「实时提词」开一次）。</p>
            ) : (
              capture.paths.map((p) => (
                <div key={p.path} data-testid={`capture-diag-${p.path}`}>
                  <p>
                    <strong>{pathLabel(p.path)}</strong>：{pathStatusLabel(p)}
                  </p>
                  <p data-testid={`capture-selfcheck-${p.path}`}>{selfCheckSummary(p.self_check)}</p>
                  <ul data-testid={`capture-counters-${p.path}`}>
                    {diagnosticsRows(p).map(([k, v]) => (
                      <li key={k}>
                        {k}：{v}
                      </li>
                    ))}
                  </ul>
                </div>
              ))
            )}
          </>
        )}
      </div>
    </div>
  );
}
