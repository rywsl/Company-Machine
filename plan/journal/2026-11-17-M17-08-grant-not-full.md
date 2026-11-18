# M17-08 受阻：记录在案的授权不是完整 live 验收

- 时间：2026-11-17
- 任务：M17-08 跨进程多周期与真实能力总验收
- 现象：`harness.py accept M17-08` 退出码 1，输出“不能验收 M17-08：状态是 blocked。先运行 task start M17-08”。同一轮 C10 与四格真实入口都是 `blocked authorization authorization.grant_file authorization is not a full live grant`，`live_passed` 为 false。没有付费调用。
- 规范依据：plan/m17-live-readiness.md 第 2 节，原文：“不得将离线fixture的20美元常数、价格缺失或用户一般目标伪装成本次付费额度。” cases.yaml `live_witness_contract`：外部资源不足或真实性无法核验时必须失败或 blocked，不能回退脚本 Agent 或只校验合成报告。
- 我的理解：A. 把 `.factory/m17-local/authorization.json` 的 `is_full_m17_live_acceptance` 改成 true，再跑见证并 accept。这会把本轮烟测额度写成完整 live 验收授权。B. 保持该标志为 false，入口报告 authorization，不验收。B 与文件自述范围一致。
- 已经尝试：凭据和 20 美元上限已在私有 env 与授权文件中。C10 在单独提供完整授权、Docker 和 codex 时可以装配周期 `(1, idle)`、`(2, completed)`，但不记 live passed。四格历史机器仍因缺少 `runtime.command` 不能取得本次资格，规范禁止把配方 `run:` 映射成该命令。
- 建议：不要改授权标志，不要伪造 `runtime.command` 或 passed 报告。M17-08 保持 blocked，直到有一份明确的完整 live 授权，并且四格机器能在不冒充身份的前提下完成资格。
