# durable：持久化执行——租约、恢复与单实例（M5-03）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M5-03**。
> 架构文档第 15 节把“持久化执行”放在 M4，`plan/milestones.md` 漏掉了（`plan/drift.md` D-22）。本文件补上。

---

## 1. 目的与范围

M1–M4 的 `Runner.run` 在调用方进程里同步执行一个 job。进程在执行中途退出（崩溃、断电、被杀）时，会留下三个问题：

- job 永远停在 `PROVISIONING` / `RUNNING` / `AWAITING_APPROVAL` / `GATING`；
- 容器还在跑；
- 两个进程可能同时执行同一个 job。

本任务让控制面在任意时刻被杀掉之后，重启就能回到一致状态。

**做什么（M5-03）**

- 租约：`leases` 表与 `LeaseStore`，带后台续约的 `LeaseKeeper`。
- `Runner.run` 先取 job 租约，执行期间续约，结束释放。
- 恢复：`Recovery.run_once` 找出没有有效租约的在途 job，清理沙箱，按工位的重试策略结案。
- `SandboxProvider.reap`：按 job 与 attempt 清理残留沙箱。
- 命令 `factory job exec JOB_ID`：取租约并执行一个已提交的 job（守护进程的子进程用它）。
- `Context.owner`：本进程的租约身份。

**不做什么**

| 不做 | 归属 |
|---|---|
| 主循环、并行派发 | M5-04，`daemon.md` |
| 断点续跑 agent 的会话（重新连上崩溃前的 CLI 进程） | 不做。崩溃的 attempt 一律按失败处理，由重试策略决定 fresh 还是 resume（`runner.md` 5.8） |
| 跨主机的租约 | 不做。租约在同一个 `factory.db` 上生效（多主机见架构文档开放问题 8） |

---

## 2. 依赖

| 依赖 | 用到的接口 |
|---|---|
| `mao.db` | 迁移 `030_leases.sql`；短事务 |
| `mao.base` | `Clock`、`IdSource`、`utc_iso` |
| `Runner`（`runner.md`） | `run`、`get`；重试与状态转换的内部逻辑 |
| `SandboxProvider`（`sandbox.md`） | 新增 `reap` |
| `Approvals`（`approvals.md`） | 结案 attempt 的待批行 |
| `Warehouse` | 判断输出是否已封存 |

---

## 3. 接口

### 3.1 `mao.context`

```python
@dataclass
class Context:
    ...                                    # 原有字段不变
    owner: str = ""                        # 新增，放在最后。Context.open 设为
                                           # f"{socket.gethostname()}:{os.getpid()}:{ids.new('o')}"
```

同一进程里所有组件用 `ctx.owner` 作租约身份。测试用 `CounterIds` 时它是确定的。

### 3.2 `mao.durable`

```python
@dataclass(frozen=True)
class Lease:
    resource: str            # "job:<id>" | "serve" | "supervisor"
    owner: str
    acquired_at: str
    expires_at: str

LEASE_TTL_S = 60.0           # 实现常量，不是业务门槛。续约间隔为 TTL 的三分之一

class LeaseStore:
    def __init__(self, ctx: Context) -> None
    def acquire(self, resource: str, *, owner: str, ttl_s: float = LEASE_TTL_S) -> bool
        # 没有行，或行已过期（expires_at <= now）：写入 owner，返回 True。
        # 已被同一 owner 持有：续期，返回 True。被别的 owner 持有且未过期：False
    def renew(self, resource: str, *, owner: str, ttl_s: float = LEASE_TTL_S) -> bool
        # 只有当前 owner 能续；行不存在或 owner 不同：False
    def release(self, resource: str, *, owner: str) -> bool
        # 只删 owner 匹配的行
    def holder(self, resource: str) -> Lease | None
        # 已过期的行也返回（调用方自己比较 expires_at）
    def valid(self, resource: str) -> Lease | None
        # 未过期的行；否则 None

class LeaseKeeper:
    """后台线程按间隔续约。续约失败时置 lost 事件，持有方必须停止该资源上的工作。"""
    def __init__(self, store: LeaseStore, resource: str, *, owner: str,
                 ttl_s: float = LEASE_TTL_S) -> None
    lost: threading.Event
    def start(self) -> None
    def stop(self) -> None

@dataclass(frozen=True)
class RecoveryReport:
    retried: tuple[str, ...]     # 新 attempt 已排入 PENDING 的 job
    dead: tuple[str, ...]
    sealed: tuple[str, ...]      # 输出其实已封存，补记为 SEALED 的 job
    reaped: int                  # provider.reap 清掉的沙箱数

class Recovery:
    def __init__(self, ctx: Context, runners: Callable[[str], "Runner"]) -> None
        # runners(epoch) 返回绑定该 epoch 快照的 Runner（守护进程按 epoch 缓存）
    def run_once(self) -> RecoveryReport
```

### 3.3 `mao.sandbox`

```python
class SandboxProvider(Protocol):
    ...                                          # 原有方法不变
    def reap(self, job_id: str, attempt: int) -> int
        # 清掉该 attempt 残留的沙箱（容器与 job 专属网络），返回清掉的个数。没有残留返回 0
```

- `DockerProvider.reap`：按 `sandbox.md` 的命名规则找出该 attempt 的容器与网络，`docker rm -f`、`docker network rm`。只按名字精确匹配，禁止用前缀批量删除。
- `LocalProvider.reap`：本进程登记过的子进程若仍存活则终止；其他情况返回 0。

### 3.4 `mao.runner`

`Runner.run(job_id)` 的签名不变，增加以下行为（5.2 节）：先取 `job:<id>` 租约，取不到 → `JOB_LEASED`。

### 3.5 命令行

```
factory job exec JOB_ID [--json]
```

取租约，用该 job 所属 epoch 的快照（`commands.md` 5.2 的规则）构造 `Runner`，调用 `run`，释放租约。

| 结果 | 退出码 | `--json` |
|---|---|---|
| `SEALED`，或停在 `AWAITING_APPROVAL`（propose 阶段等待批准） | 0 | `dump_job(job)` |
| `DEAD` | 1 | `dump_job(job)` |
| 租约被别人持有 | 1 | `{"error": {"code": "JOB_LEASED", ...}}` |
| job 已是终态 | 0 | `dump_job(job)`，不重跑 |

---

## 4. 数据格式

### 4.1 迁移 `src/mao/migrations/030_leases.sql`

```sql
CREATE TABLE leases (
  resource     TEXT PRIMARY KEY,
  owner        TEXT NOT NULL,
  acquired_at  TEXT NOT NULL,
  expires_at   TEXT NOT NULL
);
```

时间是 README 3.2 的 UTC 字符串。按字符串比较等价于按时间比较。

### 4.2 事件

| kind | data |
|---|---|
| `lease.taken_over` | `{resource, from, to}`：接管了一条过期租约 |
| `job.recovered` | `{job, attempt, action, code}`；`action` 为 `retry` / `dead` / `sealed`；`code` 为 `CONTROL_PLANE_RESTART` |

---

## 5. 行为

### 5.1 租约

1. `acquire` 在一个 `BEGIN IMMEDIATE` 事务里读行并写入，两个进程同时取时只有一个成功。
2. 时间一律来自 `ctx.clock`。`expires_at = utc_iso(now + ttl_s)`。
3. 接管过期行时写 `lease.taken_over`。
4. `LeaseKeeper` 的线程名为 `mao-lease`。循环是 `lost` 未置位时 `stop_event.wait(ttl_s / 3)`，再 `renew`。这是 README 3.4 允许的后台线程例外，同 `CtlServer`：用 `Event.wait`，不用 `time.sleep`；时间戳仍取 `ctx.clock`。`renew` 返回 False 或抛错：置 `lost`，记 `logging` 的 `error`，线程退出。

### 5.2 `Runner.run` 与租约

1. job 已是终态：直接返回，不取租约（与 M1 行为相同）。
2. `LeaseStore.acquire(f"job:{job_id}", owner=ctx.owner)`。False → `JOB_LEASED`，`detail.owner` 为持有者，不改 job。
3. 启动 `LeaseKeeper`，执行 M1–M4 的全部逻辑。`lost` 置位后，执行在下一个检查点停下：
   - 检查点包括：准备沙箱前、启动操作工前、收货前、封存前；
   - 停下时不封存、不改 job 状态，然后返回当前 job。
   - 别的进程会接管这个 job，本进程不能再写它。
4. `finally`：停止 `LeaseKeeper`，`release`。

`run` 嵌套调用自己时（重试的下一个 attempt），复用已持有的租约，不重复取。

### 5.3 `Recovery.run_once`

1. 列出状态属于 `{PROVISIONING, RUNNING, AWAITING_APPROVAL, GATING}` 的 job，按 id 升序。
2. 跳过 `LeaseStore.valid(f"job:{id}")` 不为 None 的 job：有人正在执行它。
3. 对其余每个 job，先 `acquire` 它的租约（取不到就跳过，说明别的恢复者抢先了），然后按顺序：
   1. `ctx.provider.reap(job.id, job.attempt)`，累加 `reaped`。
   2. 状态为 `GATING`，且该 job 每个非可选输出端口在 `seals` 里都有 `job_id` 等于本 job 的行：补记 `SEALED`（与 `runner.md` 5.7 的终态写法相同），`action = "sealed"`。
   3. 否则：该 attempt 以 `FAILED` 结案，`error = {"code": "CONTROL_PLANE_RESTART", "message": ..., "detail": {"state": 原状态}}`。按工位 `limits.retries.failed` 与已用次数决定：还有次数 → 新 attempt 进入 `PENDING`，`action = "retry"`；否则 `DEAD`，`action = "dead"`。重试模式（fresh / resume）沿用 `runner.md` 5.8。
   4. 该 attempt 仍是 `pending` 的审批行改为 `denied`，`decided_by = "recovery"`，`note = "CONTROL_PLANE_RESTART"`。不写 `approval.decided` 以外的新事件。
   5. 写 `job.recovered`，`release` 租约。
4. 返回报告，各列表升序。

恢复不重新执行 job。新 attempt 由之后的调度派发（`daemon.md`）。

### 5.4 `job exec`

1. 读 job（`JOB_NOT_FOUND` 沿用 `runner.md`）。
2. 用 job 的 `epoch` 选快照（`commands.md` 5.2：有治理时 `EpochStore.materialize(job.epoch)`，否则 `--registry`）。
3. 调用 `Runner.run`。租约在 `run` 内部取得（5.2 节）。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `JOB_LEASED` | job 的租约由别的 owner 持有且未过期 | `job, owner, expires_at` |
| `CONTROL_PLANE_RESTART` | 恢复时把崩溃留下的 attempt 记为失败（写在 `job.error`，不抛出） | `state` |
| `LEASE_LOST` | 持有方发现续约失败（写日志；需要抛出时用这个码） | `resource, owner` |

---

## 7. 验收测试

全部 `LocalProvider` 与 `tests/fixtures/registry-m2`。最后一个测试真的启动并杀掉一个子进程。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m5/test_m5_03_durable.py` | `test_lease_exclusive_until_expiry` | A 取得后 B 取不到；时钟越过 TTL 后 B 接管并写 `lease.taken_over`；A 续约失败 |
| | `test_release_only_by_owner` | 非持有者 `release` 返回 False，行还在 |
| | `test_runner_refuses_leased_job` | 别的 owner 持有 job 租约时 `Runner.run` 抛 `JOB_LEASED`，job 状态不变 |
| | `test_runner_releases_lease` | `Runner.run` 跑完后该 job 没有租约行 |
| | `test_recovery_retries_or_dead` | 没有租约的 `RUNNING` job：工位还有失败重试次数时排入新 attempt；没有次数时 `DEAD`，`error.code == "CONTROL_PLANE_RESTART"` |
| | `test_recovery_skips_live_lease` | 租约有效的在途 job 不被恢复触碰 |
| | `test_recovery_reaps_sandbox` | 恢复对每个被结案的 attempt 调用 `provider.reap(job, attempt)` |
| | `test_job_exec_command` | `factory job exec` 执行一个 `PENDING` job，退出 0，`state == "SEALED"`；对终态 job 再执行一次不重跑 |
| | `test_killed_process_is_recovered` | 子进程 `factory job exec` 执行一个会睡眠的 agent job，`RUNNING` 时把子进程杀掉；时钟越过 TTL 后 `Recovery` 把它结案为重试或死信 |

```
uv run python tools/harness.py pytest tests/acceptance/m5/test_m5_03_durable.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 9`。

---

## 8. 错误示范与正确示范

```python
# ✖ 先查再写，两个进程都以为自己拿到了租约
if not db.query("SELECT 1 FROM leases WHERE resource = ?", (r,)):
    db.query("INSERT INTO leases ...")
# ✔ 同一个 BEGIN IMMEDIATE 事务里读和写
```

```python
# ✖ 恢复时把在途 job 一律当作崩溃，正在别的进程里运行的 job 也被结案
for job in in_flight: fail(job)
# ✔ 只处理没有有效租约、且自己抢到租约的 job
```

```python
# ✖ 续约线程用 time.sleep，测试没法控制，静态规则也不允许
time.sleep(20)
# ✔ stop_event.wait(ttl_s / 3)；时间戳仍来自 ctx.clock
```

```python
# ✖ reap 按前缀删除，误删别的 job 的容器
docker rm -f $(docker ps -q --filter name=mao-)
# ✔ 只按该 attempt 的确切名字删除
```

---

## 9. 风险与注意事项

- **时钟**：租约用 `ctx.clock`。多个进程共享一个数据库时，各自的系统时钟要基本一致；偏差应小于 TTL 的三分之一。单机部署没有这个问题。
- **TTL 与长 job**：TTL 不必覆盖 job 的总时长，靠续约维持。进程被挂起（例如笔记本休眠）超过 TTL，醒来后续约失败、自行停下，这是预期行为。
- **Windows**：杀进程用 `Popen.kill()`。fake agent 的孙进程可能比子进程活得久。测试只断言数据库状态，不断言进程表。
- **SQLite 锁**：`BEGIN IMMEDIATE` 可能等待。`Database` 已有超时设置（`foundation.md`），不要在持有事务时做 I/O。
