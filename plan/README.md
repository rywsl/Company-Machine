# 计划资料（plan/）

`plan/` 保存项目设计、历史任务卡、阶段记录和审查资料。当前实现不要求先领取任务或先写 spec，代码、spec 与验收测试可以根据验证结果交替修改。

## 当前工作流

1. 按需要阅读相关 spec、实现和验收测试。
2. 修改代码、spec 和验收测试，使它们表达同一份当前契约。
3. 反复验证，发现不一致时在同一变更中修复。
4. 提交或推送前运行：

   ```bash
   uv run python tools/harness.py verify
   ```

`verify` 是当前唯一的 harness 入口。它会检查以下内容：

- MRL 规范中的错误码和标准库函数是否覆盖实现登记；
- factory profile 中的诊断码和输入关系是否覆盖实现；
- `spec/system` 的章节结构；
- `spec/system` 第 7 章引用的验收文件和测试函数是否存在；
- `tests/acceptance/` 中的测试文件和测试函数是否都被第 7 章引用；
- 规范关联验收测试是否没有失败、错误或跳过；
- MRL conformance 是否没有失败；
- MRL coverage 是否没有未覆盖的诊断码和规则；
- 代码策略测试是否通过。

`verify` 不读取或写入任务状态、阶段门状态、受保护文件哈希、审查 artifact 或 harness 缓存，也不使用历史里程碑的测试数量阈值。

## 历史资料

`plan/tasks/`、`plan/journal/`、`plan/milestones.md`、`plan/acceptance.md`、`plan/gates.yaml`、`plan/tasks.yaml` 和其他历史文件可以保留用于追溯。它们不再定义当前实现入口，任务卡、阶段门和旧审查流程也不由 harness 读取。

`plan/protected.lock` 不再作为当前工作流的保护机制。仓库不安装 PreToolUse hook 或 Git hook，也不通过工具阻断编辑。规范、实现和验收测试由同一变更中的 `verify` 结果保持一致。

## 工程约束

- 不新增第三方依赖。
- 控制面不调用 LLM API。
- 不使用 `shell=True`。
- 不为通过验收而削弱测试或放宽一致性条件。
- 凭据不得写入日志、提交或回复。
- 保留已有无关改动，提交和推送只在用户明确要求时进行。

## 退出码

| 退出码 | `harness verify` |
|---|---|
| 0 | 所有检查通过 |
| 1 | 规范、测试或外部一致性检查未通过 |
| 2 | 命令用法错误 |
