# 实现者须知

你是实现者，不是设计者。按已有规范实现，并用 harness 证明完成。不要发明接口、改验收标准，或提前做后续里程碑的功能。

开始前读这三份，冲突时以此处的顺序为准：

1. `plan/README.md` 第 6 节禁止事项
2. 任务卡引用的规范
3. 任务卡 `plan/tasks/<ID>.md`

每次在仓库根目录开始：

```bash
uv run python tools/harness.py doctor
uv run python tools/harness.py status
uv run python tools/harness.py task next
uv run python tools/harness.py task show <ID>
uv run python tools/harness.py task start <ID>
```

读完整张任务卡和它的必读章节再写代码。验收：

```bash
uv run python tools/harness.py accept <ID>
```

规范有歧义、和验收测试矛盾、或同一验收项连续失败 5 次时，停下来，按 `plan/README.md` 第 5 节写 `plan/journal/` 并 `task block`。不要猜。

受保护文件由 `plan/protected.lock` 登记。编辑它们会被 `.claude/settings.json` 里的 PreToolUse hook 拒绝。不要运行 `tools/harness.py lock --update`。不要新增依赖，不要调用 LLM API，不要 `shell=True`。
