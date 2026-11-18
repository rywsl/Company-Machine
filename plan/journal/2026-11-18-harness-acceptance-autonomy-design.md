# Harness 验收自治化与提速设计（2026-11-18）

## 审计结论

- `plan/state.json` 没有 `gates.M17`，所以 M17 阶段门的“未通过”是尚未执行的默认状态，不是已执行失败记录。
- `M17-08` 已记录为 `done`。已有证据包含连续性、live-readiness、repro、C10 live witness、live evidence integrity、81 条 all-known-issues、四格 operability witness、1767 条全量回归以及 lint、format、doccheck、lock。
- `M17-01` 的 `outside_paths` 中的 `lan/state.json` 当前不存在，提交历史也没有这个文件。它来自历史任务基线中把 `plan/state.json` 截断成 `lan/state.json` 的路径记录；本改造不删除或改写该记录。
- `M17-08` 的 15 个 `outside_paths` 是 M11-M16 实现提交和 M17 合并前工作树中已经存在的源码变更，主要对应 transport、daemon、gateway、agent、sandbox、MCP、factory registry 等跨任务闭环实现。本改造保留原记录，新增机器可验证审查，不把历史记录改成通过。

## 不变的正式语义

不带开发选项的 `accept TASK` 仍按任务清单执行该任务的全部精确检查。live witness、operability witness、all-known-issues、design coverage、lint、format、doccheck 和 lock 仍是正式验收的一部分；全量 `pytest tests` 由 M17 最终阶段门统一执行一次。缓存、并行和失败恢复只改变执行调度，不改变规范节点、断言、退出码或证据要求。

## 运行与恢复

每次运行生成唯一 `run_id`，写入 `.factory/harness-runs/<task>/<run-id>/manifest.json`，每个检查使用独立临时目录。manifest 记录命令、模式、fingerprint、执行/复用、退出码、stdout/stderr 摘要、证据路径、资源、进程清理和 blocker。`--from` 从指定检查继续；`--only` 只跑指定检查；`--fail-fast` 首个失败即停止。局部模式的结果只写入诊断记录，不能把任务标记为完成。

## 缓存边界

缓存 key 包含 commit 与工作树摘要、spec/plan/tasks/gates/closure 摘要、Python/uv/pytest 版本、操作系统和环境存在性摘要、完整命令及 `cache_inputs`。只有 `cacheable: true`、`live: false`、无模型/容器/资格/预算/证据完整性语义的离线检查可以缓存。缓存条目标记 `executed` 或 `reused`；live 结果即使找到旧条目也必须重新执行，不能被当作 live execution。

## 并行安全

只有显式声明 `parallel_group`、`live: false` 且资源声明不冲突的检查进入进程级并行池。每个分片继承独立 `TMP/TEMP` 和 harness 临时目录；共享数据库、端口、工厂根目录、Docker、模型上游、状态与 evidence 目录通过 `exclusive_resources` 串行化。状态和最终 evidence 汇总始终由主进程串行写入。

## Windows 可恢复性

子进程以独立进程组启动，超时、失败或运行结束时只清理本轮 manifest 登记的进程树。清理失败写入 blocker；不按名称或全局进程列表误杀其他任务。状态和 lock 文件使用有限次数、带退避的原子替换，以缓解 `WinError 5` 和短暂文件锁。运行目录按 task/run-id 隔离，不清理其他任务目录。

## 越界审查协议

`review audit TASK` 只读分析历史 `outside_paths`：当前存在性、git 提交来源、其他任务允许范围、测试和 lock 证据，并输出稳定 JSON。独立 reviewer 产生的 `.factory/reviews/<task>/<review-id>.json` 必须包含任务、来源 commit、工作树摘要、文件摘要、归属、理由、测试/规范影响、独立 reviewer 身份、上下文和输入摘要、时间与决策。harness 只接受 `accepted` 且 digest 匹配、reviewer 身份独立、无高风险文件、证据存在的 artifact；缺失、过期、身份等于当前实现 agent 或涉及治理/预算/权限/证据规则的变更进入 `needs_human`。reviewer artifact 不得由 `accept` 自动生成，也不修改源码、state、live report 或 protected.lock。

## 验证与基准

新增测试覆盖参数选择、fail-fast、缓存命中/失效、live 禁止复用、并行临时目录、失败续跑、Windows 原子替换/进程清理、正式检查集合和 reviewer digest/身份/风险规则。基准沿用最近 M17 证据：live witness 约 170 秒、all-known-issues 约 281 秒、operability 约 407 秒、全量 pytest 约 1724 秒；改造后额外记录命令级 elapsed 和复用命中数，不以缓存结果替代正式全量回归。

本轮本机基准（4 个独立、每项约 0.3 秒的离线子进程）为：串行 `jobs=1` 约 1.397 秒，并行 `jobs=4` 约 0.390 秒；同一输入指纹的四项离线缓存从约 0.388 秒降到 `--resume` 约 0.033 秒，4/4 项明确记录为 `reused`。这些是调度基准，不代表 live witness 或正式全量回归可以省略。

补充真实任务配置烟测：M17-09 的 `contract` 与 `design-coverage` 在 `jobs=2` 下均实际执行并通过，合计子进程耗时约 6.76 秒；同一输入指纹执行 `--resume` 后 2/2 项标记为 `executed=false, reused=true`，耗时约 0 秒。manifest 和 cache 条目分别记录 run id、输入指纹、命令、退出码、输出摘要、资源和 evidence path。M17-08 及 M17-09–12 的任务级全量回归已移出任务清单，M17 阶段门仍保留一次不可缓存的全量 `pytest tests`，基线约 1724 秒。
