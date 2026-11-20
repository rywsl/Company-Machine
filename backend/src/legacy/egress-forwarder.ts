import { randomBytes, timingSafeEqual } from "node:crypto";
import { lookup } from "node:dns/promises";
import { chmodSync, mkdtempSync, rmdirSync, unlinkSync } from "node:fs";
import { createServer, request, type IncomingMessage, type ServerResponse } from "node:http";
import { BlockList, createConnection, isIP, type Socket } from "node:net";
import { isAbsolute, join } from "node:path";
import { matchesEgressAllowlist, type EgressPlan } from "./egress";

type Destination = { address: string; port: number };
type Endpoint = { host: "127.0.0.1"; port: number } | { socketPath: string };

export interface EgressForwarderOptions {
  plan: EgressPlan;
  /** Host-owned parent directory for a private Unix socket. The default is loopback TCP. */
  socketDirectory?: string;
  resolve?: (host: string) => Promise<readonly string[]>;
  /** Trusted offline transport override. Receives the validated, pinned address. */
  route?: (destination: Readonly<Destination>) => Destination;
  maxConnections?: number;
  deadlineMs?: number;
}

export interface EgressForwarder {
  endpoint: Endpoint;
  authorization: string;
  close(): Promise<void>;
}

const blockedV4 = new BlockList();
for (const [address, prefix] of [
  ["0.0.0.0", 8], ["10.0.0.0", 8], ["100.64.0.0", 10], ["127.0.0.0", 8],
  ["169.254.0.0", 16], ["172.16.0.0", 12], ["192.0.0.0", 24], ["192.0.2.0", 24],
  ["192.88.99.0", 24], ["192.168.0.0", 16], ["198.18.0.0", 15], ["198.51.100.0", 24],
  ["203.0.113.0", 24], ["224.0.0.0", 3],
] as const) blockedV4.addSubnet(address, prefix, "ipv4");

const globalV6 = new BlockList();
globalV6.addSubnet("2000::", 3, "ipv6");
const blockedV6 = new BlockList();
for (const [address, prefix] of [
  ["2001::", 23], ["2001:db8::", 32], ["2002::", 16], ["3fff::", 20],
] as const) blockedV6.addSubnet(address, prefix, "ipv6");

function publicAddress(address: string): boolean {
  if (isIP(address) === 4) return !blockedV4.check(address, "ipv4");
  return isIP(address) === 6 && !address.includes("%") && globalV6.check(address, "ipv6") && !blockedV6.check(address, "ipv6");
}

function parseTarget(req: IncomingMessage, tunnel: boolean): URL | undefined {
  const raw = req.url ?? "";
  if (/[^\x21-\x7e]|[\\#]/.test(raw)) return undefined;
  if (tunnel && !/^(?:[A-Za-z0-9.-]+|\[[A-Fa-f0-9:]+\]):443$/.test(raw)) return undefined;
  if (!tunnel && !/^http:\/\//i.test(raw)) return undefined;
  try {
    const target = new URL(tunnel ? "https://" + raw : raw);
    if (target.username || target.password || target.port || !target.hostname) return undefined;
    return target;
  } catch { return undefined; }
}

const hopHeaders = [
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
  "te", "trailer", "transfer-encoding", "upgrade",
];
const ambientProxies = ["http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"];

function forwardedHeaders(headers: IncomingMessage["headers"]): IncomingMessage["headers"] {
  const fields = { ...headers };
  const connection = typeof fields.connection === "string" ? fields.connection.split(",") : [];
  for (const name of [...hopHeaders, ...connection.map((field) => field.trim().toLowerCase())]) delete fields[name];
  return fields;
}

function validInt(value: number | undefined, fallback: number): number {
  if (value === undefined) return fallback;
  if (!Number.isSafeInteger(value) || value < 1 || value > 2_147_483_647) throw new Error("Invalid egress forwarder limit");
  return value;
}

function listen(server: ReturnType<typeof createServer>, socketPath?: string): Promise<Endpoint> {
  return new Promise((resolve, reject) => {
    const onReady = () => {
      server.removeListener("error", reject);
      const address = server.address();
      if (socketPath) resolve({ socketPath });
      else if (address && typeof address !== "string") resolve({ host: "127.0.0.1", port: address.port });
      else reject(new Error("Egress forwarder received no local address"));
    };
    server.once("error", reject);
    if (socketPath) server.listen({ path: socketPath, readableAll: false, writableAll: false }, onReady);
    else server.listen({ port: 0, host: "127.0.0.1" }, onReady);
  });
}

/** An authenticated forwarder for one job's immutable plan. */
export async function startEgressForwarder(options: EgressForwarderOptions): Promise<EgressForwarder> {
  const plan = structuredClone(options.plan);
  const maxConnections = validInt(options.maxConnections, 32);
  const deadlineMs = validInt(options.deadlineMs, 120_000);
  const authorization = "Basic " + Buffer.from("mao:" + randomBytes(32).toString("hex")).toString("base64");
  const expected = Buffer.from(authorization);
  const resolve = options.resolve ?? (async (host: string) => (await lookup(host, { all: true })).map(({ address }) => address));
  const clients = new Set<Socket>();
  const upstreams = new Set<Socket>();
  const proxyChallenge = 'Basic realm="MAO Egress"';

  function rejection(req: IncomingMessage, target: URL | undefined, method: string): number | undefined {
    const supplied = req.headers["proxy-authorization"];
    if (typeof supplied !== "string") return 407;
    const bytes = Buffer.from(supplied);
    if (bytes.length !== expected.length || !timingSafeEqual(bytes, expected)) return 407;
    if (!target) return 400;
    if (plan.monitored || !matchesEgressAllowlist(plan, target, method)) return 403;
    return undefined;
  }

  async function destination(target: URL, port: number): Promise<Destination | undefined> {
    const host = target.hostname.replace(/^\[|\]$/g, "").replace(/\.$/, "");
    const addresses = isIP(host) ? [host] : await resolve(host);
    if (!addresses.length || addresses.some((address) => !publicAddress(address))) return undefined;
    const pinned = { address: addresses[0], port };
    const routed = options.route?.(pinned) ?? pinned;
    if (!isIP(routed.address) || !Number.isInteger(routed.port) || routed.port < 1 || routed.port > 65535) {
      throw new Error("Invalid egress transport address");
    }
    return routed;
  }

  function errorResponse(res: ServerResponse, status: number): void {
    if (res.destroyed) return;
    res.writeHead(status, { connection: "close", ...(status === 407 ? { "proxy-authenticate": proxyChallenge } : {}) });
    res.end();
  }

  function errorTunnel(client: Socket, status: number): void {
    if (client.destroyed) return;
    const reason = { 400: "Bad Request", 403: "Forbidden", 407: "Proxy Authentication Required", 502: "Bad Gateway" }[status];
    const challenge = status === 407 ? "Proxy-Authenticate: " + proxyChallenge + "\r\n" : "";
    client.end("HTTP/1.1 " + status + " " + reason + "\r\nConnection: close\r\n" + challenge + "Content-Length: 0\r\n\r\n");
  }

  async function forwardHttp(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const target = parseTarget(req, false);
    const method = req.method ?? "";
    const rejected = rejection(req, target, method);
    if (rejected !== undefined || !target) return errorResponse(res, rejected ?? 400);
    // Bun's HTTP client honors ambient fetch proxy configuration, which would undo address pinning.
    if (ambientProxies.some((key) => Boolean(process.env[key]))) return errorResponse(res, 502);
    const upstream = await destination(target, 80);
    if (req.socket.destroyed || res.destroyed) return;
    if (!upstream) return errorResponse(res, 403);
    const headers = forwardedHeaders(req.headers);
    headers.host = target.host;
    headers.connection = "close";
    const outbound = request({
      hostname: upstream.address, port: upstream.port, path: target.pathname + target.search, method, headers, agent: false,
    }, (incoming) => {
      res.writeHead(incoming.statusCode ?? 502, { ...forwardedHeaders(incoming.headers), connection: "close" });
      incoming.once("error", () => res.destroy());
      incoming.pipe(res);
    });
    outbound.once("error", () => res.headersSent ? res.destroy() : errorResponse(res, 502));
    res.once("close", () => outbound.destroy());
    req.once("error", () => outbound.destroy());
    req.pipe(outbound);
  }

  async function forwardTunnel(req: IncomingMessage, client: Socket, head: Uint8Array): Promise<void> {
    const target = parseTarget(req, true);
    const rejected = rejection(req, target, "CONNECT");
    if (rejected !== undefined || !target) return errorTunnel(client, rejected ?? 400);
    const upstream = await destination(target, 443);
    if (client.destroyed) return;
    if (!upstream) return errorTunnel(client, 403);
    const socket = createConnection({ host: upstream.address, port: upstream.port });
    let connected = false;
    upstreams.add(socket);
    client.once("close", () => socket.destroy());
    socket.once("close", () => upstreams.delete(socket));
    socket.once("error", () => connected ? client.destroy() : errorTunnel(client, 502));
    socket.once("connect", () => {
      if (client.destroyed) return socket.destroy();
      connected = true;
      client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      if (head.length) socket.write(head);
      client.pipe(socket).pipe(client);
    });
  }

  const server = createServer({ maxHeaderSize: 16_384, requestTimeout: deadlineMs, headersTimeout: deadlineMs }, (req, res) => {
    void forwardHttp(req, res).catch(() => res.headersSent ? res.destroy() : errorResponse(res, 502));
  });
  server.on("checkContinue", (_req, res) => errorResponse(res, 417));
  server.on("checkExpectation", (_req, res) => errorResponse(res, 417));
  server.maxConnections = maxConnections;
  server.maxRequestsPerSocket = 1;
  server.on("connection", (client) => {
    clients.add(client);
    const timer = setTimeout(() => client.destroy(), deadlineMs);
    client.once("error", () => client.destroy());
    client.once("close", () => {
      clearTimeout(timer);
      clients.delete(client);
    });
  });
  server.on("connect", (req, client, head) => {
    void forwardTunnel(req, client, head).catch(() => errorTunnel(client, 502));
  });
  server.on("upgrade", (_req, client) => errorTunnel(client, 403));

  let directory: string | undefined;
  let socketPath: string | undefined;
  let closing: Promise<void> | undefined;
  function close(): Promise<void> {
    return closing ??= new Promise<void>((resolveClose) => {
      server.close(() => resolveClose());
      for (const socket of clients) socket.destroy();
      for (const socket of upstreams) socket.destroy();
    }).finally(() => {
      if (socketPath) {
        try { unlinkSync(socketPath); }
        catch (error) { if ((error as { code?: string }).code !== "ENOENT") throw error; }
      }
      if (directory) rmdirSync(directory);
    });
  }
  try {
    if (options.socketDirectory !== undefined) {
      if (process.platform === "win32" || !isAbsolute(options.socketDirectory)) throw new Error("Egress socket directory requires an absolute Unix path");
      directory = mkdtempSync(join(options.socketDirectory, "mao-egress-"));
      chmodSync(directory, 0o700);
      socketPath = join(directory, "proxy.sock");
      if (Buffer.from(socketPath).length > 103) throw new Error("Egress socket path is too long");
    }
    const endpoint = await listen(server, socketPath);
    if (socketPath) chmodSync(socketPath, 0o600);
    return { authorization, endpoint, close };
  } catch (error) {
    await close();
    throw error;
  }
}
