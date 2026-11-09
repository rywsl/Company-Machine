# 调度（scheduler.md）

> 规范性文档，M2。模块：`mao.scheduler`。
> 依据：架构文档 6.9、10.5 的机制名称。数字门槛以已有 Spec 与调用方配置为准，本文件不另定。
> `lines.md` 第 3.2 节的 `Executor` 由本文件实现，不替换、不增删方法。
> `cache.md` 调用 `mao.scheduler.is_quarantined(bean_id)`。`connectors.md` 的 cron 由本文件触发。

---

## 1. 目的与范围

**做什么（M2）**

- 实现 `Executor`：`dispatch` 接收 `LineRunner.tick` 刚提交的 job，`wait` 等到有状态变化或超时。
- 资源队列与配额：按 CPU、内存、GPU 准入；隔舱互不借用对方的份额；共享名字的令牌桶。
- 背压：工位在制品上限，以及 run 准入上限。未配置则该控制未武装。
- 熔断：按配置的样本数与比率打开、半开、关闭。
- 死信：job 进入 `DEAD` 后登记，不再派发。
- Bean 隔离：`is_quarantined`。自动隔离的次数来自配置，没有配置就不自动隔离。
- cron：`every:<时长>`。bean 触发仍调用 `TriggerEngine.process`，本文件只决定何时允许它跑。

**不做什么**

| 不做 | 归属 |
|---|---|
| 替换 `Executor` 或改变 `InlineExecutor` | `lines.md`。`factory line run` 仍用 `InlineExecutor` |
| `map` / `switch` / `until` / `subline` 的展开 | M4。M2 编译这些构造时仍是 `LINE_UNSUPPORTED`（`lines.md` 5.1） |
| 在控制面调用模型来决定派发顺序 | D10。排序只按配置的优先级和 job id |
| 为熔断率、队列长度、法定半开次数发明默认值 | 配置缺省 = 该控制未武装，不是“用一个合理的数” |
| 死信上的重试、覆盖参数 | M3 厂长。本文件只登记和列出 |
| 新的第三方依赖 | 只用标准库 |

---

## 2. 依赖

| 依赖 | 用到的接口 |
|---|---|
| `Executor`（`lines.md` 3.2） | 只实现，见第 3.1 节的原文 |
| `Runner` | `run`、`get`。`dispatch` 不另造执行入口 |
| `LineRunner` | `tick` 的返回值交给 `dispatch`；`cancel` 用于超出 run 上限的余量；`plan` 用于背压时看后继 |
| `TriggerEngine.process` | bean 触发。本文件在准入允许时调用，不改其去重语义 |
| `Approvals.expire_due` | `wait` / `tick` 里扫审批超时 |
| `Snapshot` | 机器 `runtime.resources`、策略 `resources`、产线 `trigger` |
| `Warehouse` / `mao.jobs` | 读 job 状态与输入 Bean。不改 M1 状态机 |
| `SecretStore.redact` | 死信 detail 写入前脱敏 |
| `mao.base` | `Clock`、`parse_duration`、`utc_iso` |
| `mao.db` | 迁移 `016_scheduler.sql`。可读 `runs` / `jobs`，不另建第二套 run 状态 |

---

## 3. 接口

### 3.1 与 `lines.md` 对齐的 Executor

下面两行与 `lines.md` 第 3.2 节一致。本文件不修改这个 Protocol，也不给它加方法：

```python
class Executor(Protocol):
    def dispatch(self, job_ids: Sequence[str]) -> None   # 把 PENDING 的 job 交给执行者
    def wait(self, timeout_s: float) -> None             # 等到“有 job 状态变化”或超时
```

`ScheduledExecutor` 是这个 Protocol 的一个实现，不是另一个 Protocol。`InlineExecutor` 保持原样。

```python
class ScheduledExecutor:
    def __init__(self, ctx: Context, runner: Runner, scheduler: "Scheduler") -> None
    def dispatch(self, job_ids: Sequence[str]) -> None
    def wait(self, timeout_s: float) -> None
```

`dispatch` 只把 id 登记进队列（第 5.2 节），不在返回前假定 job 已经终态。`wait` 负责泵队列；没有进展时 `clock.sleep(timeout_s)`。因此 `run_to_completion` 会在“tick 提交了 job、但还没执行”的那一步之后进入 `wait`，这是有意的，测试不要假设 `dispatch` 返回时 job 已是 `SEALED`。

### 3.2 `mao.scheduler`

```python
@dataclass(frozen=True)
class Demand:
    cpu: float
    memory_mb: int
    gpu: int
    pool: str                                # 隔舱名；没有匹配时为 "*"

@dataclass(frozen=True)
class DeadLetter:
    job_id: str
    station: str
    error_code: str
    error: dict[str, Any]                    # 已脱敏
    at: str

class Scheduler:
    def __init__(self, ctx: Context, snapshot: Snapshot, runner: Runner) -> None
    def load_config(self, path: Path | None = None) -> None
        # 默认 $FACTORY_ROOT/scheduler.json。缺失：SCHEDULER_CONFIG_MISSING
    def demand(self, station_key: str) -> Demand
    def enqueue(self, job_ids: Sequence[str]) -> None
    def pump(self) -> list[str]
        # 准入并按 job id 升序同步 runner.run。返回本次实际执行的 id。测试的确定性入口
    def tick(self) -> dict[str, list[str]]
        # 一步：expire_due、cron、run 准入下的 bean 触发、登记新死信、pump
        # 返回 {"expired","cron","triggered","pumped","dead"}，各为升序 id 列表
    def wait_change(self, timeout_s: float) -> None
    def is_quarantined(self, bean_id: str) -> bool
    def quarantine(self, bean_id: str, *, by: str, reason: str) -> None
    def release(self, bean_id: str, *, by: str) -> None
    def dead(self, job_id: str) -> DeadLetter
    def list_dead(self) -> list[DeadLetter]              # 按 job_id 升序

def install(scheduler: Scheduler) -> None
def is_quarantined(bean_id: str) -> bool
    # 未 install 时返回 False。cache.md 调用的就是这个函数
```

`JobCache` 可选依赖本模块。没安装调度器时，没有任何 Bean 被隔离，查找语义与未接调度器时相同。

CLI：

```
factory schedule tick [--json]
factory schedule dlq ls [--json]
factory schedule quarantine BEAN_ID --reason TEXT
factory schedule release BEAN_ID
```

`factory line run` 不改，仍是 `InlineExecutor`。要走队列时，调用方自己构造 `ScheduledExecutor` 传给 `run_to_completion`，或只调用 `tick` / `pump`。

---

## 4. 数据格式

### 4.1 迁移 `src/mao/migrations/016_scheduler.sql`

```sql
CREATE TABLE schedule_queue (
  job_id     TEXT PRIMARY KEY,
  enqueued_at TEXT NOT NULL
);
CREATE TABLE dead_letters (
  job_id     TEXT PRIMARY KEY,
  station    TEXT NOT NULL,
  error_code TEXT NOT NULL,
  error      TEXT NOT NULL,
  at         TEXT NOT NULL
);
CREATE TABLE quarantines (
  bean_id TEXT PRIMARY KEY,
  reason  TEXT NOT NULL,
  by      TEXT NOT NULL,
  at      TEXT NOT NULL,
  source  TEXT NOT NULL CHECK (source IN ('manual', 'auto'))
);
CREATE TABLE cron_fires (
  line    TEXT PRIMARY KEY,
  last_at TEXT NOT NULL,
  run_id  TEXT NOT NULL
);
CREATE TABLE breaker_state (
  subject    TEXT PRIMARY KEY,             -- station:<键> | machine:<键> | provider:<host>
  state      TEXT NOT NULL CHECK (state IN ('closed', 'open', 'half_open')),
  changed_at TEXT NOT NULL,
  probes_out INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE rate_buckets (
  name       TEXT PRIMARY KEY,
  tokens     REAL NOT NULL,
  updated_at TEXT NOT NULL                  -- clock.monotonic 的十进制字符串
);
```

不在这些表里存秘密或环境变量。

### 4.2 `scheduler.json`

文件缺失：`load_config` 抛 `SCHEDULER_CONFIG_MISSING`。`Scheduler` 的其他方法在成功 `load_config` 之前不可用。下面 JSON 里出现的正数只说明字段形状；它们不是默认门槛。未武装的控制一律用 `null` 或空列表。

```json
{
  "pools": {"cpu": null, "memory_mb": null, "gpu": null},
  "compartments": [],
  "breakers": [],
  "wip": [],
  "rates": [],
  "run_cap": null,
  "toxic_failures": null,
  "tick_s": null
}
```

| 字段 | 缺省或 `null` 的含义 |
|---|---|
| `pools.<资源>` | `null` 表示该资源不限额。键缺失，而 job 的需求 `> 0`：`SCHEDULER_CAPACITY_UNSET`，该 job 不执行 |
| `compartments` | 空列表表示不划分隔舱，全部走 `"*"` |
| `breakers` | 空列表表示熔断未武装，永远 `closed` |
| `wip` | 空列表表示不做在制品背压 |
| `rates` | 空列表表示没有令牌桶 |
| `run_cap` | `null` 表示不限制新建 run 的数量 |
| `toxic_failures` | `null` 表示不自动隔离 |
| `tick_s` | 只给生产循环用。`tick()` 不读它 |

隔舱：

```json
{"name": "line-a", "match": {"line": "Line:note-pipeline@1"}, "priority": 0,
 "pools": {"cpu": 1, "memory_mb": 512, "gpu": 0}}
```

`match` 只能有 `line` 一个键。`priority` 缺省为 0，只用于排序，不是门槛。

熔断规则必须**同时**具备 `subject`、`min_samples`、`failure_rate_gt`、`open_for`、`probe_limit`。缺任一字段则整份配置 `SCHEDULER_CONFIG_INVALID`，不要补默认。五个值都由调用方写入：`min_samples` 与 `probe_limit` 是整数，`failure_rate_gt` 是比率，`open_for` 交给 `parse_duration`。可选 `reject_rate_gt`：写了才比较退回率；没写就不看退回率。

在制品：`{"station": "Station:upper@1", "max": 1}`。`max` 必须是调用方给出的整数。

令牌桶：`{"name": "fal", "capacity": 10, "refill_per_s": 1}`。两个数都必须出现。

### 4.3 事件

| kind | data |
|---|---|
| `scheduler.admitted` | `{job, compartment}` |
| `scheduler.queued` | `{job, reason}` |
| `scheduler.backpressure` | `{scope, id, reason}` |
| `scheduler.circuit` | `{subject, from, to}` |
| `scheduler.quarantine` | `{bean, by, source}` |
| `scheduler.cron` | `{line, run}` |
| `dlq.recorded` | `{job, error_code}` |

`reason` 是错误码或 `wip` / `breaker` / `quota` / `capacity` 之一，不是自由文本堆栈。

---

## 5. 行为

### 5.1 需求 `demand`

1. 读机器 `runtime.resources`，再用策略 `resources` 里**同名**的键覆盖。策略没写的键保持机器的值。
2. `cpus`：缺省 0。必须是数，否则 `SCHEDULER_RESOURCE_INVALID`。
3. `memory`：缺省 0。字符串必须匹配 `^([0-9]+)(Mi|Gi)$`。`Mi` → 该整数，`Gi` → 该整数 × 1024。无单位的数：`SCHEDULER_RESOURCE_INVALID`，不要猜成字节或 MB。示例里的 `8Gi` 即 8192。
4. `gpu`：缺省或 `none` → 0。整数 → 该整数。其他非空字符串 → 1（一块无名额，只占 `gpu` 计数）。
5. `pool`：策略 `resources.pool` 若是字符串，则需求记在该隔舱名上；否则用第 5.3 节的匹配结果。
6. 不读取策略里看不懂的其他键，也不因此报错（schema 允许自由对象）。

### 5.2 `dispatch` 与 `wait`

`ScheduledExecutor.dispatch`：

1. 丢掉已经 `DEAD` 的 id（不派发），并确保死信表有行（第 5.8 节）。
2. 其余 id 插入 `schedule_queue`，已在队列中则忽略。
3. 返回。不调用 `runner.run`。

`wait`：

1. `Approvals.expire_due()`（调度器持有 `Approvals` 时；没有则跳过）。
2. `pump()`。若本次有 job 进入终态或新的 `AWAITING_APPROVAL`，返回。
3. 否则 `clock.sleep(timeout_s)`。不要用 `time.sleep`。

顺序执行是测试与 M2 的默认定性语义：`pump` 在**当前线程**里按 job id 升序调用 `runner.run`。这会在审批处阻塞，与 `InlineExecutor` 相同。需要在等待审批时做别的事，应直接调用 `approvals.md` 的接口，而不是给 `Executor` 加线程参数。本文件不创建线程。

### 5.3 准入

`pump` 每次选出所有“现在能跑”的 job，再按 id 升序逐个 `runner.run`。选择规则（全部满足才准入）：

1. job 状态是 `PENDING` 或刚提交后的 `SUBMITTED`，且不在死信表。
2. 需求的每种资源：若 `pools` 里该键为 `null`，不检查；若键缺失且需求 `> 0`，不准入，`reason=capacity`，错误码记入 `scheduler.queued` 的同时，若调用方是在 `demand()` 里发现的则抛 `SCHEDULER_CAPACITY_UNSET`。队列路径不抛，只排队，避免一个坏 job 弄停整个 `tick`。`pump` 对这种 job 写 `scheduler.queued` 并不再重试，直到配置被重新 `load_config`。
3. 隔舱：job 所属 run 的 `line` 与某隔舱 `match.line` 相等则用该隔舱；否则用 `"*"`。隔舱 `pools` 的剩余量必须盖住需求。隔舱之间不借用。全局 `pools` 也必须盖住（全局 `null` 则不检查全局）。隔舱各项上限之和大于对应的非空全局上限：`load_config` 时 `SCHEDULER_CONFIG_INVALID`。
4. 熔断：该 job 的工位、机器、或将访问的 provider host 有 `open` 的熔断器时不准入。`half_open` 时已放出的探针数 `< probe_limit` 才准入，并给 `probes_out` 加 1。
5. 在制品：配置了该工位的 `max`，且当前 `SUBMITTED` 且未终态的 job 数 `>= max` 时不准入。后继工位（计划里 `deps` 含本节点的节点）若已达其 `max`，本 job 也不准入，`reason=wip`。单工位 job 没有 run 计划时，只看自己的工位。
6. 令牌桶：机器 `needs.rate_limits` 是非空对象时，它的每个键必须有同名 `rates` 项，否则不准入，`reason=quota`（不抛，以免弄停队列）。对象为空或没有该字段：不检查。取令牌 1 个；不够则不准入。补充按 `clock.monotonic()` 与 `refill_per_s` 计算，测试用 `FakeClock.sleep` / `advance` 推进。

两个隔舱争用有限的全局剩余时：`priority` 高的先，同优先级按 job id 升序。`priority` 缺省 0。

准入后写 `scheduler.admitted`，把需求加进“已占用”直到 `runner.run` 返回。`pump` 返回这些 id 的升序列表。

### 5.4 熔断

未出现在 `breakers` 里的 subject 保持 `closed`，不因为“失败了很多”就打开。

样本：该 subject 最近的终态 job，终态指 `SEALED`、`DEAD`、`REJECTED`。窗口长度是规则里的 `min_samples`。不足 `min_samples` 时不打开。

- 失败率 = `DEAD` 的个数 / 窗口长度。
- 退回率 = `REJECTED` 的个数 / 窗口长度。只在规则写了 `reject_rate_gt` 时计算。
- `失败率 > failure_rate_gt`，或退回率 `> reject_rate_gt`：`closed` → `open`，`changed_at = utc_iso(clock.now())`，写 `scheduler.circuit`。等于阈值不打开。

`open` 期间不准入。`clock.now() >= changed_at + parse_duration(open_for)` 时转到 `half_open`，`probes_out = 0`。半开探针若 `SEALED`，且已完成的探针数达到 `probe_limit` 且没有失败：转回 `closed`。任一探针 `DEAD` 或 `REJECTED`：回到 `open`，重新计 `open_for`。

subject 的写法：`station:` + 工位 Spec 键，`machine:` + 机器 Spec 键，`provider:` + 小写 host。一条规则只管一种 subject。

### 5.5 背压与 run 准入

`run_cap` 不是 `null` 时，非终态 run 的数量 `>= run_cap` 则：

- 不调用 `TriggerEngine.process`；
- 不启动新的 cron run；
- 写 `scheduler.backpressure`，`scope=run`。

`process()` 一次事务可能建出多个 run（`lines.md` 不能在中途停）。若调用后数量超过 `run_cap`，按 run id 降序 `LineRunner.cancel`，`by="scheduler"`，直到不超过上限。`cancel` 只对 `PENDING` / `RUNNING` 有效；刚 `start_run` 的 run 是 `PENDING`，可以取消。

工位在制品见第 5.3 节第 5 步。未配置 `wip` 的工位不限队列长度。

### 5.6 cron

产线集合与 `TriggerEngine` 相同：快照中 `lifecycle == active` 的 Line，按 Spec 键升序。触发器 `on: cron` 的 `schedule` 必须匹配 `^every:[0-9]+(ms|s|m|h|d)$`。时长部分交给 `parse_duration`。其他写法：写 `scheduler.cron` 失败事件，`code=CRON_INVALID`，跳过这一条，不中断其余产线。

到期：`cron_fires` 没有该产线，或 `clock.now() >= last_at + 时长`。第一次没有行就算到期（测试不必先空等一个周期）。

到期后 `start_run`：

- 接口输入不是空：不建 run，事件 `CRON_INPUT_REQUIRED`。cron 只用于没有接口输入的产线（Source 作第一步，见 `connectors.md` 5.5）。
- `start_run` 抛 `RUN_BUDGET_REQUIRED` 或其他 `FactoryError`：记下码，跳过，不发明预算。
- 成功：写 `cron_fires`，`trigger={"on": "cron", "schedule": <原文>, "by": "scheduler"}`，事件 `scheduler.cron`。

本函数不推进 run。推进仍由 `tick` 之外的 `LineRunner.tick` 或调用方的 `run_to_completion` 完成。`tick` 只负责创建。

### 5.7 `tick` 的顺序

在一个逻辑步骤里按这个顺序做，每一步自己的写操作用自己的事务（不要把 `runner.run` 包进事务）：

1. `expire_due`。
2. 若 run 准入允许，`TriggerEngine.process`，然后按第 5.5 节裁掉超额。
3. `cron`（同样受 run 准入）。
4. 扫描新的 `DEAD` job，补死信行。
5. 更新熔断器的 `open` → `half_open` 时钟。
6. `pump`。

返回第 3.2 节的字典。同输入、同配置、同时钟，结果的 id 列表相同。

### 5.8 死信

job 状态变为 `DEAD` 时插入 `dead_letters`。已有行则不更新（第一次的错误为准）。`error` 列是 `redact_obj` 之后的 job.error。没有 error 对象时，`error_code` 为 `DEAD`，`error` 为 `{}`。

`dispatch` 与 `pump` 都不调用死信 job 的 `runner.run`。本模块没有 `retry`。`list_dead` 只读。

### 5.9 隔离

`quarantine` 插入 `source=manual`。已隔离：`QUARANTINE_STATE_INVALID`。`release` 删除行。未隔离就 `release`：同一错误码。

自动隔离：`toxic_failures` 是整数 `k` 时，若某个 Bean 作为输入的最近 `k` 个已终态 job 全部是 `DEAD` 或 `REJECTED`，则插入 `source=auto`，`by=scheduler`。不足 `k` 个不隔离。`toxic_failures` 为 `null` 时不扫描。不把 `k` 写成代码里的常数。

`is_quarantined` 只查 `quarantines` 表。`cache.md` 的 `lookup` 用模块级函数；测试必须先 `install`。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `SCHEDULER_CONFIG_MISSING` | 没有 `scheduler.json` | `path` |
| `SCHEDULER_CONFIG_INVALID` | JSON 不合法，熔断规则缺字段，或隔舱之和超过全局 | `reason` |
| `SCHEDULER_CAPACITY_UNSET` | job 需要的资源在 `pools` 里没有键（既不是数也不是 `null`） | `resource, station` |
| `SCHEDULER_RESOURCE_INVALID` | `memory` / `cpus` 的写法不对 | `station, key, value` |
| `SCHEDULER_QUOTA_UNSET` | 机器写了 `rate_limits` 的名字，但配置没有同名桶（队列路径记入 `scheduler.queued`，不抛） | `name, station` |
| `CRON_INVALID` | `schedule` 不是 `every:<时长>`（写入事件，不抛出循环） | `line, schedule` |
| `CRON_INPUT_REQUIRED` | cron 产线仍有接口输入 | `line` |
| `DLQ_NOT_FOUND` | `dead()` 的 job 不在死信表 | `job` |
| `QUARANTINE_STATE_INVALID` | 重复隔离，或释放未隔离的 Bean | `bean, op` |
| `LINE_UNSUPPORTED` | 本模块不捕获、不重写。产线编译仍由 `lines.md` 抛出 | — |

队列路径把 `SCHEDULER_CAPACITY_UNSET` 与 `SCHEDULER_QUOTA_UNSET` 记进事件而不是抛出，是为了让 `tick` 能继续处理其他 job。`demand()` 被直接调用时仍然抛出。

---

## 7. 验收测试

夹具：`tests/fixtures/registry-m2/` 的脚本工位（一个需求 `cpus: 1`，一个需求 `cpus: 0`）。`scheduler.json` 写在 `tmp_path`，每个测试自己给出数字。规范不检查这些数字等于多少，只检查“配置写了几，行为就按几”。`FakeClock`、`CounterIds`、`LocalProvider`。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m2/test_m2_12_scheduler.py` | `test_executor_methods_match_lines` | `ScheduledExecutor` 有 `dispatch(job_ids)` 与 `wait(timeout_s)`，且可传给 `LineRunner.run_to_completion` |
| | `test_dispatch_does_not_run_inline` | `dispatch` 返回后 job 仍未终态；随后 `pump` 才执行 |
| | `test_config_missing` | 没有文件时 `load_config` 抛 `SCHEDULER_CONFIG_MISSING` |
| | `test_capacity_unset_does_not_default` | `pools` 没有 `cpu` 键、job 需要 CPU 时不准入，事件 reason 为 `capacity`；把 `cpu` 写成 `null` 后同一 job 可以执行 |
| | `test_pool_limits_parallel_admission` | `cpu` 上限小于两个 job 的需求之和时，`pump` 先跑 id 较小的一个；另一个留在队列，直到第一个返回 |
| | `test_compartment_does_not_borrow` | 两个隔舱各有自己的 `cpu`；一边占满不影响另一边，也不借用对方剩余 |
| | `test_wip_backpressure` | 配置了 `max` 且在制品已达该数时，后继 job 不准入；删掉 `wip` 项后不再限制 |
| | `test_breaker_unarmed` | `breakers` 为空时，连续失败也不打开熔断 |
| | `test_breaker_uses_configured_rate` | 测试写入 `min_samples` 与 `failure_rate_gt` 后，比率 `>` 该值才 `open`；等于该值仍 `closed` |
| | `test_half_open_probe_limit` | `open_for` 用 `FakeClock` 推进后进入 `half_open`；放出的 job 数不超过配置的 `probe_limit` |
| | `test_run_cap` | `run_cap` 已满时不调用会增加 run 的触发；`process` 若仍多建，超额的被 `cancel`，`by=scheduler` |
| | `test_cron_every` | `every:1m`：第一次 `tick` 建一个 run；不推进时钟再 `tick` 不建；`advance` 60 秒后再建。非法 schedule 为 `CRON_INVALID` 且不中断其他产线 |
| | `test_cron_requires_empty_interface` | 有接口输入的 cron 产线：`CRON_INPUT_REQUIRED`，无 run |
| | `test_dlq_not_redispatched` | `DEAD` job 出现在 `list_dead` 里；再次 `dispatch` 不调用 `runner.run` |
| | `test_quarantine_visible_to_cache` | `install` 之后 `is_quarantined` 为真；`JobCache.lookup` 不再返回以该 Bean 为输出的命中。未 `install` 时模块函数返回 `False` |
| | `test_toxic_unarmed` | `toxic_failures` 为 `null` 时，连续失败不自动隔离 |
| | `test_memory_unit` | `8Gi` 需求为 8192；`8` 或 `8GiB` 为 `SCHEDULER_RESOURCE_INVALID` |

验收命令：

```
uv run python tools/harness.py pytest tests/acceptance/m2/test_m2_12_scheduler.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 17`。

---

## 8. 错误示范与正确示范

**8.1 不要换掉 Executor**

```python
# ✖ 错误：改 lines.md 的 Protocol，加上优先级参数，或让 dispatch 返回 Future
class Executor(Protocol):
    def dispatch(self, job_ids, *, priority: int) -> list[str]: ...
# ✔ 正确：ScheduledExecutor.dispatch(self, job_ids: Sequence[str]) -> None
#         ScheduledExecutor.wait(self, timeout_s: float) -> None
#         额外能力放在 Scheduler.pump / tick 上
```

**8.2 未配置不是一个隐蔽的默认门槛**

```python
# ✖ 错误：没写熔断率就用 0.5，没写在制品就用 10，没写半开探针就用 1
if failures / n > 0.5: open_breaker()
# ✔ 正确：breakers / wip / toxic_failures / run_cap 缺省或 null → 该控制不生效
#         测试要观察熔断，必须在 scheduler.json 里写下它自己的数
```

**8.3 死信不是再跑一次**

```python
# ✖ 错误：pump 看到 DEAD 就 runner.run，指望这次能过
# ✔ 正确：登记 dead_letters，dispatch 与 pump 都跳过。重试是 M3 的事
```

**8.4 隔舱不能互相借用**

```python
# ✖ 错误：全局还剩 CPU，就把 A 隔舱的 job 算到 B 隔舱的配额上
# ✔ 正确：先看本隔舱剩余，再看全局。两边都够才准入
```

**8.5 cron 不自己发明预算，也不用线程等**

```python
# ✖ 错误：产线没有 cap_usd 时，调度器填一个 1.0 再 start_run
# ✖ 错误：time.sleep(60) 等 cron
# ✔ 正确：捕获 RUN_BUDGET_REQUIRED 并跳过。等待只通过 clock.sleep / FakeClock.advance
```

---

## 9. 风险与注意事项

- **`dispatch` 不执行。** `run_to_completion` 依赖随后的 `wait` 来 `pump`。只调用 `dispatch` 的测试会看到 job 停在队列里，这不是丢 job。
- **顺序 `pump` 会堵在审批上**，与 `lines.md` 对 `InlineExecutor` 的说明相同。调度器不为此开线程：线程与 `FakeClock` 不能一起给出确定结果（R21）。
- **一次 `TriggerEngine.process` 可能超过 `run_cap`。** 裁剪靠 `cancel`，被取消的 run 停在 `CANCELLING` / `CANCELLED`，不是从未存在。调用方要按 id 降序保留较早的 run。
- **令牌桶用 `monotonic`。** `FakeClock.advance` 会推进它。不要在桶的计算里调用 `time.monotonic()`。
- **隔离与缓存的接线**要求进程里调用过 `install`。只构造了 `Scheduler` 却没 `install` 时，`cache.md` 看不到隔离。
- **Windows**：配置路径用 `pathlib`。不启动 shell。资源单位只接受 `Mi` / `Gi`，不要接受会随区域设置变化的写法。
