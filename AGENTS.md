# 协作须知

代码、`spec/` 和验收测试可以根据实际验证结果交替修改。规范不需要先于实现提交。发现规范、实现和测试不一致时，在同一变更中修复，并重新运行一致性校验。

提交或推送前，在仓库根目录运行校验：

```bash
uv run python tools/harness.py verify
```

上述兼容入口直接转发给 `tools/harness.ts`，也可通过 `bun run verify:bun` 调用。校验覆盖 Bun 后端、Podman/Kiri、活动规范双向映射、MRL 一致性与内核规则覆盖、代码策略和前后端类型检查。历史 Python 实现和旧验收保存在 `removed/python/`，完整迁移对照由 `bun run tools/harness.ts parity` 执行。

当前工作流不要求领取任务、启动任务、通过阶段门或维护 harness 状态。`plan/tasks/`、`plan/journal/` 和其他历史计划资料可以保留作背景资料，但不作为实现入口，`verify:bun` 也不读取或写入它们。

当前工作流不使用 `plan/protected.lock`、PreToolUse hook 或 Git hook 阻断编辑。不要新增依赖，不要在控制面调用 LLM API，不要使用 `shell=True`，不要为通过校验削弱测试。保留已有无关改动，提交和推送只在用户明确要求时进行。
