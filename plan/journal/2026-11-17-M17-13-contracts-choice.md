# M17-13 受阻：冻结合同运行器不接受 M17-13

- 时间：2026-11-17
- 任务：M17-13 见证执行器与资源解析（离线交付）
- 现象：四项准备测试已通过 harness pytest，`failed=0 errors=0 skipped=0 passed=4`。随后执行 `uv run --offline --env-file .env.m17.local python plan/evidence/m17/run_contracts.py --task M17-13`，退出码 2。stderr：`argument --task: invalid choice: 'M17-13' (choose from M17-01, M17-02, M17-03, M17-04, M17-05, M17-06, M17-07, M17-08, M17-09, M17-10, M17-11, M17-12)`。`plan/evidence/m17/run_contracts.py` 的 choices 是 `range(1, 13)`，而 `plan/tasks.yaml` 的 M17-13 accept 固定调用 `--task M17-13`。
- 规范依据：`plan/tasks/M17-13.md` 验收命令原文包含 `uv run python plan/evidence/m17/run_contracts.py --task M17-13`。`plan/m17-implementation.md` 第 6 节，原文：“默认不修改 protected.lock 保护的 spec、计划、证据、旧验收或 harness。”`run_contracts.py` 在 `plan/evidence/m17/**` 保护范围内，本次例外不覆盖该文件。
- 我的理解：A. 改运行器 choices 才能让冻结 accept 退出 0。B. 没有新的设计维护授权，不能改受保护证据或同步锁。按 B 执行，不手改 state，不把四项协议通过记成 M17-13 done。
- 已经尝试：实现 `tests/system/m17_08_live.py` 与四项准备测试，两个 live 入口改为调用同一执行器。harness pytest 四项退出码 0。合同命令退出码 2。
- 建议：由设计维护把 `run_contracts.py` 的 `--task` choices 扩到 M17-13，只同步该文件哈希并 `lock --verify`，然后重新 `task start`/`accept M17-13`。
