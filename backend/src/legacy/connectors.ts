import { randomUUID } from "node:crypto";
import { chmodSync, existsSync, lstatSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import type { EvalEnvironment } from "../../mrl/evaluator";
import { compileTemplate } from "../../mrl/template";
import { DYN } from "../../mrl/types";
import type { ContainerProvider } from "../../runtime/podman";
import type { Context } from "./context";
import { FactoryError } from "./errors";
import { canonicalJson, parseDuration, utcIso } from "./foundation";
import { JobStore, type Job } from "./jobs";
import { sandboxResources } from "./sandbox";
import { stationNetwork } from "./sandbox-egress";
import type { Snapshot } from "./snapshot";
import { inputsValue, recipeEnv } from "./values";
import type { Bean } from "./warehouse";

export const receiptStatuses = ["claimed", "published", "verified", "failed", "void"] as const;
export type ReceiptStatus = typeof receiptStatuses[number];
export type ReceiptItem = { bean: string; locator: string; digest?: string };
export type ReceiptPayload = { target: string; locator: Record<string, string>; items?: ReceiptItem[]; note?: string };

export interface Receipt {
  id: string;
  station: string;
  station_name: string;
  idempotency_key: string;
  job_id: string;
  run_id: string | null;
  inputs: Record<string, string[]>;
  status: ReceiptStatus;
  target: string | null;
  locator: Record<string, string>;
  items: ReceiptItem[];
  note: string | null;
  verify_method: string;
  verify_detail: string | null;
  verified_at: string | null;
  voided_by: string | null;
  void_reason: string | null;
  created_at: string;
  updated_at: string;
}

type ReceiptRow = Omit<Receipt, "inputs" | "locator" | "items"> & { inputs: string; locator: string; items: string };
type ReceiptClaim = {
  station: string; key: string; jobId: string; runId?: string | null; inputs: Record<string, string[]>; verifyMethod: string;
};
type Verification = { ok: boolean; detail: string };
type Beans = Record<string, Bean | Bean[]>;
const verifiable: ReceiptStatus[] = ["published", "verified", "failed"];
const templateOptions = { environment: { inputs: DYN, params: DYN, run: DYN } };

function render(source: string, values: Record<string, unknown>): string {
  return source.includes("{{") ? compileTemplate(source, templateOptions).render(values as EvalEnvironment) : source;
}

export function sinkKey(snapshot: Snapshot, station: string, loaded: Beans, params: Record<string, unknown>, run: Record<string, unknown> = {}): string {
  const key = snapshot.key("Station", station);
  const source = snapshot.spec(key).idempotency_key;
  try {
    const rendered = render(source, { inputs: inputsValue(loaded), params, run });
    if (rendered) return rendered;
  } catch {
    throw new FactoryError("IDEMPOTENCY_KEY_EMPTY", "幂等键无法渲染", { station: key });
  }
  throw new FactoryError("IDEMPOTENCY_KEY_EMPTY", "幂等键渲染结果为空", { station: key });
}

function decode(row: ReceiptRow): Receipt {
  return { ...row, inputs: JSON.parse(row.inputs), locator: JSON.parse(row.locator), items: JSON.parse(row.items) };
}

function recordEvent(context: Context, jobId: string, kind: string, data: unknown): void {
  context.db.query("INSERT INTO job_events (id,job_id,kind,data,at) VALUES (?,?,?,?,?)")
    .run(context.ids.new("event"), jobId, kind, canonicalJson(data), utcIso(context.clock.now()));
}

export function recordSourceProvenance(context: Context, job: Job, outputs: Job["outputs"]): void {
  recordEvent(context, job.id, "source.ingested", { job: job.id, station: job.station, external: true, params: job.params, outputs });
}

function stateError(receipt: Receipt, op: string): FactoryError {
  return new FactoryError("RECEIPT_STATE_INVALID", "回执状态不允许执行此操作", { receipt: receipt.id, status: receipt.status, op });
}

export class ReceiptStore {
  constructor(readonly context: Context) {
    context.db.exec(`
      CREATE TABLE IF NOT EXISTS receipts (
        id TEXT PRIMARY KEY, station_name TEXT NOT NULL, station TEXT NOT NULL,
        idempotency_key TEXT NOT NULL, job_id TEXT NOT NULL, run_id TEXT, inputs TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('claimed','published','verified','failed','void')),
        target TEXT, locator TEXT NOT NULL DEFAULT '{}', items TEXT NOT NULL DEFAULT '[]', note TEXT,
        verify_method TEXT NOT NULL, verify_detail TEXT, verified_at TEXT, voided_by TEXT, void_reason TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
      );
      CREATE UNIQUE INDEX IF NOT EXISTS receipts_idem ON receipts(station_name,idempotency_key) WHERE status != 'void';
      CREATE INDEX IF NOT EXISTS receipts_job ON receipts(job_id);
      CREATE TABLE IF NOT EXISTS job_events (
        id TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS job_events_kind ON job_events(job_id,kind);
    `);
  }

  claim(input: ReceiptClaim): { kind: "new" | "duplicate"; receipt: Receipt } {
    const { station, key, jobId, runId, inputs, verifyMethod } = input;
    if (!key) throw new FactoryError("IDEMPOTENCY_KEY_EMPTY", "幂等键为空", { station });
    const name = station.replace(/^Station:/, "").replace(/@[^@]+$/, "");
    const db = this.context.db;
    return db.transaction<{ kind: "new" | "duplicate"; receipt: Receipt }>(() => {
      const active = db.query<ReceiptRow>("SELECT * FROM receipts WHERE station_name=? AND idempotency_key=? AND status!='void'").get(name, key);
      if (active) {
        if (active.status === "verified") {
          recordEvent(this.context, jobId, "sink.duplicate", { receipt: active.id, station, key, job: jobId });
          return { kind: "duplicate", receipt: decode(active) };
        }
        const detail = { station, key, receipt: active.id };
        if (active.status === "failed") throw new FactoryError("RECEIPT_UNVERIFIED_EXISTS", "同键已有未通过核验的回执", detail);
        throw new FactoryError("IDEMPOTENCY_CONFLICT", "幂等键已被占用", detail);
      }
      const at = utcIso(this.context.clock.now());
      const id = this.context.ids.new("rc");
      db.query(`INSERT INTO receipts (id,station_name,station,idempotency_key,job_id,run_id,inputs,status,verify_method,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,'claimed',?,?,?)`).run(id, name, station, key, jobId, runId ?? null, canonicalJson(inputs), verifyMethod, at, at);
      recordEvent(this.context, jobId, "sink.claimed", { receipt: id, station, key, job: jobId });
      return { kind: "new", receipt: this.get(id) };
    }).immediate();
  }

  publish(id: string, value: unknown): Receipt {
    return this.update(id, "publish", ["claimed"], (current, at) => {
      const { target, locator, items = [], note = "" } = validatePayload(value, current.job_id);
      this.context.db.query("UPDATE receipts SET status='published',target=?,locator=?,items=?,note=?,updated_at=? WHERE id=?")
        .run(target, canonicalJson(locator), canonicalJson(items), note, at, id);
      recordEvent(this.context, current.job_id, "sink.published", { receipt: id, target, locator });
    });
  }

  settle(id: string, { ok, detail }: Verification): Receipt {
    return this.update(id, "settle", verifiable, (current, at) => {
      this.context.db.query("UPDATE receipts SET status=?,verify_detail=?,verified_at=?,updated_at=? WHERE id=?")
        .run(ok ? "verified" : "failed", detail, at, at, id);
      recordEvent(this.context, current.job_id, ok ? "sink.verified" : "sink.verify_failed", { receipt: id, method: current.verify_method, detail });
    });
  }

  void(id: string, { by, reason }: { by: string; reason: string }): Receipt {
    return this.update(id, "void", receiptStatuses.filter((status) => status !== "void"), (current, at) => {
      this.context.db.query("UPDATE receipts SET status='void',voided_by=?,void_reason=?,updated_at=? WHERE id=?")
        .run(by, reason, at, id);
      recordEvent(this.context, current.job_id, "receipt.voided", { receipt: id, by, reason });
    });
  }

  noteFormatError(id: string, problems: unknown): void {
    this.update(id, "note_format_error", ["claimed"], (_current, at) => {
      const detail = Array.isArray(problems) ? problems.join(", ") : String(problems);
      this.context.db.query("UPDATE receipts SET verify_detail=?,updated_at=? WHERE id=?")
        .run(`receipt.json 格式错误：${detail}`, at, id);
    });
  }

  get(id: string): Receipt {
    const row = this.context.db.query<ReceiptRow>("SELECT * FROM receipts WHERE id=?").get(id);
    if (!row) throw new FactoryError("RECEIPT_NOT_FOUND", `找不到回执 ${id}`, { receipt: id });
    return decode(row);
  }

  list({ station, status }: { station?: string; status?: ReceiptStatus } = {}): Receipt[] {
    const filters: string[] = [];
    const values: string[] = [];
    if (station) {
      filters.push("(station=? OR station_name=?)");
      values.push(station.startsWith("Station:") ? station : `Station:${station}`, station);
    }
    if (status) {
      filters.push("status=?");
      values.push(status);
    }
    const where = filters.length ? `WHERE ${filters.join(" AND ")}` : "";
    return this.context.db.query<ReceiptRow>(`SELECT * FROM receipts ${where} ORDER BY created_at,id`).all(...values).map(decode);
  }

  forJob(jobId: string): Receipt | undefined {
    const row = this.context.db.query<ReceiptRow>("SELECT * FROM receipts WHERE job_id=? AND status!='void' ORDER BY created_at DESC,id DESC LIMIT 1").get(jobId);
    return row ? decode(row) : undefined;
  }

  forBean(beanId: string): Receipt[] {
    return this.context.db.query<ReceiptRow>(`SELECT receipts.* FROM receipts WHERE EXISTS (
      SELECT 1 FROM json_each(receipts.inputs) AS port JOIN json_each(port.value) AS bean WHERE bean.value=?
    ) ORDER BY created_at,id`).all(beanId).map(decode);
  }

  private update(id: string, op: string, states: readonly ReceiptStatus[], write: (current: Receipt, at: string) => void): Receipt {
    return this.context.db.transaction(() => {
      const current = this.get(id);
      if (!states.includes(current.status)) throw stateError(current, op);
      write(current, utcIso(this.context.clock.now()));
      return this.get(id);
    }).immediate();
  }
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function validatePayload(value: unknown, jobId?: string): ReceiptPayload {
  const problems: string[] = [];
  const detail = jobId ? { job: jobId } : {};
  if (!object(value)) throw new FactoryError("RECEIPT_INVALID", "回执载荷不合法", { ...detail, problems: ["receipt"] });
  if (typeof value.target !== "string" || !value.target) problems.push("target");
  if (!object(value.locator) || Object.values(value.locator).some((item) => typeof item !== "string")) problems.push("locator");
  if (value.items !== undefined && (!Array.isArray(value.items) || value.items.some((item) => !object(item)
    || typeof item.bean !== "string" || typeof item.locator !== "string"
    || (item.digest !== undefined && typeof item.digest !== "string")))) problems.push("items");
  if (value.note !== undefined && typeof value.note !== "string") problems.push("note");
  if (problems.length) throw new FactoryError("RECEIPT_INVALID", "回执载荷不合法", { ...detail, problems });
  return value as ReceiptPayload;
}

export async function readReceiptPayload(ctlDir: string, jobId?: string): Promise<ReceiptPayload | undefined> {
  const path = join(ctlDir, "receipt.json");
  let value: unknown;
  try {
    const stat = lstatSync(path);
    if (!stat.isFile()) throw new Error("invalid receipt file");
    value = JSON.parse(await Bun.file(path).text());
  } catch (error) {
    if ((error as { code?: unknown }).code === "ENOENT") return undefined;
    throw new FactoryError("RECEIPT_INVALID", "receipt.json 无法读取", { ...(jobId ? { job: jobId } : {}), problems: ["receipt.json"] });
  }
  return validatePayload(value, jobId);
}

export function renderReceiptPayload(template: Pick<ReceiptPayload, "target" | "locator"> | undefined, loaded: Beans, params: Record<string, unknown>): ReceiptPayload | undefined {
  if (!template) return undefined;
  const env = recipeEnv(loaded, params);
  return { target: render(template.target, env), locator: Object.fromEntries(Object.entries(template.locator).map(([key, value]) => [key, render(value, env)])) };
}

export async function verifyReceipt(context: Context, snapshot: Snapshot, provider: ContainerProvider, receipt: Receipt): Promise<Verification> {
  if (!verifiable.includes(receipt.status)) throw stateError(receipt, "verify");
  try {
    const job = new JobStore(context.db, context.clock, context.ids).get(receipt.job_id);
    const station = snapshot.spec(receipt.station);
    const machine = snapshot.spec(snapshot.key("Machine", station.machine));
    const recipe = machine.recipes?.[receipt.verify_method];
    if (!Array.isArray(recipe?.run) || !recipe.run.length || (!machine.runtime?.image && provider.name !== "local")) {
      throw new FactoryError("RECEIPT_VERIFY_RECIPE_INVALID", "核验配方缺少命令或镜像");
    }
    const network = stationNetwork(context.root, snapshot, receipt.station);
    const token = randomUUID().slice(0, 12);
    const principal = `${job.id}-verify-${token}`;
    const root = join(context.root, "jobs", job.id, `verify-${token}`);
    const inDir = join(root, "in");
    const outDir = join(root, "out");
    const ctlDir = join(root, "ctl");
    const workspace = join(root, "workspace");
    const stdout = join(root, "verify.stdout");
    for (const dir of [inDir, outDir, ctlDir, workspace]) mkdirSync(dir, { recursive: true });
    if (process.platform === "linux" && provider.name !== "local") {
      chmodSync(root, 0o700);
      for (const dir of [outDir, workspace]) chmodSync(dir, 0o777);
    }
    const loaded: Beans = {};
    for (const [port, ids] of Object.entries(job.inputs)) {
      const list = String(station.inputs?.[port]?.type).startsWith("list<");
      const beans: Bean[] = [];
      for (const [index, id] of ids.entries()) {
        const destination = list ? join(inDir, port, String(index)) : join(inDir, port);
        await context.warehouse.materialize(id, destination, "port");
        beans.push(context.warehouse.get(id));
      }
      if (list) loaded[port] = beans;
      else if (beans.length) loaded[port] = beans[0];
    }
    await Promise.all([
      Bun.write(join(ctlDir, "receipt.json"), canonicalJson({ target: receipt.target, locator: receipt.locator, items: receipt.items, note: receipt.note ?? "" })),
      Bun.write(join(ctlDir, "job.json"), canonicalJson({ job_id: job.id, attempt: job.attempt, inputs: inputsValue(loaded), budget_usd: station.limits?.budget_usd ?? 0 })),
    ]);
    const tools = join(context.root, "tools", "factory");
    const env = recipeEnv(loaded, job.params);
    const argv = recipe.run.map((word: string) => render(word, env));
    const sandbox = await provider.open({
      name: `mao-${principal}`, image: machine.runtime?.image ?? "local", workdir: "/workspace", ...network,
      ...sandboxResources(machine.runtime?.resources),
      mounts: [
        { source: inDir, target: "/factory/in", readonly: true },
        { source: outDir, target: "/factory/out", readonly: false },
        { source: ctlDir, target: "/factory/ctl", readonly: true },
        { source: workspace, target: "/workspace", readonly: false },
        { source: tools, target: "/opt/factory", readonly: true },
      ],
    });
    let result;
    try {
      const timeoutMs = Math.min(300, parseDuration(station.limits?.timeout ?? "300s")) * 1000;
      result = await sandbox.exec(argv, {
        timeoutMs, stdout, stderr: join(root, "verify.stderr"),
        env: { FACTORY_JOB_ID: job.id, FACTORY_ATTEMPT: String(job.attempt) },
      });
    } finally {
      await sandbox.close();
    }
    if (result.timedOut || (result.exitCode !== 0 && result.exitCode !== 1)) {
      throw new FactoryError("RECEIPT_VERIFY_ERROR", "回执核验设施故障", {
        receipt: receipt.id, cause: { exit_code: result.exitCode, timed_out: result.timedOut },
      });
    }
    const output = existsSync(stdout) ? (await Bun.file(stdout).slice(0, 16 * 1024).text()).trim() : "";
    let detail = output;
    try {
      const data: unknown = JSON.parse(output);
      if (object(data) && Object.hasOwn(data, "detail")) detail = typeof data.detail === "string" ? data.detail : canonicalJson(data.detail);
    } catch { /* Plain text is also valid. */ }
    return { ok: result.exitCode === 0, detail };
  } catch (error) {
    if (error instanceof FactoryError && error.code === "RECEIPT_VERIFY_ERROR") throw error;
    const reason = error instanceof FactoryError ? error.code : error instanceof Error ? error.name : String(error);
    throw new FactoryError("RECEIPT_VERIFY_ERROR", "回执核验设施故障", { receipt: receipt.id, cause: { reason } });
  }
}
