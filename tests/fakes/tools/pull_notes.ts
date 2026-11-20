#!/usr/bin/env bun
import { readdir } from "node:fs/promises";
import { dirname, join } from "node:path";
const args = Bun.argv.slice(2);
if (args.length !== 2) process.exit(2);
const files = (await readdir(args[0], { withFileTypes: true })).filter((entry) => entry.isFile() && entry.name.endsWith(".txt")).map((entry) => entry.name).sort();
if (!files.length) process.exit(1);
await Bun.write(args[1], await Bun.file(join(args[0], files[0])).arrayBuffer());
