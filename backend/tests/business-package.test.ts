import { expect, test } from "bun:test";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createApp } from "../src/server";
import { createBusinessPackage } from "../src/business-package";
import { digestBytes } from "../src/legacy/foundation";
import { VaultStore } from "../src/vault";

function factory(root: string) {
  const database = join(root, "factory.db");
  return createApp({ storageRoot: root, dbPath: database, legacyPath: database, dispatchWork: false,
    vault: new VaultStore(join(root, "vault.json"), join(root, "vault.key")) });
}

async function cli(root: string, ...arguments_: string[]) {
  const bun = Bun as unknown as { spawn(argv: string[], options: object): { exited: Promise<number>; stdout: ReadableStream; stderr: ReadableStream } };
  const child = bun.spawn([Bun.argv[0], "run", "backend/src/cli.ts", "business", ...arguments_], {
    env: { ...process.env, FACTORY_ROOT: root, FACTORY_DB_PATH: join(root, "factory.db"), BUSINESS_DB_PATH: join(root, "factory.db"),
      MAO_VAULT_PATH: join(root, "vault.json"), MAO_VAULT_KEY_PATH: join(root, "vault.key") },
    stdout: "pipe", stderr: "pipe",
  });
  const [code, stdout, stderr] = await Promise.all([child.exited, new Response(child.stdout).text(), new Response(child.stderr).text()]);
  return { code, stdout, stderr };
}

async function intake(app: ReturnType<typeof factory>, system: string, name: string, contents: string) {
  const body = new FormData();
  body.set("name", name);
  body.set("business_system_id", system);
  body.append("files", new File([contents], `${name}.txt`));
  const response = await app.fetch(new Request("http://localhost/api/v1/projects/intake", { method: "POST", body }));
  expect(response.status).toBe(201);
  return await response.json() as { id: string; sources: Array<{ fileRef: string }> };
}

test("moves selected business data and file bytes into a fresh factory root", { timeout: 60_000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-business-package-"));
  const sellerRoot = join(root, "seller");
  const packageRoot = join(root, "public");
  const buyerRoot = join(root, "buyer");
  const seller = factory(sellerRoot);
  try {
    const selected = seller.store.createBusinessSystem({ name: "Portable factory" });
    const other = seller.store.createBusinessSystem({ name: "Unselected factory" });
    const project = await intake(seller, selected.id, "Public input", "source document\n");
    await intake(seller, other.id, "PRIVATE-UNRELATED-MARKER", "unselected bytes\n");
    seller.store.createBusinessRun(selected.id);
    const exported = await cli(sellerRoot, "export", selected.id, "--package", packageRoot);
    expect(exported.code).toBe(0);
    expect(readdirSync(packageRoot).sort()).toEqual(["blobs", "business.json", "manifest.json"]);
    expect(JSON.parse(exported.stdout).blobs).toHaveLength(1);
    expect(await Bun.file(join(packageRoot, "business.json")).text()).not.toContain("PRIVATE-UNRELATED-MARKER");

    const verified = await cli(join(root, "isolated"), "verify", packageRoot);
    expect(verified.code).toBe(0);
    expect(JSON.parse(verified.stdout)).toEqual({ ok: true, problems: [], acceptance: "not_run" });
    expect(existsSync(join(root, "isolated"))).toBe(false);
    const imported = await cli(join(root, "isolated"), "import", packageRoot, "--root", buyerRoot);
    expect(imported.code).toBe(0);
    expect(JSON.parse(imported.stdout)).toMatchObject({ root: buyerRoot, system: { name: "Portable factory" }, acceptance: "not_run" });
    const buyer = factory(buyerRoot);
    try {
      const system = buyer.store.listBusinessSystems()[0];
      const [importedProject] = buyer.store.listProjects(system.id);
      expect(system.id).not.toBe(selected.id);
      expect(importedProject.id).not.toBe(project.id);
      expect(buyer.store.listBusinessRunsWithUnits(system.id)).toEqual([]);
      const digest = project.sources[0].fileRef.slice(6);
      const download = await buyer.fetch(new Request(`http://localhost/api/v1/projects/${importedProject.id}/files/${digest}`));
      expect(download.status).toBe(200);
      expect(await download.text()).toBe("source document\n");
    } finally { buyer.stop(); }
  } finally { seller.stop(); rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 }); }
});

test("rejects extra bytes, unsafe manifest paths and changed bytes before installing", { timeout: 60_000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-business-corrupt-"));
  const packageRoot = join(root, "package");
  const target = join(root, "new-factory");
  try {
    await createBusinessPackage(packageRoot, { schema: "mao.business-system/1", system: { name: "Portable" } }, root, []);
    const manifestPath = join(packageRoot, "manifest.json");
    const manifest = JSON.parse(await Bun.file(manifestPath).text());
    await Bun.write(join(packageRoot, "extra.txt"), "extra payload");
    const extra = await cli(root, "verify", packageRoot);
    expect(extra.code).toBe(1);
    expect(JSON.parse(extra.stdout)).toMatchObject({ ok: false, acceptance: "not_run" });
    rmSync(join(packageRoot, "extra.txt"));
    await Bun.write(manifestPath, JSON.stringify({ ...manifest, blobs: [{ digest: `sha256:${"0".repeat(64)}`, size: 0, path: "../outside" }] }));
    expect((await cli(root, "verify", packageRoot)).code).toBe(1);
    await Bun.write(manifestPath, JSON.stringify(manifest));
    await Bun.write(join(packageRoot, "business.json"), "tampered");
    expect((await cli(root, "import", packageRoot, "--root", target)).code).toBe(1);
    expect(existsSync(target)).toBe(false);
    expect(existsSync(join(root, "outside"))).toBe(false);
  } finally { rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 }); }
});

test("rejects known configured secret bytes inside a selected binary payload", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-business-package-secret-"));
  const path = join(root, "package");
  try {
    const source = join(root, "source.bin");
    const secret = "known-test-credential";
    const bytes = new Uint8Array(200_000);
    bytes.set(new TextEncoder().encode(secret), 65_532);
    await Bun.write(source, bytes);
    const digest = digestBytes(bytes);
    const hex = digest.slice(7);
    mkdirSync(join(root, "cas", "sha256", hex.slice(0, 2)), { recursive: true });
    await Bun.write(join(root, "cas", "sha256", hex.slice(0, 2), hex.slice(2)), Bun.file(source));
    await expect(createBusinessPackage(path, { schema: "mao.business-system/1", system: { name: "Portable" }, ref: `cas://${digest}` }, root, [secret]))
      .rejects.toMatchObject({ code: "ASSET_SECRET" });
    expect(existsSync(path)).toBe(false);
  } finally { rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 }); }
});
