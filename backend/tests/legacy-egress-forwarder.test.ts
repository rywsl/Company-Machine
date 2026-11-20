import { expect, test } from "bun:test";
import { mkdtempSync, rmSync, statSync } from "node:fs";
import { createServer, request, type IncomingMessage } from "node:http";
import { createConnection } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import type { EgressPlan } from "../src/legacy/egress";
import { startEgressForwarder, type EgressForwarder } from "../src/legacy/egress-forwarder";

const allowedHost = "allowed.example";
const pinnedAddress = "1.1.1.1";

function plan(methods: readonly string[], overrides: Partial<EgressPlan> = {}): EgressPlan {
  return {
    station: "Station:proxy-fixture@1", policy: "Policy:proxy-fixture@1", network: "egress",
    allow: [{ tier: "providers", host: allowedHost, methods }], onUnlisted: "deny", monitored: false, ...overrides,
  };
}

async function body(message: IncomingMessage): Promise<string> {
  const chunks: Uint8Array[] = [];
  for await (const chunk of message) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

function throughForwarder(
  proxy: EgressForwarder, target: string,
  options: { headers?: Record<string, string>; method?: string; body?: string } = {},
): Promise<{ status: number | undefined; headers: IncomingMessage["headers"]; body: string }> {
  return new Promise((resolve, reject) => {
    const endpoint = "socketPath" in proxy.endpoint ? proxy.endpoint : { hostname: proxy.endpoint.host, port: proxy.endpoint.port };
    const req = request({
      ...endpoint, path: target, method: options.method ?? "GET", agent: false, headers: options.headers ?? {},
    }, (res) => { void body(res).then((text) => resolve({ status: res.statusCode, headers: res.headers, body: text }), reject); });
    req.once("error", reject);
    req.end(options.body);
  });
}

async function fixtureServer(listener: NonNullable<Parameters<typeof createServer>[1]>) {
  const server = createServer({}, listener);
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen({ host: "127.0.0.1", port: 0 }, resolve);
  });
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Missing test listener address");
  return { server, port: address.port };
}

function tunnel(
  proxy: EgressForwarder, authority: string, authorization?: string, payload = "", waitFor = "\r\n\r\n",
): Promise<string> {
  return new Promise((resolve, reject) => {
    const endpoint = proxy.endpoint;
    const socket = createConnection("socketPath" in endpoint ? { path: endpoint.socketPath } : endpoint);
    let text = "";
    socket.once("error", reject);
    socket.once("connect", () => {
      const credentials = authorization === undefined ? "" : "Proxy-Authorization: " + authorization + "\r\n";
      socket.write("CONNECT " + authority + " HTTP/1.1\r\nHost: " + authority + "\r\n" + credentials + "\r\n" + payload);
    });
    socket.on("data", (data) => {
      text += Buffer.from(data).toString();
      if (!text.includes(waitFor)) return;
      socket.destroy();
      resolve(text);
    });
    socket.once("close", () => reject(new Error("Tunnel closed before its expected response")));
  });
}

test("authenticates HTTP, pins the resolved address and forwards only the allowed method and end-to-end headers", async () => {
  let routed = 0;
  const upstream = await fixtureServer((req, res) => {
    void body(req).then((data) => {
      res.writeHead(201, { connection: "close, x-upstream-internal", "x-upstream-internal": "remove" });
      res.end(JSON.stringify({ method: req.method, url: req.url, headers: req.headers, data }));
    });
  });
  const proxy = await startEgressForwarder({
    plan: plan(["POST"]), resolve: async () => [pinnedAddress],
    route: ({ address, port }) => {
      expect({ address, port }).toEqual({ address: pinnedAddress, port: 80 });
      routed++;
      return { address: "127.0.0.1", port: upstream.port };
    },
  });
  try {
    const target = "http://" + allowedHost + "/business?a=1";
    const authorization = { "proxy-authorization": proxy.authorization };
    const denied = await throughForwarder(proxy, target);
    expect(denied.status).toBe(407);
    expect(denied.headers["proxy-authenticate"]).toContain("Basic");
    expect((await throughForwarder(proxy, target, { headers: authorization })).status).toBe(403);
    expect(routed).toBe(0);
    const forwarded = await throughForwarder(proxy, target, {
      method: "POST", body: "material", headers: {
        ...authorization, host: "wrong.example", connection: "x-proxy-hop", "x-proxy-hop": "remove", "x-project": "keep",
      },
    });
    expect(forwarded.status).toBe(201);
    expect(forwarded.headers["x-upstream-internal"]).toBeUndefined();
    const received = JSON.parse(forwarded.body);
    expect(received).toMatchObject({ method: "POST", url: "/business?a=1", data: "material", headers: { host: allowedHost, "x-project": "keep" } });
    expect(received.headers["proxy-authorization"]).toBeUndefined();
    expect(received.headers["x-proxy-hop"]).toBeUndefined();
    expect(routed).toBe(1);
  } finally {
    await proxy.close();
    upstream.server.close();
  }
});

test("rejects unauthenticated, unauthorized and unsafe CONNECT before resolution or dial", async () => {
  let resolved = 0;
  let routed = 0;
  const proxy = await startEgressForwarder({
    plan: plan(["*"], { onUnlisted: "ask" }),
    resolve: async () => { resolved++; return ["::ffff:127.0.0.1", pinnedAddress]; },
    route: () => { routed++; throw new Error("unexpected dial"); },
  });
  try {
    expect(await tunnel(proxy, allowedHost + ":443")).toContain("407 Proxy Authentication Required");
    expect(await tunnel(proxy, allowedHost + ":443", "Basic invalid")).toContain("407 Proxy Authentication Required");
    expect(await tunnel(proxy, "denied.example:443", proxy.authorization)).toContain("403 Forbidden");
    expect(await tunnel(proxy, allowedHost + ":22", proxy.authorization)).toContain("400 Bad Request");
    expect(resolved).toBe(0);
    expect(await tunnel(proxy, allowedHost + ":443", proxy.authorization)).toContain("403 Forbidden");
    expect(resolved).toBe(1);
    expect(routed).toBe(0);
  } finally { await proxy.close(); }
});

test("rejects a CONNECT if the HTTPS method is opaque or a monitor must authorize it", async () => {
  for (const policy of [plan(["GET"]), plan(["*"], { monitored: true })]) {
    let resolved = false;
    const proxy = await startEgressForwarder({ plan: policy, resolve: async () => { resolved = true; return []; } });
    try {
      expect(await tunnel(proxy, allowedHost + ":443", proxy.authorization)).toContain("403 Forbidden");
      expect(resolved).toBe(false);
    } finally { await proxy.close(); }
  }
});

test("relays a permitted CONNECT with early bytes and closes active tunnels", async () => {
  const { createServer: createTcpServer } = await import("node:net");
  const upstream = createTcpServer((socket) => socket.on("data", (data) => socket.write(data)));
  await new Promise<void>((resolve) => upstream.listen({ port: 0, host: "127.0.0.1" }, resolve));
  const address = upstream.address();
  if (!address || typeof address === "string") throw new Error("Missing test listener address");
  let routed = 0;
  const proxy = await startEgressForwarder({
    plan: plan(["*"]), resolve: async () => [pinnedAddress],
    route: (destination) => {
      expect(destination).toEqual({ address: pinnedAddress, port: 443 });
      routed++;
      return { address: "127.0.0.1", port: address.port };
    },
  });
  try {
    expect(await tunnel(proxy, allowedHost + ":443", proxy.authorization, "early-data", "early-data"))
      .toContain("200 Connection Established\r\n\r\nearly-data");
    expect(routed).toBe(1);
  } finally {
    await proxy.close();
    upstream.close();
  }
});

test("creates and removes a private Unix listener on Unix hosts", async () => {
  if (process.platform === "win32") return;
  const directory = mkdtempSync(join(tmpdir(), "mao-fwd-"));
  try {
    const proxy = await startEgressForwarder({ plan: plan(["*"]), socketDirectory: directory });
    try {
      if (!("socketPath" in proxy.endpoint)) throw new Error("Expected a Unix socket");
      const path = proxy.endpoint.socketPath;
      expect(statSync(dirname(path)).mode & 0o777).toBe(0o700);
      expect(statSync(path).mode & 0o777).toBe(0o600);
      expect(await tunnel(proxy, allowedHost + ":443")).toContain("407 Proxy Authentication Required");
      await proxy.close();
      expect(() => statSync(path)).toThrow();
    } finally { await proxy.close(); }
  } finally { rmSync(directory, { recursive: true, force: true }); }
});
