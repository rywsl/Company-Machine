# autonomy：自治闭环的装配与端到端验收（M5-07）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M5-07**。
> M5-01 至 M5-06 分别交付了纠偏、命令行、持久化执行、守护进程、MCP 操作面和值守进程。本文件只做两件事：规定它们在 `factory serve` 里怎样装配在一起；用架构 13.2 的闭环验收整体行为。
> 本任务不新增 Python 接口。验收失败时，修的是前面任务的实现，不是在这里补特例。

---

## 1. 目的与范围

架构第 15 节 M3 的完成标准是“复现 13.2：缺少转换 → 厂长编写 → 自动衔接 → 推广，且运行中的 run 不受影响”。M3 的验收只在进程内直接调用流水线，从来没有让一个真正被启动的 agent 走完这条路。M5-07 补上这一点：

- 草稿缺少转换 → 治理拒绝并开出转换需求（`supervisor.md` 5.6）；
- 守护进程的信号泵为需求开票（`daemon.md` 5.5）；
- 值守进程在沙箱里启动厂长，厂长在 Registry 克隆里写转换工位、提交、经 MCP 提议（`supervisor.md` 5.1）；
- 治理流水线验证，等级 T2 需要交叉复核 → 信号泵开复核票 → 值守启动**另一个** runtime 复核（`supervisor.md` 5.2）；
- 影子 → 金丝雀（真实 run 被路由到候选 epoch）→ 推广 → 转换需求被解决；
- 人重新提议原来的草稿，这次通过验证。

全程没有任何测试代码替厂长或复核者调用 `propose` / `add_review`，也没有直接调用 `advance`：推进只发生在守护进程的 `tick` 里。

**做什么（M5-07）**

- `factory serve --once` 在 `supervise = true` 时的行为（3.1）。
- 端到端验收（第 7 章）。
- 写 M5 交接记录（`plan/README.md` 第 5 节、`plan/milestones.md` 交接规则第 4 条）。

**不做什么**

| 不做 | 归属 |
|---|---|
| 真实 Claude Code / Codex、Docker 上的同一闭环 | M6-03，`live.md` |
| 新的组件接口 | 不做。发现缺口时按 `plan/README.md` 第 5 节写日志并阻塞 |

---

## 2. 依赖

| 依赖 | 用途 |
|---|---|
| `Daemon`、`ServeConfig`、`InlineJobExecutor`（`daemon.md`） | 推进治理、触发与派发 run、信号泵 |
| `ForemanSupervisor`、`SupervisorConfig`（`supervisor.md`） | 启动厂长与复核者 |
| `McpServer`（`mcp.md`）、`factoryctl mcp` | agent 的操作面 |
| `ChangePipeline`、`EpochStore`（`governance.md`） | 流水线、epoch |
| `ConversionStore`、`WakeQueue`（`foreman.md`） | 需求与唤醒票 |

---

## 3. 接口

### 3.1 `factory serve --once` 与值守

`daemon.md` 3.3 的 `--once` 增补：`ServeConfig.supervise` 为真时，在同一线程里依次执行

1. `daemon.start()`，一次 `daemon.tick()`；
2. `ForemanSupervisor(ctx, epochs, SupervisorConfig.load(root), pipeline=daemon 使用的同一个 pipeline).start()`，一次 `run_once()`，`shutdown()`；
3. `daemon.shutdown(timeout_s=60)`。

`--json` 输出 `{"tick": TickReport.to_json(), "session": SessionRecord.to_json() 或 null}`。`supervise` 为假时输出不变（`TickReport`）。

`supervise = true` 而没有治理配置 → `SUPERVISOR_CONFIG_INVALID`（`supervisor.md` 3.6）。

不带 `--once` 时按 `daemon.md` 5.7：值守在线程 `mao-supervisor` 里运行，有自己的 `Context`。

---

## 4. 数据格式

没有新的数据格式。端到端用到的 `factory.json` 段：`governance`（`governance.md`）、`serve`（`daemon.md` 4.1）、`foreman`（`supervisor.md` 4.1）。

---

## 5. 行为

### 5.1 闭环中各步的责任

| 步骤 | 谁做 | 依据 |
|---|---|---|
| 草稿编译出 `E-NO-CONVERTER`，开转换需求 | `ChangePipeline` 第 V1/V2 步 | `supervisor.md` 5.6 |
| 为需求开 `conversion` 票 | 守护进程信号泵 | `daemon.md` 5.5 |
| 启动厂长，接 MCP | 值守 | `supervisor.md` 5.1 |
| 写转换工位、提交、提议 | 厂长（agent） | `supervisor.md` 4.7 的 task |
| 需求标 `in_progress` | 值守（会话留下了提议） | `supervisor.md` 5.5 |
| 验证、计算等级 | 守护进程 → `ChangePipeline.advance` | `governance.md` 5.7 |
| 开 `review` 票 | 守护进程信号泵 | `daemon.md` 5.5 |
| 选与提出者不同的 runtime 启动复核 | 值守 | `supervisor.md` 5.2 |
| 影子、金丝雀路由、推广 | 守护进程 → 流水线、`EpochRouter` | `governance.md` 5.9、5.10；`daemon.md` 5.3 |
| 需求标 `resolved` | 推广时 `resolve_matching` | `governance.md` 5.10 第 3 步 |

### 5.2 宪法

厂长在克隆里修改 `constitution/` 并提议：`changeset_propose` 的结果是 `isError`，错误码 `CONSTITUTION_FORBIDDEN`（或 `FOREMAN_FORBIDDEN`，`foreman.md` 5.6），不产生 ChangeSet，active 不变。会话本身按 `supervisor.md` 5.5 判定：`drift` 票的会话正常退出即为 `ok`。值守不因为厂长“试图越权”而做额外处理：越权在操作面上已经无效。

---

## 6. 错误

没有新的错误码。端到端里出现的码都来自前面的规范：`REGISTRY_INVALID`（治理拒绝草稿）、`CONSTITUTION_FORBIDDEN`、`FOREMAN_FORBIDDEN`、`SUPERVISOR_CONFIG_INVALID`。

---

## 7. 验收测试

夹具 `tests/acceptance/m5/auto_world.py`（`supervisor.md` 第 7 章）。初始 Registry 去掉 `stations/note-to-text.yaml` 与 `lines/note-pipeline.yaml`：前者是要由厂长补上的转换，后者是依赖这条转换的草稿。治理流水线注入通过型的契约与影子替身；守护进程用 `InlineJobExecutor`；`LocalProvider`、`FakeClock`、`CounterIds`。最后一个测试用真实子进程跑 `factory serve --once`。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m5/test_m5_07_autonomy.py` | `test_missing_converter_loop` | 5.1 全表：草稿被拒并开需求；`tick` 开转换票；厂长会话提议；`tick` 推到 `verified` 并开复核票；复核会话用 `fake-reviewer@1`；之后只靠 `tick` 走完影子、金丝雀（真实 run 落在候选 epoch）、推广；需求 `resolved`、两张票 `done`、两次会话 `ok`；人基于新 active 重新提议草稿得到 `verified` |
| | `test_constitution_stays_human` | 厂长修改宪法并提议：工具结果 `isError`，码为 `CONSTITUTION_FORBIDDEN` 或 `FOREMAN_FORBIDDEN`；没有 ChangeSet；active 不变 |
| | `test_serve_once_hosts_supervisor` | 子进程 `factory serve --once`（`supervise = true`）：一次 tick 之后跑一次值守，已有的唤醒票被处理，会话 `ok`、票 `done` |

```
uv run python tools/harness.py pytest tests/acceptance/m5/test_m5_07_autonomy.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 3`。

---

## 8. 错误示范与正确示范

```python
# ✖ 验收不过，就在测试里替厂长调用 propose，或在实现里识别 fake-agent 走捷径
pipeline.propose(doc, commit, by="fake-agent@1", timeout_s=60)
# ✔ 只有 agent 经 MCP 提议；测试只观察结果
```

```python
# ✖ 为了让金丝雀有样本，在测试里直接插 runs 行
# ✔ 给触发器喂一个带标签的 Bean，让守护进程按路由真的跑一个 run
```

```python
# ✖ serve --once 时另起线程跑值守，测试结果取决于线程调度
# ✔ --once 在同一线程里先 tick 再 run_once
```

---

## 9. 风险与注意事项

- **步数**：端到端用“推进到某个状态为止，最多 N 轮”的写法，不假定每个阶段恰好一轮。实现可以在一轮里多做事，但不能少做：最多 30 轮仍到不了目标状态即为失败。
- **它是集成测试**：失败时先看是哪一步的责任方（5.1 的表），回到对应任务修。不要在 `mao.supervisor` 或 `mao.daemon` 里为这个场景写分支。
- **时钟**：`FakeClock` 不前进。金丝雀与影子不依赖时间流逝；退避与观察期不在这条路径上。
