# M2-06 受阻：复核验收与夹具路由互相矛盾

- 时间：2026-11-11
- 任务：M2-06 审批路由、超时、复核与两阶段 propose
- 现象：恢复 `mao.approvals` 后，`uv run python -m pytest tests/acceptance/m2/test_m2_06_approvals.py --tb=line -q` 为 12 passed、1 failed。`test_review_counts_distinct_runtimes` 在 `by` 以 `runtime` 开头时按票计，已通过。仍失败的是 `test_review_quorum_unset`：`open_proposal(quote@1, cost_estimate_usd=9.0)` 没有抛错。同一调用在 `test_auto_if_false_parks_for_foreman` 必须返回 `route=foreman` 且 `pending`。
- 规范依据：`spec/system/approvals.md` 第 5.2 节与第 7 章。原文：“否则：最终路由 = else”；“宪法没有该 limit 时，review 路由得到 REVIEW_QUORUM_UNSET”。第 7 章还写“同一工位的变体（补丁，不改受保护文件的原件）：else: review”以及“宪法补丁把 limit(approval_review_quorum, 2.0) 写进夹具的副本”。
- 我的理解：A. 按夹具 `tests/fixtures/registry-m2/stations/quote.yaml` 的 `else: foreman` 实现，估价 9.0 得到 pending/foreman，`test_auto_if_false_parks_for_foreman` 通过，另外两个测试失败。B. 把同一调用改成 review 或抛 REVIEW_QUORUM_UNSET，则已通过的 foreman 测试失败。两条路不能同时满足现有断言。
- 已经尝试：按第 5.2 节实现 open/apply/expire/preset，并让超时、缺规则、脱敏和 Sink propose 不占回执的测试通过。没有修改受保护测试或夹具。
- 建议：在 `tests/acceptance/m2/test_m2_06_approvals.py` 里按规范第 7 章给复核用例打补丁（`else: review`，以及法定人数 2 的宪法副本），不要改 `quote.yaml` 原件。补丁落地后现有实现的 review 分支即可验收。
