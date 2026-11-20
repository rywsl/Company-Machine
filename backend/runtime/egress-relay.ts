import { createHash } from "node:crypto";
import { chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { startEgressForwarder, type EgressForwarder } from "../src/legacy/egress-forwarder";
import type { EgressPlan } from "../src/legacy/egress";
import { FactoryError } from "../src/legacy/errors";
import { bindSandboxEgress, type SandboxEgress } from "../src/legacy/sandbox-egress";
import type { CommandRunner } from "./podman";

const socket = "/run/mao-egress.sock";
const port = 18080;
const relay = [
  'import { createConnection, createServer } from "node:net";',
  'import { networkInterfaces } from "node:os";',
  'if (Object.keys(networkInterfaces()).some((name) => name !== "lo")) process.exit(1);',
  'const server = createServer((client) => {',
  '  const upstream = createConnection({ path: ' + JSON.stringify(socket) + ' });',
  '  client.once("error", () => upstream.destroy());',
  '  client.once("close", () => upstream.destroy());',
  '  upstream.once("error", () => client.destroy());',
  '  client.pipe(upstream).pipe(client);',
  '});',
  'server.maxConnections = 32;',
  'server.listen(' + port + ', "127.0.0.1");',
].join("\n");

const probe = [
  'import { createConnection } from "node:net";',
  'const timer = setTimeout(() => process.exit(1), 3000);',
  'for (;;) {',
  '  const ready = await new Promise((done) => {',
  '    const client = createConnection({ host: "127.0.0.1", port: ' + port + ' });',
  '    client.once("error", () => done(false));',
  '    client.once("connect", () => client.write("CONNECT readiness.invalid:443 HTTP/1.1\\r\\nHost: readiness.invalid:443\\r\\n\\r\\n"));',
  '    let response = "";',
  '    client.on("data", (data) => { response += data.toString(); });',
  '    client.once("close", () => done(response.startsWith("HTTP/1.1 407 ")));',
  '  });',
  '  if (ready) break;',
  '  await Bun.sleep(50);',
  '}',
  'clearTimeout(timer);',
].join("\n");

export function egressRelayName(name: string): string {
  return "mao-egress-" + createHash("sha256").update(name).digest("hex").slice(0, 24);
}

type LaunchOptions = {
  name: string;
  image: string;
  plan: EgressPlan;
  command: (...args: string[]) => string[];
  run: CommandRunner;
  startForwarder?: typeof startEgressForwarder;
};

export type ContainerEgress = SandboxEgress & { network: string };

export async function launchEgressRelay(options: LaunchOptions): Promise<ContainerEgress> {
  const { command, run, plan, image } = options;
  const name = egressRelayName(options.name);
  let forwarder: EgressForwarder | undefined;
  let launched = false;
  let closing: Promise<void> | undefined;
  const close = () => closing ??= (async () => {
    try { await forwarder?.close(); }
    finally {
      if (launched) {
        const result = await run(command("rm", "-f", name), { timeoutMs: 15_000 });
        if (result.exitCode !== 0 && !/no such container|no container with name|not found/i.test(new TextDecoder().decode(result.stderr))) {
          throw new FactoryError("EGRESS_PROXY_UNAVAILABLE", "无法清理沙箱出网代理", { name: options.name });
        }
      }
    }
  })();
  try {
    forwarder = await (options.startForwarder ?? startEgressForwarder)({ plan, socketDirectory: tmpdir() });
    if (!("socketPath" in forwarder.endpoint) || forwarder.endpoint.socketPath.includes(",")) throw new Error("Invalid relay socket mount");
    // Podman remaps the workload UID. The host-only parent remains mode 0700 and only this socket is mounted.
    chmodSync(forwarder.endpoint.socketPath, 0o666);
    launched = true;
    const started = await run(command(
      "run", "--name", name, "--detach", "--pull=never", "--network", "none", "--init", "--user", "10001:10001",
      "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--pids-limit", "32",
      "--mount", "type=bind,source=" + forwarder.endpoint.socketPath + ",target=" + socket + ",readonly",
      "--entrypoint", "bun", image, "-e", relay,
    ), { timeoutMs: 30_000 });
    const id = new TextDecoder().decode(started.stdout).trim();
    if (started.exitCode !== 0 || !/^[a-f0-9]{64}$/.test(id)) throw new Error("Failed to start relay");
    const ready = await run(command("exec", id, "bun", "-e", probe), { timeoutMs: 5_000 });
    if (ready.exitCode !== 0) throw new Error("The relay could not reach its forwarder");
    const binding = bindSandboxEgress(forwarder, port);
    return { network: "container:" + id, env: binding.env, close };
  } catch {
    await close().catch(() => {});
    throw new FactoryError("EGRESS_PROXY_UNAVAILABLE", "隔离出网代理未就绪", { name: options.name });
  }
}
