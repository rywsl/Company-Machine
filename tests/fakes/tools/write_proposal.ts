#!/usr/bin/env bun
import { mkdir } from "node:fs/promises";
import { dirname } from "node:path";
const args = Bun.argv.slice(2);
if (args.length !== 2) process.exit(2);
await mkdir(dirname(args[1]), { recursive: true });
const body = { summary: "quote", cost_estimate_usd: Number(args[0]), time_estimate_s: 1 };
await Bun.write(`${args[1]}.tmp`, JSON.stringify(body));
await (await import("node:fs/promises")).rename(`${args[1]}.tmp`, args[1]);
