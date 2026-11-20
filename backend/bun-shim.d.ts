declare module "*.mrl" {
  const source: string;
  export default source;
}

declare module "bun:sqlite" {
  export interface Statement<T = any> {
    readonly isFinalized: boolean;
    all(...args: any[]): T[];
    get(...args: any[]): T | null;
    run(...args: any[]): { lastInsertRowid?: number | bigint };
    finalize(): void;
  }

  export class Database {
    constructor(path: string, options?: { create?: boolean; readonly?: boolean });
    exec(sql: string): void;
    transaction<T>(callback: () => T): { (): T; immediate(): T };
    prepare<T = any>(sql: string): Statement<T>;
    query<T = any>(sql: string): Statement<T>;
    close(force?: boolean): void;
  }
}

declare module "node:crypto" {
  export const randomUUID: () => string;
  export const randomBytes: (size: number) => any;
  export const timingSafeEqual: (left: Uint8Array, right: Uint8Array) => boolean;
  export const createHash: (algorithm: string) => { update(value: unknown): any; digest(format: "hex"): string };
  export const createCipheriv: (...args: any[]) => any;
  export const createDecipheriv: (...args: any[]) => any;
}

declare module "node:fs" {
  export const openSync: (path: string, flags: string) => number;
  export const closeSync: (fd: number) => void;
  export const rmdirSync: (path: string) => void;
  export const appendFileSync: (path: string, data: string) => void;
  export const constants: { readonly COPYFILE_EXCL: number };
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
  export const open: (path: string, flags: string) => Promise<{
    read(buffer: Uint8Array, offset: number, length: number, position: number | null): Promise<{ bytesRead: number }>;
    write(buffer: Uint8Array, offset: number, length: number, position: number | null): Promise<{ bytesWritten: number }>;
    close(): Promise<void>;
  }>;
  export const copyFile: (source: string, target: string, mode?: number) => Promise<void>;
  export const mkdir: (...args: any[]) => Promise<void>;
  export const rm: (...args: any[]) => Promise<void>;
  export const readdir: (...args: any[]) => Promise<string[]>;
}

declare module "bun:test" {
  export const describe: (name: string, body: () => void) => void;
  export const test: (name: string, bodyOrOptions: (() => void | Promise<void>) | { timeout?: number }, body?: () => void | Promise<void>) => void;
  export const expect: (value: any) => any;
}

declare const Bun: {
  argv: string[];
  which(executable: string): string | null;
  build(options: { entrypoints: string[]; target: "bun" }): Promise<{ outputs: Blob[] }>;
  Glob: new (pattern: string) => { match(path: string): boolean };
  YAML: { parse(source: string): unknown; stringify(value: unknown): string };
  serve(options: { port: number; hostname?: string; fetch: (request: Request, server: { timeout(request: Request, seconds: number): void }) => Response | Promise<Response> }): { url: URL; stop(): void };
  file(path: string): Blob;
  write(path: string, source: unknown): Promise<number>;
};
declare const process: { env: Record<string, string | undefined>; platform: string; pid: number; getuid?(): number; kill(pid: number, signal: 0): boolean; cwd(): string; exit(code?: number): never; exitCode?: number; stdout: { write(value: string): boolean }; stderr: { write(value: string): boolean } };
declare const Buffer: {
  from(value: any, encoding?: string): any;
  concat(values: any[]): any;
};
interface ImportMeta { main?: boolean; dir: string }
