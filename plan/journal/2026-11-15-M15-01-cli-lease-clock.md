# M15-01 受阻：CLI 看不到 FakeClock 上的 serve 租约

- 时间：2026-11-15
- 任务：M15-01 线程与回复
- 现象：`uv run python tools/harness.py pytest tests/acceptance/m15/test_m15_01_thread.py` 结果 `passed 9, failed 1`。失败项 `test_post_human_writes_thread_and_mailbox`。`world.cli("talk", "send", ...)` 退出码 1，stdout 为 `{"error":{"code":"TALK_SERVE_DOWN","detail":{"resource":"serve"},"message":"serve 没有在跑"}}`，stderr 为空。同目录最小复现：`hold_serve(world.ctx)` 之后再走 `world.cli`，得到同一个码。其余 9 个测试通过，包括没有租约行时的 `TALK_SERVE_DOWN`，以及用同一个 `world.ctx` 调用 `post_human` 的路径。
- 规范依据：`spec/system/talk.md` 5.1 第 5 步，原文：“`LeaseStore(ctx).valid("serve")` 不是 `None`，否则 `TALK_SERVE_DOWN`”。同文件第 9 章：“`serve` 租约过期视为没在跑。” `spec/system/durable.md` 3.1：`valid` “未过期的行；否则 None”。命令必须只调用 `mao.talk`（`talk.md` 3.3）。
- 我的理解：A：按规范调用 `LeaseStore(ctx).valid("serve")`。`factory talk send` 经 `open_context` 新开 `Context`，时钟是 `SystemClock`（今天 2026-11-15）。夹具 `hold_serve` 用世界的 `FakeClock(START)`，`START` 是 2026-11-10，`ttl_s=3600`，`expires_at` 为 `2026-11-10T01:00:00.000Z`。系统时钟下这行已过期，`valid` 返回 `None`，必须抛 `TALK_SERVE_DOWN`。这样验收的 CLI 成功路径失败。B：把已过期的租约当成有效，或在检查前用系统时钟重新 `acquire`。没有租约的用例仍能失败，但过期租约不再视为没在跑，和第 9 章、`durable.md` 的 `valid` 冲突。
- 已经尝试：实现了迁移 `041_talk.sql`、`mao.talk` 的 `post_human` / `list_messages` / `show` / `talk_reply`、`factory talk ls/show/send`、厂长工具 `talk_reply`、常驻厂长 `wake_id`、任务书那句，以及轮次结束的未回复判定。这些路径在同一个 `ctx` 上通过。没有改受保护文件，没有 `lock --update`。
- 建议：人改夹具或验收，不要改 `valid` 的含义。任选其一：让 `hold_serve` 用 CLI 能看见的时钟写入未过期的 `expires_at`；或让 `test_post_human_writes_thread_and_mailbox` 的成功发送走已持有 `FakeClock` 的 `ctx`，CLI 只覆盖命令登记。改完再 `lock --update`。在此之前不要让实现把过期租约当成 serve 在跑。
