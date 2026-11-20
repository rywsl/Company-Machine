import { chmodSync, mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CtlServer, type CtlResponse } from "./legacy/factoryctl";
import { parseRepositoryRegistration, type RegisterRepository } from "./project-registration";

export const foremanMailboxTarget = "/mao/foreman";

export const foremanMailboxContract = [
  "通过当前轮的 MAO 文件邮箱执行获准的业务操作。",
  "请求目录是 /mao/foreman/requests，响应目录是 /mao/foreman/responses。",
  "选择本轮唯一的 r- 加 32 个小写十六进制字符作为 id。先写临时文件，再以 0644 权限原子重命名为 <id>.json。",
  "请求 JSON 包含 id、op 和 args。等待并读取 responses/<id>.json 的 ok、result 和 error 后再继续。",
  "business.units 使用 systemId。business.graph 使用 systemId 和 graph。",
  "普通对话可用 project.register 登记仓库，传入 repoUrl 和可选 name。提供当前轮可见的 systemId 把项目添加到该业务系统，省略时新建业务系统。响应提供 projectId 和 systemId，后续操作直接使用这些 ID。",
  "business.bind 使用 systemId、unitId、harness 和可选 model。",
  "business.artifact 使用 projectId、kind、ref、summary 和可选的 recordType、files。files 只含文件引用。",
  "business.conversion 使用 systemId、kind、summary 和可选 inputs、outputs。business.run 使用 systemId 和可选 inputs。",
  "缺少必要密钥时使用 project.secret.request，提供 projectId、name 和可选 purpose、scope。每次申请一个所需密钥。响应只包含请求 id 和状态，请勿传入密钥值。",
  "文件和物料使用引用。",
].join(" ");

type FetchBusiness = (request: Request) => Promise<Response>;
type Scope = {
  systems: ReadonlySet<string>;
  projectSystem: (projectId: string) => string | undefined;
  projectId?: string;
  redactText?: (value: string) => string;
};
type Operation = { method: "GET" | "POST" | "PATCH"; path: string; body?: Record<string, unknown> };

const object = (value: unknown): value is Record<string, unknown> =>
  Boolean(value && typeof value === "object" && !Array.isArray(value));
const identifier = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,191}$/.test(value);
const text = (value: unknown): value is string => typeof value === "string" && Boolean(value.trim());
const reference = (value: unknown): value is string =>
  text(value) && !/^(?:[\\/]|[a-z]:[\\/]|(?:file|vault|secret):)/i.test(value.trim());
const references = (value: unknown): boolean => value === undefined || Array.isArray(value) && value.every(reference);

function projectInScope(projectId: unknown, scope: Scope): projectId is string {
  if (!identifier(projectId) || (scope.projectId && projectId !== scope.projectId)) return false;
  const systemId = scope.projectSystem(projectId);
  return systemId !== undefined && scope.systems.has(systemId);
}

function secretRequest(args: Record<string, unknown>, scope: Scope): Operation | undefined {
  const { projectId, name, purpose, scope: usageScope } = args;
  if (Object.keys(args).some((key) => !["projectId", "name", "purpose", "scope"].includes(key))) return;
  if (!projectInScope(projectId, scope) || !text(name)) return;
  const redact = scope.redactText ?? ((value) => value);
  if (redact(name) !== name) return;
  if ((purpose !== undefined && typeof purpose !== "string") || (usageScope !== undefined && typeof usageScope !== "string")) return;
  return { method: "POST", path: "/api/v1/secrets/requests", body: {
    project_id: projectId,
    name,
    purpose: purpose === undefined ? undefined : redact(purpose.trim()),
    scope: usageScope === undefined ? undefined : redact(usageScope.trim()),
  } };
}

function operation(op: string, args: Record<string, unknown>, scope: Scope): Operation | undefined {
  const { systemId, projectId, unitId, harness, model, graph, kind, ref, summary, recordType, files, inputs, outputs } = args;
  if (op === "project.secret.request") return secretRequest(args, scope);
  if (op === "business.artifact") {
    if (!projectInScope(projectId, scope) || !reference(ref) || !text(kind) || !text(summary)) return;
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
  if (op === "project.secret.request") return { id: value.request_id, status: value.state };
  if (op === "business.bind" && object(value.unit) && object(value.binding)) {
    return { id: value.unit.id, harness: value.binding.harnessRuntime, model: value.binding.model };
  }
  return { id: value.id, status: value.status, ref: value.ref };
}

function denied(id: string, code: string, message: string): CtlResponse {
  return { id, ok: false, result: null, error: { code, message } };
}

export function openForemanMailbox(scope: Scope, fetchBusiness: FetchBusiness, registerRepository?: RegisterRepository) {
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
    const systems = new Set(scope.systems);
    const operations = { ...scope, systems };
    const registrations = new Map<string, NonNullable<ReturnType<RegisterRepository>>>();
    const server = new CtlServer(root, {
      async handle({ id, op, args }) {
        if (!active) return denied(id, "FOREMAN_MAILBOX_CLOSED", "厂长邮箱已关闭");
        if (op === "project.register") {
          const input = parseRepositoryRegistration(args);
          if (!registerRepository || !input || (input.systemId !== undefined && !systems.has(input.systemId))) {
            return denied(id, "FOREMAN_OPERATION_INVALID", "登记需要无凭据的仓库 URL，指定业务系统时须在当前轮范围内");
          }
          const result = registrations.get(id) ?? registerRepository(input);
          if (!result) return denied(id, "FOREMAN_OPERATION_FAILED", "目标业务系统已不存在");
          registrations.set(id, result);
          systems.add(result.systemId);
          return { id, ok: true, result, error: null };
        }
        const request = operation(op, args, operations);
        if (!request) return denied(id, "FOREMAN_OPERATION_INVALID", "操作参数无效，或目标不在当前轮的范围内");
        const response = await fetchBusiness(new Request("http://localhost" + request.path, {
          method: request.method,
          ...(request.body && { headers: { "content-type": "application/json" }, body: JSON.stringify(request.body) }),
        }));
        if (op === "project.secret.request" && response.status === 400) {
          return denied(id, "FOREMAN_OPERATION_INVALID", "项目密钥名称无效");
        }
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
