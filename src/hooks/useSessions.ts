/**
 * 会话账的数据层（S3）：TanStack Query 封装。
 *
 * # 三个刷新节奏，以及为什么不一样
 *
 * - **列表 60s**：列表上有"进行中"的会话（`closed=false`），它的时长与账行数
 *   一直在长，所以列表确实需要轮询。60s 是"够新"和"别老打扰"之间的折中。
 * - **详情 300s**：一次**已结束**的会话不会再变，300s 只是"重新进来时多久内
 *   复用缓存"，不做轮询 —— 对着已经写完的账每 5 分钟问一遍没有意义。
 *   进行中的会话走列表那一路（60s）就够看清它在长。
 * - **状态 10s**：只用来知道"当前开着的会话是谁"（列表页标"进行中"、
 *   预判删除会撞 409），量极小，可以勤一点。
 *
 * # 删除后的一致性
 *
 * 删掉一次会话要让**列表**和**该会话的详情缓存**同时失效：只清列表的话，
 * 立刻切回那条的详情会看到一份已不存在的账（缓存还在）。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  deleteSession,
  fetchSessionStatus,
  fetchSessions,
  fetchSessionTimeline,
  type DeleteSessionResult,
  type SessionStatusResponse,
  type SessionsPage,
  type SessionTimeline,
} from "../lib/api";

export const SESSION_LIST_STALE_MS = 60_000;
export const SESSION_DETAIL_STALE_MS = 300_000;
export const SESSION_STATUS_STALE_MS = 10_000;

/** 列表默认一页 20 条（服务端上限 200；列表不是导出通道）。 */
export const SESSION_PAGE_SIZE = 20;

export function sessionsKey(limit: number, offset: number) {
  return ["sessions", limit, offset] as const;
}

export function sessionTimelineKey(sessionId: string) {
  return ["session-timeline", sessionId] as const;
}

/** 会话列表（时间倒序 + 分页）。 */
export function useSessions(limit = SESSION_PAGE_SIZE, offset = 0) {
  return useQuery<SessionsPage>({
    queryKey: sessionsKey(limit, offset),
    queryFn: () => fetchSessions(limit, offset),
    staleTime: SESSION_LIST_STALE_MS,
    refetchInterval: SESSION_LIST_STALE_MS,
  });
}

/** 一次会话的时间线。`sessionId` 为 null 时**不发请求**（还没选中的详情页）。 */
export function useSessionTimeline(sessionId: string | null) {
  return useQuery<SessionTimeline>({
    queryKey: sessionTimelineKey(sessionId ?? ""),
    queryFn: () => fetchSessionTimeline(sessionId ?? ""),
    enabled: sessionId != null && sessionId !== "",
    staleTime: SESSION_DETAIL_STALE_MS,
  });
}

/** 当前开着的会话（没有则 `session_id` 为 null）。 */
export function useSessionStatus() {
  return useQuery<SessionStatusResponse>({
    queryKey: ["session-status"],
    queryFn: fetchSessionStatus,
    staleTime: SESSION_STATUS_STALE_MS,
    refetchInterval: SESSION_STATUS_STALE_MS,
  });
}

/**
 * 删除一次会话。
 *
 * 失败**原样把服务端的话透给调用方**：409（会话进行中）与 404（本来就没有）
 * 是两种不同的情况，用户看到的说法就该不同，不该被前端糊成一句"删除失败"。
 */
export function useDeleteSession() {
  const queryClient = useQueryClient();
  return useMutation<DeleteSessionResult, Error, string>({
    mutationFn: (sessionId: string) => deleteSession(sessionId),
    onSuccess: (_data, sessionId) => {
      queryClient.removeQueries({ queryKey: sessionTimelineKey(sessionId) });
      queryClient.invalidateQueries({ queryKey: ["sessions"] });
      queryClient.invalidateQueries({ queryKey: ["session-status"] });
    },
  });
}
