import { Teleprompter } from "../components/Teleprompter";
import { useLiveQA } from "../hooks/useLiveQA";
import { capturePaths, type CapturePath } from "../lib/liveqa";
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
    captureRequested,
    platform,
    notice,
    triggerManual,
    reset,
  } = useLiveQA();

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
      {captureRequested && (
        <p data-testid="live-capture-requested">
          已收到采集开关（F1.6）。采集服务尚未接线，当前不会有音频上行。
        </p>
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
