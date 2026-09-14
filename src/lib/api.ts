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

// NOTE (R8): the token travels in memory only — never log it, never persist it.
export async function getSidecarCredentials(): Promise<SidecarCredentials> {
  return invoke<SidecarCredentials>("get_sidecar_credentials");
}

/** Authenticated sidecar fetch: attaches `Authorization: Bearer <token>`. */
export async function sidecarFetch(
  path: string,
  init?: RequestInit,
): Promise<Response> {
  const { port, token } = await getSidecarCredentials();
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${token}`);
  return fetch(`http://127.0.0.1:${port}${path}`, { ...init, headers });
}
