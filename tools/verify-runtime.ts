import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { selectedContainerProvider } from "../backend/runtime/selected";
import { Context, Gate, Runner, loadSnapshot } from "../backend/src/legacy";
import { imageIds, renderRegistry, writeJson } from "../tests/fakes/fakelib";

const verifyMounts = `
import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
assert.equal(process.getuid(), 10001);
for (const path of ["/factory/in/task/task.json", "/opt/factory/bin/factoryctl"]) {
  assert.throws(() => writeFileSync(path, "unexpected"));
}
`;

async function main(): Promise<void> {
  const provider = selectedContainerProvider();
  const health = await provider.ensureReady();
  assert.ok(health.available, health.reason ?? `${provider.name} unavailable`);
  const images = await imageIds();
  const root = await mkdtemp(join(tmpdir(), "mao-runtime-"));
  const context = Context.open(root);
  let jobId: string | undefined;
  let passed = false;
  try {
    const registry = await renderRegistry(join(root, "registry"), { images });
    const snapshot = await loadSnapshot(registry);
    const gate = new Gate(snapshot, context.warehouse, join(root, "tmp", "gate"), provider);
    const expected = "one\ntwo\n";
    const path = await writeJson(join(root, "task.json"), { cost_usd: 0.1, default: [
      { do: "run", argv: ["bun", "-e", verifyMounts] },
      { do: "ctl", args: ["inputs", "--json"] },
      { do: "ctl", args: ["log", "mounted factoryctl"] },
      { do: "ctl", args: ["approval", "request", "--kind", "cost", "--summary", "fixture", "--cost-usd", "0.1"] },
      { do: "write", path: "/workspace/drafts/note.txt", text: expected },
      { do: "ctl", args: ["check", "note", "/workspace/drafts/note.txt"] },
    ] });
    const { report, bean } = await gate.admitExternal("agent.task@1.0", { "task.json": path }, {});
    assert.ok(bean, report.data.feedback);
    context.warehouse.sealMany([[bean.id, report.data.facts[bean.id]]], null, report.digest());
    const runner = new Runner(context, snapshot, provider);
    jobId = runner.submit("writer@1", { task: bean.id }, {}).id;
    const finished = await runner.run(jobId);
    assert.equal(finished.state, "SEALED", JSON.stringify(finished.error));
    assert.equal(finished.attempt, 1);
    assert.equal(runner.approvals.list({ jobId })[0]?.decidedBy, "auto");
    const output = context.warehouse.get(finished.outputs.note[0]);
    assert.equal(output.facts.lines, 2);
    assert.equal(await Bun.file(context.warehouse.blobPath(output.payload[0].digest)).text(), expected);
    assert.deepEqual(context.warehouse.lineage(output.id, "up").beans, [bean.id]);
    assert.match(await Bun.file(join(root, "jobs", jobId, "1", "ctl", "log.jsonl")).text(), /mounted factoryctl/);
    console.log(`${provider.name}: 作业 ${jobId} 已封存，预检、审批和物料血缘通过`);
    passed = true;
  } finally {
    context.close();
    if (passed) await rm(root, { recursive: true, force: true });
    else console.error(`验收数据 ${root}${jobId ? `，作业 ${jobId}` : ""}`);
  }
}

try {
  await main();
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
