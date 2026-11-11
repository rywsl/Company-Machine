# observability：工厂状态、指标与事件流（M7-01）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M7-01**。
> M5、M6 之后工厂可以无人值守地运行。人需要一个地方看“现在好不好”：守护进程和值守在不在线，有没有死信、放弃的唤醒票、安全模式，今天花了多少钱。本文件定义这个只读视图，以及给外部监控用的指标文本和事件流。

---

## 1. 目的与范围

**做什么（M7-01）**

- `mao.status.factory_status`：一次读取汇总出的状态文档（4.1），含派生的告警。
- `mao.status.render_metrics`：Prometheus 文本格式（0.0.4）的指标。
- `mao.status.tail_events`：按时间与种类过滤事件。
- 命令行：`factory status`、`factory metrics`、`factory events`。

**不做什么**

| 不做 | 归属 |
|---|---|
| Web 页面 | M7-02，`console.md`（复用本文件的函数） |
| 推送告警（邮件、IM） | 不做。外部监控抓取 `factory metrics` 或 `factory status --json` |
| 任何写操作 | 禁止。本模块只读数据库与文件 |

---

## 2. 依赖

| 依赖 | 用途 |
|---|---|
| `Context`（README 4.1） | 数据库、时钟 |
| `LeaseStore`（`durable.md`） | `serve`、`supervisor` 是否在线 |
| `serve.json`（`daemon.md` 4.2） | 心跳 |
| `EpochStore`、`ChangePipeline.safe_mode`（`governance.md`） | 指针、安全模式（有治理配置时） |

---

## 3. 接口

```python
# mao.status
def factory_status(ctx: Context, *, epochs: EpochStore | None = None,
                   pipeline: ChangePipeline | None = None) -> dict[str, Any]
def render_metrics(ctx: Context, *, epochs: EpochStore | None = None,
                   pipeline: ChangePipeline | None = None) -> str
def tail_events(ctx: Context, *, since: str | None = None, kind: str | None = None,
                limit: int = 100) -> list[dict[str, Any]]
```

```
factory status [--json]
factory metrics
factory events [--since ISO] [--kind PREFIX] [--limit N] [--json]
```

- `status`：没有告警退出码 0；有 `error` 级告警退出码 1；只有 `warning` 级告警退出码 0。
- `metrics`：打印 `render_metrics` 的结果，退出码 0。
- `events`：`--limit` 缺省 100，最大 10000；超出 → 用法错误（退出码 2）。

命令属于 `commands.md` 的命令表，由本任务加入。三个命令都不需要快照，不要求 `--registry`；有治理配置时按 `factory.json` 构造 `EpochStore` 与 `ChangePipeline` 传给函数。

---

## 4. 数据格式

### 4.1 状态文档

```json
{
  "at": "2026-11-11T08:00:00.000Z",
  "serve": {"online": true, "owner": "host:123:o-0001", "expires_at": "...", "heartbeat_at": "...", "tick": 812},
  "supervisor": {"online": false, "owner": null, "expires_at": null},
  "epochs": {"active": "<40>", "candidate": null, "previous": "<40>"},
  "safe_mode": {"on": false, "since": null, "until": null, "level": 0},
  "jobs": {"PENDING": 0, "RUNNING": 1, "SEALED": 120, "FAILED": 2, "DEAD": 1},
  "runs": {"RUNNING": 1, "SUCCEEDED": 40},
  "changesets": {"active": 3, "rejected": 1},
  "wakes": {"open": 1, "taken": 0, "failed": 0, "done": 12},
  "approvals_pending": 0,
  "spend": {"today_usd": 1.25, "foreman_today_usd": 0.4},
  "alerts": [{"code": "DLQ_NONEMPTY", "severity": "warning", "detail": {"count": 1}}]
}
```

- `serve` / `supervisor`：`online` 为对应租约（资源名 `serve`、`supervisor`）的 `LeaseStore.valid(...)` 不为 None；`owner`、`expires_at` 取该租约（不在线时为 null）。`serve` 另有 `heartbeat_at`（`serve.json` 的 `last_tick_at`）与 `tick`，没有文件时为 null。
- `epochs`、`safe_mode`：没有治理配置（`epochs` 为 None）时为 null。`epochs` 取 `EpochStore` 的三个指针。`safe_mode` 取 `pipeline.safe_mode()`；`pipeline` 为 None 而有 `epochs` 时，函数自己构造 `ChangePipeline(ctx, epochs)` 再取。本模块只调用 `pipeline.safe_mode()`，不调用其他方法。
- `jobs`、`runs`、`changesets`、`wakes`：按状态计数，只列出现过的状态，键升序。没有行时为 `{}`。`approvals_pending`：`approvals` 表 `status = 'pending'` 的行数。
- `spend`（“当天”按 UTC，以 `ctx.clock.now()` 的日期为准）：
  - `jobs_today` = `model_usage` 中 `at` 在当天、且 `job_id` 不是厂长会话 id 的 `cost_usd` 之和，加上 `updated_at` 在当天、且没有任何 `model_usage` 行的 job 的 `jobs.cost_usd` 之和（transcript 结算）；
  - `foreman_today_usd` = `started_at` 在当天的 `foreman_sessions.cost_usd` 之和（会话的模型用量已经在这里，不再从 `model_usage` 重复计入）；
  - `today_usd` = `jobs_today + foreman_today_usd`。都保留 6 位小数。
- `at` 是 `ctx.clock.now()`。

### 4.2 告警

按下表顺序生成，`alerts` 保持这个顺序：

| `code` | `severity` | 条件 | `detail` |
|---|---|---|---|
| `SERVE_DOWN` | error | 有 `serve` 段配置但 `serve.online` 为假 | — |
| `SAFE_MODE` | error | `safe_mode.on` | `level` |
| `WAKES_FAILED` | error | `wakes.failed > 0` | `count` |
| `SUPERVISOR_DOWN` | warning | 有 `foreman` 段配置但值守不在线（`serve.supervise` 为真时看 `serve` 进程） | — |
| `DLQ_NONEMPTY` | warning | `jobs.DEAD > 0` | `count` |
| `HEARTBEAT_STALE` | warning | `serve.online` 而 `heartbeat_at` 早于 `now - 3 × max(tick_s, 1)` 秒 | `heartbeat_at` |

告警码不是 `FactoryError` 的错误码，不登记进 `mao.errors.CODES`。

### 4.3 指标

文本格式 0.0.4。下表的每个指标都输出 `# HELP` 与 `# TYPE` 行（即使没有样本），样本按标签值升序。只输出下表的指标：

| 名字 | 类型 | 标签 | 值 |
|---|---|---|---|
| `mao_up` | gauge | `component`（`serve` / `supervisor`） | 在线为 1，否则 0 |
| `mao_jobs` | gauge | `state` | 4.1 的 `jobs` |
| `mao_runs` | gauge | `state` | 4.1 的 `runs` |
| `mao_changesets` | gauge | `state` | 4.1 的 `changesets` |
| `mao_wakes` | gauge | `status` | 4.1 的 `wakes` |
| `mao_safe_mode` | gauge | — | 开启为 1；没有治理时为 0 |
| `mao_spend_usd_total` | counter | `source`（`foreman` / `model` / `transcript`） | 历史累计，三个来源的口径与 4.1 相同，只是不限当天 |
| `mao_foreman_sessions_total` | counter | `state` | `foreman_sessions` 按状态计数 |
| `mao_alerts` | gauge | `code` | 当前告警，每个出现的码为 1 |

计数与 0/1 输出整数（`1`，不是 `1.0`）；金额输出 `repr(round(x, 6))`。没有标签的指标写成 `名字 值`。标签值按文本格式规则转义 `\`、`"`、换行。

### 4.4 事件

`tail_events` 返回 `[{"id", "kind", "at", "job_id", "data"}]`，按 `seq` 升序。`since` 是 `at` 的包含下界；`kind` 是前缀匹配（`"foreman."` 匹配所有厂长事件）。取满足条件的**最后** `limit` 条，再按 `seq` 升序返回。`data` 解析为 JSON 后，其中每个字符串值按 `foreman.md` 4.4 第 2 条的模式打码（不对 JSON 文本整体打码，那会破坏结构）。

---

## 5. 行为

1. 三个函数都只读。不取租约，不写事件，不改文件。
2. 一次 `factory_status` 内的所有计数来自同一个读事务（SQLite 的一次 `BEGIN` … `COMMIT`），避免数字之间互相矛盾。
3. 配置段是否存在只看 `factory.json`，不探测进程。
4. 不在状态文档里输出任何秘密值、transcript 正文或 Bean 内容。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `EVENTS_QUERY_INVALID` | `since` 不是 ISO 时间，或 `limit` 不在 1–10000 | `field` |

---

## 7. 验收测试

`registry-m2` 与治理夹具，`LocalProvider`、`FakeClock`、`CounterIds`。状态由测试直接写入数据库或通过组件产生。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m7/test_m7_01_status.py` | `test_status_counts` | 不同状态的 job、run、唤醒票计数正确，键只含出现过的状态 |
| | `test_status_alerts_order` | 死信、放弃的唤醒票、安全模式同时存在时，`alerts` 的码与顺序符合 4.2；`factory status` 退出码 1 |
| | `test_status_without_governance` | 单快照模式：`epochs`、`safe_mode` 为 null，命令仍成功 |
| | `test_spend_today` | 当天的模型用量、transcript 成本、厂长会话分别计入；前一天的不计入 |
| | `test_metrics_text` | `# TYPE` 行的指标名集合恰好是 4.3 的表；`mao_up` 两个样本；`mao_jobs` 的样本与计数一致且按标签升序 |
| | `test_tail_events` | `since` 包含下界、`kind` 前缀匹配、取最后 `limit` 条后升序；`data` 已打码 |

```
uv run python tools/harness.py pytest tests/acceptance/m7/test_m7_01_status.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 6`。

---

## 8. 错误示范与正确示范

```python
# ✖ status 顺便“修一下”：发现租约过期就删掉
# ✔ 只读；修复属于调和器与人
```

```python
# ✖ 逐表各开一个查询，数字之间对不上
# ✔ 同一个读事务
```

```python
# ✖ 指标名随手起，每次发布都变
# ✔ 只输出 4.3 的表；新增指标由人改本文件
```

---

## 9. 风险与注意事项

- **查询成本**：计数是全表聚合。表很大时需要索引或物化计数，由人改本文件；不要在本任务里加缓存（状态必须是现在的）。
- **时区**：“当天”按 UTC。
- **秘密**：事件的 `data` 理论上不含秘密（各组件已保证），这里再打一次码，作为纵深防御。
