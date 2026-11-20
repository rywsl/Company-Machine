import type { Bootstrap, ForemanTurn, Graph, EventRecord, AssetRecord, HarnessRecord, HarnessProfile, OperatorBindingView, SecretRecord, StreamEvent, TalkMessage } from "./types";

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
    const detail = payload && typeof payload === "object" ? payload as Record<string, unknown> : {};
    const message = typeof detail.message === "string" ? detail.message
      : typeof detail.error === "string" ? detail.error : "本地服务请求失败";
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
export const sendTalk = (text: string) => api<{ state: string; message: TalkMessage; foremanTurn: ForemanTurn }>("/api/web/talk", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ text }),
});
export const cancelForemanTurn = (id: string) =>
  api<{ foremanTurn: ForemanTurn }>(`/api/v1/foreman/inbox/${encodeURIComponent(id)}/cancel`, { method: "POST" });

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
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  const source = new EventSource(`/api/v1/events${query}`);
  const handle = (message: MessageEvent<string>) => {
    try {
      const data = JSON.parse(message.data) as Record<string, unknown>;
      onEvent({
        id: message.lastEventId || undefined,
        type: message.type || String(data.type ?? "message"),
        data,
        at: typeof data.at === "string" ? data.at : undefined,
      });
    } catch {
      onError?.();
    }
  };
  source.onmessage = handle;
  for (const eventName of ["mao.event", "talk.message", "run.updated", "job.updated", "asset.sealed", "graph.updated", "bootstrap.updated"]) {
    source.addEventListener(eventName, handle as EventListener);
  }
  source.onerror = () => onError?.();
  return () => source.close();
}
