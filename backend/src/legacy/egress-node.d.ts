declare module "node:dns/promises" {
  export function lookup(host: string, options: { all: true }): Promise<Array<{ address: string; family: number }>>;
}

declare module "node:net" {
  export interface Socket {
    destroyed: boolean;
    destroy(): this;
    end(data?: string | Uint8Array): this;
    write(data: string | Uint8Array): boolean;
    pipe<T extends Socket>(destination: T): T;
    once(event: "connect" | "close", listener: () => void): this;
    once(event: "error", listener: (error: Error) => void): this;
    on(event: "data", listener: (data: Uint8Array) => void): this;
  }
  export function createConnection(options: { host: string; port: number } | { path: string }): Socket;
  export function createServer(listener: (client: Socket) => void): {
    listen(options: { host: string; port: number }, callback: () => void): void;
    address(): { port: number } | string | null;
    close(): void;
  };
  export function isIP(value: string): 0 | 4 | 6;
  export class BlockList {
    addSubnet(address: string, prefix: number, type: "ipv4" | "ipv6"): void;
    check(address: string, type: "ipv4" | "ipv6"): boolean;
  }
}

declare module "node:http" {
  import type { Socket } from "node:net";
  type Headers = Record<string, string | string[] | undefined>;
  type Address = { address: string; port: number; family: string };
  interface ClientRequest {
    destroy(): this;
    end(data?: string): this;
    once(event: "error", listener: (error: Error) => void): this;
  }
  export interface IncomingMessage extends AsyncIterable<Uint8Array> {
    method?: string;
    url?: string;
    statusCode?: number;
    headers: Headers;
    socket: Socket;
    once(event: "error", listener: (error: Error) => void): this;
    pipe<T extends ClientRequest | ServerResponse>(destination: T): T;
  }
  export interface ServerResponse {
    destroyed: boolean;
    headersSent: boolean;
    destroy(): this;
    writeHead(status: number, headers?: Headers): this;
    end(data?: string): this;
    once(event: "close", listener: () => void): this;
  }
  type Listener = (req: IncomingMessage, res: ServerResponse) => void;
  interface Server {
    maxConnections: number;
    maxRequestsPerSocket: number;
    address(): Address | string | null;
    listen(options: { host: string; port: number } | { path: string; readableAll?: boolean; writableAll?: boolean }, callback: () => void): this;
    close(callback?: () => void): this;
    once(event: "error", listener: (error: Error) => void): this;
    removeListener(event: "error", listener: (error: Error) => void): this;
    on(event: "connection", listener: (socket: Socket) => void): this;
    on(event: "connect" | "upgrade", listener: (req: IncomingMessage, socket: Socket, head: Uint8Array) => void): this;
    on(event: "checkContinue" | "checkExpectation", listener: Listener): this;
  }
  export function createServer(options: { maxHeaderSize?: number; requestTimeout?: number; headersTimeout?: number }, listener?: Listener): Server;
  export function request(
    options: { hostname?: string; port?: number; socketPath?: string; path: string; method: string; headers: Headers; agent: false },
    callback: (response: IncomingMessage) => void,
  ): ClientRequest;
}
