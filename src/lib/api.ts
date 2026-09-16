import { invoke } from "@tauri-apps/api/core";

/** Tauri degraded-mode event (mirrors Rust `DEGRADED_EVENT`). */
export const SIDECAR_DEGRADED_EVENT = "sidecar://degraded";

export type SidecarCredentials = {
  port: number;
  token: string;
};

export type SidecarDegraded = {
  reason: string;
  detail: string;
} | null;

export type ApiErrorKind =
  | "unauthorized"
  | "network"
  | "timeout"
  | "http"
  | "expired";

export class ApiError extends Error {
  kind: ApiErrorKind;
  status?: number;
  /** 服务端错误体（JSON 可解析时为对象，否则为 undefined；R17 导入流用它读 422 详情）。 */
  payload?: unknown;

  constructor(kind: ApiErrorKind, message: string, status?: number, payload?: unknown) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.payload = payload;
  }
}

// NOTE (R8): the token travels in memory only — never log it, never persist it.
export async function getSidecarCredentials(): Promise<SidecarCredentials> {
  return invoke<SidecarCredentials>("get_sidecar_credentials");
}

export function classifyFetchError(e: unknown): ApiError {
  if (e instanceof ApiError) return e;
  if (e instanceof DOMException && e.name === "AbortError") {
    return new ApiError("timeout", "请求超时");
  }
  if (e instanceof TypeError) {
    // fetch 本体抛 TypeError = 网络不可达/CORS/连接被拒。
    return new ApiError("network", `网络错误：${(e as Error).message}`);
  }
  return new ApiError("network", String(e));
}

/** Authenticated sidecar fetch: attaches `Authorization: Bearer <token>`. */
export async function sidecarFetch(
  path: string,
  init?: RequestInit,
  timeoutMs = 30000,
): Promise<Response> {
  const { port, token } = await getSidecarCredentials().catch((e) => {
    throw classifyFetchError(e);
  });
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${token}`);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  // 调用方取消信号透传（否则 useQA 的 cancel 永远到不了 fetch）。
  const externalSignal = init?.signal as AbortSignal | undefined;
  const onExternalAbort = () => controller.abort();
  externalSignal?.addEventListener("abort", onExternalAbort, { once: true });
  try {
    return await fetch(`http://127.0.0.1:${port}${path}`, {
      ...init,
      headers,
      signal: controller.signal,
    });
  } catch (e) {
    // 调用方主动取消：原样透出 AbortError（调用方可区分“取消”与“超时”）；
    // 超时定时器触发的 abort 走错误分类。
    if (externalSignal?.aborted) throw e;
    throw classifyFetchError(e);
  } finally {
    clearTimeout(timer);
    externalSignal?.removeEventListener?.("abort", onExternalAbort);
  }
}

async function parseJsonOrThrow(res: Response, what: string) {
  if (res.status === 401) {
    throw new ApiError("unauthorized", "未授权（token 缺失或失效）", 401);
  }
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    let payload: unknown;
    try {
      payload = body ? JSON.parse(body) : undefined;
    } catch {
      payload = undefined;
    }
    throw new ApiError("http", `${what}失败：HTTP ${res.status} ${body}`, res.status, payload);
  }
  return res.json();
}

/** Authenticated JSON REST helpers (token auto-injected, errors classified). */
export async function apiGet<T>(path: string, timeoutMs = 30000): Promise<T> {
  const res = await sidecarFetch(path, { method: "GET" }, timeoutMs);
  return parseJsonOrThrow(res, `GET ${path}`);
}

export async function apiPost<T>(
  path: string,
  body: unknown,
  timeoutMs = 30000,
): Promise<T> {
  const res = await sidecarFetch(
    path,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
    timeoutMs,
  );
  return parseJsonOrThrow(res, `POST ${path}`);
}

export async function apiPut<T>(
  path: string,
  body: unknown,
  timeoutMs = 30000,
): Promise<T> {
  const res = await sidecarFetch(
    path,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
    timeoutMs,
  );
  return parseJsonOrThrow(res, `PUT ${path}`);
}

export async function apiDelete<T>(path: string, timeoutMs = 30000): Promise<T> {
  const res = await sidecarFetch(path, { method: "DELETE" }, timeoutMs);
  return parseJsonOrThrow(res, `DELETE ${path}`);
}

// ------------------------------------------------------------------ 会话账（S3 前端接线）
//
// 这四个端点由 taskS1 在 `sidecar/src/routers/session.py` 实现（`GET /sessions`、
// `GET /session/{id}`、`DELETE /session/{id}`、`GET /session/status`）。
// 形状**逐字镜像**服务端：字段名是 snake_case，不做重命名 —— 重命名会让
// "前端改一处、后端改一处"变成两份契约，而这里只有一份（后端）。
//
// R14：下面所有字段都是**数字与枚举**，没有转写文本、没有问题与答案。
// 账里本来就没有这些（写入侧按 R14 不记），不是读端点做了脱敏 ——
// 要让时间线显示文本，必须先裁定 F11.3（录什么/存多久/给谁看）并改写入格式。

export type SessionSummary = {
  session_id: string;
  /** 会话首行的写入时刻（epoch 毫秒；服务端为 `MIN(ts_ms)`）。 */
  started_ms: number;
  /** 会话最后一行的写入时刻；未关的会话会随新事件继续增长。 */
  last_ms: number;
  /** 该会话的账行总数（含 session_begin/end、asr_*、qa_exchange）。 */
  events: number;
  store_id: string | null;
  /** 有没有 `session_end` 行。false = 进程被杀那一类，不是数据损坏。 */
  closed: boolean;
  /**
   * 该会话的问答次数。**S1 的列表端点目前不下发**（只给总账行数），
   * 下发后前端直接采用（前向兼容；未下发时列表退化为展示总事件数）。
   */
  qa_exchanges?: number;
};

export type SessionsPage = {
  sessions: SessionSummary[];
  total: number;
  limit: number;
  offset: number;
};

export type SessionEvent = {
  id: number;
  event_type: string;
  stage: string | null;
  /** 写入时刻（epoch 毫秒）；v002 之前的老行为 null。 */
  ts_ms: number | null;
  store_id: string | null;
  /** content-free：数字与枚举。脏 JSON 时是 `{_unparsable: 原始文本}`。 */
  metadata: Record<string, unknown>;
};

export type SessionTimeline = {
  session_id: string;
  events: SessionEvent[];
  count: number;
};

export type DeleteSessionResult = { session_id: string; deleted: number };

export type SessionStatusResponse = {
  session_id: string | null;
  source: string | null;
  counters: Record<string, number>;
  event_counts: Record<string, number>;
  /** 每类账行最后一行的**字段名**（只有 key，没有 value）。 */
  event_fields: Record<string, string[]>;
};

/** 会话列表（时间倒序 + 分页）。 */
export function fetchSessions(limit = 20, offset = 0): Promise<SessionsPage> {
  const q = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return apiGet<SessionsPage>(`/sessions?${q.toString()}`);
}

/** 一次会话的完整时间线（按发生序，行 id 升序）。不存在 → 404。 */
export function fetchSessionTimeline(sessionId: string): Promise<SessionTimeline> {
  return apiGet<SessionTimeline>(`/session/${encodeURIComponent(sessionId)}`);
}

/** 当前会话（列表页据此标"进行中"，并预判删除会撞 409）。 */
export function fetchSessionStatus(): Promise<SessionStatusResponse> {
  return apiGet<SessionStatusResponse>("/session/status");
}

/**
 * 物理删除一次会话的全部账行（不是软删）。
 * - 404：这个会话一行都没有（拼错 id / 库被清过）。
 * - 409：目标是**当前开着的会话**，先 `/session/end` 再删。
 */
export function deleteSession(sessionId: string): Promise<DeleteSessionResult> {
  return apiDelete<DeleteSessionResult>(`/session/${encodeURIComponent(sessionId)}`);
}

// ------------------------------------------------------------------ 会话录制的管理面（S4）

/**
 * 录制设置。**默认关**：没存过 / 文件读不出来一律按关闭处理（宁可少记）。
 * `retention_days` 为 `null` 表示永久保留（不自动删）。
 */
export type SessionSettings = {
  enabled: boolean;
  retention_days: number | null;
  updated_ms: number;
};

export type SessionUsage = {
  sessions: number;
  events: number;
  payload_bytes: number;
  oldest_ms: number | null;
  newest_ms: number | null;
  active_session_id: string | null;
};

export type PurgeResult = { deleted: number; sessions_deleted: number };

export type SessionExport = { filename: string; content: string };

export function fetchSessionSettings(): Promise<SessionSettings> {
  return apiGet<SessionSettings>("/sessions/settings");
}

/** 只改传了的字段（`retention_days` 缺席 = 别动这一项，null = 永久）。 */
export function putSessionSettings(body: {
  enabled?: boolean;
  retention_days?: number | null;
}): Promise<SessionSettings> {
  return apiPut<SessionSettings>("/sessions/settings", body);
}

export function fetchSessionUsage(): Promise<SessionUsage> {
  return apiGet<SessionUsage>("/sessions/usage");
}

/**
 * 清除全部会话账行（**物理删除**）。
 * `confirm` 为假 → 400（二次确认在前端，这里是第二道闸）。
 */
export function purgeSessions(): Promise<PurgeResult> {
  return apiPost<PurgeResult>("/sessions/purge", { confirm: true });
}

/** 单会话导出（**只有 JSON**：MD 渲染器不存在，按裁定不新建）。 */
export function exportSession(sessionId: string): Promise<SessionExport> {
  return apiGet<SessionExport>(`/session/${encodeURIComponent(sessionId)}/export`);
}

// ------------------------------------------------------------------ 面试陪练（S5）

/** 库里实际存在的类目（不硬编码 PRD 的 6 个 —— 用户导入的库可能一个都没有）。 */
export type RehearsalCategory = { category: string; count: number };

export type RehearsalCategories = {
  store_id: string;
  categories: RehearsalCategory[];
  total: number;
};

/** 一道练习题。自评要"对照 standard_question/official_answer"，故两者都带回来。 */
export type RehearsalQuestion = {
  id: string;
  standard_question: string;
  official_answer: string;
  category: string;
};

export type RehearsalDraw = {
  store_id: string;
  questions: RehearsalQuestion[];
  /** 实际抽到几道（`n` 超过池子时会小于请求值，小库是常态）。 */
  drawn: number;
  pool: number;
  /** 给定 seed 时回同一个值；未给定回 null。 */
  seed: number | null;
  note?: string;
};

/** 自评档位。三档固定 —— 自由文本会让"自评分布"无法聚合。 */
export type RehearsalVerdict = "correct" | "partial" | "unknown";

export type RehearsalVerdictResult = {
  /** false = 没记账（无会话 / 录制开关关着）。**不是错误**：练习本身照常。 */
  recorded: boolean;
  session_id: string | null;
};

/**
 * 开一次会话。`source` 区分"这次会话是采集还是练习"（落进 `session_begin` 的 stage）。
 *
 * `recording: false` 表示**录制开关关着**（S4 门禁）—— 不建会话，但**不是错误**：
 * 调用方据此明示"本次不记账"，而不是当成失败。
 */
export function beginSession(source = "capture"): Promise<{
  session_id: string | null;
  already: boolean;
  recording: boolean;
}> {
  return apiPost("/session/begin", { source });
}

/** 关当前会话。没有会话时 `closed:false`（幂等，不报错）。 */
export function endSession(sessionId?: string): Promise<{
  session_id: string | null;
  closed: boolean;
}> {
  return apiPost("/session/end", sessionId != null ? { session_id: sessionId } : {});
}

export function fetchRehearsalCategories(
  storeId?: string,
): Promise<RehearsalCategories> {
  const q = new URLSearchParams();
  if (storeId != null) q.set("store_id", storeId);
  const s = q.toString();
  return apiGet<RehearsalCategories>(`/rehearsal/categories${s ? `?${s}` : ""}`);
}

/**
 * 抽题（无放回）。
 * `seed` 给定即确定性 —— 同一组题可以重练，测试也能断言顺序。
 */
export function drawRehearsalQuestions(body: {
  store_id?: string;
  category?: string;
  n?: number;
  seed?: number;
}): Promise<RehearsalDraw> {
  return apiPost<RehearsalDraw>("/rehearsal/draw", body);
}

/** 记一次自评。把档位/类目/语料 id 与题面/标准答案/用户作答一并发送（账有文本，日志无文本——R14）。 */
export function postRehearsalVerdict(body: {
  qa_id: string;
  category?: string;
  verdict: RehearsalVerdict;
  store_id?: string;
  question_text?: string;
  official_answer?: string;
  user_answer?: string;
}): Promise<RehearsalVerdictResult> {
  return apiPost<RehearsalVerdictResult>("/rehearsal/verdict", body);
}

// ------------------------------------------------------------------ 模板系统（S7）

/** 列表项（不含 payload —— 列表不搬整包正文）。 */
export type TemplateSummary = {
  id: string;
  name: string;
  description: string | null;
  category: string;
  version: number;
  /** true = 随 sidecar 打包的预置模板（不可改不可删）。 */
  is_preset: boolean;
  created_at: string | null;
  updated_at: string | null;
};

/** 模板载荷：与知识导入同构（`qa_pairs` + `fields`）。 */
export type TemplatePayload = {
  qa_pairs?: Record<string, unknown>[];
  fields?: Record<string, unknown>[];
};

export type TemplateDetail = TemplateSummary & { payload: TemplatePayload };

export type CreatedTemplate = { id: string; message: string };
export type UpdatedTemplate = { message: string };
export type DeletedTemplate = { deleted: string };
export type StoreFromTemplate = { store_id: string };

export function fetchTemplates(category?: string): Promise<TemplateSummary[]> {
  const q = new URLSearchParams();
  if (category != null && category !== "") q.set("category", category);
  const s = q.toString();
  return apiGet<TemplateSummary[]>(`/templates${s ? `?${s}` : ""}`);
}

export function fetchTemplate(templateId: string): Promise<TemplateDetail> {
  return apiGet<TemplateDetail>(`/templates/${encodeURIComponent(templateId)}`);
}

export function createTemplate(body: {
  name: string;
  description?: string;
  category: string;
  payload: TemplatePayload;
  is_preset?: boolean;
}): Promise<CreatedTemplate> {
  return apiPost<CreatedTemplate>("/templates", body);
}

export function updateTemplate(
  templateId: string,
  body: {
    name?: string;
    description?: string;
    category?: string;
    payload?: TemplatePayload;
  },
): Promise<UpdatedTemplate> {
  return apiPut<UpdatedTemplate>(`/templates/${encodeURIComponent(templateId)}`, body);
}

/** 删除自定义模板。预置模板 → 403（服务端拒绝，前端不该把它当"删掉了"）。 */
export function deleteTemplate(templateId: string): Promise<DeletedTemplate> {
  return apiDelete<DeletedTemplate>(`/templates/${encodeURIComponent(templateId)}`);
}

/** 「按模板建库」：读模板 payload → 建 store → 批量导入问答与字段。 */
export function createStoreFromTemplate(body: {
  template_id: string;
  name: string;
}): Promise<StoreFromTemplate> {
  return apiPost<StoreFromTemplate>("/store/from-template", body);
}
