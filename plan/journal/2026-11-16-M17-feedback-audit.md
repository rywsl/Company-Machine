# M17 运行反馈与评价接口调查

- 基线：c538768；只在独立 M17 worktree；不改生产 src、不干预 M16。
- 复现：`uv run --offline python tools/harness.py pytest plan/evidence/m17/repro/test_feedback.py --basetemp=.factory/m17-repro/feedback`
- 结果：3 failed、0 errors、0 skipped，均在目标断言；包含真实成功与失败产线。

## F01 发起者与结果没有闭合

`agent_tool._run_start`（src/mao/agents.py）返回 run id 与 PENDING，符合 agents.md §3.4 旧契约。以 scout 经真实 MCP 启动 note-trigger，真实 Runner 执行至 SUCCEEDED 和 FAILED；run.state 事件、job id 和成功产物都存在。连续调用 AgentHost.collect 两次，scout 只有自己的 schedule 邮件，没有任何关联 run 的终态消息。共享 MCP 有 job_get，没有 run_get/nodes，发起者不能用获得的 run id 直接查节点结果。失败不会产生 Bean，也不能用 Bean 订阅推导失败原因。

分类：已复现的持续自治接口缺口，不能宣称违反原 M13 的 run_start 返回约定。M17 新增 ResultStore 与通知契约，复用已有 run 和 mailbox。

## F02 评价查询只回显请求

成功执行真实 upper 工位后，`ForemanQuery.metrics_query('Station:upper@1','success_rate')` 返回 `{'station':'Station:upper@1','metric':'success_rate'}`，无 value/sample_count/window。可见源码 src/mao/foreman.py 的 metrics_query。foreman.md 未冻结窗口与形状，governance.md 的 run_metrics 有统计定义，但不足以推断 station 级接口如何聚合。

分类：真实查询功能不完整且契约有缺口。M17 先明确按参与该工位的终态 run、窗口、分母和空样本语义再实现；不能让模型凭文本自评宣布有效。

## 证据限度

LocalProvider、真实 SQLite/Registry/MCP/Runner，工具仅是无收费的本地确定性工具；不证明某个真实模型能自主判断业务价值。每项都先断言 run 真正达到预期状态和事件，避免把环境故障当反馈缺口。
