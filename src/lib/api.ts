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

  constructor(kind: ApiErrorKind, message: string, status?: number) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
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
  try {
    return await fetch(`http://127.0.0.1:${port}${path}`, {
      ...init,
      headers,
      signal: controller.signal,
    });
  } catch (e) {
    throw classifyFetchError(e);
  } finally {
    clearTimeout(timer);
  }
}

async function parseJsonOrThrow(res: Response, what: string) {
  if (res.status === 401) {
    throw new ApiError("unauthorized", "未授权（token 缺失或失效）", 401);
  }
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new ApiError("http", `${what}失败：HTTP ${res.status} ${body}`, res.status);
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
