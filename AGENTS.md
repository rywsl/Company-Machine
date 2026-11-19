# 协作须知

代码、`spec/` 和验收测试可以根据实际验证结果交替修改。规范不需要先于实现提交。发现规范、实现和测试不一致时，在同一变更中修复，并重新运行一致性校验。

提交或推送前，在仓库根目录运行：

```bash
uv run python tools/harness.py verify
```

`verify` 检查 MRL 规范与实现、factory profile、`spec/system` 章节结构、规范第 7 章与验收测试的双向映射，运行关联验收测试、MRL conformance、MRL coverage 和代码策略测试。验收测试不得有失败、错误或跳过，其他检查失败时也必须修复后再提交。

当前工作流不要求领取任务、启动任务、通过阶段门或维护 harness 状态。`plan/tasks/`、`plan/journal/` 和其他历史计划资料可以保留作背景资料，但不作为实现入口，`verify` 也不读取或写入它们。

当前工作流不使用 `plan/protected.lock`、PreToolUse hook 或 Git hook 阻断编辑。不要新增依赖，不要在控制面调用 LLM API，不要使用 `shell=True`，不要为通过校验削弱测试。保留已有无关改动，提交和推送只在用户明确要求时进行。
