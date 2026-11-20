#!/usr/bin/env bun
const args = Bun.argv.slice(2);
if (args.length !== 2) {
  console.error("用法：upper-text IN OUT");
  process.exit(2);
}
const text = await Bun.file(args[0]).text();
const lines = text.split(/\r?\n/);
if (lines.includes("#fail")) {
  console.error("upper-text: 输入要求失败（#fail）");
  process.exit(3);
}
if (lines.includes("#slow")) await Bun.sleep(30_000);
await Bun.write(args[1], lines.includes("#keep-lower") ? text : text.toUpperCase());
