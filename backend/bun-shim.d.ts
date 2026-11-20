declare module "bun:sqlite" {
  export class Database {
    constructor(path: string, options?: { create?: boolean; readonly?: boolean });
    exec(sql: string): void;
    query<T = any>(sql: string): {
      all(...args: any[]): T[];
      get(...args: any[]): T | null;
      run(...args: any[]): { lastInsertRowid?: number | bigint };
      finalize?(): void;
    };
    close(force?: boolean): void;
  }
}

declare module "node:crypto" {
  export const randomUUID: () => string;
  export const randomBytes: (size: number) => any;
  export const createHash: (algorithm: string) => { update(value: unknown): any; digest(format: "hex"): string };
  export const createCipheriv: (...args: any[]) => any;
}

declare module "node:fs" {
  export const mkdirSync: (...args: any[]) => void;
  export const readdirSync: (...args: any[]) => any[];
  export const statSync: (...args: any[]) => any;
  export const renameSync: (...args: any[]) => void;
  export const unlinkSync: (...args: any[]) => void;
  export const rmSync: (...args: any[]) => void;
  export const readFileSync: (...args: any[]) => any;
  export const writeFileSync: (...args: any[]) => void;
  export const chmodSync: (...args: any[]) => void;
  export const existsSync: (...args: any[]) => boolean;
}

declare module "node:path" {
  export const dirname: (path: string) => string;
  export const extname: (path: string) => string;
  export const join: (...parts: string[]) => string;
  export const resolve: (...parts: string[]) => string;
  export const normalize: (path: string) => string;
}

declare module "node:fs/promises" {
  export const mkdir: (...args: any[]) => Promise<void>;
  export const rm: (...args: any[]) => Promise<void>;
}

declare module "bun:test" {
  export const describe: (name: string, body: () => void) => void;
  export const test: (name: string, body: () => void | Promise<void>) => void;
  export const expect: (value: any) => any;
}

declare const Bun: {
  argv: string[];
  YAML: { parse(source: string): unknown };
  serve(options: { port: number; hostname?: string; fetch: (request: Request) => Response | Promise<Response> }): { url: URL; stop(): void };
  file(path: string): Blob;
  write(path: string, source: unknown): Promise<number>;
};
declare const process: { env: Record<string, string | undefined>; cwd(): string; exit(code?: number): never; exitCode?: number };
declare const Buffer: {
  from(value: any, encoding?: string): any;
  concat(values: any[]): any;
};
interface ImportMeta { main?: boolean; dir: string }
