import type { Bootstrap, Graph, EventRecord, AssetRecord, HarnessRecord, SecretRecord, TalkMessage } from "./types";

export class ApiError extends Error {
  status: number;
  payload: unknown;

  constructor(message: string, status: number, payload: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { Accept: "application/json", ...(options.headers ?? {}) },
  });
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const message = typeof payload === "object" && payload && "message" in payload
      ? String(payload.message)
      : "本地服务请求失败";
    throw new ApiError(message, response.status, payload);
  }
  return payload as T;
}

export const getBootstrap = () => api<Bootstrap>("/api/web/bootstrap");
export const getGraph = (scope: "factory" | "workarea", workarea?: string) =>
  api<Graph>(`/api/web/graph?scope=${scope}${workarea ? `&workarea=${encodeURIComponent(workarea)}` : ""}`);
export const getTalk = () => api<TalkMessage[]>("/api/web/talk?limit=80");
export const getAssets = () => api<AssetRecord[]>("/api/web/assets?limit=100");
export const getLogs = () => api<EventRecord[]>("/api/web/logs?limit=100");
export const getHarnesses = () => api<HarnessRecord[]>("/api/web/harnesses");
export const getSecrets = () => api<SecretRecord[]>("/api/web/secrets?limit=100");
export const sendTalk = (text: string) => api<{ state: string; message: TalkMessage }>("/api/web/talk", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ text }),
});
