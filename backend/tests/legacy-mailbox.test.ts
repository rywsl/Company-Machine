import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, symlinkSync } from "node:fs";
import { join, resolve } from "node:path";
import { CtlServer, main, readProposal, request, writeProposal, type CtlRequest } from "../src/legacy/factoryctl";

function fixture() {
  const root = resolve(process.platform === "linux" ? "/tmp" : "tmp", "legacy-mailbox-" + randomUUID());
  const ctl = join(root, "ctl");
  const requests = join(ctl, "requests");
  const responses = join(ctl, "responses");
  for (const folder of [requests, responses]) mkdirSync(folder, { recursive: true });
  return { root, ctl, requests, responses, close: () => rmSync(root, { recursive: true, force: true, maxRetries: 4 }) };
}

describe("Bun factoryctl mailbox", () => {
  test("writes an async response once, then runs its callback before stop finishes", async () => {
    const paths = fixture();
    let canApprove = false;
    let observed: CtlRequest | undefined;
    let answered = 0;
    const server = new CtlServer(paths.ctl, {
      async handle(body) {
        observed = body;
        if (!canApprove) return undefined;
        return { id: body.id, ok: true, result: { decision: "approve" }, error: null };
      },
    }, {
      async afterResponse(body) {
        expect(JSON.parse(readFileSync(join(paths.responses, body.id + ".json"), "utf8")).result).toEqual({ decision: "approve" });
        answered += 1;
      },
    });
    try {
      const id = "r-12345678";
      await Bun.write(join(paths.requests, id + ".json"), JSON.stringify({ id, op: "approval", args: { kind: "expense" } }));
      expect(await server.pollOnce()).toBe(0);
      expect(observed?.args).toEqual({ kind: "expense" });
      canApprove = true;
      server.start(15);
      expect(await server.pollOnce()).toBe(1);
      await server.stop();
      expect(answered).toBe(1);
      expect(await server.pollOnce()).toBe(0);
    } finally { await server.stop(); paths.close(); }
  });

  test("handles concurrent clients and preserves check verdict or approval failure responses", async () => {
    const paths = fixture();
    const server = new CtlServer(paths.ctl, {
      handle(body) {
        if (body.op === "check") return { id: body.id, ok: true, result: { verdict: "reject", feedback: "missing" }, error: null };
        return { id: body.id, ok: false, result: null, error: { code: "APPROVAL_DENIED", message: "Denied" } };
      },
    });
    try {
      server.start(5);
      const options = { ctl: paths.ctl, timeoutMs: 3000, pollIntervalMs: 5 };
      const [check, approval] = await Promise.all([
        request("check", { port: "note", path: "/workspace/draft.txt" }, options),
        request("approval", { kind: "expense", summary: "Draft", cost_usd: 1 }, options),
      ]);
      expect(check.id).not.toBe(approval.id);
      expect(check).toMatchObject({ ok: true, result: { verdict: "reject", feedback: "missing" } });
      expect(approval).toMatchObject({ ok: false, error: { code: "APPROVAL_DENIED", message: "Denied" } });
      await server.stop();
      await expect(request("check", { port: "note" }, { ...options, timeoutMs: 20 })).rejects.toThrow("等待宿主响应超时");
    } finally { await server.stop(); paths.close(); }
  });

  test("rejects malformed request files and request paths that are directories", async () => {
    const paths = fixture();
    let dispatched = 0;
    const server = new CtlServer(paths.ctl, { handle() { dispatched += 1; return undefined; } });
    try {
      const directory = join(paths.root, "outside");
      mkdirSync(directory);
      symlinkSync(directory, join(paths.requests, "r-12345678.json"), process.platform === "win32" ? "junction" : "dir");
      await Bun.write(join(paths.requests, "r-23456789.json"), JSON.stringify({ id: "r-wrong", op: "check", args: {} }));
      await Bun.write(join(paths.requests, "r-34567890.json.part"), "partial");
      await Bun.write(join(paths.requests, "r-456789ab.json"), JSON.stringify({ id: "r-456789ab", args: {} }));
      expect(await server.pollOnce()).toBe(3);
      const reply = (id: string) => JSON.parse(readFileSync(join(paths.responses, id + ".json"), "utf8"));
      expect(reply("r-12345678")).toMatchObject({ ok: false, error: { code: "CTL_REQUEST_INVALID" } });
      expect(reply("r-23456789")).toMatchObject({ ok: false, error: { code: "CTL_REQUEST_INVALID" } });
      expect(reply("r-456789ab")).toMatchObject({ ok: false, error: { code: "CTL_REQUEST_INVALID" } });
      expect(dispatched).toBe(0);

      const linkedRoot = join(paths.root, "linked");
      symlinkSync(paths.ctl, linkedRoot, process.platform === "win32" ? "junction" : "dir");
      const linked = new CtlServer(linkedRoot, { handle() { return undefined; } });
      await expect(linked.pollOnce()).rejects.toThrow();
    } finally { paths.close(); }
  });

  test("defers an unanswered check, publishes its exact reply once and ignores partial files", async () => {
    const paths = fixture();
    const seen: CtlRequest[] = [];
    const response = { id: "r-0000000a", ok: true, result: { verdict: "reject", feedback: "行数不够" }, error: null };
    const server = new CtlServer(paths.ctl, {
      handle(body) { seen.push(body); return seen.length === 1 ? undefined : response; },
    });
    try {
      await Bun.write(join(paths.requests, "r-0000000a.json"), JSON.stringify({ id: response.id, op: "check", args: {} }));
      await Bun.write(join(paths.requests, "r-0000000b.json.part"), "{");
      const responseFile = join(paths.responses, "r-0000000a.json");
      expect(await server.pollOnce()).toBe(0);
      expect(existsSync(responseFile)).toBe(false);
      expect(await server.pollOnce()).toBe(1);
      expect(JSON.parse(readFileSync(responseFile, "utf8"))).toEqual(response);
      expect(await server.pollOnce()).toBe(0);
      expect(seen.map(({ op }) => op)).toEqual(["check", "check"]);
      expect(readdirSync(paths.responses)).toEqual(["r-0000000a.json"]);
    } finally { paths.close(); }
  });

  test("dispatches a complete check once and skips both answered and partial requests", async () => {
    const paths = fixture();
    const seen: CtlRequest[] = [];
    const id = "r-0000000a";
    const response = { id, ok: true, result: { verdict: "reject", feedback: "行数不够" }, error: null };
    const server = new CtlServer(paths.ctl, { handle(body) { seen.push(body); return response; } });
    try {
      await Bun.write(join(paths.requests, `${id}.json`), JSON.stringify({ id, op: "check", args: {} }));
      await Bun.write(join(paths.requests, "r-0000000b.json.part"), "{");
      expect(await server.pollOnce()).toBe(1);
      expect(JSON.parse(readFileSync(join(paths.responses, `${id}.json`), "utf8"))).toEqual(response);
      expect(await server.pollOnce()).toBe(0);
      expect(seen.map(({ op }) => op)).toEqual(["check"]);
      expect(readdirSync(paths.responses)).toEqual([`${id}.json`]);
    } finally { paths.close(); }
  });

  test("runs the check, log, fail and contract commands at the mailbox boundary", async () => {
    const paths = fixture();
    const previous = process.env.FACTORY_CTL;
    const stdout = process.stdout.write;
    const stderr = process.stderr.write;
    const outputs: string[] = [];
    const received: CtlRequest[] = [];
    const reply = { verdict: "reject", feedback: "行数不够" };
    const server = new CtlServer(paths.ctl, { handle(body) { received.push(body); return { id: body.id, ok: true, result: reply, error: null }; } });
    try {
      process.env.FACTORY_CTL = paths.ctl;
      process.stdout.write = (text) => { outputs.push(text); return true; };
      process.stderr.write = () => true;
      await Bun.write(join(paths.ctl, "contract.json"), JSON.stringify({ approval: { timeout_s: 5 }, limits: {} }));
      server.start(5);
      expect(await main(["check", "note", "/workspace/a.txt"])).toBe(0);
      expect(JSON.parse(outputs[0])).toEqual(reply);
      expect(received.map(({ op, args }) => ({ op, args }))).toEqual([{ op: "check", args: { port: "note", path: "/workspace/a.txt" } }]);
      expect(await main(["log", "hello {x}"])).toBe(0);
      expect(await main(["log", "第二行"])).toBe(0);
      const entries = readFileSync(join(paths.ctl, "log.jsonl"), "utf8").trim().split(/\r?\n/);
      expect(entries.map((line: string) => JSON.parse(line).message)).toEqual(["hello {x}", "第二行"]);
      expect(await main(["fail", "坏了"])).toBe(1);
      expect(JSON.parse(readFileSync(join(paths.ctl, "fail.json"), "utf8"))).toEqual({ reason: "坏了" });
      rmSync(join(paths.ctl, "contract.json"));
      expect(await main(["contract"])).toBe(4);
      delete process.env.FACTORY_CTL;
      expect(await main(["log", "x"])).toBe(4);
    } finally {
      process.stdout.write = stdout;
      process.stderr.write = stderr;
      if (previous === undefined) delete process.env.FACTORY_CTL; else process.env.FACTORY_CTL = previous;
      await server.stop();
      paths.close();
    }
  });

  test("parses approval flags and reads an unknown budget through the CLI", async () => {
    const paths = fixture();
    const previous = process.env.FACTORY_CTL;
    const stdout = process.stdout.write;
    const stderr = process.stderr.write;
    const outputs: string[] = [];
    let received: CtlRequest | undefined;
    const server = new CtlServer(paths.ctl, {
      handle(body) {
        received = body;
        return { id: body.id, ok: true, result: { decision: "approve" }, error: null };
      },
    });
    try {
      process.env.FACTORY_CTL = paths.ctl;
      process.stdout.write = (text) => { outputs.push(text); return true; };
      process.stderr.write = () => true;
      await Bun.write(join(paths.ctl, "job.json"), JSON.stringify({ job_id: "job-test", budget_usd: 2 }));
      expect(await main(["budget", "--json"])).toBe(0);
      expect(JSON.parse(outputs[0])).toMatchObject({ budget_usd: 2, spent_usd: null, remaining_usd: null });
      expect(await main(["approval", "request", "--kind", "expense", "--summary", "Draft", "--cost-usd", "-1"])).toBe(2);
      server.start(5);
      expect(await main(["approval", "request", "--kind", "expense", "--summary", "Draft"])).toBe(0);
      expect(received?.args).toEqual({ kind: "expense", summary: "Draft", cost_usd: 0 });
      expect(JSON.parse(outputs[1])).toEqual({ decision: "approve" });
      expect(await main(["proposal", "--summary", "Draft", "--cost-usd", "2", "--time-s", "5"])).toBe(0);
      expect(readProposal(paths.ctl)).toEqual({ summary: "Draft", cost_estimate_usd: 2, time_estimate_s: 5 });
      expect(await main(["proposal", "--summary", "Invalid", "--cost-usd", "2"])).toBe(2);
    } finally {
      process.stdout.write = stdout;
      process.stderr.write = stderr;
      if (previous === undefined) delete process.env.FACTORY_CTL; else process.env.FACTORY_CTL = previous;
      await server.stop();
      paths.close();
    }
  });

  test("reads only regular proposal files and accepts an atomically replaced proposal", async () => {
    const paths = fixture();
    try {
      expect(() => readProposal(paths.ctl)).toThrow("没有 proposal.json");
      const summary = { summary: "Quote", cost_estimate_usd: 1.5, time_estimate_s: 30 };
      await writeProposal(paths.ctl, summary);
      expect(readProposal(paths.ctl)).toEqual(summary);
      const next = { ...summary, cost_estimate_usd: 2, plan: { filename: "report" } };
      await writeProposal(paths.ctl, next);
      expect(readProposal(paths.ctl)).toEqual(next);
      await expect(writeProposal(paths.ctl, { ...next, cost_estimate_usd: -1 })).rejects.toThrow("方案需要");
      expect(readProposal(paths.ctl)).toEqual(next);
      const file = join(paths.ctl, "proposal.json");
      rmSync(file);
      symlinkSync(paths.responses, file, process.platform === "win32" ? "junction" : "dir");
      expect(() => readProposal(paths.ctl)).toThrow("无法读取方案文件");
    } finally { paths.close(); }
  });
});
