# 开发

代码、`spec/` 和验收测试可以按验证结果需要交替修改。spec 不需要先于实现提交。

## 提交前验证

提交或推送前运行：

```bash
uv run python tools/harness.py verify
```

`verify` 检查 MRL 错误码和标准库函数、factory profile、`spec/system` 章节结构，以及第 7 章与 `tests/acceptance/` 的双向映射。随后运行关联验收测试、MRL conformance、MRL coverage 和代码策略测试。关联验收测试必须没有失败、错误或跳过。

## 修改流程

发现代码、spec 和测试不一致时，在同一变更中修复并重新运行 `verify`。当前仓库不要求领取或启动任务、通过阶段门、维护任务状态、使用受保护文件锁、生成审查 artifact，或安装 PreToolUse hook、Git hook。`plan/tasks/`、`plan/journal/` 和里程碑资料可以作为历史背景保留，但不再是当前实现入口，`verify` 也不读取它们。

## 工程约束

不要新增依赖，不要在控制面调用 LLM API，不要使用 `shell=True`，不要为了通过验证削弱测试。不要把凭据写入日志、提交或回复。保留无关改动，提交和推送只在用户明确要求时进行。
