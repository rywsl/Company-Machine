import type { AssetPageData, Bootstrap, BusinessRun, ForemanTurn, Graph, EventRecord, HarnessRecord, HarnessProfile, OperatorBindingView, SecretRecord, StreamEvent, TalkMessage } from "./types";

export type WebScope = { business_system_id?: string; project_id?: string };

function scopeQuery(scope?: WebScope): string {
  if (!scope) return "";
  const query = new URLSearchParams();
  if (scope.business_system_id) query.set("business_system_id", scope.business_system_id);
  if (scope.project_id) query.set("project_id", scope.project_id);
  const encoded = query.toString();
  return encoded ? `&${encoded}` : "";
}

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

function authHeaders(): Record<string, string> {
  const runtime = globalThis as typeof globalThis & { __MAO_AUTH_TOKEN__?: string; __MAO_CSRF_TOKEN__?: string };
  const token = runtime.__MAO_AUTH_TOKEN__ ?? import.meta.env.VITE_MAO_AUTH_TOKEN
    ?? (typeof window !== "undefined" ? window.localStorage.getItem("mao-auth-token") ?? undefined : undefined);
  const csrf = runtime.__MAO_CSRF_TOKEN__ ?? import.meta.env.VITE_MAO_CSRF_TOKEN
    ?? (typeof window !== "undefined" ? window.localStorage.getItem("mao-csrf-token") ?? undefined : undefined);
  return {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(csrf ? { "x-mao-csrf": csrf } : {}),
  };
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { Accept: "application/json", ...authHeaders(), ...(options.headers ?? {}) },
  });
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const detail = payload && typeof payload === "object" ? payload as Record<string, unknown> : {};
    const message = typeof detail.message === "string" ? detail.message
      : typeof detail.error === "string" ? detail.error : "本地服务请求失败";
    throw new ApiError(message, response.status, payload);
  }
  return payload as T;
}

export const getBootstrap = (scope?: WebScope) => api<Bootstrap>(`/api/web/bootstrap?assetLimit=100${scopeQuery(scope)}`);
export const getGraph = (scope: "factory" | "workarea", workarea?: string) =>
  api<Graph>(`/api/web/graph?scope=${scope}${workarea ? `&workarea=${encodeURIComponent(workarea)}` : ""}`);
export const getTalk = (scope?: WebScope) => api<TalkMessage[]>(`/api/web/talk?limit=80${scopeQuery(scope)}`);
export const getAssetsPage = (olderCursor?: string, signal?: AbortSignal, scope?: WebScope) =>
  api<AssetPageData>(`/api/web/assets/page?limit=100${olderCursor ? `&older=${encodeURIComponent(olderCursor)}` : ""}${scopeQuery(scope)}`, { signal });
export const getLogs = (scope?: WebScope) => api<EventRecord[]>(`/api/web/logs?limit=100${scopeQuery(scope)}`);
export const getHarnesses = () => api<HarnessRecord[]>("/api/web/harnesses");
export const configureHarness = (config: Record<string, unknown>) => api<{ profile: HarnessProfile; foreman: Bootstrap["foreman"] }>("/api/web/harnesses/configure", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(config),
});
export const updateOperatorBinding = (systemId: string, unitId: string, harness: string, model?: string) =>
  api<{ binding: OperatorBindingView }>(`/api/v1/business-systems/${encodeURIComponent(systemId)}/units/${encodeURIComponent(unitId)}/binding`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ harness_runtime: harness, model: model ?? null }),
  });
export const verifyHarness = (profile_id: string) => api<{ profile: HarnessProfile }>("/api/web/harnesses/verify", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ profile_id }),
});
export const getSecrets = () => api<SecretRecord[]>("/api/web/secrets?limit=100");
export const ensureRuntime = () => api<{ dependency: { state: string; message?: string } }>("/api/v1/runtime/ensure-ready", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
});
export const sendTalk = (text: string, scope?: WebScope) => api<{ state: string; message: TalkMessage; foremanTurn: ForemanTurn }>("/api/web/talk", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ text, ...scope }),
});
export const cancelForemanTurn = (id: string) =>
  api<{ foremanTurn: ForemanTurn }>(`/api/v1/foreman/inbox/${encodeURIComponent(id)}/cancel`, { method: "POST" });

export const getBusinessRuns = (systemId: string) =>
  api<BusinessRun[]>(`/api/v1/business-systems/${encodeURIComponent(systemId)}/runs`);

export const createBusinessRun = (systemId: string, inputs: string[] = []) =>
  api<BusinessRun>(`/api/v1/business-systems/${encodeURIComponent(systemId)}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ trigger: "web", inputs }),
  });

export const dispatchBusinessRun = (runId: string) =>
  api<{ state: string; run_id: string }>(`/api/v1/runs/${encodeURIComponent(runId)}/dispatch`, { method: "POST" });

export const cancelBusinessRun = (runId: string) =>
  api<BusinessRun>(`/api/v1/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });

export const retryBusinessRun = (runId: string) =>
  api<BusinessRun>(`/api/v1/runs/${encodeURIComponent(runId)}/retry`, { method: "POST" });

export function submitProjectIntake(payload: {
  business_system_id?: string;
  name?: string;
  repo_url?: string;
  files?: Array<{ name: string; digest?: string; ref?: string }>;
  local_folder?: string;
}, uploads: File[] = []) {
  if (uploads.length) {
    const form = new FormData();
    for (const field of ["business_system_id", "name", "repo_url", "local_folder"] as const) {
      const value = payload[field];
      if (value) form.set(field, value);
    }
    for (const file of uploads) form.append("files", file, file.name);
    return api<{ project_id: string; state: string }>("/api/v1/projects/intake", { method: "POST", body: form });
  }
  return api<{ project_id: string; state: string }>("/api/v1/projects/intake", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export const requestProjectSecret = (payload: { project_id: string; name: string; purpose?: string; scope?: string }) =>
  api<{ request_id: string; state: string }>("/api/v1/secrets/requests", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

export const putProjectSecret = (request_id: string, value: string) => api<{ request_id: string; state: string }>(`/api/v1/secrets/requests/${encodeURIComponent(request_id)}/value`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ value }),
});

export function subscribeToEvents(
  onEvent: (event: StreamEvent) => void,
  onError?: () => void,
  cursor?: string,
): () => void {
  const controller = new AbortController();
  let closed = false;
  let nextCursor = cursor;
  let retryTimer: number | undefined;
  const parse = (frame: string) => {
    let id = "";
    let event = "message";
    const data: string[] = [];
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith("id:")) id = line.slice(3).trim();
      else if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (!data.length) return;
    try {
      const payload = JSON.parse(data.join("\n")) as Record<string, unknown>;
      if (id) nextCursor = id;
      onEvent({ id: id || undefined, type: event, data: payload, at: typeof payload.at === "string" ? payload.at : undefined });
    } catch { onError?.(); }
  };
  const connect = () => {
    if (closed) return;
    const query = nextCursor ? `?cursor=${encodeURIComponent(nextCursor)}` : "";
    void fetch(`/api/v1/events${query}`, { headers: authHeaders(), signal: controller.signal }).then(async (response) => {
    if (!response.ok || !response.body) throw new Error(`event stream failed: ${response.status}`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = "";
    try {
      while (!closed) {
        const chunk = await reader.read();
        if (chunk.done) break;
        pending += decoder.decode(chunk.value, { stream: true });
        const frames = pending.split(/\r?\n\r?\n/);
        pending = frames.pop() ?? "";
        for (const frame of frames) parse(frame);
      }
      pending += decoder.decode();
      if (pending.trim()) parse(pending);
      if (!closed) {
        onError?.();
        retryTimer = window.setTimeout(connect, 1_000);
      }
    } finally { reader.releaseLock(); }
    }).catch(() => {
      if (!closed) {
        onError?.();
        retryTimer = window.setTimeout(connect, 1_000);
      }
    });
  };
  connect();
  return () => { closed = true; if (retryTimer !== undefined) window.clearTimeout(retryTimer); controller.abort(); };
}
