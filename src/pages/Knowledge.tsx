import { useEffect, useState } from "react";
import { useKnowledgeStore } from "../stores/knowledgeStore";
import { useStore } from "../hooks/useStore";
import { useImportCommit, useImportPreview } from "../hooks/useImport";
import { ColumnMapping } from "../components/ColumnMapping";
import { ReviewPanel } from "../components/ReviewPanel";
import {
  IMPORT_MAX_BYTES,
  buildInitialMapping,
  cleanExcelMapping,
  detectImportFormat,
  excelPreviewToSummary,
  fileToBase64,
  importErrorMessage,
  parseUnsupportedDetail,
  pdfPreviewToSummary,
  validateExcelMapping,
  type ExcelCommitMapping,
  type ImportFormat,
} from "../lib/importFlow";

export function Knowledge() {
  const { list, create, switchStore, remove, compile } = useStore();
  const { stores, selectedId, setStores, setSelectedId } = useKnowledgeStore();
  const [newName, setNewName] = useState("");
  const [importText, setImportText] = useState("");
  const [importFormat, setImportFormat] = useState<"markdown" | "json">("markdown");
  const [importTarget, setImportTarget] = useState("");

  // ---- P1 审核流（Excel/PDF；.md/.json 直连流程不动） ----
  const preview = useImportPreview();
  const commit = useImportCommit();
  const [auditFile, setAuditFile] = useState<{
    name: string;
    format: ImportFormat;
    contentB64: string;
  } | null>(null);
  const [auditStep, setAuditStep] = useState<"idle" | "mapping" | "review" | "done">("idle");
  const [auditMapping, setAuditMapping] = useState<ExcelCommitMapping>({});
  const [pdfEntity, setPdfEntity] = useState("");
  const [mappingProblems, setMappingProblems] = useState<string[]>([]);
  const [auditFileError, setAuditFileError] = useState<string | null>(null);
  const [fileKey, setFileKey] = useState(0);

  /** 取消：内存状态全清、请求状态重置、文件框重挂载——无残留。 */
  function resetAudit() {
    setAuditFile(null);
    setAuditStep("idle");
    setAuditMapping({});
    setPdfEntity("");
    setMappingProblems([]);
    setAuditFileError(null);
    preview.reset();
    commit.reset();
    setFileKey((k) => k + 1);
  }

  async function onPickAuditFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.currentTarget.files?.[0];
    if (file == null) return;
    setMappingProblems([]);
    setAuditFileError(null);
    preview.reset();
    commit.reset();
    const format = detectImportFormat(file.name);
    if (format == null) {
      setAuditFileError(`不支持的文件类型：${file.name}（仅 .xlsx/.xls/.pdf）`);
      return;
    }
    if (file.size > IMPORT_MAX_BYTES) {
      setAuditFileError(
        `文件过大（${(file.size / 1048576).toFixed(1)} MiB，上限 10 MiB），请拆分后导入`,
      );
      return;
    }
    const contentB64 = await fileToBase64(file);
    const info = { name: file.name, format, contentB64 };
    setAuditFile(info);
    preview.mutate(
      { format, content_b64: contentB64, filename: file.name },
      {
        onSuccess: (data) => {
          if (data.format === "excel") {
            setAuditMapping(buildInitialMapping(data));
            setAuditStep("mapping");
          } else {
            setAuditStep("review");
          }
        },
      },
    );
  }

  function gotoReview() {
    if (preview.data?.format !== "excel" || auditFile == null) return;
    const problems = validateExcelMapping(preview.data, auditMapping);
    setMappingProblems(problems);
    if (problems.length === 0) setAuditStep("review");
  }

  function doCommit() {
    if (auditFile == null || preview.data == null) return;
    const target = importTarget.trim();
    const base = {
      ...(target ? { store_name: target } : {}),
      ...(selectedId != null && !target ? { store_id: selectedId } : {}),
      format: auditFile.format,
      content_b64: auditFile.contentB64,
      filename: auditFile.name,
    } as const;
    const body =
      auditFile.format === "excel"
        ? { ...base, mapping: cleanExcelMapping(auditMapping) }
        : pdfEntity.trim()
          ? { ...base, mapping: { entity: pdfEntity.trim() } }
          : { ...base };
    commit.mutate(body, {
      onSuccess: () => setAuditStep("done"),
    });
  }

  const excelData = preview.data?.format === "excel" ? preview.data : null;
  const pdfData = preview.data?.format === "pdf" ? preview.data : null;
  const reviewSummary =
    excelData != null
      ? excelPreviewToSummary(excelData, auditMapping)
      : pdfData != null
        ? pdfPreviewToSummary(pdfData, pdfEntity || undefined)
        : null;
  const reviewProblems = excelData != null ? validateExcelMapping(excelData, auditMapping) : [];
  const unsupported = preview.isError ? parseUnsupportedDetail(preview.error) : null;
  const targetLabel =
    importTarget.trim() ||
    stores.find((s) => s.id === selectedId)?.name ||
    "当前库";

  useEffect(() => {
    if (list.data != null) {
      setStores(list.data);
      const current = list.data.find((s) => s.is_current);
      if (current != null) setSelectedId(current.id);
    }
  }, [list.data, setStores, setSelectedId]);

  async function onPickFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.currentTarget.files?.[0];
    if (file == null) return;
    const text = await file.text();
    setImportText(text);
    const lower = file.name.toLowerCase();
    setImportFormat(lower.endsWith(".json") ? "json" : "markdown");
  }

  function doCompile() {
    const content = importText.trim();
    if (!content) return;
    const target = importTarget.trim();
    compile.mutate({
      ...(target ? { store_name: target } : {}),
      ...(selectedId != null && !target ? { store_id: selectedId } : {}),
      format: importFormat,
      content,
    });
  }

  return (
    <div>
      <h2>知识管理</h2>

      <div>
        <h3>知识库</h3>
        {list.isLoading && <p>加载中…</p>}
        {list.isError && <p>加载失败</p>}
        <ul data-testid="store-list">
          {stores.map((s) => (
            <li key={s.id}>
              <span>
                {s.name}（问答 {s.qa_count} / 字段 {s.field_count}）
              </span>{" "}
              {s.is_current && <span data-testid="current-badge">当前</span>}{" "}
              <button
                type="button"
                disabled={switchStore.isPending}
                onClick={() => switchStore.mutate(s.id)}
              >
                切换
              </button>{" "}
              <button
                type="button"
                disabled={remove.isPending}
                onClick={() => {
                  if (window.confirm(`删除知识库「${s.name}」？级联清理问答与向量。`)) {
                    remove.mutate(s.id);
                  }
                }}
              >
                删除
              </button>
            </li>
          ))}
        </ul>
        <form
          className="row"
          onSubmit={(e) => {
            e.preventDefault();
            const name = newName.trim();
            if (name) {
              create.mutate(name);
              setNewName("");
            }
          }}
        >
          <input
            data-testid="store-name-input"
            value={newName}
            onChange={(e) => setNewName(e.currentTarget.value)}
            placeholder="新知识库名称"
          />
          <button data-testid="store-create" type="submit" disabled={create.isPending}>
            创建
          </button>
        </form>
      </div>

      <div>
        <h3>导入（.md / .json）</h3>
        <input
          data-testid="import-file"
          type="file"
          accept=".md,.markdown,.json,.txt"
          onChange={onPickFile}
        />
        <div>
          <label>
            格式：
            <select
              data-testid="import-format"
              value={importFormat}
              onChange={(e) => setImportFormat(e.currentTarget.value as "markdown" | "json")}
            >
              <option value="markdown">markdown</option>
              <option value="json">json</option>
            </select>
          </label>{" "}
          <label>
            目标库名（空=当前选中库）：
            <input
              data-testid="import-target"
              value={importTarget}
              onChange={(e) => setImportTarget(e.currentTarget.value)}
              placeholder="默认当前库"
            />
          </label>
        </div>
        <textarea
          data-testid="import-text"
          value={importText}
          onChange={(e) => setImportText(e.currentTarget.value)}
          rows={8}
          cols={60}
          placeholder="粘贴 Markdown / JSON，或上方选择文件"
        />
        <div>
          <button
            data-testid="import-compile"
            type="button"
            disabled={compile.isPending || importText.trim() === ""}
            onClick={doCompile}
          >
            {compile.isPending ? "编译中…" : "编译导入"}
          </button>
        </div>
        {compile.isError && <p>编译失败</p>}
        {compile.data != null && (
          <pre data-testid="compile-stats">
            {JSON.stringify(compile.data.stats, null, 2)}
          </pre>
        )}
      </div>

      <div>
        <h3>导入（Excel / PDF，走审核流）</h3>
        <input
          key={fileKey}
          data-testid="audit-file"
          type="file"
          accept=".xlsx,.xls,.pdf"
          onChange={(e) => void onPickAuditFile(e)}
        />
        <p>目标库与上方「目标库名」共用（空=当前选中库）。先预览提案 → 确认映射 → 审核语义范围 → 入库。</p>
        {auditFileError != null && <p data-testid="audit-file-error">{auditFileError}</p>}
        {preview.isPending && <p data-testid="audit-preview-loading">预览解析中…（只读，不写库）</p>}
        {preview.isError && unsupported != null && (
          <div data-testid="audit-unsupported">
            <p>该 PDF 为扫描件/图片型，本版不支持 OCR，已拒绝（未写入任何数据）。</p>
            <p>{unsupported.message}</p>
            {unsupported.pages.length > 0 && <p>不可读页：{unsupported.pages.join("、")}</p>}
            <button data-testid="audit-cancel" type="button" onClick={resetAudit}>
              取消
            </button>
          </div>
        )}
        {preview.isError && unsupported == null && (
          <div>
            <p data-testid="audit-error">{importErrorMessage(preview.error)}</p>
            <button data-testid="audit-cancel" type="button" onClick={resetAudit}>
              取消
            </button>
          </div>
        )}
        {auditStep === "mapping" && excelData != null && (
          <div>
            <ColumnMapping
              preview={excelData}
              mapping={auditMapping}
              onMappingChange={setAuditMapping}
            />
            {mappingProblems.length > 0 && (
              <ul data-testid="audit-mapping-problems">
                {mappingProblems.map((p, i) => (
                  <li key={i}>{p}</li>
                ))}
              </ul>
            )}
            <button data-testid="audit-goto-review" type="button" onClick={gotoReview}>
              进入审核
            </button>{" "}
            <button data-testid="audit-cancel" type="button" onClick={resetAudit}>
              取消
            </button>
          </div>
        )}
        {auditStep === "review" && reviewSummary != null && (
          <ReviewPanel
            summary={reviewSummary}
            targetLabel={targetLabel}
            problems={reviewProblems}
            entityOverride={pdfEntity}
            showEntityOverride={pdfData != null}
            onEntityOverrideChange={setPdfEntity}
            isPending={commit.isPending}
            onConfirm={doCommit}
            onCancel={resetAudit}
          />
        )}
        {commit.isError && (
          <div>
            <p data-testid="audit-commit-error">{importErrorMessage(commit.error)}</p>
            <button data-testid="audit-cancel" type="button" onClick={resetAudit}>
              取消
            </button>
          </div>
        )}
        {auditStep === "done" && commit.data != null && (
          <div>
            <p data-testid="audit-done">入库完成（store {commit.data.store_id}）。</p>
            <pre data-testid="audit-done-stats">
              {JSON.stringify(commit.data.stats, null, 2)}
            </pre>
            <button data-testid="audit-close" type="button" onClick={resetAudit}>
              关闭
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
