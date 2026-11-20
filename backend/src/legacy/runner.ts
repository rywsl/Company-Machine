import { chmodSync, cpSync, existsSync, lstatSync, mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { compileTemplate } from "../../mrl/template";
import { DYN } from "../../mrl/types";
import type { EvalEnvironment } from "../../mrl/evaluator";
import { PodmanError, type ContainerProvider, type Sandbox } from "../../runtime/podman";
import { Approvals, readProposal, type Approval } from "./approvals";
import { readReceiptPayload, ReceiptStore, recordSourceProvenance, renderReceiptPayload, sinkKey, verifyReceipt } from "./connectors";
import { installFactoryctl, type Context } from "./context";
import { FactoryError } from "./errors";
import { CtlServer, harvest, type CtlRequest, type CtlResponse, type HarvestRequest } from "./factoryctl";
import { parseDuration } from "./foundation";
import { Gate } from "./gate";
import { JobStore, terminalStates, type Job, type JobPhase } from "./jobs";
import { buildInvocation, expandControlVars, parseTranscript, renderBrief } from "./operator";
import { sandboxResources } from "./sandbox";
import { stationNetwork } from "./sandbox-egress";
import { nominalTypeId, type Snapshot } from "./snapshot";
import { inputsValue, paramsValue, recipeEnv } from "./values";
import type { Bean } from "./warehouse";

const PREFLIGHT_FILE = `
import { constants, openSync, fstatSync, readSync, closeSync } from "node:fs";
const limit = 16 * 1024 * 1024;
const file = openSync(Bun.argv[1], constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) | (constants.O_NONBLOCK ?? 0));
try {
  const stat = fstatSync(file);
  if (!stat.isFile() || stat.size > limit) process.exit(1);
  const bytes = Buffer.allocUnsafe(stat.size);
  let offset = 0;
  while (offset < bytes.length) {
    const read = readSync(file, bytes, offset, bytes.length - offset);
    if (read === 0) break;
    offset += read;
  }
  await Bun.write(Bun.stdout, bytes.subarray(0, offset));
} finally {
  closeSync(file);
}
`;

const SHARE_ATTEMPT_FILES = `
import { constants, openSync, fstatSync, fchmodSync, closeSync, readdirSync } from "node:fs";
function visit(path) {
  let fd;
  try { fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK); }
  catch (error) { if (["ELOOP", "ENOENT", "ENXIO"].includes(error.code)) return; throw error; }
  try {
    const stat = fstatSync(fd);
    if (!stat.isFile() && !stat.isDirectory()) return;
    if (stat.uid === process.getuid()) {
      const mode = stat.mode & 0o777;
      const shared = mode | (stat.isDirectory() ? 0o007 : 0o004);
      if (shared !== mode) fchmodSync(fd, shared);
    }
    if (stat.isDirectory()) {
      const current = "/proc/self/fd/" + fd;
      for (const item of readdirSync(current)) visit(current + "/" + item);
    }
  } finally { closeSync(fd); }
}
for (const path of ["/workspace", "/factory/out"]) visit(path);
`;

export class Runner {
  readonly jobs: JobStore;
  readonly approvals: Approvals;
  readonly receipts: ReceiptStore;

  constructor(readonly context: Context, readonly snapshot: Snapshot, readonly provider: ContainerProvider) {
    this.jobs = new JobStore(context.db, context.clock, context.ids);
    this.approvals = new Approvals(context, snapshot, this.jobs);
    this.receipts = new ReceiptStore(context);
  }

  submit(stationRef: string, inputs: Record<string, string | string[]>, params: Record<string, unknown>, runValue?: Record<string, unknown>,
    options: { proposalOnly?: boolean; timeoutMs?: number } = {},
  ): Job {
    const station = this.snapshot.key("Station", stationRef);
    const spec = this.snapshot.spec(station);
    const role = spec.role;
    if (role === "source" && runValue !== undefined) {
      const budget = runValue?.budget;
      if (!runValue || !["id", "epoch", "line"].every((name) => Object.hasOwn(runValue, name))
        || !budget || typeof budget !== "object" || Array.isArray(budget)
        || !["cap_usd", "spent_usd", "remaining_usd"].every((name) => Object.hasOwn(budget, name))) {
        throw new FactoryError("POLICY_VIOLATION", "运行值不完整，拒绝启动", { station, role, reason: "run" });
      }
    }
    if (options.proposalOnly && (role !== "sink" || !spec.phases?.includes("propose"))) {
      throw new FactoryError("CONTRACT_SINK_NO_PROPOSE", "试运行需要两阶段 Sink 工位", { station });
    }
    if (options.timeoutMs !== undefined && (!Number.isFinite(options.timeoutMs) || options.timeoutMs <= 0)) {
      throw new FactoryError("SANDBOX_SPEC_INVALID", "任务超时必须为正有限数", { station });
    }
    const gate = new Gate(this.snapshot, this.context.warehouse, join(this.context.root, "tmp", "gate"), this.provider);
    const checkedInputs = gate.checkInputs(station, inputs, params);
    const effectiveParams = paramsValue(this.snapshot, station, params);
    const ids = Object.fromEntries(Object.entries(checkedInputs).map(([port, value]) => [
      port, (Array.isArray(value) ? value : [value]).map((bean) => bean.id),
    ]));
    return this.jobs.create({
      station, epoch: this.snapshot.epoch, inputs: ids, params: effectiveParams, runValue,
      phase: spec.phases?.includes("propose") ? "propose" : "execute",
      idempotencyKey: role === "sink" && !options.proposalOnly ? sinkKey(this.snapshot, station, checkedInputs, effectiveParams, runValue) : undefined,
      ...options,
    });
  }

  adopt(id: string): Job { return this.jobs.get(id); }

  async run(id: string): Promise<Job> {
    const job = this.jobs.get(id);
    if (terminalStates.has(job.state)) return job;
    if (job.epoch !== this.snapshot.epoch) throw new FactoryError("EPOCH_MISMATCH", "Job snapshot does not match", { jobId: id });
    const station = this.snapshot.spec(job.station);
    if (station.role === "source") {
      const machine = this.snapshot.key("Machine", station.machine);
      const form = this.snapshot.spec(machine).form;
      if (form === "service") {
        throw new FactoryError("FORM_UNSUPPORTED", "Source 工位不支持服务型机器", { station: job.station, machine, form });
      }
    }
    if (job.proposalOnly && job.phase !== "propose") {
      throw new FactoryError("JOB_STATE_INVALID", "试运行任务只可执行方案阶段", { jobId: id, state: job.state });
    }
    this.jobs.acquireExecution(id);

    try {
      return await this.runLoop(this.jobs.get(id));
    } finally {
      this.jobs.releaseExecution(id);
    }
  }

  private async runLoop(initial: Job): Promise<Job> {
    let job = initial;
    const station = this.snapshot.spec(job.station);
    const role = station.role;
    const initialPhase: JobPhase = station.phases?.includes("propose") ? "propose" : "execute";
    if (terminalStates.has(job.state)) return job;
    if (job.state === "PENDING" && !["transform", "converter", "source", "sink"].includes(role)) {
      return this.jobs.transition(job.id, "DEAD", {
        error: { code: "ROLE_UNSUPPORTED", message: "工位角色需要专用执行路径", detail: { role, station: job.station } },
      });
    }
    if (job.state === "GATING") await this.provider.reap(job.id, job.attempt);
    if (role === "sink" && job.phase === "execute" && (job.state === "RUNNING" || job.state === "AWAITING_APPROVAL")) {
      await this.provider.reap(job.id, job.attempt);
      this.approvals.closePending({ jobId: job.id, attempt: job.attempt }, "工位执行进程已中断");
      return this.failSink(job, { code: "OPERATOR_INTERRUPTED" });
    }
    while (!terminalStates.has(job.state)) {
      if (job.state === "FAILED" || job.state === "REJECTED") {
        const limit = station.limits?.retries?.[job.state.toLowerCase()] ?? 0;
        const retry = this.jobs.countTransitions(job.id, job.state) <= limit;
        const patch = retry && initialPhase === "propose" ? { phase: initialPhase, costUsd: 0 } : {};
        job = this.jobs.transition(job.id, retry ? "PENDING" : "DEAD", patch);
        continue;
      }
      if (job.state === "PENDING") {
        job = this.jobs.transition(job.id, "PROVISIONING", { phase: initialPhase });
      }
      if (job.phase === "propose" && (job.state === "RUNNING" || job.state === "AWAITING_APPROVAL")) {
        this.approvals.expireDue({ jobId: job.id, attempt: job.attempt });
        const proposal = this.approvals.proposal(job.id, job.attempt);
        if (proposal) {
          job = this.continueProposal(job, proposal);
          if (job.state === "AWAITING_APPROVAL") return job;
          continue;
        }
      }
      if (job.state === "AWAITING_APPROVAL") {
        await this.provider.reap(job.id, job.attempt);
        this.approvals.closePending({ jobId: job.id, attempt: job.attempt }, "工位执行进程已中断");
        job = this.jobs.transition(job.id, "FAILED", { error: { code: "OPERATOR_INTERRUPTED" } });
        continue;
      }
      if (job.state !== "PROVISIONING" && job.state !== "RUNNING" && job.state !== "GATING") {
        throw new FactoryError("JOB_STATE_INVALID", "当前状态不能执行工位", { jobId: job.id, state: job.state });
      }
      try {
        if (initialPhase === "propose" && job.phase === "execute" && job.state !== "GATING" &&
            this.approvals.proposal(job.id, job.attempt)?.status !== "approved") {
          throw new FactoryError("PROPOSAL_MISSING", "本次尝试需要已批准的方案", { jobId: job.id, attempt: job.attempt });
        }
        if (role === "sink" && job.state === "PROVISIONING" && job.phase === "execute") {
          job = this.claimSink(job, station.verify_receipt);
          if (terminalStates.has(job.state)) return job;
        }
        if (job.state !== "GATING") job = await this.runAttempt(job);
        if (job.phase === "propose" && job.state === "AWAITING_APPROVAL") return job;
        if (job.state === "GATING") job = await this.finishAttempt(job);
      } catch (error) {
        job = this.jobs.get(job.id);
        if (terminalStates.has(job.state) || job.state === "FAILED" || job.state === "REJECTED") throw error;
        const factoryError = error instanceof FactoryError || error instanceof PodmanError;
        const failure = {
          code: factoryError ? error.code : "OPERATOR_FAILED",
          message: error instanceof Error ? error.message : String(error),
          ...(factoryError && error.detail ? { detail: error.detail } : {}),
        };
        const failedState = job.state === "RUNNING" || job.state === "AWAITING_APPROVAL" || job.state === "GATING";
        if (role === "sink" && job.phase === "execute") {
          if (job.state === "RUNNING" || job.state === "AWAITING_APPROVAL") return this.failSink(job, failure);
          if (job.state === "GATING") return this.jobs.transition(job.id, "DEAD", { error: failure });
          job = this.jobs.transition(job.id, "FAILED", { error: failure });
          continue;
        }
        if (!failedState && job.phase !== "propose") return this.jobs.transition(job.id, "DEAD", { error: failure });
        job = this.jobs.transition(job.id, "FAILED", { error: failure });
      }
    }
    return job;
  }

  private claimSink(job: Job, verifyMethod: string): Job {
    if (this.receipts.forJob(job.id)) return job;
    try {
      const key = job.idempotencyKey ?? sinkKey(this.snapshot, job.station, this.loadInputs(job), job.params, job.runValue);
      const claimed = this.receipts.claim({ station: job.station, key, jobId: job.id,
        runId: typeof job.runValue?.id === "string" ? job.runValue.id : undefined, inputs: job.inputs, verifyMethod });
      return claimed.kind === "duplicate" ? this.jobs.transition(job.id, "SEALED") : job;
    } catch (error) {
      if (!(error instanceof FactoryError)) throw error;
      return this.jobs.transition(job.id, "DEAD", { error: { code: error.code, message: error.message, detail: error.detail } });
    }
  }

  private continueProposal(job: Job, proposal: Approval, costUsd = job.costUsd): Job {
    if (proposal.status === "pending") {
      return job.state === "AWAITING_APPROVAL" ? job : this.jobs.transition(job.id, "AWAITING_APPROVAL", { costUsd });
    }
    if (proposal.status === "approved") {
      if (job.proposalOnly) return this.jobs.transition(job.id, "SEALED", { costUsd });
      return this.jobs.transition(job.id, "PROVISIONING", { phase: "execute", costUsd });
    }
    return this.jobs.transition(job.id, "FAILED", {
      costUsd, error: { code: this.approvals.ctlResult(proposal)?.code ?? "APPROVAL_DENIED", detail: { approval: proposal.id } },
    });
  }

  private attemptPath(job: Job, attempt = job.attempt): string {
    return join(this.context.root, "jobs", job.id, String(attempt));
  }

  private loadInputs(job: Job): Record<string, Bean | Bean[]> {
    const inputs = this.snapshot.spec(job.station).inputs;
    return Object.fromEntries(Object.entries(job.inputs).map(([port, ids]) => {
      const beans = ids.map((id) => this.context.warehouse.get(id));
      return [port, String(inputs[port].type).startsWith("list<") ? beans : beans[0]];
    }));
  }

  private async runAttempt(job: Job): Promise<Job> {
    const base = this.attemptPath(job);
    const proposing = job.phase === "propose";
    const attemptRoot = proposing ? join(base, "propose") : base;
    const inDir = join(attemptRoot, "in");
    const outDir = join(attemptRoot, "out");
    const ctlDir = join(attemptRoot, "ctl");
    const workspace = join(base, "workspace");
    const logs = join(attemptRoot, "logs");
    if (existsSync(attemptRoot)) {
      if (job.state === "RUNNING") await this.provider.reap(job.id, job.attempt);
      for (const dir of [inDir, outDir, ctlDir, logs]) rmSync(dir, { recursive: true, force: true });
    }
    mkdirSync(attemptRoot, { recursive: true, mode: 0o700 });
    const station = this.snapshot.spec(job.station);
    const network = stationNetwork(this.context.root, this.snapshot, job.station);
    if (!existsSync(workspace) && job.attempt > 1 && station.limits?.retries?.mode === "resume") {
      const previous = join(this.attemptPath(job, job.attempt - 1), "workspace");
      if (existsSync(previous)) cpSync(previous, workspace, { recursive: true, verbatimSymlinks: true });
    }
    for (const dir of [inDir, outDir, ctlDir, workspace, logs, join(attemptRoot, "tmp")]) {
      mkdirSync(dir, { recursive: true });
    }
    const requests = join(ctlDir, "requests");
    mkdirSync(requests);
    mkdirSync(join(ctlDir, "responses"), { mode: 0o755 });
    if (process.platform === "linux" && this.provider.name !== "local") {
      // The sandbox runs as UID 10001, which needs write access to these bind
      // mounts. The enclosing attempt directory limits host-side access.
      chmodSync(attemptRoot, 0o700);
      for (const dir of [outDir, workspace]) chmodSync(dir, 0o777);
      for (const dir of [ctlDir, requests]) chmodSync(dir, 0o1777);
      chmodSync(inDir, 0o755);
    }

    const machine = this.snapshot.spec(this.snapshot.key("Machine", station.machine));
    const recipe = machine.recipes[station.recipe];
    const runtime = this.snapshot.spec(this.snapshot.key("OperatorRuntime", station.operator.runtime));
    const loaded = this.loadInputs(job);
    for (const [port, value] of Object.entries(loaded)) {
      if (Array.isArray(value)) {
        for (const [index, bean] of value.entries()) {
          await this.context.warehouse.materialize(bean.id, join(inDir, port, String(index)), "port");
        }
      } else {
        await this.context.warehouse.materialize(value.id, join(inDir, port), "port");
      }
    }
    const budgetUsd = Number(station.limits?.budget_usd ?? 0);
    const brief = renderBrief(this.snapshot, job.station, { inputs: loaded, params: job.params, phase: job.phase, attempt: job.attempt, feedback: job.feedback, budgetUsd });
    const control = { FACTORY_JOB_ID: job.id, FACTORY_ATTEMPT: String(job.attempt), FACTORY_PHASE: job.phase };
    const invocation = expandControlVars(
      buildInvocation(this.snapshot, job.station, brief, { inputs: loaded, params: job.params, phase: job.phase }), control,
    );
    await Bun.write(join(ctlDir, "job.json"), JSON.stringify({
      job_id: job.id, attempt: job.attempt, phase: job.phase, inputs: inputsValue(loaded), budget_usd: budgetUsd,
    }));
    await Bun.write(join(ctlDir, "brief.md"), brief.markdown);
    const timeout = station.approval?.timeout;
    await Bun.write(join(ctlDir, "contract.json"), JSON.stringify({
      station: job.station, outputs: station.outputs ?? {}, budget_usd: budgetUsd,
      approval: { timeout_s: timeout === undefined ? 600 : parseDuration(timeout) },
    }));

    if (!machine.runtime?.image && this.provider.name !== "local") {
      throw new FactoryError("COMPOSE_UNSUPPORTED", "Station requires a pinned machine image", { station: job.station });
    }
    const inject = this.provider.name === "local" ? undefined : runtime.inject;
    const volume = `mao-${job.id}-${job.attempt}-operator`;
    const factoryctl = await installFactoryctl(this.context.root);
    if (inject) await this.provider.prepareInjectVolume(inject.volume, volume);
    const sandbox = await this.provider.open({
      name: `mao-${job.id}-${job.attempt}`,
      image: machine.runtime?.image ?? "local",
      mounts: [
        { source: inDir, target: "/factory/in", readonly: true },
        { source: outDir, target: "/factory/out", readonly: false },
        { source: ctlDir, target: "/factory/ctl", readonly: false },
        { source: workspace, target: "/workspace", readonly: false },
        { source: factoryctl, target: "/opt/factory/bin/factoryctl", readonly: true },
        ...(inject ? [{ source: `volume:${volume}`, target: inject.mount as string, readonly: true }] : []),
      ],
      workdir: "/workspace",
      ...network,
      ...sandboxResources(machine.runtime?.resources),
    }).catch(async (error: unknown) => {
      if (inject) await this.provider.removeVolume(volume).catch(() => {});
      throw error;
    });
    const stdout = join(logs, "stdout");
    const mailbox = this.mailbox(job, sandbox, loaded, attemptRoot);
    try {
      if (job.state === "PROVISIONING") this.jobs.transition(job.id, "RUNNING");
      mailbox.start();
      let result: Awaited<ReturnType<Sandbox["exec"]>>;
      try {
        result = await sandbox.exec(invocation.argv, {
          timeoutMs: Math.min(invocation.timeoutS * 1000, job.timeoutMs ?? Infinity),
          stdout,
          stderr: join(logs, "stderr"),
          env: { ...invocation.env, ...control },
        });
      } finally {
        await mailbox.stop();
      }
      if (process.platform === "linux" && process.getuid?.() !== 0 && this.provider.name !== "local") {
        const sharing = await sandbox.exec(["bun", "-e", SHARE_ATTEMPT_FILES], {
          timeoutMs: 30_000, stdout: join(logs, "permissions.stdout"), stderr: join(logs, "permissions.stderr"),
        });
        if (sharing.exitCode !== 0) console.warn(`工位 ${job.id} 的部分文件未共享给宿主`);
      }
      const summary = invocation.transcript && existsSync(stdout) ? parseTranscript(stdout, invocation.transcript) : undefined;
      const accrued = !proposing && station.phases?.includes("propose") ? job.costUsd : 0;
      const costUsd = Number((accrued + (summary?.costUsd ?? 0)).toFixed(6));
      if (costUsd > budgetUsd && this.jobs.get(job.id).state === "RUNNING") {
        if (!proposing && station.role === "sink") return this.failSink(job, { code: "BUDGET_EXCEEDED", cost_usd: costUsd, budget_usd: budgetUsd }, costUsd);
        return this.jobs.transition(job.id, "DEAD", {
          costUsd,
          error: { code: "BUDGET_EXCEEDED", message: "成本超过工位预算", detail: { cost_usd: costUsd, budget_usd: budgetUsd } },
        });
      }
      const failPath = join(ctlDir, "fail.json");
      const declaredFailure = existsSync(failPath) && lstatSync(failPath).isFile();
      const approvals = this.approvals.list({ jobId: job.id, attempt: job.attempt });
      const expired = approvals.some((item) => item.decidedBy === "timeout");
      const code = expired ? "APPROVAL_TIMEOUT"
        : result.timedOut ? "OPERATOR_TIMEOUT"
        : this.jobs.get(job.id).state === "AWAITING_APPROVAL" ? "OPERATOR_FAILED"
        : declaredFailure ? "OPERATOR_FAILED"
        : invocation.transcript && !summary?.hasResult ? "AGENT_ERROR"
        : result.exitCode !== 0 ? "OPERATOR_FAILED" : undefined;
      if (code) {
        if (!proposing && station.role === "sink") return this.failSink(job, { code, exit_code: result.exitCode }, costUsd);
        return this.jobs.transition(job.id, "FAILED", { costUsd, error: { code, detail: { exit_code: result.exitCode } } });
      }
      if (proposing) {
        const payload = readProposal(ctlDir, job.id);
        return this.context.db.transaction(() => {
          const proposal = this.approvals.openProposal({ jobId: job.id, attempt: job.attempt, payload, inputs: loaded, params: job.params });
          return this.continueProposal(this.jobs.get(job.id), proposal, costUsd);
        }).immediate();
      }
      return this.jobs.transition(job.id, "GATING", { costUsd });
    } finally {
      try {
        await sandbox.close();
        if (inject) {
          await this.provider.removeVolume(volume).catch((error: unknown) => {
            console.warn(`无法回收工位 ${job.id} 的操作工注入卷`, error instanceof Error ? error.message : String(error));
          });
        }
      } finally {
        this.approvals.closePending({ jobId: job.id, attempt: job.attempt }, "工位执行已结束");
      }
    }
  }

  private mailbox(job: Job, sandbox: Sandbox, inputs: Record<string, Bean | Bean[]>, root: string): CtlServer {
    const pendingResponses = new Set<string>();
    return new CtlServer(join(root, "ctl"), {
      handle: async ({ id, op, args }) => {
        if (op === "check") return this.preflight(job, sandbox, inputs, { id, op, args }, root);
        if (op !== "approval" || typeof args.kind !== "string" || typeof args.summary !== "string"
          || (args.cost_usd !== undefined && (typeof args.cost_usd !== "number" || !Number.isFinite(args.cost_usd) || args.cost_usd < 0))) {
          return { id, ok: false, result: null, error: { code: "CTL_REQUEST_INVALID", message: "请求参数不合法" } };
        }
        const request = this.approvals.openRequest({
          jobId: job.id, attempt: job.attempt, requestId: id,
          payload: { kind: args.kind, summary: args.summary, cost_usd: args.cost_usd as number | undefined }, inputs, params: job.params,
        });
        this.approvals.expireDue({ jobId: job.id, attempt: job.attempt });
        const current = this.approvals.get(request.id);
        const resolved = this.approvals.ctlResult(current);
        if (!resolved) {
          pendingResponses.add(id);
          if (this.jobs.get(job.id).state === "RUNNING") this.jobs.transition(job.id, "AWAITING_APPROVAL");
          return undefined;
        }
        const result = { ...resolved, by: current.decidedBy };
        const ok = resolved.decision === "approve";
        return { id, ok, result, error: ok ? null : { code: resolved.code, message: resolved.note || resolved.code } };
      },
    }, {
      afterResponse: (request) => {
        if (pendingResponses.delete(request.id) && !pendingResponses.size && this.jobs.get(job.id).state === "AWAITING_APPROVAL") {
          this.jobs.transition(job.id, "RUNNING");
        }
      },
    });
  }

  private async preflight(job: Job, sandbox: Sandbox, inputs: Record<string, Bean | Bean[]>, request: CtlRequest, attemptRoot: string): Promise<CtlResponse> {
    const { id, args } = request;
    const { port, path } = args;
    const outputs = this.snapshot.spec(job.station).outputs ?? {};
    const parts = typeof path === "string" ? path.split("/") : [];
    const validPath = typeof path === "string" && /^\/(workspace\/|factory\/out\/)/.test(path)
      && !/[\\\x00-\x1f]/.test(path) && parts.slice(1).every((part) => part && part !== "." && part !== "..");
    if (typeof port !== "string" || !Object.hasOwn(outputs, port) || !validPath) {
      return { id, ok: false, result: null, error: { code: "CTL_REQUEST_INVALID", message: "预检需要已声明的输出端口和产物路径" } };
    }
    const root = mkdtempSync(join(attemptRoot, "tmp", "check-"));
    const candidate = join(root, "candidate", port);
    mkdirSync(candidate, { recursive: true });
    try {
      // Read in the sandbox so a workspace path cannot redirect the host to files outside its mounts.
      const result = await sandbox.exec(["bun", "-e", PREFLIGHT_FILE, "--", path], {
        timeoutMs: 10_000, stdout: join(candidate, parts.at(-1)!), stderr: join(root, "read.stderr"),
      });
      if (result.exitCode !== 0 || result.timedOut) {
        return { id, ok: true, result: { verdict: "reject", feedback: "无法读取预检文件" }, error: null };
      }
      const gate = new Gate(this.snapshot, this.context.warehouse, join(root, "gate"), this.provider, job.timeoutMs);
      const { data } = await gate.inspect(job.station, { jobId: job.id, attempt: job.attempt, outDir: join(root, "candidate"), inputs, params: job.params, port });
      return { id, ok: true, result: { verdict: data.verdict, feedback: data.feedback }, error: null };
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  }

  private async finishAttempt(job: Job): Promise<Job> {
    const attemptRoot = this.attemptPath(job);
    const outDir = join(attemptRoot, "out");
    const station = this.snapshot.spec(job.station);
    const machine = this.snapshot.spec(this.snapshot.key("Machine", station.machine));
    const recipe = machine.recipes[station.recipe];
    const inputs = this.loadInputs(job);
    if (station.role === "sink") {
      return this.finishSink(job, recipe, inputs, join(attemptRoot, "ctl"));
    }
    const request = this.harvestRequest(recipe.outputs ?? {}, inputs, job.params);
    if (Object.keys(request.outputs).length) {
      const result = await harvest({
        request, ctl: join(attemptRoot, "ctl"), workspace: join(attemptRoot, "workspace"), out: outDir, in: join(attemptRoot, "in"),
      });
      if (!result.ok) {
        return this.jobs.transition(job.id, "FAILED", { error: { code: "HARVEST_FAILED", detail: result } });
      }
    }
    const gate = new Gate(this.snapshot, this.context.warehouse, join(attemptRoot, "gate"), this.provider, job.timeoutMs);
    const report = await gate.inspect(job.station, {
      jobId: job.id, attempt: job.attempt, outDir, inputs, params: job.params,
    });
    if (report.data.verdict !== "pass") {
      const error = report.data.verdict === "error";
      return this.jobs.transition(job.id, error ? "FAILED" : "REJECTED", {
        feedback: report.data.feedback, error: { code: error ? "GATE_ERROR" : "GATE_REJECTED" },
      });
    }
    const seals = Object.values(report.data.outputs).flat()
      .map((id): [string, Record<string, unknown>] => [id, report.data.facts[id] ?? {}]);
    return this.context.db.transaction(() => {
      this.context.warehouse.sealMany(seals, job.id, report.digest());
      this.context.warehouse.recordLineage(job.id, job.inputs, report.data.outputs);
      const external = station.role === "source";
      if (external) recordSourceProvenance(this.context, job, report.data.outputs);
      return this.jobs.transition(job.id, "SEALED", { outputs: report.data.outputs, external });
    }).immediate();
  }

  private failSink(job: Job, cause: Record<string, unknown>, costUsd = job.costUsd): Job {
    return this.context.db.transaction(() => {
      const latest = this.jobs.get(job.id);
      const error = { code: "SINK_OUTCOME_UNKNOWN", detail: { job: job.id, cause } };
      if (latest.state === "AWAITING_APPROVAL") this.jobs.transition(job.id, "FAILED", { error, costUsd });
      return this.jobs.transition(job.id, "DEAD", { error, costUsd });
    }).immediate();
  }

  private async finishSink(job: Job, recipe: { receipt?: Parameters<typeof renderReceiptPayload>[0] }, inputs: Record<string, Bean | Bean[]>, ctl: string): Promise<Job> {
    let receipt = this.receipts.forJob(job.id);
    if (!receipt) throw new FactoryError("RECEIPT_NOT_FOUND", "找不到工位的发布回执", { job: job.id });
    if (receipt.status === "claimed") {
      try {
        const payload = await readReceiptPayload(ctl, job.id) ?? renderReceiptPayload(recipe.receipt, inputs, job.params);
        if (!payload) throw new FactoryError("RECEIPT_MISSING", "发布工位没有留下回执", { job: job.id });
        receipt = this.receipts.publish(receipt.id, payload);
      } catch (error) {
        if (error instanceof FactoryError && error.code === "RECEIPT_INVALID") this.receipts.noteFormatError(receipt.id, String(error.detail?.problems ?? "receipt.json"));
        throw error;
      }
    }
    if (receipt.status === "verified") return this.jobs.transition(job.id, "SEALED");
    const result = await verifyReceipt(this.context, this.snapshot, this.provider, receipt);
    const receiptId = receipt.id;
    return this.context.db.transaction(() => {
      this.receipts.settle(receiptId, result);
      return result.ok ? this.jobs.transition(job.id, "SEALED") : this.jobs.transition(job.id, "DEAD", {
        error: { code: "RECEIPT_UNVERIFIED", detail: { job: job.id, receipt: receiptId } },
      });
    }).immediate();
  }

  private harvestRequest(
    outputs: Record<string, { type: string; harvest?: string; optional?: boolean }>,
    inputs: Record<string, Bean | Bean[]>,
    params: Record<string, unknown>,
  ): HarvestRequest {
    return {
      outputs: Object.fromEntries(Object.entries(outputs).flatMap(([port, declaration]) => {
        if (!declaration.harvest) return [];
        const list = declaration.type.startsWith("list<");
        const type = list ? declaration.type.slice(5, -1) : declaration.type;
        const payload: Array<{ path: string; required?: boolean }> = this.snapshot.spec(this.snapshot.typeKey(nominalTypeId(type))).payload ?? [];
        const required = payload.filter((rule) => rule.required !== false && !rule.path.includes("*"));
        const pattern = declaration.harvest.includes("{{")
          ? compileTemplate(declaration.harvest, { environment: { inputs: DYN, params: DYN } })
            .render(recipeEnv(inputs, params) as EvalEnvironment)
          : declaration.harvest;
        return [[port, {
          harvest: pattern, place_as: required.length === 1 ? required[0].path.split("/").at(-1) : "file",
          optional: declaration.optional ?? false, list,
        }]];
      })),
    };
  }
}
