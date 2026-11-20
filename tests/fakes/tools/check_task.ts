#!/usr/bin/env bun
const args = Bun.argv.slice(2);
if (args.length !== 1) {
  console.error("用法：check-task PATH");
  process.exit(2);
}
let value: unknown;
try {
  value = JSON.parse(await Bun.file(args[0]).text());
} catch (error) {
  console.log(JSON.stringify({ reason: error instanceof Error ? error.message : String(error) }));
  process.exit(1);
}
if (!value || typeof value !== "object" || Array.isArray(value)) {
  console.log(JSON.stringify({ reason: "脚本必须是 JSON 对象" }));
  process.exit(1);
}
const script = value as Record<string, unknown>;
const defaults = script.default;
const attempts = script.attempts;
if (defaults !== undefined && !Array.isArray(defaults)) {
  console.log(JSON.stringify({ reason: "default 必须是列表" }));
  process.exit(1);
}
if (attempts !== undefined && (!attempts || typeof attempts !== "object" || Array.isArray(attempts))) {
  console.log(JSON.stringify({ reason: "attempts 必须是对象" }));
  process.exit(1);
}
let steps = Array.isArray(defaults) ? defaults.length : 0;
for (const [key, actions] of Object.entries((attempts ?? {}) as Record<string, unknown>)) {
  if (!/^\d+$/.test(key) || !Array.isArray(actions)) {
    console.log(JSON.stringify({ reason: "attempts 的键必须是数字字符串，值必须是列表" }));
    process.exit(1);
  }
  steps += actions.length;
}
console.log(JSON.stringify({ steps }));
