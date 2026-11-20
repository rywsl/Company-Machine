import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AcpTransport, defaultAcpSpawn, type AcpSpawn } from "./acp";
import { OfficialCliTransport, chooseTransport, harnessEndpointEnv, resolveOfficialHarness, type HarnessSession, type OfficialSpawn } from "./harness";
import type { HarnessProfile } from "./models";

const verificationReply = "MAO-OK";
type Outcome = { state: "verified" | "failed"; reason: string };

interface VerificationOptions {
  spawn?: OfficialSpawn;
  acpSpawn?: AcpSpawn;
  ensureRuntime: () => Promise<void>;
  signal: AbortSignal;
  timeoutMs?: number;
}

const failed = (reason: string): Outcome => ({ state: "failed", reason });

export async function verifyHarness(profile: HarnessProfile, options: VerificationOptions): Promise<Outcome> {
  const definition = resolveOfficialHarness(profile.runtime);
  if (!definition) return failed("未找到配置的官方 Harness");
  let env: Record<string, string>;
  try {
    env = harnessEndpointEnv(definition, profile.baseUrl);
  } catch {
    return failed("Harness Base URL 无效");
  }
  const useAcp = chooseTransport(definition, Boolean(options.acpSpawn || process.env.MAO_ACP_AVAILABLE === "1")) === "acp";

  let stopped = false;
  let workspace: string | undefined;
  let session: HarnessSession | undefined;
  let pendingKill: (() => void) | undefined;

  function trackProcess<T extends { kill(): void }>(child: T): T {
    const kill = () => {
      if (pendingKill !== kill) return;
      pendingKill = undefined;
      try { child.kill(); } catch { /* The process may already have exited. */ }
    };
    pendingKill = kill;
    return { ...child, kill };
  }

  let finishInterruption!: (outcome: Outcome) => void;
  const interruption = new Promise<Outcome>((resolve) => { finishInterruption = resolve; });
  function interrupt(reason: string) {
    stopped = true;
    finishInterruption(failed(reason));
  }
  const abort = () => interrupt("官方 Harness 验证已取消");
  if (options.signal.aborted) abort();
  options.signal.addEventListener("abort", abort, { once: true });
  const timer = setTimeout(() => interrupt("官方 Harness 验证超时"), options.timeoutMs ?? 45_000);
  const acpSpawn = options.acpSpawn ?? (profile.role === "operator" ? options.spawn : undefined) ?? defaultAcpSpawn;
  const transport = useAcp
    ? new AcpTransport(definition, (...args) => trackProcess(acpSpawn(...args)))
    : new OfficialCliTransport(definition, options.spawn);

  async function probe(): Promise<Outcome> {
    if (stopped) return failed("官方 Harness 验证已取消");
    if (profile.image) {
      try {
        await options.ensureRuntime();
      } catch {
        return failed("容器运行时未就绪");
      }
    }
    if (stopped) return failed("官方 Harness 验证已取消");

    workspace = mkdtempSync(join(tmpdir(), "mao-harness-verify-"));
    const started = await transport.start({
      contract: "This is a text-only connectivity check. Do not use tools, files, or project resources.",
      task: `Reply with exactly ${verificationReply}.`,
      model: profile.model,
      cwd: workspace,
      env,
      containerImage: profile.image,
      network: profile.network === "egress" ? "egress" : "none",
    });
    if (stopped) {
      await started.close();
      return failed("官方 Harness 验证已取消");
    }
    session = started;

    let reply = "";
    let finished = false;
    for await (const event of session.events()) {
      if (event.type === "session.failed") return failed("官方 Harness 会话失败");
      if (event.type === "tool.started" || event.type === "tool.finished" || event.type === "record" || event.type === "secret.request") {
        return failed("官方 Harness 验证应只返回文本");
      }
      if (event.type === "text.delta" && event.text) reply += event.text;
      if (event.type === "session.finished") {
        finished = true;
        if (!reply && event.text) reply = event.text;
      }
      if (reply.length > 128) return failed("官方 Harness 未返回预期文本");
    }
    return finished && reply.trim() === verificationReply
      ? { state: "verified", reason: "官方 Harness 已返回验证响应" }
      : failed("官方 Harness 未返回预期文本");
  }

  try {
    return await Promise.race([interruption, probe()]);
  } catch {
    return failed("官方 Harness 无法完成验证");
  } finally {
    stopped = true;
    clearTimeout(timer);
    options.signal.removeEventListener("abort", abort);
    try { await session?.close(); } catch { /* The process may already have exited. */ }
    pendingKill?.();
    if (workspace) {
      try { rmSync(workspace, { recursive: true, force: true }); } catch { /* The process may still be exiting. */ }
    }
  }
}
