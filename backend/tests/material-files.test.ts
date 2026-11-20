import { expect, test } from "bun:test";
import { chmodSync, existsSync, linkSync, mkdirSync, mkdtempSync, readdirSync, renameSync, rmSync, statSync, symlinkSync } from "node:fs";
import { open } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { createMaterialFileStage } from "../src/material-files";
import { Context } from "../src/legacy/context";
import { digestBytes } from "../src/legacy/foundation";

async function fixture(work: (context: Context, stage: ReturnType<typeof createMaterialFileStage>) => Promise<void>): Promise<void> {
  const context = Context.open(mkdtempSync(join(tmpdir(), "material-files-test-")));
  const stage = createMaterialFileStage(context.warehouse);
  try { await work(context, stage); }
  finally {
    stage.close();
    context.close();
    rmSync(context.root, { recursive: true, force: true, maxRetries: 4, retryDelay: 75 });
  }
}

test("collects declared staged file bytes and prepares scoped read-only input mount sources", async () => fixture(async ({ warehouse }, stage) => {
  const bytes = new Uint8Array(1024 * 1024 + 17).fill(29);
  bytes[1024 * 1024] = 4;
  await Bun.write(join(stage.outputDirectory, "报告.md"), bytes);
  await Bun.write(join(stage.outputDirectory, "空.txt"), "");
  await Bun.write(join(stage.outputDirectory, "未声明.txt"), "leave in the stage");

  const files = await stage.collect([{ staged: "报告.md", path: "构建/报告.md" }, { staged: "空.txt" }]);
  const digest = digestBytes(bytes);
  expect(files[0]).toEqual({ ref: "cas://" + digest, digest, size: bytes.length, path: "构建/报告.md", mediaType: "text/markdown" });
  expect(files[1]).toMatchObject({ size: 0, path: "空.txt", mediaType: "text/plain" });
  expect(existsSync(warehouse.blobPath(digestBytes(new TextEncoder().encode("leave in the stage"))))).toBe(false);

  const blob = warehouse.blobPath(digest);
  chmodSync(blob, 0o600);
  const { mounts, files: inputs } = await stage.prepareInputs([files[0], files[1], { ...files[0], path: "第二处报告.md" }]);
  expect(mounts.map(({ index }) => index)).toEqual([1, 2]);
  expect(inputs.map(({ path, containerPath }) => [path, containerPath])).toEqual([
    ["构建/报告.md", "/mao/inputs/1"], ["空.txt", "/mao/inputs/2"], ["第二处报告.md", "/mao/inputs/1"],
  ]);
  expect(new Uint8Array(await Bun.file(mounts[0].source).arrayBuffer())).toEqual(bytes);
  if (process.platform === "linux") {
    expect(mounts[0].source).not.toBe(blob);
    expect(statSync(mounts[0].source).mode & 0o777).toBe(0o644);
    expect(statSync(blob).mode & 0o777).toBe(0o600);
  }
  stage.close();
  expect(existsSync(dirname(stage.outputDirectory))).toBe(false);
  expect(new Uint8Array(await Bun.file(blob).arrayBuffer())).toEqual(bytes);
  await expect(stage.collect([])).rejects.toMatchObject({ code: "MATERIAL_STAGE_CLOSED" });
}));

test("rejects staged links, directories and paths outside the declared flat file set", async () => fixture(async (context, stage) => {
  const privatePath = join(context.root, "unrelated.txt");
  await Bun.write(privatePath, "unrelated bytes");
  const output = stage.outputDirectory;
  mkdirSync(join(output, "dir"));
  await Bun.write(join(output, "file.txt"), "declared file");
  const linked = join(output, "hardlink.txt");
  const original = join(output, "original.txt");
  await Bun.write(original, "hard-linked bytes");
  linkSync(original, linked);
  const cases = [
    [{ staged: "../unrelated.txt" }], [{ staged: resolve(privatePath) }], [{ staged: "dir/file.txt" }],
    [{ staged: "dir" }], [{ staged: "hardlink.txt" }], [{ staged: "file.txt", path: "../unrelated.txt" }],
    [{ staged: "file.txt" }, { staged: "file.txt", path: "duplicate.txt" }],
  ];
  if (process.platform === "linux") {
    symlinkSync(privatePath, join(output, "symlink.txt"));
    cases.push([{ staged: "symlink.txt" }]);
  }
  for (const files of cases) await expect(stage.collect(files)).rejects.toMatchObject({ code: "MATERIAL_FILE_INVALID" });
  expect(existsSync(context.warehouse.blobPath(digestBytes(new TextEncoder().encode("unrelated bytes"))))).toBe(false);
  const missing = { digest: "sha256:" + "f".repeat(64), size: 10 };
  await expect(stage.prepareInputs([missing])).rejects.toMatchObject({ code: "MATERIAL_FILE_UNAVAILABLE" });
  const [stored] = await stage.collect([{ staged: "file.txt" }]);
  await expect(stage.prepareInputs([{ ...stored, size: stored.size + 1 }])).rejects.toMatchObject({ code: "MATERIAL_FILE_UNAVAILABLE" });
}));

test("stores the opened file identity and discards copies whose size changed", async () => fixture(async ({ warehouse }, stage) => {
  const source = join(stage.outputDirectory, "identity.txt");
  await Bun.write(source, "old bytes");
  const handle = await open(source, "r");
  try {
    renameSync(source, join(stage.outputDirectory, "original.txt"));
    await Bun.write(source, "different bytes");
    const wanted = digestBytes(new TextEncoder().encode("old bytes"));
    const stored = await warehouse.putOpenedFile(handle, 9);
    expect(stored).toEqual({ digest: wanted, size: 9 });
    expect(await Bun.file(warehouse.blobPath(wanted)).text()).toBe("old bytes");
    await expect(warehouse.putOpenedFile(handle, 8)).rejects.toMatchObject({ code: "CAS_SIZE_CHANGED" });
    await expect(warehouse.putOpenedFile(handle, 10)).rejects.toMatchObject({ code: "CAS_SIZE_CHANGED" });
  } finally { await handle.close(); }
}));

test("rejects credential bytes split between CAS reads in binary material", async () => fixture(async ({ root, warehouse }, stage) => {
  const credential = "fixture-私人🔑-value";
  const encoded = new TextEncoder().encode(credential);
  const boundary = 1024 * 1024;
  const bytes = new Uint8Array(boundary + encoded.length).fill(0xff);
  bytes[0] = 0;
  bytes.set(encoded, boundary - 10);
  await Bun.write(join(stage.outputDirectory, "output.bin"), bytes);
  const digest = digestBytes(bytes);
  try {
    await stage.collect([{ staged: "output.bin" }], ["", "different-token", credential]);
    throw new Error("Expected staged credential rejection");
  } catch (error) {
    expect(error).toMatchObject({ code: "MATERIAL_FILE_SECRET", detail: {} });
    expect(String(error)).not.toContain(credential);
  }
  expect(existsSync(warehouse.blobPath(digest))).toBe(false);
  expect(readdirSync(join(root, "tmp"))).toEqual([]);

  bytes[boundary] = 0;
  await Bun.write(join(stage.outputDirectory, "output.bin"), bytes);
  const [collected] = await stage.collect([{ staged: "output.bin" }], ["", credential]);
  expect(collected.digest).toBe(digestBytes(bytes));
}));

test("rejects staged filenames and display paths containing a provided credential", async () => fixture(async (_context, stage) => {
  const credential = "filename-credential";
  const files = [
    { staged: credential + ".txt" },
    { staged: "output.txt", path: "reports/" + credential + ".txt" },
  ];
  for (const file of files) {
    try {
      await stage.collect([file], [credential]);
      throw new Error("Expected staged credential rejection");
    } catch (error) {
      expect(error).toMatchObject({ code: "MATERIAL_FILE_SECRET", detail: {} });
      expect(String(error)).not.toContain(credential);
    }
  }
}));
