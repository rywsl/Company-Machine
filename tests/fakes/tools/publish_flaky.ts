#!/usr/bin/env bun
import { copyFile } from "node:fs/promises";
import { join } from "node:path";
const source = Bun.argv[2];
if (source && await Bun.file(source).exists()) await copyFile(source, `${source}.published`);
process.exit(1);
