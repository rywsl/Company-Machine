# daemon：常驻控制面 `factory serve`（M5-04）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M5-04**。
> M1–M4 的每个组件都能单独调用，但没有一个进程把它们串起来长期运行。触发器只在有人调用 `trigger process` 时才工作，治理只在有人调用 `governance tick` 时才推进。结果是：
> - `EpochRouter` 没有调用方，金丝雀永远等不到样本（`plan/drift.md` D-23）；
> - `conversion`、`dead-letter`、`review` 三类唤醒票没有生产者（D-24）。
>
> 本文件定义一个确定性的主循环。它不是 agent，不调用模型（D10）。
> 信号泵只在签核方式为 `review` 时开复核唤醒票，见 `delegation.md` 5.7（M11）。
> **M12/M13 增补**：`daemon.instances`、调度前的 `reconcile`、`TickReport.instances` 见 `instances.md` 5.9，`daemon.ingress` 见 `ingress.md` 3.2（M12）；`run_serve` 的 `mao-agents` 线程见 `agents.md` 5.7（M13）。冲突时以新规范为准。

---

## 1. 目的与范围

**做什么（M5-04）**

- `mao.daemon.Daemon`：按固定顺序推进全部控制回路（5.2 节）。
- 执行器：`InlineJobExecutor`（测试）与 `ProcessExecutor`（生产，每个 job 一个 `factory job exec` 子进程）。
- 每个 epoch 的组件缓存，新 run 经 `EpochRouter` 路由（金丝雀）。
- 信号泵：为转换需求、死信、待复核、安全模式写唤醒票。
- 定时契约 canary（`contract-tests.md` 5.7）。
- 单实例（`serve` 租约）与心跳文件。
- 命令 `factory serve`。
- 对 M2 接口的增补（只增加，不改变）：
  - `Scheduler.tick(pump=..., route=...)`；
  - `Scheduler.admit`；
  - `TriggerEngine.process(route=...)`。

**不做什么**

| 不做 | 归属 |
|---|---|
| 启动厂长或复核者的 CLI | M5-06，`supervisor.md`（独立进程，也可由 `serve` 托管，见 5.7） |
| 指标端点、结构化日志 | M7-01，`observability.md` |
| 多主机 | 不做 |

---

## 2. 依赖

| 依赖 | 用到的接口 |
|---|---|
| `LeaseStore`、`LeaseKeeper`、`Recovery`（`durable.md`） | `serve` 租约；每轮恢复 |
| `Snapshot`、`EpochStore`、`EpochRouter`、`ChangePipeline`（`governance.md`） | 物化快照、路由、`tick` |
| `Runner`、`LineRunner`、`TriggerEngine`（`runner.md`、`lines.md`） | 推进 run |
| `Scheduler`（`scheduler.md`） | 审批超时、cron、死信、准入 |
| `Reconciler`（`reconciler.md`） | 审计 |
| `ContractRunner`（`contract-tests.md`） | `canary` |
| `WakeQueue`、`ConversionStore`（`foreman.md`） | 信号泵 |

---

## 3. 接口

### 3.1 `mao.daemon`

```python
@dataclass(frozen=True)
class ServeConfig:
    tick_s: float                 # 两轮之间的等待
    max_parallel_jobs: int        # 执行器同时运行的 job 上限，>= 1
    audit_every_s: float | None   # None：不跑审计
    canary_every_s: float | None  # None：不跑定时 canary
    docker_timeout_s: float       # 审计里 docker / git 调用的超时（reconciler.md 3）
    supervise: bool               # True：同一进程里托管厂长值守（5.7）

    @classmethod
    def load(cls, root: Path) -> "ServeConfig"
        # 读 $FACTORY_ROOT/factory.json 的 "serve" 段。段缺失 → SERVE_CONFIG_MISSING；
        # 字段缺失或类型不对 → SERVE_CONFIG_INVALID（detail.field）。不补默认值

@dataclass(frozen=True)
class TickReport:
    tick: int                     # 从 1 开始
    at: str
    epochs: tuple[str, ...]       # 本轮推进过的 epoch，按 5.3 的顺序
    recovered: tuple[str, ...]
    triggered: tuple[str, ...]    # 新建的 run id
    advanced: tuple[str, ...]     # 本轮 tick 过的 run id
    dispatched: tuple[str, ...]   # 交给执行器的 job id
    finished: tuple[str, ...]     # 本轮回收的 job id
    governance: tuple[str, ...]   # 状态变化的 ChangeSet id
    audited: bool
    canaries: tuple[str, ...]     # 本轮写入的 contract_runs id
    wakes: tuple[str, ...]        # 本轮新开的唤醒票 id
    def to_json(self) -> dict[str, Any]

class JobExecutor(Protocol):
    def dispatch(self, job_ids: Sequence[str]) -> None
    def running(self) -> list[str]            # 仍在执行的 job id，升序
    def reap(self) -> list[str]               # 自上次调用以来结束的 job id，升序
    def stop(self, timeout_s: float) -> None

class InlineJobExecutor:
    def __init__(self, runner_for: Callable[[str], Runner]) -> None
        # runner_for(job_id) 返回绑定该 job 所属 epoch 的 Runner。dispatch 按 id 升序同步 run；
        # running() 永远为 []；reap() 返回上次 reap 之后 dispatch 过的全部 id

class ProcessExecutor:
    def __init__(self, root: Path, argv_prefix: Sequence[str], *, max_parallel: int,
                 log_dir: Path) -> None
        # 每个 job 启动 [*argv_prefix, "job", "exec", job_id] 子进程，stdout/stderr 写 log_dir/<job_id>.log

class Daemon:
    def __init__(self, ctx: Context, config: ServeConfig, *,
                 executor: JobExecutor | None = None,
                 registry: Path | None = None,
                 epochs: EpochStore | None = None,
                 pipeline: ChangePipeline | None = None) -> None
        # 有治理（factory.json 有 governance.registry）时 epochs 缺省为 EpochStore(ctx)，pipeline 缺省为
        # ChangePipeline(ctx, epochs)；没有治理时必须给 registry（单快照模式），否则 SERVE_CONFIG_INVALID。
        # executor 缺省为 ProcessExecutor（argv_prefix 见 5.6）
    def start(self) -> RecoveryReport           # 取 serve 租约，跑一次恢复
    def tick(self) -> TickReport
    def serve(self, stop: threading.Event) -> None
    def shutdown(self, timeout_s: float) -> None
```

### 3.2 对 M2 接口的增补

```python
class Scheduler:
    def tick(self, *, pump: bool = True,
             route: Callable[[str], LineRunner] | None = None) -> dict[str, list[str]]
        # pump=False：跳过 scheduler.md 5.7 的第 6 步。route 原样传给 TriggerEngine.process 与 cron
    def admit(self, *, running: Sequence[str]) -> list[str]
        # scheduler.md 5.3 的准入，但不执行。running 是正在别处执行的 job，它们的需求算作已占用。
        # 只考虑 epoch 等于本 Scheduler 快照的 PENDING job。写 scheduler.admitted、取令牌、计探针。返回升序 id

class TriggerEngine:
    def process(self, *, route: Callable[[str], LineRunner] | None = None) -> list[str]
        # route(key) 返回新 run 应使用的 LineRunner；key = line + ":" + canonical_json(inputs)。
        # None 时用构造时的 lines（M2 行为）
```

不带新参数调用时，行为与 M2 完全相同。

```python
class WakeQueue:   # foreman.md 3.1 的增补
    def file(self, signal: str, subject: str, payload: Mapping[str, Any], *,
             since: str | None = None) -> Wake
        # 去重范围从“open 票”扩大到“open 或 taken 票”：值守正在处理的票不会被再开一张。
        # since 给出时：同一 (signal, subject) 已有 done 票且其 at >= since，也返回那张票，不新开。
        # since 为 None 时，done 票不参与去重（M3 的调用方行为不变）。
```

`taken` 参与去重是修正 `foreman.md` 5.1 的缺口：原文只查 `open`。信号泵每轮都会检查同一条件，值守取走票之后、处理完之前，泵会再开一张一样的票，厂长被重复唤醒。

### 3.3 命令行

```
factory serve [--once] [--json]
```

- `--once`：`start`、一次 `tick`、`shutdown`，`--json` 时打印 `TickReport`；
- 否则 `serve` 直到收到 SIGINT / SIGTERM（Windows：Ctrl+C / Ctrl+Break）。

退出码：
- 0：正常退出；
- 1：`SERVE_ALREADY_RUNNING`、`LEASE_LOST`；
- 2：用法错误；
- 4：配置或环境错误。

---

## 4. 数据格式

### 4.1 `factory.json` 的 `serve` 段（人维护）

```json
{
  "serve": {
    "tick_s": 2.0,
    "max_parallel_jobs": 4,
    "audit_every_s": 600,
    "canary_every_s": 86400,
    "docker_timeout_s": 60,
    "supervise": false
  }
}
```

这些数是**操作员的配置**，本规范不规定取值，示例里的数只说明形状。`audit_every_s`、`canary_every_s` 写 `null` 表示关闭。

### 4.2 心跳文件 `$FACTORY_ROOT/serve.json`

每轮结束时原子写入（`.part` 再 `os.replace`）：

```json
{"owner": "host:1234:o-0001", "started_at": "...", "tick": 42, "last_tick_at": "...",
 "epochs": ["<commit>"], "running": ["j-0007"]}
```

只用于观察，不用于互斥（互斥靠租约）。

### 4.3 事件

| kind | data |
|---|---|
| `serve.started` | `{owner}` |
| `serve.tick` | `TickReport.to_json()` 的计数版本：每个列表换成长度 |
| `serve.stopped` | `{owner, reason}` |
| `canary.failed` | `{station, result}` |

---

## 5. 行为

### 5.1 `start`

1. `LeaseStore.acquire("serve", owner=ctx.owner)`。False → `SERVE_ALREADY_RUNNING`，`detail` 含持有者与 `expires_at`。
2. 启动 `LeaseKeeper("serve")`。
3. `Recovery(ctx, runners).run_once()`。
4. 写 `serve.started`，返回恢复报告。

`start` 之前调用 `tick` → `SERVE_NOT_STARTED`。

### 5.2 `tick` 的顺序

一轮按下面的顺序执行。每一步用自己的短事务。`runner.run` 不在任何事务里。

1. **租约**：`LeaseKeeper.lost` 已置位 → 抛 `LEASE_LOST`。
2. **恢复**：`Recovery.run_once()`，结果记入 `recovered`。
3. **活跃 epoch**：按 5.3 节求出本轮的 epoch 列表，并为每个 epoch 取得（或复用缓存的）组件：`Snapshot`、`Runner`、`LineRunner`、`Scheduler`、`TriggerEngine`。
4. **active epoch 的调度**：`scheduler.tick(pump=False, route=self.route)`。它会做审批超时、bean 触发、cron、死信登记、熔断时钟。新建的 run 记入 `triggered`。
5. **推进 run**：按 epoch 顺序，对该 epoch 每个未终态的 run（id 升序）调用 `lines.tick(run_id)`，记入 `advanced`。
6. **派发**：剩余容量 = `max_parallel_jobs - len(executor.running())`。按 epoch 顺序：`ids = scheduler.admit(running=executor.running())`，截取不超过剩余容量的前若干个，`executor.dispatch(ids)`，记入 `dispatched`，剩余容量相应减少。
7. **回收**：`executor.reap()`，记入 `finished`。
8. **治理**：有治理时 `pipeline.tick()`，状态变化的 ChangeSet 记入 `governance`。
9. **审计**：`audit_every_s` 不为 None，且距上次审计 `>=` 该值（第一轮总是审计）。有治理时调用 `Reconciler(ctx, epochs).audit_once(timeout_s=docker_timeout_s)`，`audited = True`；没有治理时跳过。
10. **canary**：`canary_every_s` 不为 None 且到期（第一轮不跑）。对 active 快照中被至少一个 GoldenSet 引用的工位（键升序），各调用一次 `ContractRunner(ctx, snap, runner).canary(station)`。结果不是 `pass`：写 `canary.failed`，并 `WakeQueue.file("drift", f"canary:{station}", {"result": result.id})`。结果 id 记入 `canaries`。
11. **信号泵**：5.5 节。
12. **心跳**：写 `serve.json` 和 `serve.tick`。

同样的数据库、配置、时钟和执行器，`TickReport` 逐字节相同。

### 5.3 活跃 epoch 与路由

**单快照模式**（没有治理）：epoch 列表只有 `registry` 快照的 epoch。路由永远返回它的 `LineRunner`。

**治理模式**：

1. 集合 = `{active}` ∪ `{candidate}`（若有）∪ 未终态 run 的 epoch ∪ 未终态 job 的 epoch。
2. 顺序：active 在前，candidate 其次，其余按 commit 升序。
3. 组件按 commit 缓存，快照由 `epochs.materialize(commit)` 得到。某个 commit 已不在集合里时，从缓存中移除。
4. `route(key)`：`commit = EpochRouter(ctx, epochs).choose(key)`，返回该 commit 的 `LineRunner`（不在缓存则当场建立）。

cron 与 bean 触发只经 active epoch 的 `Scheduler` 处理一次。其他 epoch 只推进自己已有的 run 和 job。

### 5.4 执行器

**`InlineJobExecutor`**：

- `dispatch` 按 id 升序，对每个 job 调用 `runner_for(job_id).run(job_id)`。`JOB_LEASED` 不算错误：跳过，不记入 `reap`。
- 其他 `FactoryError` 原样抛出。

**`ProcessExecutor`**：

- `dispatch`：对每个 id 启动 `subprocess.Popen([*argv_prefix, "job", "exec", job_id], stdout=日志, stderr=STDOUT, stdin=DEVNULL)`，不经 shell。同一个 job 已有子进程时跳过。
- `running`：存活子进程对应的 job id。
- `reap`：`poll()` 不为 None 的子进程，关闭日志文件，返回其 job id。退出码不影响守护进程：job 的结果以数据库为准，崩溃的子进程由下一轮恢复处理。
- `stop(timeout_s)`：等待子进程结束，超时后 `kill`。

### 5.5 信号泵

每轮按下面的顺序检查。`WakeQueue.file` 本身按 `(signal, subject)` 去重（`foreman.md` 5.1），所以重复检查不会重复开票。只有新插入的票记入 `wakes`。

| 条件 | `signal` | `subject` | `payload` | `since` |
|---|---|---|---|---|
| `ConversionStore.list(status="open")` 中的每一条 | `conversion` | 需求 id | `{"id": 需求 id}` | 需求的 `updated_at` |
| `Scheduler.list_dead()` 中的每一条 | `dead-letter` | job id | `{"id": job id, "code": error_code}` | job 的 `updated_at` |
| 状态为 `verified`、`len(pipeline.approvals(id)) < reviews_required` 的 ChangeSet | `review` | ChangeSet id | `{"id": cs_id, "need": 差额}` | ChangeSet 的 `stage_started_at` |
| `pipeline.safe_mode()["on"]` 为真 | `other` | `safe-mode` | `{"level": 等级}` | `safe_mode()["since"]` |

`since` 的作用：同一次发生的条件只唤醒一次。例如死信 job 一直是 `DEAD`，厂长处理完（票 `done`）之后，泵不会每轮再开一张；job 被重试后再次进入 `DEAD`，`updated_at` 变了，才会开新票。

“新插入”的判定：`file` 返回的票的 `at` 等于本轮时钟时间，并且它不在本轮之前的 open 列表里。实现可以先列出 open 票再比较。

### 5.6 `factory serve` 的装配

1. `ctx = open_context(args)`（`commands.md` 5.1）。`config = ServeConfig.load(root)`。
2. `argv_prefix = [sys.executable, "-m", "mao", "--root", root, "--provider", provider]`。`--provider local` 时追加 `--local-commands` 与 `--registry`（单快照模式）。子进程继承本进程的环境变量。
3. `daemon = Daemon(ctx, config, executor=ProcessExecutor(root, argv_prefix, max_parallel=config.max_parallel_jobs, log_dir=root / "logs" / "jobs"), registry=...)`。
4. `config.supervise` 为真：同时启动厂长值守（5.7）。
5. 注册信号处理：设置 `stop` 事件。`daemon.serve(stop)`。

`serve(stop)`：`start()`；循环 `tick()`，然后 `stop.wait(config.tick_s)`，直到 `stop` 被置位；最后 `shutdown(timeout_s=60)`。`tick` 抛出 `LEASE_LOST` 时立即停止循环，不再派发。

`shutdown`：不再派发；`executor.stop(timeout_s)`；停 `LeaseKeeper`；释放 `serve` 租约；写 `serve.stopped`。

### 5.7 托管值守

`supervise = true` 时，`factory serve` 在同一进程里另起线程 `mao-supervisor`，运行 `ForemanSupervisor.serve(stop)`（`supervisor.md`）。两者共享 `stop` 事件，但各用各的租约（`serve`、`supervisor`）和各自的 `Context`（SQLite 连接不跨线程共享）。`supervise = false` 时，值守由独立的 `factory foreman supervise` 进程运行。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `SERVE_CONFIG_MISSING` | `factory.json` 没有 `serve` 段 | `path` |
| `SERVE_CONFIG_INVALID` | 字段缺失、类型不对；没有治理又没给 registry | `field` |
| `SERVE_ALREADY_RUNNING` | `serve` 租约被别的 owner 持有 | `owner, expires_at` |
| `SERVE_NOT_STARTED` | `start` 之前调用 `tick` | — |

`LEASE_LOST` 见 `durable.md`。

---

## 7. 验收测试

全部 `FakeClock`、`CounterIds`、`LocalProvider`；最后一个测试用真实子进程。夹具是 `tests/fixtures/registry-m2`（单快照模式）与 `gov_world`（治理模式）。`scheduler.json` 由测试写入，全部控制未武装（`pools` 各项 `null`）。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m5/test_m5_04_daemon.py` | `test_config_required` | 没有 `serve` 段 → `SERVE_CONFIG_MISSING`；缺字段 → `SERVE_CONFIG_INVALID` |
| | `test_single_instance` | 第二个 `Daemon.start` 得到 `SERVE_ALREADY_RUNNING`；第一个 `shutdown` 后可以启动 |
| | `test_trigger_to_sealed` | 贴了标签的 Bean 经若干轮 `tick` 后：run 新建（`triggered`）、job 派发并封存、run `SUCCEEDED` |
| | `test_tick_is_deterministic` | 两个相同的工厂，前 6 轮的 `TickReport` 完全相同 |
| | `test_canary_routing` | 存在 `percent=100` 的金丝雀时，触发器新建的 run 绑定候选 commit |
| | `test_signal_pump` | 打开的转换需求、死信 job 各产生一张唤醒票；第二轮不重复开票；死信票 `done` 之后再跑一轮，同一个 job 不再开票 |
| | `test_review_wake` | `verified` 且复核不足的 ChangeSet 产生 `review` 票，载荷含差额 |
| | `test_start_recovers` | 没有租约的 `RUNNING` job 在 `start` 时被恢复 |
| | `test_process_executor` | `ProcessExecutor` 用真实子进程执行一个 `PENDING` job，`reap` 回收后 job 为 `SEALED` |

```
uv run python tools/harness.py pytest tests/acceptance/m5/test_m5_04_daemon.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 9`。

---

## 8. 错误示范与正确示范

```python
# ✖ 主循环里直接 runner.run：一个长 job 卡住治理、审批超时和审计
for job_id in pending: runner.run(job_id)
# ✔ 只做准入与派发；执行在子进程里，结果以数据库为准
executor.dispatch(scheduler.admit(running=executor.running()))
```

```python
# ✖ 所有 run 都用 active 快照推进，金丝雀上的 run 用错了结构
lines_active.tick(run_id)
# ✔ 按 run 记录的 epoch 取组件
self._workers[run.epoch].lines.tick(run_id)
```

```python
# ✖ 守护进程替厂长做决定：发现死信就自动改 Registry
# ✔ 只写唤醒票（信号泵），判断交给厂长
WakeQueue(ctx).file("dead-letter", job_id, {"id": job_id, "code": code})
```

```python
# ✖ 第二个 serve 进程靠 serve.json 判断有没有人在跑
# ✔ 靠 serve 租约；serve.json 只用于观察
```

---

## 9. 风险与注意事项

- **一轮的时长**：步骤 9、10 可能很慢（审计重跑 validator；canary 跑真实工位）。它们按间隔执行。`tick_s` 是两轮之间的等待，不是一轮的上限。
- **子进程与 Windows**：`ProcessExecutor` 只传参数列表。信号处理在 Windows 上只有 Ctrl+C / Ctrl+Break。服务化（Windows 服务、systemd）不在本规范内。
- **缓存的快照**：epoch 是不可变的，缓存安全。active 变化后，新 run 走新的 epoch，旧 run 继续用旧快照，这就是快照隔离。
- **数据库连接**：守护进程、子进程、命令行各自打开连接。不要把 `Context` 传给别的线程。
