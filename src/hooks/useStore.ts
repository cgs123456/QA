import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiDelete, apiGet, apiPost, apiPut } from "../lib/api";
import { useKnowledgeStore, type StoreInfo } from "../stores/knowledgeStore";

export type CompileStats = Record<string, number>;

export type CompileResult = {
  store_id: string;
  stats: CompileStats;
};

export function useStoresQuery() {
  return useQuery({
    queryKey: ["stores"],
    queryFn: () => apiGet<StoreInfo[]>("/knowledge/list"),
  });
}

export function useCreateStore() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => apiPost<StoreInfo>("/store", { name }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["stores"] }),
  });
}

export function useSwitchStore() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (storeId: string) =>
      apiPut<StoreInfo>("/stores/current", { store_id: storeId }),
    onSuccess: (store) => {
      queryClient.invalidateQueries({ queryKey: ["stores"] });
      useKnowledgeStore.getState().setSelectedId(store.id);
    },
  });
}

export function useDeleteStore() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (storeId: string) =>
      apiDelete<{ deleted: string }>(`/store/${storeId}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["stores"] }),
  });
}

export function useCompile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      store_id?: string;
      store_name?: string;
      format: "markdown" | "json";
      content: string;
      filename?: string;
    }) => apiPost<CompileResult>("/knowledge/compile", body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["stores"] }),
  });
}

/** 聚合 hook：列表同步到 zustand + CRUD mutations（供 Knowledge 页）。 */
export function useStore() {
  const list = useStoresQuery();
  const create = useCreateStore();
  const switchStore = useSwitchStore();
  const remove = useDeleteStore();
  const compile = useCompile();
  return { list, create, switchStore, remove, compile };
}
