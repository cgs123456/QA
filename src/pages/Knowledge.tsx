import { useEffect, useState } from "react";
import { useKnowledgeStore } from "../stores/knowledgeStore";
import { useStore } from "../hooks/useStore";

export function Knowledge() {
  const { list, create, switchStore, remove, compile } = useStore();
  const { stores, selectedId, setStores, setSelectedId } = useKnowledgeStore();
  const [newName, setNewName] = useState("");
  const [importText, setImportText] = useState("");
  const [importFormat, setImportFormat] = useState<"markdown" | "json">("markdown");
  const [importTarget, setImportTarget] = useState("");

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
    </div>
  );
}
