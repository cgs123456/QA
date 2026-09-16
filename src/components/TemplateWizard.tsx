import { useState } from "react";
import type { TemplatePayload, TemplateSummary } from "../lib/api";

interface TemplateWizardProps {
  templates: TemplateSummary[];
  selectedTemplateId: string;
  onTemplateSelect: (id: string) => void;
  newStoreName: string;
  onStoreNameChange: (name: string) => void;
  onCreateFromTemplate: (templateId: string, storeName: string) => Promise<void>;
  onCreateCustomTemplate: (
    templateName: string,
    description: string,
    category: string,
    payload: TemplatePayload,
  ) => Promise<void>;
  onClose: () => void;
  templateError: string | null;
  saving: boolean;
}

const CATEGORIES = [
  { value: "interview", label: "面试题库" },
  { value: "knowledge", label: "企业知识库" },
  { value: "product", label: "产品规格书" },
  { value: "custom", label: "自定义" },
];

export function TemplateWizard({
  templates,
  selectedTemplateId,
  onTemplateSelect,
  newStoreName,
  onStoreNameChange,
  onCreateFromTemplate,
  onCreateCustomTemplate,
  onClose,
  templateError,
  saving,
}: TemplateWizardProps) {
  const [step, setStep] = useState<"select" | "custom" | "confirm">("select");
  const [customName, setCustomName] = useState("");
  const [customDescription, setCustomDescription] = useState("");
  const [customCategory, setCustomCategory] = useState("custom");
  const [payloadQa, setPayloadQa] = useState("");
  const [payloadFields, setPayloadFields] = useState("");
  const [customError, setCustomError] = useState<string | null>(null);

  const selectedTemplate = templates.find((t) => t.id === selectedTemplateId);

  if (step === "select") {
    return (
      <div>
        <h2>从模板创建知识库</h2>
        <p style={{ color: "#666", marginBottom: 16 }}>
          选择一个预置模板，或创建自定义模板。模板包含预设的问答对与字段定义，创建后即可直接使用。
        </p>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 12, marginBottom: 16 }}>
          {CATEGORIES.map((cat) => {
            const catTemplates = templates.filter((t) => t.category === cat.value);
            if (catTemplates.length === 0 && cat.value !== "custom") return null;
            return (
              <div key={cat.value} style={{ border: "1px solid #ddd", borderRadius: 8, padding: 16 }}>
                <h4 style={{ margin: "0 0 8px 0" }}>{cat.label}</h4>
                {catTemplates.map((t) => (
                  <label
                    key={t.id}
                    style={{
                      display: "block",
                      padding: "8px 12px",
                      margin: "4px 0",
                      border: selectedTemplateId === t.id ? "2px solid #007bff" : "1px solid #eee",
                      borderRadius: 4,
                      cursor: "pointer",
                      background: selectedTemplateId === t.id ? "#f0f7ff" : "white",
                    }}
                    onClick={() => onTemplateSelect(t.id)}
                  >
                    <strong>{t.name}</strong>{" "}
                    {t.is_preset && <span style={{ fontSize: 11, color: "#28a745", marginLeft: 8 }}>预置</span>}
                    <div style={{ fontSize: 12, color: "#666", marginTop: 4 }}>{t.description}</div>
                  </label>
                ))}
                {cat.value === "custom" && (
                  <button
                    type="button"
                    style={{
                      width: "100%",
                      padding: "12px",
                      border: "2px dashed #007bff",
                      borderRadius: 4,
                      background: "transparent",
                      color: "#007bff",
                      cursor: "pointer",
                    }}
                    onClick={() => {
                      setStep("custom");
                    }}
                  >
                    + 创建自定义模板
                  </button>
                )}
              </div>
            );
          })}
        </div>

        {selectedTemplateId && (
          <div style={{ marginTop: 16 }}>
            <label style={{ display: "block", marginBottom: 8 }}>
              新知识库名称：
              <input
                data-testid="wizard-store-name"
                type="text"
                value={newStoreName}
                // 原写 `(e) => e.currentTarget.value` —— 求值完就丢，等于**空操作**：
                // 受控输入框被 value 钉死，用户根本改不动名字（"创建知识库"永远禁用）。
                onChange={(e) => onStoreNameChange(e.currentTarget.value)}
                placeholder="如：我的面试题库"
                style={{ width: "100%", padding: 8, marginTop: 4, marginBottom: 16 }}
              />
            </label>
            <div style={{ display: "flex", gap: 8 }}>
              <button
                type="button"
                disabled={!newStoreName.trim() || saving}
                onClick={() => {
                  if (newStoreName.trim()) {
                    onCreateFromTemplate(selectedTemplateId, newStoreName.trim());
                  }
                }}
              >
                {saving ? "创建中…" : "创建知识库"}
              </button>
              <button type="button" onClick={onClose}>取消</button>
            </div>
            {templateError != null && (
              <p data-testid="wizard-error" style={{ color: "#c00" }}>
                {templateError}
              </p>
            )}
          </div>
        )}
      </div>
    );
  }

  if (step === "custom") {
    return (
      <div>
        <h2>创建自定义模板</h2>
        <p style={{ color: "#666", marginBottom: 16 }}>
          自定义模板将保存到模板列表，可随时复用。预置模板不可修改/删除。
        </p>

        {/* 错误只在**保存按钮上方**显示一次（原来顶部也有一份，同一条错误会在长表单里
            出现两遍，getByText 都会撞到多个匹配）。 */}
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div>
            <label style={{ display: "block", marginBottom: 4 }}>模板名称 *</label>
            <input
              type="text"
              value={customName}
              onChange={(e) => setCustomName(e.currentTarget.value)}
              placeholder="如：我的面试模板 v2"
              style={{ width: "100%", padding: 8 }}
            />
          </div>

          <div>
            <label style={{ display: "block", marginBottom: 4 }}>描述</label>
            <textarea
              value={customDescription}
              onChange={(e) => setCustomDescription(e.currentTarget.value)}
              rows={3}
              style={{ width: "100%", padding: 8 }}
            />
          </div>

          <div>
            <label style={{ display: "block", marginBottom: 4 }}>分类 *</label>
            <select
              value={customCategory}
              onChange={(e) => setCustomCategory(e.currentTarget.value)}
              style={{ width: "100%", padding: 8 }}
            >
              {CATEGORIES.filter((c) => c.value !== "custom").map((c) => (
                <option key={c.value} value={c.value}>{c.label}</option>
              ))}
            </select>
          </div>

          <div>
            <label style={{ display: "block", marginBottom: 4 }}>问答对 (JSON 数组，可留空) *</label>
            <textarea
              value={payloadQa}
              onChange={(e) => setPayloadQa(e.currentTarget.value)}
              rows={8}
              placeholder='[{"standard_question":"问题","official_answer":"答案","category":"分类","usage_status":"fixed"}]'
              style={{ width: "100%", padding: 8, fontFamily: "monospace", fontSize: 12 }}
            />
          </div>

          <div>
            <label style={{ display: "block", marginBottom: 4 }}>字段定义 (JSON 数组，可留空)</label>
            <textarea
              value={payloadFields}
              onChange={(e) => setPayloadFields(e.currentTarget.value)}
              rows={8}
              placeholder='[{"entity":"实体","field_name":"字段名","field_value":"值","aliases":["别名1","别名2"]}]'
              style={{ width: "100%", padding: 8, fontFamily: "monospace", fontSize: 12 }}
            />
          </div>
        </div>

        {customError && <div style={{ color: "red", marginTop: 12 }}>{customError}</div>}

        <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
          <button
            type="button"
            disabled={saving || !customName.trim()}
            onClick={async () => {
              if (!customName.trim()) {
                setCustomError("模板名称必填");
                return;
              }
              try {
                const payload: TemplatePayload = {};
                if (payloadQa.trim()) {
                  try { payload.qa_pairs = JSON.parse(payloadQa); } catch { setCustomError("问答对 JSON 格式错误"); return; }
                }
                if (payloadFields.trim()) {
                  try { payload.fields = JSON.parse(payloadFields); } catch { setCustomError("字段定义 JSON 格式错误"); return; }
                }
                await onCreateCustomTemplate(customName.trim(), customDescription, customCategory, payload);
                setStep("select");
                setCustomName("");
                setCustomDescription("");
                setPayloadQa("");
                setPayloadFields("");
              } catch (e: unknown) {
                setCustomError(e instanceof Error ? e.message : "保存失败");
              }
            }}
          >
            {saving ? "保存中…" : "保存模板"}
          </button>
          <button type="button" onClick={() => setStep("select")}>返回</button>
        </div>
      </div>
    );
  }

  if (step === "confirm") {
    return (
      <div>
        <h2>确认创建</h2>
        <p>即将创建知识库：<strong>{newStoreName}</strong></p>
        <p>基于模板：<strong>{selectedTemplate?.name}</strong></p>
        {templateError != null && (
          <p data-testid="wizard-error">{templateError}</p>
        )}
        <div style={{ marginTop: 16, display: "flex", gap: 8 }}>
          <button type="button" onClick={() => setStep("select")}>返回</button>
          <button
            type="button"
            data-testid="wizard-confirm-create"
            disabled={saving || !newStoreName.trim()}
            // 原写 `onClick={async () => {}}` —— **空函数**：确认按钮点了没有任何反应。
            onClick={() => {
              if (newStoreName.trim()) {
                void onCreateFromTemplate(selectedTemplateId, newStoreName.trim());
              }
            }}
          >
            {saving ? "创建中…" : "确认创建"}
          </button>
        </div>
      </div>
    );
  }

  return null;
}