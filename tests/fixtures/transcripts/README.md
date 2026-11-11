# transcript 夹具

`spec/system/runtimes.md` 第 7 章使用。**受保护，只能由人修改。**

- `claude-stream.jsonl`：Claude Code `--output-format stream-json --verbose` 的形状（system/init、assistant、user 的 tool_result、result）。
- `codex-exec.jsonl`：Codex `exec --json` 的形状，两轮，最后一条 agent_message 是结果。
- `codex-failed.jsonl`：出现 `error` 与 `turn.failed`，末尾一行不是 JSON。

形状取自固定版本的 CLI（`tools/live_versions.json`）。CLI 升级后格式变了，由人更新这些文件和 `runtimes.md`。
