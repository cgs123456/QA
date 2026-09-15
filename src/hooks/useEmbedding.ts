import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost } from "../lib/api";

export type EmbeddingEntry = {
  name: string;
  display: string;
  kind: "local" | "cloud";
  dim: number;
  table: string;
  needs_key: boolean;
  note: string;
  ready: boolean;
};

export type RebuildingSnapshot = {
  rebuild_id: string;
  status: "queued" | "running" | "done" | "error";
  from: string;
  to: string;
  total: number | null;
  done: number;
  tokens: number;
  error: string | null;
};

export type EmbeddingState = {
  active: string;
  dim: number;
  table: string;
  rebuilding: RebuildingSnapshot | null;
  available: EmbeddingEntry[];
};

export type RebuildStatus = RebuildingSnapshot;

export type SwitchResult = {
  active: string;
  rebuilding: RebuildingSnapshot | null;
};

/** 当前态（含在途重建；重建中时自动 1s 轮询，直到 rebuilding 清空）。 */
export function useEmbeddingProvider() {
  return useQuery({
    queryKey: ["embedding-provider"],
    queryFn: () => apiGet<EmbeddingState>("/embedding/provider"),
    refetchInterval: (data) => (data?.rebuilding != null ? 1000 : false),
  });
}

/** 切换 provider（可达校验 + 起后台重建；成功后刷新当前态）。 */
export function useSwitchEmbedding() {
  const queryClient = useQueryClient();
  return useMutation({
    // 可达探测最长 30s，超时放宽到 60s。
    mutationFn: (provider: string) =>
      apiPost<SwitchResult>("/embedding/provider", { provider }, 60000),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["embedding-provider"] }),
  });
}

/** 重建进度轮询（仅在途时启用；终态即停）。 */
export function useRebuildStatus(rebuildId: string | null, active: boolean) {
  return useQuery({
    queryKey: ["embedding-rebuild", rebuildId],
    queryFn: () => apiGet<RebuildStatus>(`/embedding/rebuild/${rebuildId ?? ""}`),
    enabled: active && rebuildId != null,
    refetchInterval: (data) => {
      const s = data?.status;
      return s === "done" || s === "error" ? false : 1000;
    },
  });
}
