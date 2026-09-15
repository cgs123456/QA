import { Teleprompter } from "../components/Teleprompter";
import { useLiveQA } from "../hooks/useLiveQA";
import { capturePaths, type CapturePath } from "../lib/liveqa";
import { captureSummary, pathLabel, pathStatusLabel, selfCheckSummary } from "../lib/capture";
import type { QuestionReason } from "../lib/trigger";

const PATH_LABELS: Record<CapturePath, string> = {
  loopback: "系统回环",
  mic: "麦克风",
};

const SKIP_LABELS: Record<QuestionReason, string> = {
  too_short: "太短",
  trailing_mark: "句尾疑问词",
  question_prefix: "疑问词开头",
  declarative: "陈述句",
  fallthrough: "兜底",
};

/**
 * 实时提词（F1.5 + F1.6 的产品面）。
 *
 * 页面本身不做任何策略判断：它订阅 `asr://final`、渲染状态机给出的卡片。
 * 「什么时候响、响什么」全在 `lib/liveqa.ts` 与 `lib/trigger.ts` 里。
 * 采集开关同理——它只调命令，状态由 Rust 回推（见 `lib/capture.ts`）。
 */
export function LiveQA() {
  const {
    cards,
    lastSkip,
    pending,
    busy,
    lastError,
    lastPath,
    lastTranscript,
    capture,
    captureError,
    platform,
    notice,
    triggerManual,
    requestCaptureToggle,
    reset,
    // taskP7：本页是提词窗的数据源。卡片算好之后推给提词窗，那边只渲染不跑会话
    // （否则同一个问题会发两次检索）。
  } = useLiveQA({ broadcast: true });

  const paths = capturePaths(platform);

  return (
    <div>
      <h2>实时提词</h2>

      {/* 平台矩阵：macOS 无系统回环，必须明示「仅麦克风」 */}
      <p data-testid="live-capture">
        采集路径：{paths.map((p) => PATH_LABELS[p]).join(" + ")}
        {notice != null && <strong data-testid="live-notice">（{notice}）</strong>}
      </p>

      <div className="row">
        <button
          type="button"
          data-testid="live-capture-toggle"
          onClick={() => void requestCaptureToggle()}
        >
          {capture.running ? "停止采集" : "开始采集"}
        </button>
        <button type="button" data-testid="live-manual" onClick={triggerManual}>
          手动提词（Ctrl+Shift+Space）
        </button>
        <button type="button" data-testid="live-reset" onClick={reset}>
          清空
        </button>
      </div>

      <p data-testid="live-status">
        {busy ? "检索中…" : pending > 0 ? "锁定期内，已排队 1 条" : "待命"}
        {lastPath != null && ` · 最近来源：${PATH_LABELS[lastPath as CapturePath] ?? lastPath}`}
        {lastSkip != null && ` · 上句跳过（${SKIP_LABELS[lastSkip]}）`}
      </p>

      {/* 采集真值：跑没跑、跑得对不对，都以 Rust 回推的快照为准 */}
      <p data-testid="live-capture-state">{captureSummary(capture)}</p>
      {capture.paths.length > 0 && (
        <ul data-testid="live-capture-paths">
          {capture.paths.map((p) => (
            <li key={p.path} data-testid={`live-capture-path-${p.path}`}>
              {pathLabel(p.path)}：{pathStatusLabel(p)} · {selfCheckSummary(p.self_check)}
            </li>
          ))}
        </ul>
      )}
      {captureError != null && (
        <p data-testid="live-capture-error">采集失败：{captureError}</p>
      )}
      {lastError != null && <p data-testid="live-error">检索失败：{lastError}</p>}
      <p className="live-transcript" data-testid="live-transcript">
        {lastTranscript ?? "（尚未收到转写）"}
      </p>

      <h3>提词框</h3>
      <Teleprompter cards={cards} />
    </div>
  );
}
