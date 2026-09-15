import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiPost } from "../lib/api";
import type { ExcelCommitMapping, ImportFormat, ImportPreview } from "../lib/importFlow";

export type ImportPreviewRequest = {
  format: ImportFormat;
  content_b64: string;
  filename?: string;
};

export type ImportCommitRequest = {
  store_id?: string;
  store_name?: string;
  format: ImportFormat;
  content_b64: string;
  filename?: string;
  /** Excel 必填（列映射）；PDF 仅允许 `{entity}` 或省略。 */
  mapping?: ExcelCommitMapping | { entity?: string };
};

export type ImportCommitResult = {
  store_id: string;
  stats: Record<string, number | number[]>;
};

/** dry-run 预览（大文件解析偏慢，超时放宽到 60s；只读，不写库）。 */
export function useImportPreview() {
  return useMutation({
    mutationFn: (body: ImportPreviewRequest) =>
      apiPost<ImportPreview>("/knowledge/import/preview", body, 60000),
  });
}

/** 确认入库（全量 commit，超时 120s；成功后刷新知识库列表）。 */
export function useImportCommit() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ImportCommitRequest) =>
      apiPost<ImportCommitResult>("/knowledge/import/commit", body, 120000),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["stores"] }),
  });
}
