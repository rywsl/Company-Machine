# M17-08 受阻：离线 9/9 不是验收

- 时间：2026-11-17
- 任务：M17-08 跨进程多周期与真实能力总验收
- 现象：`tasks.yaml` 的 accept 含 `live-witness`、`live-evidence-integrity`、`operability-live-witness`、`operability-live-integrity`。重跑这些命令后，C10 与四格 pytest 均退出码 1，`failed=1 passed=0 skipped=0`。断言原文是 `live witness blocked` 与 `operability live witness blocked`：`model credential is unset`、`approved budget is unset`，以及 `Machine:openmontage@1`、`Machine:minimax-h3@1`、`Machine:minimax-dance@1`、`Machine:grok-dance@1` 未配置。`verify_live.py` 返回 `ok=false status=blocked`，其中含 `REPORT_NOT_PASSED` 与 `REPORT_HAS_BLOCKERS`。`verify_plan.py --live-index` 返回 `ok=false`，issue 为 `LIVE_INDEX_INVALID:ValueError`。Docker Server 28.3.2 与 codex/claude 存在。`XAI_API_KEY`、`OPENAI_API_KEY`、`MAO_BUDGET_USD` 均未设置。工具原文在 `{SCRATCH}/live-blocked.txt`。命名的离线 harness pytest 为 `passed=9 failed=0 errors=0 skipped=0`，这不是 `harness.py accept M17-08`。
- 规范依据：`spec/system/operability.md` 第 1 节，原文：“没有实际资源时 live 为 blocked，M17 阶段门不得通过。”同文件第 6 节，原文：“真实产品/四格模型资源不足可先完成离线实现，但M17最终门保持失败/blocked，不能给全覆盖已修复结论。”任务卡原文：“blocked/failed不能通过accept或gate。”以及“不能仅满足该数字。”
- 我的理解：A. 离线九项只是 accept 中的 `acceptance` 一项，不能单独把任务标成 done。B. live 四项与离线项同属一次 accept，缺资源时 accept 不能退出 0。按 B 执行：不 `task start`，不把离线 9/9 写成验收通过。
- 已经尝试：按 accept 命令重跑 C10、`verify_live.py`、四格 witness 和 `verify_plan.py --live-index`。四条都退出码 1，状态是 blocked 或 ok=false。没有把 skip、xfail 或离线 JSON 记成 passed。
- 建议：保持 M17-08 blocked。补齐模型凭据、已批准预算和四件历史产品后再 `task start` 并运行完整 accept。不要手改 `plan/state.json`，也不要为了离线项单独验收。
