import { expect, test } from "bun:test";
import { chooseFastestMirror, mirrorImageReference, parseDockerHubImage, selectDockerHubMirror } from "./dockerhub-mirror";

test("normalizes Docker Hub image names without rewriting non-Docker Hub registries", () => {
  expect(parseDockerHubImage("ubuntu")).toEqual({ path: "library/ubuntu", reference: "latest" });
  expect(parseDockerHubImage("docker.io/oven/bun:1.3-alpine")).toEqual({ path: "oven/bun", reference: "1.3-alpine" });
  expect(mirrorImageReference("ubuntu:24.04", "docker.m.daocloud.io")).toBe("docker.m.daocloud.io/library/ubuntu:24.04");
  expect(mirrorImageReference("ghcr.io/example/app:latest", "docker.m.daocloud.io")).toBeUndefined();
});

test("chooses the highest measured manifest throughput with a stable tie break", () => {
  expect(chooseFastestMirror([
    { mirror: "slow.example", image: "slow", reachable: true, bytes: 100, durationMs: 100 },
    { mirror: "fast.example", image: "fast", reachable: true, bytes: 500, durationMs: 100 },
    { mirror: "unavailable.example", image: "unavailable", reachable: false, bytes: 10, durationMs: 1 },
  ])).toMatchObject({ mirror: "fast.example", image: "fast" });
});

test("probes domestic mirrors only for mainland China and falls back on GeoIP failure", async () => {
  const requests: string[] = [];
  const fetcher = async (input: RequestInfo | URL): Promise<Response> => {
    const url = String(input);
    requests.push(url);
    if (url.includes("geo.example")) return new Response(JSON.stringify({ country_code: "CN" }), { status: 200 });
    if (url.includes("fast.example")) return new Response(new Uint8Array(2_000), { status: 200 });
    await new Promise((resolve) => setTimeout(resolve, 5));
    return new Response(new Uint8Array(100), { status: 200 });
  };
  const selected = await selectDockerHubMirror("oven/bun:1.3-alpine", {
    fetcher,
    geoIpUrl: "https://geo.example/country",
    mirrors: ["slow.example", "fast.example"],
    timeoutMs: 1_000,
  });
  expect(selected.mainlandChina).toBe(true);
  expect(selected.mirror).toBe("fast.example");
  expect(selected.image).toBe("fast.example/oven/bun:1.3-alpine");
  expect(requests.some((url) => url.includes("/v2/oven/bun/manifests/1.3-alpine"))).toBe(true);

  const outside = await selectDockerHubMirror("oven/bun:1.3-alpine", {
    fetcher: async () => new Response(JSON.stringify({ country_code: "US" }), { status: 200 }),
    mirrors: ["fast.example"],
  });
  expect(outside).toMatchObject({ mainlandChina: false, image: "oven/bun:1.3-alpine", probes: [] });
});
