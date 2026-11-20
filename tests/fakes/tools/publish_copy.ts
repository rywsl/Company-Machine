#!/usr/bin/env bun
import { createHash } from "node:crypto";
import { mkdir } from "node:fs/promises";
import { basename, join } from "node:path";
const args = Bun.argv.slice(2);
if (args.length !== 2) process.exit(2);
const source = Bun.file(args[0]);
if (!(await source.exists())) process.exit(1);
await mkdir(args[1], { recursive: true });
const target = join(args[1], basename(args[0]));
const bytes = new Uint8Array(await source.arrayBuffer());
await Bun.write(target, bytes);
const digest = `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
const ctl = process.env.FACTORY_CTL ?? "/factory/ctl";
await mkdir(ctl, { recursive: true });
const receipt = { target: args[1], locator: { path: target }, items: [{ bean: "", locator: target, digest }], note: "" };
await Bun.write(join(ctl, "receipt.json.tmp"), JSON.stringify(receipt));
await (await import("node:fs/promises")).rename(join(ctl, "receipt.json.tmp"), join(ctl, "receipt.json"));
