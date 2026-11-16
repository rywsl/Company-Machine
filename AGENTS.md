# 协作须知

按用户当前任务承担设计或实现，不绑定特定模型。用户要求先讨论时，只研究和讨论；授权明确后，在范围内自主推进。

设计时：研究现状、制定方案，维护架构、`spec/`、`plan/`、任务卡和验收设计。先明确需求、契约和验收，再进入实现；不要把未定方案当作既定要求。

实现时：按已有规范和任务卡开发，并用 harness 证明完成。不要临时发明接口、降低验收标准，或提前做后续里程碑的功能。

旧文档中“只负责实现、设计文件只能由人修改”的分工，以本文件为准；其余工程约束继续有效。

实现前依次阅读：

1. `plan/README.md` 第 6 节禁止事项（授权设计或维护的受保护文件按下文处理）
2. 任务卡引用的规范
3. 完整任务卡 `plan/tasks/<ID>.md`

进入实现时，在仓库根目录运行：

```bash
uv run python tools/harness.py doctor
uv run python tools/harness.py status
uv run python tools/harness.py task next
uv run python tools/harness.py task show <ID>
uv run python tools/harness.py task start <ID>
```

读完任务卡和必读章节再写代码。验收：

```bash
uv run python tools/harness.py accept <ID>
```

规范有歧义、和验收测试矛盾，或同一验收项因同一原因连续失败 5 次时，暂停受影响的实现，按 `plan/README.md` 第 5 节写 `plan/journal/` 并 `task block`。有设计授权时可先解决规范问题，再重新验证；不要猜。

受保护文件由 `plan/protected.lock` 登记。实现时不得修改；获准设计或维护时可修改对应文件，审阅差异后仅同步这些文件的哈希，并运行 `uv run python tools/harness.py lock --verify`。禁止全量 `lock --update`、重登记无关变更或绕过工具权限。

不要新增依赖，不要在控制面调用 LLM API，不要 `shell=True`。禁止为通过验收削弱测试。保留已有无关改动；提交与推送只在用户明确要求时进行。
