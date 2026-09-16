/**
 * 设置页的「会话录制」区（S4，R19/R22/R23 的用户面）。
 *
 * 三件事必须都能**一眼看懂**，不能让人猜：
 * 1. **开关默认关**，关着的时候一次会话都不会记（不是"记了但不显示"）；
 * 2. **保留策略**要说清"多久"和"按什么算到期"，不能只写"30 天"；
 * 3. **清除全部**要二次确认，并说明删的是全部、不可恢复。
 *
 * 导出入口不在这里 —— 它挂在 `pages/History.tsx` 的单会话详情里
 * （导出是"对某一次会话做一件事"，跟着列表走比藏在设置里好找）。
 */

import { useState } from "react";

import {
  useSessionSettings,
  useSessionUsage,
  useUpdateSessionSettings,
  usePurgeSessions,
} from "../hooks/useSessionRecording";
import {
  formatDateTime,
  retentionHint,
  retentionLabel,
  usageLine,
  SESSION_PURGE_CONFIRM_TEXT,
  type RetentionDays,
} from "../lib/sessions";

export function SessionRecording() {
  const settings = useSessionSettings();
  const usage = useSessionUsage();
  const update = useUpdateSessionSettings();
  const purge = usePurgeSessions();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const enabled = settings.data?.enabled ?? false;
  const retention = (settings.data?.retention_days ?? null) as RetentionDays;

  function change(next: { enabled?: boolean; retention_days?: number | null }) {
    setError(null);
    update.mutate(next, { onError: (e) => setError(String(e)) });
  }

  return (
    <div>
      <h3>会话录制</h3>

      {settings.isError && <p data-testid="recording-error">设置读取失败</p>}
      {error != null && <p data-testid="recording-save-error">保存失败：{error}</p>}

      <label>
        <input
          type="checkbox"
          data-testid="recording-toggle"
          checked={enabled}
          disabled={update.isPending || settings.isPending}
          onChange={(e) => change({ enabled: e.currentTarget.checked })}
        />
        开启会话录制（默认关闭）
      </label>
      <p data-testid="recording-state">
        {enabled ? "开启：每次采集都会留一条可回放的会话账" : "关闭：采集不会留下任何会话账"}
      </p>

      <label>
        保留策略{" "}
        <select
          data-testid="recording-retention"
          value={retention === null ? "forever" : String(retention)}
          disabled={update.isPending}
          onChange={(e) =>
            change({
              retention_days:
                e.currentTarget.value === "forever" ? null : Number(e.currentTarget.value),
            })
          }
        >
          <option value="forever">永久保留</option>
          <option value="30">保留 30 天</option>
          <option value="7">保留 7 天</option>
        </select>
      </label>
      <p data-testid="recording-retention-hint">{retentionHint(retention)}（{retentionLabel(retention)}）</p>

      <p data-testid="recording-usage">
        {usage.data != null
          ? usageLine(usage.data)
          : usage.isError
            ? "数据量读取失败"
            : "数据量加载中…"}
      </p>
      {usage.data != null && usage.data.events > 0 && (
        <p data-testid="recording-range">
          最早 {formatDateTime(usage.data.oldest_ms)} · 最近 {formatDateTime(usage.data.newest_ms)}
        </p>
      )}

      <button
        type="button"
        data-testid="recording-purge"
        disabled={purge.isPending || (usage.data?.events ?? 0) === 0}
        onClick={() => {
          setError(null);
          setConfirming(true);
        }}
      >
        立即清除全部会话
      </button>
      {usage.data?.active_session_id != null && (
        <p data-testid="recording-active-hint">
          有会话正在进行中（{usage.data.active_session_id}）：先停止采集才能清除。
        </p>
      )}

      {confirming && (
        <div data-testid="purge-confirm" role="dialog" aria-modal="true">
          <p data-testid="purge-confirm-text">
            清除全部会话：{SESSION_PURGE_CONFIRM_TEXT}
          </p>
          <button
            type="button"
            data-testid="purge-confirm-ok"
            onClick={() =>
              purge.mutate(undefined, {
                onSettled: () => setConfirming(false),
                onError: (e) => setError(String(e)),
              })
            }
          >
            确认清除
          </button>
          <button type="button" data-testid="purge-confirm-cancel" onClick={() => setConfirming(false)}>
            取消
          </button>
        </div>
      )}
    </div>
  );
}
