import { chmodSync, constants, lstatSync, mkdirSync, mkdtempSync, realpathSync, rmSync } from "node:fs";
import { copyFile, open } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { FactoryError } from "./legacy/errors";
import { mediaTypeOf, type Warehouse } from "./legacy/warehouse";

export const materialInputRoot = "/mao/inputs";
export const materialOutputRoot = "/mao/outputs";

export type MaterialInputMount = { source: string; index: number };
export type MaterialBlobGrant = { digest: string; size: number };
export type StagedMaterialFile = { staged: string; path?: string };
export type CollectedMaterialFile = MaterialBlobGrant & { ref: string; path: string; mediaType: string };

type FileStat = ReturnType<typeof lstatSync>;

function safeName(name: unknown): name is string {
  return typeof name === "string" && name.length > 0 && !/[\\/:*?"<>|\u0000-\u001f\u007f]/.test(name)
    && !/[. ]$/.test(name) && !/^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(name);
}

function stagedFiles(entries: readonly StagedMaterialFile[]): Array<{ staged: string; path: string }> {
  const names = new Set<string>();
  const paths = new Set<string>();
  return entries.map((entry) => {
    const staged = entry?.staged;
    const path = entry?.path ?? staged;
    if (!safeName(staged) || typeof path !== "string" || !path.split("/").every(safeName) || names.has(staged) || paths.has(path)) {
      throw new FactoryError("MATERIAL_FILE_INVALID", "产物需要唯一的暂存文件名和相对路径");
    }
    names.add(staged);
    paths.add(path);
    return { staged, path };
  });
}

function sameFile(left: FileStat, right: FileStat): boolean {
  return right.isFile() && !right.isSymbolicLink() && right.nlink === 1
    && left.dev === right.dev && left.ino === right.ino && left.size === right.size
    && left.mtimeMs === right.mtimeMs && left.ctimeMs === right.ctimeMs;
}

function materialContainsCredential(): FactoryError {
  return new FactoryError("MATERIAL_FILE_SECRET", "产物文件含本轮提供给 Operator 的密钥");
}

function inspectMaterialChunks(credentials: readonly string[]): ((chunk: Uint8Array) => void) | undefined {
  if (!credentials.length) return undefined;
  const patterns = credentials.map((credential) => Buffer.from(credential, "utf8"));
  const overlap = patterns.reduce((length, pattern) => Math.max(length, pattern.length - 1), 0);
  let tail = Buffer.from([]);
  return (chunk) => {
    const bytes = Buffer.from(chunk.buffer, chunk.byteOffset, chunk.byteLength);
    const boundary = overlap ? Buffer.concat([tail, bytes.subarray(0, overlap)]) : tail;
    if (patterns.some((pattern) => bytes.includes(pattern) || boundary.includes(pattern))) throw materialContainsCredential();
    if (overlap) tail = bytes.length >= overlap ? Buffer.from(bytes.subarray(-overlap)) : boundary.subarray(-overlap);
  };
}

export function createMaterialFileStage(warehouse: Warehouse) {
  const root = mkdtempSync(join(realpathSync(tmpdir()), "mao-material-"));
  const outputDirectory = join(root, "outputs");
  let closed = false;
  const close = () => {
    if (closed) return;
    rmSync(root, { recursive: true, force: true, maxRetries: 3 });
    closed = true;
  };

  try {
    mkdirSync(outputDirectory);
    chmodSync(outputDirectory, 0o1777);
  } catch (error) {
    close();
    throw error;
  }

  /** Collect only after the successful official turn and sandbox shutdown. */
  async function collect(entries: readonly StagedMaterialFile[], secretValues: readonly string[] = []): Promise<CollectedMaterialFile[]> {
    if (closed) throw new FactoryError("MATERIAL_STAGE_CLOSED", "物料暂存区已关闭");
    const credentials = [...new Set(secretValues.filter(Boolean))];
    const result: CollectedMaterialFile[] = [];
    for (const { staged, path } of stagedFiles(entries)) {
      if (credentials.some((credential) => staged.includes(credential) || path.includes(credential))) throw materialContainsCredential();
      try {
        const source = join(outputDirectory, staged);
        const before = lstatSync(source);
        if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1) throw new Error("invalid file type");
        const flags = constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) | (constants.O_NONBLOCK ?? 0);
        const input = await open(source, flags);
        let stored: MaterialBlobGrant;
        try {
          if (!sameFile(before, await input.stat())) throw new Error("staged file changed");
          stored = await warehouse.putOpenedFile(input, before.size, inspectMaterialChunks(credentials));
          if (!sameFile(before, await input.stat())) throw new Error("staged file changed");
        } finally { await input.close(); }
        result.push({ ...stored, ref: "cas://" + stored.digest, path, mediaType: mediaTypeOf(path) });
      } catch (error) {
        if (error instanceof FactoryError && error.code === "MATERIAL_FILE_SECRET") throw error;
        throw new FactoryError("MATERIAL_FILE_INVALID", "无法收取已声明的产物文件", { path });
      }
    }
    return result;
  }

  /** The caller supplies only grants selected for the current Operator's authorized input records. */
  async function prepareInputs<T extends MaterialBlobGrant>(grants: readonly T[]) {
    if (closed) throw new FactoryError("MATERIAL_STAGE_CLOSED", "物料暂存区已关闭");
    const mounted = new Map<string, MaterialInputMount & { size: number }>();
    const files: Array<T & { containerPath: string }> = [];
    let privateCopies: string | undefined;
    for (const grant of grants) {
      let mount = mounted.get(grant.digest);
      if (!mount) {
        let source: string;
        let stat: FileStat;
        try {
          source = await warehouse.ensureLocal(grant.digest);
          stat = lstatSync(source);
          if (!stat.isFile() || stat.isSymbolicLink() || stat.size !== grant.size) throw new Error("invalid CAS file");
          source = realpathSync(source);
        } catch {
          throw new FactoryError("MATERIAL_FILE_UNAVAILABLE", "找不到与输入物料匹配的 CAS 文件", { digest: grant.digest });
        }
        const index = mounted.size + 1;
        if ((stat.mode & 0o004) === 0) {
          privateCopies ??= mkdtempSync(join(root, "input-"));
          const staged = join(privateCopies, String(index));
          await copyFile(source, staged);
          chmodSync(staged, 0o644);
          source = staged;
        }
        mount = { source, index, size: stat.size };
        mounted.set(grant.digest, mount);
      }
      if (mount.size !== grant.size) {
        throw new FactoryError("MATERIAL_FILE_UNAVAILABLE", "输入物料摘要的大小记录不一致", { digest: grant.digest });
      }
      files.push({ ...grant, containerPath: materialInputRoot + "/" + mount.index });
    }
    return { mounts: [...mounted.values()].map(({ source, index }) => ({ source, index })), files };
  }

  return { outputDirectory, collect, prepareInputs, close };
}
