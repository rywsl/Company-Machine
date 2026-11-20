# 协作须知

代码、`spec/` 和验收测试可以根据实际验证结果交替修改。规范不需要先于实现提交。发现规范、实现和测试不一致时，在同一变更中修复，并重新运行一致性校验。

提交或推送前，在仓库根目录运行 Bun 校验：

```bash
bun run verify:bun
```

`verify:bun` 运行 Bun 后端、Podman/Kiri runtime 和 Bun MRL 测试。历史 Python 实现和旧验收保存在 `removed/python/`，不作为活动控制面入口。

当前工作流不要求领取任务、启动任务、通过阶段门或维护 harness 状态。`plan/tasks/`、`plan/journal/` 和其他历史计划资料可以保留作背景资料，但不作为实现入口，`verify:bun` 也不读取或写入它们。

当前工作流不使用 `plan/protected.lock`、PreToolUse hook 或 Git hook 阻断编辑。不要新增依赖，不要在控制面调用 LLM API，不要使用 `shell=True`，不要为通过校验削弱测试。保留已有无关改动，提交和推送只在用户明确要求时进行。
