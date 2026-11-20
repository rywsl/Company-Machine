import { createHash } from "node:crypto";
import { lstatSync, mkdirSync, mkdtempSync, readdirSync, renameSync, rmSync } from "node:fs";
import { copyFile } from "node:fs/promises";
import { basename, dirname, join, resolve, sep } from "node:path";
import { FactoryError } from "./legacy/errors";
import { canonicalJson } from "./legacy/foundation";
import type { SqliteStore } from "./db";

const format = "mao.business-package/1";
const digestPattern = /^sha256:[a-f0-9]{64}$/;
const documentName = "business.json";
const manifestName = "manifest.json";
type FileDescription = { digest: string; size: number };
type BusinessPackage = { format: typeof format; document: FileDescription; blobs: FileDescription[] };

function invalid(reason: string): never {
  throw new FactoryError("PACKAGE_CORRUPT", "业务包校验失败", { reason });
}

function kind(path: string): "file" | "directory" | "other" | "missing" {
  try {
    const status = lstatSync(path);
    return status.isFile() ? "file" : status.isDirectory() ? "directory" : "other";
  } catch (error) {
    if ((error as { code?: string }).code === "ENOENT") return "missing";
    throw error;
  }
}

function requireFresh(target: string): void {
  if (kind(target) !== "missing") throw new FactoryError("PACKAGE_TARGET_NOT_EMPTY", "业务包目标必须是新目录", { path: target });
}

export async function publishBusinessDirectory<T>(destination: string, build: (stage: string) => Promise<T>): Promise<T> {
  const target = resolve(destination);
  requireFresh(target);
  const parent = dirname(target);
  mkdirSync(parent, { recursive: true });
  const prefix = resolve(parent, `.${basename(target)}-`);
  const stage = mkdtempSync(prefix);
  try {
    const result = await build(stage);
    requireFresh(target);
    renameSync(stage, target);
    return result;
  } finally {
    if (resolve(stage).startsWith(prefix) && dirname(resolve(stage)) + sep === parent + sep) {
      rmSync(stage, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
    }
  }
}

function referencedDigests(document: unknown): string[] {
  const found = new Set<string>();
  const pending = [document];
  while (pending.length) {
    const value = pending.pop();
    if (typeof value === "string" && value.startsWith("cas://")) {
      const digest = value.slice(6);
      if (!digestPattern.test(digest)) invalid("invalid CAS reference");
      found.add(digest);
    }
    if (value && typeof value === "object") {
      for (const child of Object.values(value)) pending.push(child);
    }
  }
  return [...found].sort();
}

export function businessPackageBlob(root: string, digest: string): string {
  if (!digestPattern.test(digest)) invalid("digest");
  return join(root, "blobs", digest.slice(7));
}

async function inspect(path: string, secrets: readonly string[] = []): Promise<FileDescription> {
  if (kind(path) !== "file") invalid("expected regular file");
  const hash = createHash("sha256");
  const tokens = secrets.filter(Boolean).map((secret) => Buffer.from(secret, "utf8"));
  const overlap = Math.max(0, ...tokens.map(({ length }) => length - 1));
  let tail = Buffer.from([]);
  let size = 0;
  for await (const chunk of Bun.file(path).stream() as AsyncIterable<Uint8Array>) {
    hash.update(chunk);
    size += chunk.byteLength;
    if (tokens.length) {
      const current = Buffer.concat([tail, chunk]);
      if (tokens.some((token) => current.includes(token))) throw new FactoryError("ASSET_SECRET", "业务包引用的文件包含已配置的密钥");
      tail = current.subarray(Math.max(0, current.length - overlap));
    }
  }
  return { digest: `sha256:${hash.digest("hex")}`, size };
}

export async function createBusinessPackage(destination: string, document: unknown, warehouseRoot: string, secrets: readonly string[]) {
  return publishBusinessDirectory(destination, async (stage) => {
    const doc = join(stage, documentName);
    await Bun.write(doc, `${canonicalJson(document)}\n`);
    const description = await inspect(doc, secrets);
    const digests = referencedDigests(document);
    const blobs: FileDescription[] = [];
    const cas = join(warehouseRoot, "cas");
    const sha256 = join(cas, "sha256");
    if (digests.length) {
      if (kind(cas) !== "directory" || kind(sha256) !== "directory") invalid("invalid CAS directory");
      mkdirSync(join(stage, "blobs"));
    }
    for (const digest of digests) {
      const hex = digest.slice(7);
      const bucket = join(sha256, hex.slice(0, 2));
      if (kind(bucket) !== "directory") invalid("invalid CAS bucket");
      const source = join(bucket, hex.slice(2));
      if (kind(source) !== "file") invalid("missing CAS payload");
      const destination = businessPackageBlob(stage, digest);
      await copyFile(source, destination);
      const blob = await inspect(destination, secrets);
      if (blob.digest !== digest) invalid("CAS digest mismatch");
      blobs.push(blob);
    }
    const manifest: BusinessPackage = { format, document: description, blobs };
    await Bun.write(join(stage, manifestName), `${canonicalJson(manifest)}\n`);
    return manifest;
  });
}

function fileDescription(value: unknown): value is FileDescription {
  if (!value || typeof value !== "object" || !("size" in value) || !("digest" in value)) return false;
  return Object.keys(value).length === 2 && typeof value.digest === "string" && digestPattern.test(value.digest)
    && typeof value.size === "number" && Number.isSafeInteger(value.size) && value.size >= 0;
}

async function readJson(path: string): Promise<unknown> {
  if (kind(path) !== "file") invalid("expected regular JSON file");
  try { return JSON.parse(await Bun.file(path).text()); }
  catch (error) { if (error instanceof SyntaxError) invalid("invalid JSON"); throw error; }
}

export async function readBusinessPackage(directory: string) {
  const root = resolve(directory);
  if (kind(root) !== "directory") invalid("package directory is missing");
  const data = await readJson(join(root, manifestName));
  if (!data || typeof data !== "object" || !("format" in data) || data.format !== format
    || !("document" in data) || !fileDescription(data.document) || !("blobs" in data) || !Array.isArray(data.blobs)
    || !data.blobs.every(fileDescription) || Object.keys(data).some((key) => !["format", "document", "blobs"].includes(key))) invalid("manifest");
  const expected = [documentName, manifestName, ...(data.blobs.length ? ["blobs"] : [])].sort();
  if (canonicalJson(readdirSync(root).sort()) !== canonicalJson(expected)) invalid("unexpected package files");
  const doc = join(root, documentName);
  if (kind(doc) !== "file") invalid("expected regular JSON file");
  const bytes = new Uint8Array(await Bun.file(doc).arrayBuffer());
  const description = { digest: `sha256:${createHash("sha256").update(bytes).digest("hex")}`, size: bytes.byteLength };
  if (description.digest !== data.document.digest || description.size !== data.document.size) invalid("document checksum");
  let document: unknown;
  try { document = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)); }
  catch { invalid("invalid business JSON"); }
  if (!document || typeof document !== "object" || !("schema" in document) || document.schema !== "mao.business-system/1") invalid("business document schema");
  const closure = referencedDigests(document);
  if (canonicalJson(data.blobs.map(({ digest }) => digest)) !== canonicalJson(closure)) invalid("referenced CAS closure");
  if (closure.length) {
    if (kind(join(root, "blobs")) !== "directory") invalid("blob directory");
    const files = readdirSync(join(root, "blobs")).sort();
    if (canonicalJson(files) !== canonicalJson(closure.map((digest) => digest.slice(7)))) invalid("unexpected blob files");
    for (const blob of data.blobs) {
      const actual = await inspect(businessPackageBlob(root, blob.digest));
      if (actual.digest !== blob.digest || actual.size !== blob.size) invalid("blob checksum");
    }
  }
  return { root, document, blobs: data.blobs };
}

export function registerVerifiedArtifactFiles(store: SqliteStore, systemId: string, blobs: readonly FileDescription[]): void {
  const sizes = new Map(blobs.map(({ digest, size }) => [digest, size]));
  store.db.transaction(() => {
    for (const artifact of store.listBusinessSystemArtifacts(systemId)) {
      const verified = (artifact.files ?? []).flatMap(({ ref }, position) => {
        if (!ref.startsWith("cas://")) return [];
        const digest = ref.slice(6);
        const size = sizes.get(digest);
        return size === undefined ? [] : [{ digest, size, position }];
      });
      store.registerArtifactFiles(artifact.id, verified);
    }
  }).immediate();
}
