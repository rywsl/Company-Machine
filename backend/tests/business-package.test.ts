import { expect, test } from "bun:test";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createApp } from "../src/server";
import { businessPackageBlob, createBusinessPackage, readBusinessPackage } from "../src/business-package";
import { digestBytes } from "../src/legacy/foundation";
import { Warehouse } from "../src/legacy/warehouse";
import { createMaterialFileStage } from "../src/material-files";
import { VaultStore } from "../src/vault";
import { LocalBlobProvider } from "../src/providers";

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
    const outputPath = join(sellerRoot, "generated.txt");
    const outputText = "output that a downstream Operator can read\n";
    await Bun.write(outputPath, outputText);
    const sellerWarehouse = new Warehouse(sellerRoot, seller.store.db, seller.store.clock);
    const outputDigest = await sellerWarehouse.putFile(outputPath);
    const outputSize = new TextEncoder().encode(outputText).byteLength;
    const material = seller.store.addArtifact({
      projectId: project.id, recordType: "material", kind: "build.report", ref: "material://report", summary: "Build report",
      files: [{ ref: "cas://" + outputDigest, path: "generated.txt", digest: outputDigest, mediaType: "text/plain" }],
      metadata: {},
    });
    seller.store.registerArtifactFiles(material.id, [{ position: 0, digest: outputDigest, size: outputSize }]);
    seller.store.createBusinessRun(selected.id);
    const exported = await cli(sellerRoot, "export", selected.id, "--package", packageRoot);
    expect(exported.code).toBe(0);
    expect(readdirSync(packageRoot).sort()).toEqual(["blobs", "business.json", "manifest.json"]);
    expect(JSON.parse(exported.stdout).blobs).toHaveLength(2);
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
      const [importedMaterial] = buyer.store.listArtifacts(importedProject.id);
      expect(importedMaterial.id).not.toBe(material.id);
      expect(importedMaterial.files).toEqual(material.files);
      const grants = buyer.store.artifactFileGrants(system.id, [importedMaterial.id]);
      expect(grants).toEqual([{ artifactId: importedMaterial.id, position: 0, digest: outputDigest, size: outputSize }]);
      const buyerWarehouse = new Warehouse(buyerRoot, buyer.store.db, buyer.store.clock);
      const stage = createMaterialFileStage(buyerWarehouse);
      try {
        const { mounts } = await stage.prepareInputs(grants);
        expect(await Bun.file(mounts[0].source).text()).toBe(outputText);
      } finally { stage.close(); }

      const metadataImport = await buyer.fetch(new Request("http://localhost/api/v1/business-systems/import", {
        method: "POST", headers: { "content-type": "application/json" }, body: await Bun.file(join(packageRoot, "business.json")).text(),
      }));
      expect(metadataImport.status).toBe(201);
      const { system: metadataSystem, records } = await metadataImport.json();
      expect(records[0].files).toEqual(material.files);
      expect(buyer.store.artifactFileGrants(metadataSystem.id, [records[0].id])).toEqual([]);
    } finally { buyer.stop(); }

    await Bun.write(businessPackageBlob(packageRoot, outputDigest), "Changed" + outputText.slice(7));
    const changed = await cli(join(root, "isolated"), "verify", packageRoot);
    expect(changed.code).toBe(1);
    expect(JSON.parse(changed.stdout)).toMatchObject({ ok: false, problems: [{ reason: "blob checksum" }] });
    const rejectedRoot = join(root, "rejected-import");
    expect((await cli(join(root, "isolated"), "import", packageRoot, "--root", rejectedRoot)).code).toBe(1);
    expect(existsSync(rejectedRoot)).toBe(false);
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

test("rejects external CAS directories when exporting a closed business package", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-business-package-cas-"));
  try {
    const payload = new TextEncoder().encode("fixture bytes outside the selected warehouse");
    const digest = digestBytes(payload);
    const hex = digest.slice(7);
    const outside = join(root, "external");
    const parts = ["cas", "sha256", hex.slice(0, 2)];
    const externalBucket = join(outside, ...parts);
    mkdirSync(externalBucket, { recursive: true });
    await Bun.write(join(externalBucket, hex.slice(2)), payload);
    const document = { schema: "mao.business-system/1", system: { name: "Portable" }, ref: `cas://${digest}` };

    for (const [index, directory] of parts.entries()) {
      const warehouse = join(root, `warehouse-${index}`);
      const parents = parts.slice(0, index);
      mkdirSync(join(warehouse, ...parents), { recursive: true });
      symlinkSync(join(outside, ...parents, directory), join(warehouse, ...parents, directory), "junction");
      const destination = join(root, `package-${index}`);
      await expect(createBusinessPackage(destination, document, warehouse, []))
        .rejects.toMatchObject({ code: "PACKAGE_CORRUPT", detail: { reason: index === 2 ? "invalid CAS bucket" : "invalid CAS directory" } });
      expect(existsSync(destination)).toBe(false);
      expect(readdirSync(root).some((name) => name.startsWith(`.package-${index}-`))).toBe(false);
    }

    const valid = join(root, "valid");
    await createBusinessPackage(valid, document, outside, []);
    expect((await readBusinessPackage(valid)).blobs).toEqual([{ digest, size: payload.byteLength }]);
    expect(new Uint8Array(await Bun.file(businessPackageBlob(valid, digest)).arrayBuffer())).toEqual(payload);
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

test("exports a business package from an injected blob provider", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-business-package-remote-"));
  try {
    const blobRoot = join(root, "remote-cache");
    const provider = new LocalBlobProvider(blobRoot);
    const payload = new TextEncoder().encode("provider-backed bytes");
    const digest = await provider.put(payload);
    const packageRoot = join(root, "package");
    await createBusinessPackage(packageRoot, { schema: "mao.business-system/1", system: { name: "Remote source" }, ref: `cas://${digest}` }, join(root, "no-local-cas"), [], provider);
    expect((await readBusinessPackage(packageRoot)).blobs).toEqual([{ digest, size: payload.byteLength }]);
    expect(new TextDecoder().decode(await Bun.file(businessPackageBlob(packageRoot, digest)).arrayBuffer())).toBe("provider-backed bytes");
  } finally { rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 }); }
});
