declare module "*.mrl" {
  const source: string;
  export default source;
}

declare module "bun:sqlite" {
  export const constants: { SQLITE_OPEN_READONLY: number; SQLITE_OPEN_URI: number };
  export interface Statement<T = any> {
    readonly isFinalized: boolean;
    all(...args: any[]): T[];
    get(...args: any[]): T | null;
    run(...args: any[]): { changes?: number; lastInsertRowid?: number | bigint };
    finalize(): void;
  }

  export class Database {
    constructor(path: string, options?: number | { create?: boolean; readonly?: boolean });
    exec(sql: string): void;
    transaction<T>(callback: () => T): { (): T; immediate(): T };
    prepare<T = any>(sql: string): Statement<T>;
    query<T = any>(sql: string): Statement<T>;
    close(force?: boolean): void;
  }
}

declare module "node:url" {
  export function pathToFileURL(path: string): URL;
}

declare module "node:util" {
  export function isDeepStrictEqual(a: unknown, b: unknown): boolean;
}

declare module "node:crypto" {
  export const randomUUID: () => string;
  export const randomBytes: (size: number) => any;
  export const createHmac: (algorithm: string, key: unknown) => { update(value: unknown): any; digest(format?: string): any };
  export const timingSafeEqual: (left: Uint8Array, right: Uint8Array) => boolean;
  export const createHash: (algorithm: string) => { update(value: unknown): any; digest(format: "hex"): string };
  export const createCipheriv: (...args: any[]) => any;
  export const createDecipheriv: (...args: any[]) => any;
}

declare module "node:fs" {
  export const openSync: (path: string, flags: string) => number;
  export const closeSync: (fd: number) => void;
  export const rmdirSync: (path: string) => void;
  export const appendFileSync: (path: string, data: string, options?: string) => void;
  export const constants: {
    readonly COPYFILE_EXCL: number;
    readonly O_RDONLY: number;
    readonly O_NOFOLLOW?: number;
    readonly O_NONBLOCK?: number;
  };
  export const linkSync: (source: string, target: string) => void;
  export const symlinkSync: (target: string, path: string, type?: "file" | "dir" | "junction") => void;
  export const mkdirSync: (...args: any[]) => void;
  export const mkdtempSync: (prefix: string) => string;
  export const readdirSync: (...args: any[]) => any[];
  export const statSync: (...args: any[]) => any;
  export const lstatSync: (...args: any[]) => any;
  export const realpathSync: (path: string) => string;
  export const cpSync: (...args: any[]) => void;
  export const copyFileSync: (source: string, target: string, mode?: number) => void;
  export const renameSync: (...args: any[]) => void;
  export const unlinkSync: (...args: any[]) => void;
  export const rmSync: (...args: any[]) => void;
  export const readFileSync: (...args: any[]) => any;
  export const writeFileSync: (...args: any[]) => void;
  export const chmodSync: (...args: any[]) => void;
  export const existsSync: (...args: any[]) => boolean;
}

declare module "node:os" {
  export const tmpdir: () => string;
}

declare module "node:path" {
  export const sep: string;
  export const isAbsolute: (path: string) => boolean;
  export const dirname: (path: string) => string;
  export const extname: (path: string) => string;
  export const join: (...parts: string[]) => string;
  export const basename: (path: string) => string;
  export const resolve: (...parts: string[]) => string;
  export const relative: (from: string, to: string) => string;
  export const normalize: (path: string) => string;
}

declare module "node:fs/promises" {
  export const open: (path: string, flags: string | number) => Promise<{
    read(buffer: Uint8Array, offset: number, length: number, position: number | null): Promise<{ bytesRead: number }>;
    write(buffer: Uint8Array, offset: number, length: number, position: number | null): Promise<{ bytesWritten: number }>;
    stat(): Promise<ReturnType<typeof import("node:fs").lstatSync>>;
    close(): Promise<void>;
  }>;
  export const copyFile: (source: string, target: string, mode?: number) => Promise<void>;
  export const mkdir: (...args: any[]) => Promise<void>;
  export const rm: (...args: any[]) => Promise<void>;
  export const readdir: (...args: any[]) => Promise<any[]>;
  export const readFile: (...args: any[]) => Promise<any>;
  export const rename: (...args: any[]) => Promise<void>;
  export const stat: (...args: any[]) => Promise<any>;
  export const writeFile: (...args: any[]) => Promise<void>;
}

declare module "bun:test" {
  export const describe: (name: string, body: () => void) => void;
  export const test: (name: string, bodyOrOptions: (() => void | Promise<void>) | { timeout?: number }, body?: () => void | Promise<void>) => void;
  export const expect: (value: any) => any;
}

declare const Bun: {
  SQL: new (url: string, options?: Record<string, unknown>) => {
    unsafe<T = Record<string, unknown>>(sql: string, params?: readonly unknown[]): Promise<T[]>;
    begin<T>(work: (transaction: {
      unsafe<R = Record<string, unknown>>(sql: string, params?: readonly unknown[]): Promise<R[]>;
    }) => Promise<T> | T): Promise<T>;
    close(): Promise<void>;
    readonly options?: Record<string, unknown>;
  };
  argv: string[];
  which(executable: string): string | null;
  build(options: { entrypoints: string[]; target: "bun" }): Promise<{ outputs: Blob[] }>;
  Glob: new (pattern: string) => { match(path: string): boolean };
  YAML: { parse(source: string): unknown; stringify(value: unknown): string };
  spawnSync(argv: string[], options?: { stdout?: "pipe" | "inherit"; stderr?: "pipe" | "inherit"; env?: Record<string, string> }): { exitCode: number; stdout: Uint8Array; stderr: Uint8Array };
  serve(options: { port: number; hostname?: string; fetch: (request: Request, server: { timeout(request: Request, seconds: number): void }) => Response | Promise<Response> }): { url: URL; port: number; stop(): void };
  file(path: string): Blob;
  write(path: string, source: unknown): Promise<number>;
};
declare const process: { env: Record<string, string | undefined>; platform: string; pid: number; getuid?(): number; kill(pid: number, signal: 0): boolean; cwd(): string; exit(code?: number): never; exitCode?: number; stdout: { write(value: string): boolean }; stderr: { write(value: string): boolean } };
declare const Buffer: {
  from(value: any, encoding?: string): any;
  from(value: ArrayBufferLike, offset: number, length: number): any;
  byteLength(value: string | ArrayBuffer | ArrayBufferView, encoding?: string): number;
  concat(values: any[]): any;
};
type Buffer = any;
declare namespace NodeJS { type ProcessEnv = Record<string, string | undefined>; }
interface ImportMeta { main?: boolean; dir: string }
