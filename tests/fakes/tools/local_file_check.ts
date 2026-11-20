#!/usr/bin/env bun
import { createHash } from "node:crypto";
import { join } from "node:path";
const ctl = process.env.FACTORY_CTL ?? "/factory/ctl";
const receiptFile = Bun.file(join(ctl, "receipt.json"));
if (!(await receiptFile.exists())) process.exit(1);
const receipt = JSON.parse(await receiptFile.text()) as { locator?: { path?: string }; items?: Array<{ digest?: string }> };
const path = receipt.locator?.path;
if (!path || !(await Bun.file(path).exists())) process.exit(1);
const digest = `sha256:${createHash("sha256").update(new Uint8Array(await Bun.file(path).arrayBuffer())).digest("hex")}`;
if (receipt.items?.[0]?.digest && receipt.items[0].digest !== digest) process.exit(1);
