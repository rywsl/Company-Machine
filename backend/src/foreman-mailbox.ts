import { chmodSync, mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CtlServer, type CtlResponse } from "./legacy/factoryctl";

export const foremanMailboxTarget = "/mao/foreman";

export const foremanMailboxContract = [
  "通过当前轮的 MAO 文件邮箱执行获准的业务操作。",
  "请求目录是 /mao/foreman/requests，响应目录是 /mao/foreman/responses。",
  "选择本轮唯一的 r- 加 32 个小写十六进制字符作为 id。先写临时文件，再以 0644 权限原子重命名为 <id>.json。",
  "请求 JSON 包含 id、op 和 args。等待并读取 responses/<id>.json 的 ok、result 和 error 后再继续。",
  "business.units 使用 systemId。business.graph 使用 systemId 和 graph。",
  "business.bind 使用 systemId、unitId、harness 和可选 model。",
  "business.artifact 使用 projectId、kind、ref、summary 和可选的 recordType、files。files 只含文件引用。",
  "business.conversion 使用 systemId、kind、summary 和可选 inputs、outputs。business.run 使用 systemId 和可选 inputs。",
  "文件和物料使用引用，密钥通过项目资源请求流程处理。",
].join(" ");

type FetchBusiness = (request: Request) => Promise<Response>;
type Scope = { systems: ReadonlySet<string>; projectSystem: (projectId: string) => string | undefined; projectId?: string };
type Operation = { method: "GET" | "POST" | "PATCH"; path: string; body?: Record<string, unknown> };

const object = (value: unknown): value is Record<string, unknown> =>
  Boolean(value && typeof value === "object" && !Array.isArray(value));
const identifier = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,191}$/.test(value);
const text = (value: unknown): value is string => typeof value === "string" && Boolean(value.trim());
const reference = (value: unknown): value is string =>
  text(value) && !/^(?:[\\/]|[a-z]:[\\/]|(?:file|vault|secret):)/i.test(value.trim());
const references = (value: unknown): boolean => value === undefined || Array.isArray(value) && value.every(reference);

function operation(op: string, args: Record<string, unknown>, scope: Scope): Operation | undefined {
  const { systemId, projectId, unitId, harness, model, graph, kind, ref, summary, recordType, files, inputs, outputs } = args;
  if (op === "business.artifact") {
    if (!identifier(projectId) || (scope.projectId && projectId !== scope.projectId)) return;
    const system = scope.projectSystem(projectId);
    if (!system || !scope.systems.has(system) || !reference(ref) || !text(kind) || !text(summary)) return;
    if (recordType !== undefined && recordType !== "artifact" && recordType !== "material") return;
    if (files !== undefined && (!Array.isArray(files) || files.some((file) => !object(file) || !reference(file.ref)))) return;
    return { method: "POST", path: "/api/v1/projects/" + projectId + "/artifacts", body: {
      kind, ref, summary, record_type: recordType, files: (files as Array<{ ref: string }> | undefined)?.map(({ ref: fileRef }) => ({ ref: fileRef })),
    } };
  }
  if (!identifier(systemId) || !scope.systems.has(systemId)) return;
  const path = "/api/v1/business-systems/" + systemId;
  switch (op) {
    case "business.units":
      return { method: "GET", path: path + "/units" };
    case "business.graph":
      if (object(graph)) return { method: "POST", path: path + "/graph", body: { graph, by: "foreman" } };
      return;
    case "business.bind":
      if (identifier(unitId) && text(harness) && (model === undefined || text(model))) {
        return { method: "PATCH", path: path + "/units/" + encodeURIComponent(unitId) + "/binding", body: { harness_runtime: harness, model } };
      }
      return;
    case "business.conversion":
      if (text(kind) && text(summary) && references(inputs) && references(outputs)) {
        return { method: "POST", path: path + "/conversions", body: { kind, summary, inputs, outputs } };
      }
      return;
    case "business.run":
      if (references(inputs)) return { method: "POST", path: path + "/runs", body: { trigger: "foreman", inputs } };
  }
}

function reported(op: string, value: unknown): unknown {
  if (op === "business.units") {
    if (!Array.isArray(value)) return [];
    return value.filter(object).map(({ id, kind, projectId, name, status }) => ({ id, kind, projectId, name, status }));
  }
  if (!object(value)) return null;
  if (op === "business.bind" && object(value.unit) && object(value.binding)) {
    return { id: value.unit.id, harness: value.binding.harnessRuntime, model: value.binding.model };
  }
  return { id: value.id, status: value.status, ref: value.ref };
}

function denied(id: string, code: string, message: string): CtlResponse {
  return { id, ok: false, result: null, error: { code, message } };
}

export function openForemanMailbox(scope: Scope, fetchBusiness: FetchBusiness) {
  const root = mkdtempSync(join(tmpdir(), "mao-foreman-"));
  let active = true;
  let stopped: Promise<void> | undefined;
  try {
    const requests = join(root, "requests");
    const responses = join(root, "responses");
    mkdirSync(requests);
    mkdirSync(responses);
    chmodSync(requests, 0o1777);
    chmodSync(responses, 0o755);
    const server = new CtlServer(root, {
      async handle({ id, op, args }) {
        if (!active) return denied(id, "FOREMAN_MAILBOX_CLOSED", "厂长邮箱已关闭");
        const request = operation(op, args, scope);
        if (!request) return denied(id, "FOREMAN_OPERATION_INVALID", "操作参数无效，或目标不在当前轮的范围内");
        const response = await fetchBusiness(new Request("http://localhost" + request.path, {
          method: request.method,
          ...(request.body && { headers: { "content-type": "application/json" }, body: JSON.stringify(request.body) }),
        }));
        if (!response.ok) return denied(id, "FOREMAN_OPERATION_FAILED", "业务操作失败（HTTP " + response.status + "）");
        return { id, ok: true, result: reported(op, await response.json()), error: null };
      },
    }, { responseMode: 0o644 });
    server.start();
    return {
      root,
      flush: () => server.pollOnce(),
      stop: () => { active = false; return stopped ??= server.stop(); },
      remove: () => rmSync(root, { recursive: true, force: true, maxRetries: 3 }),
    };
  } catch (error) {
    rmSync(root, { recursive: true, force: true, maxRetries: 3 });
    throw error;
  }
}
