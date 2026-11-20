#!/usr/bin/env bun
const args = Bun.argv.slice(2);
if (args.length !== 1) {
  console.error("用法：count-text PATH");
  process.exit(2);
}
let text: string;
try {
  text = await Bun.file(args[0]).text();
} catch (error) {
  console.error(`count-text: 无法读取 ${args[0]}：${error instanceof Error ? error.message : String(error)}`);
  process.exit(2);
}
if (!text.trim()) {
  console.log(JSON.stringify({ reason: "文本为空" }));
  process.exit(1);
}
const chars = [...text.replace(/\s/g, "")].length;
const lines = text.split(/\r?\n/).length - (text.endsWith("\n") ? 1 : 0);
console.log(JSON.stringify({ chars, lines, has_lower: /[a-z]/.test(text) }));
