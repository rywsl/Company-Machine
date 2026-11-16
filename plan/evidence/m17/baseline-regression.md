# 附加基线回归核查：旧服务池验收失败

设计维护后选跑旧 ChangeSet、OpenAPI 与 Agent schema 验收：29 total、28 passed、1 failed、0 errors、0 skipped。

失败是 `tests/acceptance/m4/test_m4_03_gateway.py::test_pooled_reset_between_jobs`，断言第349行：第一次 fetch 的宿主 dest 仍出现在第二次 argv。

为了排除本次 schema/L0 设计改动，另用 `git archive c538768` 导出完整未改动项目到 `.factory/m17-repro/pristine-c538768`；以同一解释器、显式指向导出的 src/tests，在该目录运行原始节点。仍为 **1 failed、0 errors、0 skipped**，相同断言。初次调用路径写成相对于外层worktree，因harness内部切换ROOT返回file-not-found；纠正后才取得上述有效失败，不能把初次命令算作复现。

有效重跑命令（在导出项目目录）：

```bash
PYTHONPATH="$PWD/src:$PWD" /home/ubuntu/.codex/worktrees/m17-autonomy-audit/machine_agent_org/.venv/bin/python tools/harness.py pytest tests/acceptance/m4/test_m4_03_gateway.py::test_pooled_reset_between_jobs --basetemp=/home/ubuntu/.codex/worktrees/m17-autonomy-audit/machine_agent_org/.factory/m17-repro/pristine-pool --tb=short
```

当前归因边界：旧夹具两次直接复用同一个 sandbox，且将同一个宿主 factory 根挂到 `/factory`；网关两次 fetch 同一个 `/factory/out/video/video.mp4`，故宿主路径自然相同。这个断言不能单独证明真实 InstancePool/Runner 的跨job输出泄漏，也不能据此宣布生产安全。真实两个 job 的挂载、reset、旧文件与句柄隔离需要 M17-04 进一步证明。

处理：纳入 M17-04 服务恢复/隔离验收与旧回归问题。保留原测试；不以 skip、放宽断言、关闭 reset 或改假客户端来获得绿灯。若 M16 已消除此失败，在完成后整合时重核并记录；否则在真实复现与规范澄清后进行授权设计维护，不能把冻结基线阶段门状态当作本次全套回归成功。此附加项不计入25项诊断/24失败数字。

## 已冻结的关闭方案（R01）

continuity.md §5.9已对照M9端口布局及M12池生命周期明确语义：固定路径可复用，真实文件/上下文归属不可串用；不把旧断言失败冒充已证明的生产泄漏。M17-04限定维护旧节点为真实两job/池测试，加强内容和生命周期断言，并新增独立pool isolation正反例。两节点均进入closure.yaml及精确执行器，不留在普通日志中；最终离线门升至77用例。当前未改该受保护测试。
