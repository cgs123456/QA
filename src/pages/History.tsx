/**
 * 历史会话（S3）：列表 → 单会话时间线，以及删除。
 *
 * # 三个状态，各有各的说法
 *
 * - **加载中 / 出错 / 空**，三种都要有明确的字。尤其是**空态**：一次会话都没有
 *   时，用户需要知道"为什么没有"和"怎么才有"，而不是一个空白列表 ——
 *   所以空态里直接给出去设置的入口（`onOpenSettings`）。
 * - **读失败要报错，不回空列表**。会话账的读端点在库故障时抛 500，前端照实显示；
 *   "读不到"伪装成"这段时间没发生过事"是最坏的一种默认值。
 *
 * # 删除
 *
 * 两步：先确认框（含 R22 文案），再 `DELETE`。确认框不是走过场 ——
 * 删除是**物理删除**（服务端不留软删标记，见 `routers/session.py`），
 * 删了就真没了，所以确认文案必须说清后果。
 *
 * 正在进行的会话**在前端就先拦一次**（`canDelete`），但后端仍是唯一真值：
 * 真撞上 409 时，显示的是服务端那句话，不是前端这句。
 *
 * # R21（UI 面）
 *
 * 从列表到详情再到删除，这一页**不发起任何检索**。时间线是回放，不是问答。
 */

import { useState } from "react";

import { SessionDetail } from "../components/SessionDetail";
import {
  canDelete,
  deleteErrorMessage,
  formatDateTime,
  sessionStatsLine,
  storeNameById,
  SESSION_DELETE_CONFIRM_TEXT,
} from "../lib/sessions";
import { useSessionStatus, useSessionTimeline, useSessions, useDeleteSession } from "../hooks/useSessions";
import { useExportSession } from "../hooks/useSessionRecording";
import { useStoresQuery } from "../hooks/useStore";
import type { SessionSummary } from "../lib/api";

export function History({ onOpenSettings }: { onOpenSettings: () => void }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  /** 待确认删除的会话（null = 确认框未打开）。 */
  const [pending, setPending] = useState<SessionSummary | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const list = useSessions();
  const status = useSessionStatus();
  const stores = useStoresQuery();
  const timeline = useSessionTimeline(selectedId);
  const remove = useDeleteSession();
  const exportOne = useExportSession();

  const activeId = status.data?.session_id ?? null;
  const sessions = list.data?.sessions ?? [];

  /** 导出 = 把服务端给的文本落成文件。失败原样显示，不猜。 */
  function exportCurrent() {
    exportOne.mutate(selectedId ?? "", {
      onSuccess: (res) => {
        const url = URL.createObjectURL(new Blob([res.content], { type: "application/json" }));
        const a = document.createElement("a");
        a.href = url;
        a.download = res.filename;
        a.click();
        URL.revokeObjectURL(url);
      },
    });
  }

  function confirmDelete() {
    if (pending == null) return;
    const target = pending;
    setDeleteError(null);
    remove.mutate(target.session_id, {
      onSuccess: () => {
        setPending(null);
        if (selectedId === target.session_id) setSelectedId(null);
      },
      onError: (e) => {
        setPending(null);
        setDeleteError(deleteErrorMessage(e));
      },
    });
  }

  // ------------------------------------------------------------------ 详情
  if (selectedId != null) {
    if (timeline.isPending) return <p data-testid="tl-loading">时间线加载中…</p>;
    if (timeline.isError) {
      return (
        <p data-testid="tl-error">
          时间线读取失败：{deleteErrorMessage(timeline.error)}
          <button type="button" onClick={() => setSelectedId(null)}>
            返回列表
          </button>
        </p>
      );
    }
    return (
      <SessionDetail
        sessionId={selectedId}
        events={timeline.data?.events ?? []}
        storeName={storeNameById(stores.data, timeline.data?.events?.[0]?.store_id ?? null)}
        onBack={() => setSelectedId(null)}
        onExport={exportCurrent}
        exportPending={exportOne.isPending}
        exportError={exportOne.error == null ? null : String(exportOne.error)}
      />
    );
  }

  // ------------------------------------------------------------------ 列表
  return (
    <div>
      <h2>历史会话</h2>
      {list.isPending && <p data-testid="history-loading">加载中…</p>}
      {list.isError && (
        <p data-testid="history-error">
          会话列表读取失败：{deleteErrorMessage(list.error)}
        </p>
      )}

      {list.data != null && sessions.length === 0 && (
        <div data-testid="history-empty">
          <p>还没有任何会话账。</p>
          <p>
            会话账只在**开启会话录制**后才会产生 —— 去设置页打开它，下一次采集就会留下
            一条可回放的记录。
          </p>
          <button type="button" data-testid="history-goto-settings" onClick={onOpenSettings}>
            去设置开启录制
          </button>
        </div>
      )}

      {sessions.length > 0 && (
        <ul data-testid="history-list">
          {sessions.map((s) => {
            const guard = canDelete(s, activeId);
            const running = activeId != null && s.session_id === activeId;
            return (
              <li key={s.session_id} data-testid="history-row" data-session={s.session_id}>
                <button
                  type="button"
                  data-testid="history-open"
                  onClick={() => setSelectedId(s.session_id)}
                >
                  {formatDateTime(s.started_ms)}
                </button>{" "}
                <span data-testid="history-stats">{sessionStatsLine(s)}</span>{" "}
                <span data-testid="history-store">{storeNameById(stores.data, s.store_id)}</span>
                {running && <em data-testid="history-running">进行中</em>}
                {!s.closed && !running && <em data-testid="history-unclosed">未正常结束</em>}
                <button
                  type="button"
                  data-testid="history-delete"
                  disabled={!guard.ok || remove.isPending}
                  title={guard.ok ? undefined : guard.reason}
                  onClick={() => {
                    setDeleteError(null);
                    setPending(s);
                  }}
                >
                  删除
                </button>
              </li>
            );
          })}
        </ul>
      )}

      {deleteError != null && <p data-testid="history-delete-error">删除失败：{deleteError}</p>}

      {pending != null && (
        <div data-testid="history-confirm" role="dialog" aria-modal="true">
          <p data-testid="confirm-text">
            删除会话 {pending.session_id}：{SESSION_DELETE_CONFIRM_TEXT}，且不可恢复。
          </p>
          <button type="button" data-testid="confirm-ok" onClick={confirmDelete}>
            确认删除
          </button>
          <button type="button" data-testid="confirm-cancel" onClick={() => setPending(null)}>
            取消
          </button>
        </div>
      )}
    </div>
  );
}
