/**
 * 会话录制管理面的数据层（S4）：设置 / 数据量 / 清除 / 导出。
 *
 * 刷新节奏：设置与数据量都按 **60s**。它们只在用户改设置或会话在长的时候变，
 * 再快没有意义；改设置走 mutation 立刻失效刷新，所以用户不会看到滞后。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  exportSession,
  fetchSessionSettings,
  fetchSessionUsage,
  purgeSessions,
  putSessionSettings,
  type PurgeResult,
  type SessionExport,
  type SessionSettings,
  type SessionUsage,
} from "../lib/api";

export const SESSION_SETTINGS_STALE_MS = 60_000;

export function useSessionSettings() {
  return useQuery<SessionSettings>({
    queryKey: ["session-settings"],
    queryFn: fetchSessionSettings,
    staleTime: SESSION_SETTINGS_STALE_MS,
    refetchInterval: SESSION_SETTINGS_STALE_MS,
  });
}

export function useSessionUsage() {
  return useQuery<SessionUsage>({
    queryKey: ["session-usage"],
    queryFn: fetchSessionUsage,
    staleTime: SESSION_SETTINGS_STALE_MS,
    refetchInterval: SESSION_SETTINGS_STALE_MS,
  });
}

export function useUpdateSessionSettings() {
  const queryClient = useQueryClient();
  return useMutation<
    SessionSettings,
    Error,
    { enabled?: boolean; retention_days?: number | null }
  >({
    mutationFn: (body) => putSessionSettings(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["session-settings"] });
      queryClient.invalidateQueries({ queryKey: ["session-usage"] });
    },
  });
}

/** 清除全部会话：清完要把列表、数据量、设置里的活动会话一起失效。 */
export function usePurgeSessions() {
  const queryClient = useQueryClient();
  return useMutation<PurgeResult, Error, void>({
    mutationFn: () => purgeSessions(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sessions"] });
      queryClient.invalidateQueries({ queryKey: ["session-usage"] });
      queryClient.invalidateQueries({ queryKey: ["session-status"] });
    },
  });
}

/** 单会话导出（一次性动作，不进缓存）。 */
export function useExportSession() {
  return useMutation<SessionExport, Error, string>({
    mutationFn: (sessionId) => exportSession(sessionId),
  });
}
