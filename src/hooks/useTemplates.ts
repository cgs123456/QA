/**
 * 模板系统的数据层（S7）：TanStack Query 封装。
 *
 * **修（2026-09-16）**：原实现 import 了 `../lib/api` 里**不存在**的 `useApi`，
 * 而 `api.ts` 也完全没有模板接口 —— 这个 hook 在运行时必炸（`useApi` 是 undefined）。
 * 它当时没被发现，是因为 `Knowledge.test.tsx` 把 hook 层整个 mock 掉了：
 * **页面测试测的是"页面怎么画"，测不到 hook 本身能不能跑**。
 * 现按 `useSessions.ts` 的既有惯例重写：`api.ts` 出封装，这里只做 Query 组合。
 *
 * # 刷新节奏
 *
 * **列表 300s**：预置模板随包发布、自定义模板由用户自己增删改，且本页的增删改
 * 都会显式 `invalidate`，所以不需要轮询。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createStoreFromTemplate,
  createTemplate,
  deleteTemplate,
  fetchTemplate,
  fetchTemplates,
  updateTemplate,
  type CreatedTemplate,
  type DeletedTemplate,
  type StoreFromTemplate,
  type TemplateDetail,
  type TemplatePayload,
  type TemplateSummary,
  type UpdatedTemplate,
} from "../lib/api";

export const TEMPLATE_LIST_STALE_MS = 300_000;

/** 列表缓存 key。增删改后统一失效它（前缀匹配）。 */
export const TEMPLATES_KEY = ["templates"] as const;

/** 知识库列表的 key（按模板建库后要让它失效）。 */
const STORES_KEY = ["stores"] as const;

export function useTemplates(category?: string) {
  const query = useQuery<TemplateSummary[]>({
    queryKey: [...TEMPLATES_KEY, category ?? "all"],
    queryFn: () => fetchTemplates(category),
    staleTime: TEMPLATE_LIST_STALE_MS,
  });
  return {
    templates: query.data ?? [],
    isLoading: query.isLoading,
    isError: query.isError,
    error: query.error,
  };
}

/** 模板详情（含 payload）。`enabled` 由调用方控制，避免没选中就拉。 */
export function useTemplateDetail(templateId: string | null) {
  return useQuery<TemplateDetail>({
    queryKey: ["template", templateId],
    queryFn: () => fetchTemplate(templateId as string),
    enabled: templateId != null && templateId !== "",
    staleTime: TEMPLATE_LIST_STALE_MS,
  });
}

/** 「按模板建库」。成功后知识库列表也要失效 —— 新库得出现在列表里。 */
export function useCreateFromTemplate() {
  const qc = useQueryClient();
  return useMutation<StoreFromTemplate, Error, { template_id: string; name: string }>({
    mutationFn: (body) => createStoreFromTemplate(body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: TEMPLATES_KEY });
      void qc.invalidateQueries({ queryKey: STORES_KEY });
    },
  });
}

export function useCreateTemplate() {
  const qc = useQueryClient();
  return useMutation<
    CreatedTemplate,
    Error,
    {
      name: string;
      description?: string;
      category: string;
      payload: TemplatePayload;
      is_preset?: boolean;
    }
  >({
    mutationFn: (body) => createTemplate(body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: TEMPLATES_KEY });
    },
  });
}

export function useUpdateTemplate() {
  const qc = useQueryClient();
  return useMutation<
    UpdatedTemplate,
    Error,
    {
      template_id: string;
      body: {
        name?: string;
        description?: string;
        category?: string;
        payload?: TemplatePayload;
      };
    }
  >({
    mutationFn: ({ template_id, body }) => updateTemplate(template_id, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: TEMPLATES_KEY });
    },
  });
}

export function useDeleteTemplate() {
  const qc = useQueryClient();
  return useMutation<DeletedTemplate, Error, string>({
    mutationFn: (templateId) => deleteTemplate(templateId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: TEMPLATES_KEY });
    },
  });
}
