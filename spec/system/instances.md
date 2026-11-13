# instances：机器实例的生命周期——once、pooled、resident（M12）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M12-01 至 M12-04**。设计说明见 `plan/design-resident.md`。
> 现状（设计文档第 1 章）：`runtime.instance` 没有任何代码读取，`pooled` 不存在；容器命令固定为 `sleep`，服务型机器在 Docker 下没有服务进程；没有不属于 job 的常驻机器，也没有让机器主动投递事件的办法。
> 本文件取代 `gateway.md` 5.1 第 3、4 条（实例模式）与第 9 章“pooled 容器的残留”中关于复用的部分；`gateway.md` 其余部分不变。

---

## 1. 目的与范围

**做什么**

- `runtime.instance` 的三种模式在所有机器形态上生效：
  - `once`：每个 attempt 一个容器，结束即销毁。这是现有行为，也是缺省。
  - `pooled`：按工位复用容器，job 之间执行 `reset`。
  - `resident`：每个工位一个常驻容器，由 `factory serve` 维护，不属于任何 job。
- `runtime.command`：容器的主进程。
- 实例表、租借、拷入拷出、`reset`、健康检查、退避重启、恢复。
- 调度准入按实例容量限制并发。
- `factoryctl emit`：常驻实例把文件封存为工位输出端口的 Bean。
- 命令行 `factory instance`、控制台页面、厂长的只读 MCP 工具。

**不做什么**

| 不做 | 原因 |
|---|---|
| compose 机器 | 仍是 `COMPOSE_UNSUPPORTED`。多容器编排另立规范 |
| 一个实例同时服务多个 job | 挂载在建容器时固定。实例一次只借给一个 job，`pooled` 靠 `pool_size` 并行 |
| 把机器端口发布到宿主 | 入站只经 `ingress.md` 的网关 |
| 跨主机调度实例 | 单机 |

---

## 2. 依赖

| 依赖 | 用途 |
|---|---|
| `SandboxProvider`、`SandboxSpec`、`Sandbox`（`sandbox.md`） | 建容器、`exec`、关闭；本文件增补 3.1 |
| `Runner`（`runner.md`） | job 的执行；本文件增补 5.3 |
| `ServiceGateway.run_recipe`（`gateway.md`） | 服务型配方；增补 `reset` 参数 |
| `Scheduler.admit`、`pump`（`scheduler.md`） | 准入；增补 5.4 |
| `Daemon.tick`（`daemon.md`） | 新步骤 5.5 |
| `register_check_egress`、`close_egress`、`credentialed_env`（`capabilities.md` 5.1） | 实例主体 |
| `Gate.inspect`、`Warehouse`（`gate.md`、`warehouse.md`） | emit 的出厂检验与封存 |
| `WakeQueue.file`（`foreman.md` 3.1） | `crashed` 的唤醒票 |
| `CtlServer`（`factoryctl.md`） | 实例主进程的控制通道 |

---

## 3. 接口

### 3.1 `mao.sandbox` 的增补

```python
@dataclass(frozen=True)
class SandboxSpec:
    ...                                    # 原有字段不变
    command: tuple[str, ...] = ()          # 主进程；空元组表示沿用 sleep

class Sandbox(Protocol):
    ...
    def alive(self) -> bool                # 主进程（Docker 下为容器）是否还在

class SandboxProvider(Protocol):
    ...
    def attach(self, spec: SandboxSpec) -> Sandbox
        # 接上一个已经存在的同名沙箱，不新建。Docker：容器不存在或没有运行 → INSTANCE_LOST
        # LocalProvider：只建立路径映射，不启动进程
```

### 3.2 `mao.instances`

```python
Mode = Literal["once", "pooled", "resident"]

@dataclass(frozen=True)
class InstanceConfig:
    mode: Mode
    pool_size: int                         # pooled 为 runtime.pool_size（缺省 1）；其余为 1
    reset: tuple[str, ...]                 # 仅 pooled
    command: tuple[str, ...]               # runtime.command；没有为 ()
    health: tuple[str, ...]                # runtime.health.run；没有为 ()
    health_timeout_s: float                # runtime.health.timeout_s，缺省 30
    restart_max: int                       # runtime.restart.max，缺省 5；仅 resident
    emit_per_min: int                      # runtime.emit_per_min，缺省 60；仅 resident

def instance_config(machine: Mapping[str, Any]) -> InstanceConfig   # 规则见 4.1；违反 → INSTANCE_INVALID

@dataclass(frozen=True)
class InstanceRecord:
    id: str                                # "in-" + ids 生成的后缀
    station: str                           # 工位 Spec 键
    mode: str                              # "pooled" | "resident"
    digest: str                            # 4.3 的配置摘要
    state: str                             # starting | ready | busy | failed | crashed | stopped
    sandbox: str                           # 沙箱名 "mao-inst-<id>"
    job: str | None                        # 正在借用的 job id
    jobs_served: int
    failures: int                          # 连续失败次数（resident）
    next_restart_at: str | None
    started_at: str
    updated_at: str
    error: dict[str, Any] | None           # {"code", "message"}，打码后

@dataclass
class Lease:
    instance: InstanceRecord
    sandbox: Sandbox                       # exec 的 env 自动并入 job_env（5.3 第 3 步）
    job_env: Mapping[str, str]
    host_dir: Path                         # $FACTORY_ROOT/instances/<id>
    def ctl_dir(self) -> Path              # host_dir / "job" / "ctl"
    def stage_in(self, attempt_dir: Path) -> None
    def stage_out(self, attempt_dir: Path) -> None

@dataclass(frozen=True)
class InstancesConfig:                     # factory.json 的 instances 段（4.4）
    health_every_s: float = 30.0
    idle_ttl_s: float = 600.0
    reset_timeout_s: float = 120.0
    backoff_base_s: float = 5.0
    backoff_max_s: float = 600.0

class Instances:
    def __init__(self, ctx: Context, snapshot: Snapshot, *,
                 config: InstancesConfig | None = None, wakes: WakeQueue | None = None,
                 runner_for: Callable[[], Runner] | None = None) -> None
        # config 为 None 时读 factory.json 的 instances 段（4.4）；wakes 为 None 时用 WakeQueue(ctx)
    def acquire(self, job: Job, spec: SandboxSpec) -> Lease          # 5.2；仅 pooled、resident
    def release(self, lease: Lease, *, destroy: bool = False) -> None  # 5.2 第 5 步
    def ready(self, station: str) -> bool                            # 准入用（5.4）
    def reconcile(self, snapshot: Snapshot | None = None) -> dict[str, list[str]]
        # 5.5；snapshot 为 None 时用构造时的快照。键 started/stopped/failed/crashed，值为升序实例 id
    def recover(self) -> list[str]                                   # 5.6；返回被处理的实例 id，升序
    def list(self, *, station: str | None = None, state: str | None = None) -> list[InstanceRecord]  # id 升序
    def restart(self, ref: str) -> InstanceRecord                    # ref 是实例 id 或工位键
    def stop(self, ref: str) -> InstanceRecord
    def close(self) -> None
        # 停止本对象启动的控制通道，关闭本对象持有、而行已经 stopped 的沙箱。不停止仍在运行的实例

def instance_spec(ctx: Context, snapshot: Snapshot, station: str, instance_id: str) -> SandboxSpec   # 4.2
```

### 3.3 emit

```python
# mao.runner
class Runner:
    def adopt(self, station: str, out_dir: Path, *, labels: Mapping[str, str] | None = None,
              by: str) -> Job
        # 5.7 第 6 步：为一份已经放好的输出建 job 并走出厂检验与封存，不开沙箱、不跑操作工

# mao.instances
def emit(runner: Runner, *, station: str, port: str, source: Path, instance_id: str | None,
         labels: Mapping[str, str] | None = None, trigger: Mapping[str, Any] | None = None) -> str
    # 5.7；返回 Bean id。ingress.md 的 deliver: bean 也调用它（instance_id 为 None，见 ingress.md 5.2）
```

`factoryctl emit --port P --dir D [--label K=V]...`：

- 控制通道取环境变量 `FACTORY_INSTANCE_CTL`，没有时退出 4。
- 请求 `{"op": "emit", "port": P, "dir": D, "labels": {K: V}}`。`D` 是 Bean 载荷所在的目录，布局与 job 的 `/factory/out/<端口>/` 相同，必须在 `/factory/instance/out/` 下。
- 成功时 stdout 是 `{"bean": "<id>", "job": "<id>"}`，退出 0。ctl 返回错误时退出 1，stderr 一行：错误码，有 `detail.reason` 时后接一个空格和它（例如 `EMIT_INVALID port`）。

### 3.4 命令行

```text
factory instance list [--station S] [--state X] [--json]
factory instance restart <实例 id | 工位键>
factory instance stop <实例 id | 工位键>
```

`list` 按 `commands.md` 的 JSON 规则输出 `InstanceRecord`。`restart` 把 `failures` 清零，`state` 改为 `failed` 且 `next_restart_at` 为现在，下一次 `reconcile` 立即重建；`pooled` 实例直接销毁。`stop` 销毁并置 `stopped`；被 `stop` 的 `resident` 实例在其工位下一次被 `restart` 之前不再重建（`error.code = "INSTANCE_STOPPED"` 作为标记）。

### 3.5 MCP 与控制台

- 厂长的只读工具 `instance_list`（参数 `station`、`state`，都可选），返回 `list` 的 JSON。
- 控制台 `/instances`、`/api/instances`，只读，字段同上。

---

## 4. 数据格式

### 4.1 Machine 的 `runtime` 增补

```yaml
runtime:
  image: repo@sha256:...
  instance: resident          # once | per_job | pooled | resident，缺省 once
  command: [python3, /app/listen.py]
  health: {run: [python3, /app/health.py], timeout_s: 10}
  restart: {max: 5}           # 仅 resident
  emit_per_min: 60            # 仅 resident
  pool_size: 2                # 仅 pooled
  reset: [sh, /app/reset.sh]  # pooled 必须有；resident 不能有
```

`instance_config` 的规则（违反任一条 → `INSTANCE_INVALID`，`detail.reason` 是下表的原因）：

| 条件 | `reason` |
|---|---|
| `instance` 不在 `once`、`per_job`、`pooled`、`resident` 中 | `instance` |
| `pooled` 没有 `reset` | `reset-missing` |
| `resident` 有 `reset` | `reset-resident` |
| `pool_size` 出现在非 `pooled` 机器上，或不是 `>= 1` 的整数 | `pool_size` |
| `restart`、`emit_per_min` 出现在非 `resident` 机器上 | `resident-only` |
| `command`、`health.run` 不是非空字符串列表 | `argv` |
| `reset` 是含引号的字符串（与 `gateway.md` 5.1 相同） | `reset-quote` |

`reset` 是字符串时按空白拆分。`per_job` 当作 `once`。机器 schema 增加这些字段（`additionalProperties` 本来就为真，schema 只登记类型）。

### 4.2 实例的沙箱

`instance_spec(ctx, snapshot, station, id)`：

| 项 | 值 |
|---|---|
| `name` | `mao-inst-<id>` |
| `image` | 机器 `runtime.image` |
| `command` | `runtime.command` |
| 挂载 | `instances/<id>/job/in` → `/factory/in`（只读）；`job/out` → `/factory/out`；`job/ctl` → `/factory/ctl`；`instances/<id>/main` → `/factory/instance`；`tools/factory` → `/opt/factory`（只读）；仅 `resident`：`instances/state/<工位名>` → `/factory/state` |
| 工作区 | Docker 且 `form: workspace`：命名卷 `mao-inst-<id>-ws`；否则 `instances/<id>/workspace` → `/workspace` |
| `env` | `FACTORY_INSTANCE=<id>`、`FACTORY_STATION=<工位键>`、`FACTORY_INSTANCE_CTL=/factory/instance/ctl`；仅 `resident`：`FACTORY_STATE=/factory/state`；策略出网时并上实例主体的凭据（5.8） |
| `network`、资源 | 与 `Runner` 为该工位构造的一样（`capabilities.md` 5.0、5.6） |

`instances/<id>/main/` 下建 `ctl/`、`out/`、`inbox/`。`<工位名>` 是 `metadata.name`，不含版本。

### 4.3 配置摘要

`digest = sha256(canonical_json({"station": 工位 spec, "machine": 机器 spec, "policy": 策略 spec}))` 的十六进制。三者任一变化，摘要就变。

### 4.4 `factory.json` 的 `instances` 段（人维护，可省略）

```json
"instances": {"health_every_s": 30, "idle_ttl_s": 600, "reset_timeout_s": 120,
              "backoff_base_s": 5, "backoff_max_s": 600}
```

不是正数 → `SERVE_CONFIG_INVALID`（`field`）。

### 4.5 迁移 `src/mao/migrations/039_instances.sql`

```sql
CREATE TABLE instances (
  id              TEXT PRIMARY KEY,
  station         TEXT NOT NULL,
  mode            TEXT NOT NULL CHECK (mode IN ('pooled', 'resident')),
  digest          TEXT NOT NULL,
  state           TEXT NOT NULL CHECK (state IN ('starting','ready','busy','failed','crashed','stopped')),
  sandbox         TEXT NOT NULL,
  job             TEXT,
  jobs_served     INTEGER NOT NULL DEFAULT 0,
  failures        INTEGER NOT NULL DEFAULT 0,
  next_restart_at TEXT,
  idle_since      TEXT,
  started_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  error           TEXT
);
CREATE INDEX instances_station ON instances(station, state);
CREATE TABLE emits (
  id       TEXT PRIMARY KEY,               -- emit job id
  instance TEXT,                           -- 入站投递为 NULL
  station  TEXT NOT NULL,
  port     TEXT NOT NULL,
  bean     TEXT NOT NULL,
  trigger  TEXT NOT NULL,                  -- canonical JSON
  at       TEXT NOT NULL
);
CREATE INDEX emits_station_at ON emits(station, at);
```

### 4.6 事件

| kind | data |
|---|---|
| `instance.started` | `{instance, station, mode}` |
| `instance.leased` | `{instance, job}` |
| `instance.released` | `{instance, job, reset}`（`reset` 为是否执行了 reset） |
| `instance.failed` | `{instance, station, code, reason}` |
| `instance.crashed` | `{instance, station, failures}` |
| `instance.stopped` | `{instance, station, reason}`，`reason` ∈ `idle`、`replaced`、`removed`、`manual`、`reset`、`timeout`、`recovered`、`restarted` |
| `instance.emit` | `{instance, station, port, bean, job}`（入站投递时 `instance` 为 `null`） |
| `job.adopted` | `{job, station, by}` |

---

## 5. 行为

### 5.1 主进程

`DockerProvider.build_run_argv`：`spec.command` 为空时与 `sandbox.md` 5.2 完全相同。非空时，镜像之后的部分换成：

```text
/bin/sh, -c, 'mkdir -p /tmp/home && exec "$@"', sh, *spec.command
```

`Docker` 的 `alive()`：`docker inspect --format {{.State.Running}} <name>` 输出 `true`。命令失败也返回假，不抛错。

`LocalProvider.open`：`spec.command` 非空时，按 `sandbox.md` 5.6 映射 argv、env、工作目录后用 `subprocess.Popen` 启动，stdout、stderr 写到 `root/tmp/sandboxes/<name>/main.log`。`alive()`：打开它的对象上为 `Popen.poll() is None`；没有命令、或由 `attach` 得到的对象恒为真。`close` 终止主进程（先 `terminate`，5 秒后 `kill`）。

`attach`：Docker 下检查容器在运行，返回绑定到这个名字的沙箱，`close` 仍是 `docker rm -f`；LocalProvider 下等于不启动进程的 `open`。

`once` 机器也使用 `runtime.command`：`Runner._spec` 把它放进 `SandboxSpec.command`。服务型机器靠它启动服务；没有它时服务不会运行，`gateway.md` 5.4 的健康检查报 `SERVICE_UNHEALTHY`。

### 5.2 租借（pooled、resident）

`acquire(job, spec)`，`spec` 是 `Runner` 为这个 attempt 构造的 `SandboxSpec`：

1. `cfg = instance_config(机器)`；`once` → `INSTANCE_INVALID`（`reason = "once"`），调用方不该来这里。
2. 在一个事务里选实例：
   - `pooled`：该工位 `state = 'ready'`、`digest` 等于当前摘要的实例，id 升序取第一个，置 `busy`。没有时，若该工位未 `stopped` 的实例数 `< pool_size`，插入一行 `starting`；否则 `INSTANCE_BUSY`。
   - `resident`：该工位唯一的 `ready` 实例，置 `busy`；没有 → `INSTANCE_NOT_READY`。
3. 新插入的 `pooled` 实例：建目录，`provider.open(instance_spec(...))`，有 `health` 时执行健康检查（5.5 第 4 步），成功置 `busy` 并写 `instance.started`；失败销毁（`stopped`，`reason = "reset"` 之外的失败写 `instance.failed`），抛 `INSTANCE_UNHEALTHY`。已有实例用 `provider.attach`；`INSTANCE_LOST` 时把该行置 `stopped`（`reason = "recovered"`）并回到第 2 步重选一次。
4. `job` 列写 job id，写 `instance.leased`。返回 `Lease`：`job_env` 是 `spec.env`（含 `FACTORY_JOB_ID`、`FACTORY_ATTEMPT`、出网凭据与秘密），`sandbox` 的 `exec` 把 `job_env` 并入调用方给的 `env`（调用方的键优先）。
5. `release(lease, destroy)`：
   - 清空 `job/in`、`job/out`、`job/ctl`，`job` 列置空，`jobs_served + 1`，写 `instance.released`。
   - `destroy` 为真（exec 超时，Docker 已 kill 容器）→ 关闭沙箱，`stopped`，`reason = "timeout"`。
   - `pooled`：执行 `reset`（`exec`，超时 `reset_timeout_s`，输出写到 `instances/<id>/logs/reset-<n>.log`）。非 0 或超时 → 关闭，`stopped`，`reason = "reset"`，写 `instance.failed`（`code = SERVICE_FAILED`）。成功后有 `health` 时再做健康检查，失败同样销毁。都通过置 `ready`，`idle_since` 为现在。
   - `resident`：置 `ready`，不 reset。
   - `release` 自身的失败不改变 job 的结果。

### 5.3 `Runner` 的接线

`Runner._provision_execute` 与 `_gate_recovery` 在打开沙箱处分支：

1. `instance_config(机器).mode == "once"`：与现在相同（`provider.open`，`finally` 中 `close`）。
2. 否则 `lease = instances.acquire(job, prepared.spec)`，`lease.stage_in(attempt_dir)`：把 attempt 目录的 `in/`、`ctl/` 原样拷到 `job/in`、`job/ctl`，清空 `job/out`。
3. `CtlServer` 轮询 `lease.ctl_dir()`，不是 attempt 目录的 `ctl/`。`exec` 用 `lease.sandbox`。
4. `exec` 之后、收货之前 `lease.stage_out(attempt_dir)`：把 `job/out` 拷回 attempt 目录的 `out/`（先清空目标），把 `job/ctl` 中 attempt 目录没有的文件拷回 `ctl/`。之后的收货、回执、出厂检验都读 attempt 目录，不变。
5. `finally`：`instances.release(lease, destroy=exec 超时)`。
6. 服务型机器调用 `gateway.run_recipe(..., reset=False)`：网关不再自己执行 `reset`，由第 5.2 节负责。`run_recipe` 新增关键字参数 `reset: bool = True`，直接调用网关时行为不变（M4 的测试依赖它）。
7. `acquire` 抛的 `INSTANCE_BUSY`、`INSTANCE_NOT_READY` 按 M1 的失败规则处理（会消耗 attempt）。正常情况下准入（5.4）保证不会发生。

`Runner` 构造参数增加 `instances: Instances | None = None`，保存为公开属性 `runner.instances`。为 `None` 而工位不是 `once` 时，job 在静态检查中 `DEAD`，`INSTANCE_UNSUPPORTED`。`open_runner(ctx, snapshot)` 构造 `Instances(ctx, snapshot, runner_for=lambda: runner)` 并传入；`factory serve`、`factory job exec` 等经 `open_runner` 装配的入口因此都有它。

静态检查（`runner.md` 5.3）增加一行：`instance_config` 抛错 → `INSTANCE_INVALID`，job `DEAD`。

### 5.4 准入

`Scheduler.admit` 与 `pump` 的选择规则增加一条。对 `pooled`、`resident` 工位的 job：

- 该工位“占用中”的 job 数 = 状态在 `PROVISIONING`、`RUNNING`、`AWAITING_APPROVAL`、`GATING` 的 job 数，加上本轮已经选中的同工位 job 数；
- `pooled`：占用中 `>= pool_size` 时不选；
- `resident`：占用中 `>= 1`，或 `instances.ready(工位)` 为假时不选。

没被选中的 job 留在 `PENDING`，不写事件，不消耗 attempt。`Scheduler` 构造参数增加 `instances: Instances | None = None`；为 `None` 时这条规则不生效。

### 5.5 常驻实例的维护：`reconcile`

`desired` = active 快照中满足下列条件的工位键，升序：`metadata.lifecycle` 缺省或为 `active`，机器 `instance_config(...).mode == "resident"`。机器的 `runtime.instance` 写的是 `resident`、但 `instance_config` 抛错的工位：写 `instance.failed`（`instance` 为 `null`，`code = INSTANCE_INVALID`），跳过；同一工位同一摘要只写一次（本对象内存里记住）。其他模式的配置错误由 `Runner` 的静态检查报告，这里不管。

按工位键升序，对每个工位：

1. **没有未停止的实例**：工位上次被 `stop` 标记（3.4）则跳过；否则插入 `starting`，建目录，`open(instance_spec(...))`，健康检查通过置 `ready`，写 `instance.started`，`failures = 0`。打开或健康检查失败 → 走第 3 步的失败处理。
2. **摘要变了**：先按第 1 步为新摘要建一个实例；旧实例 `job` 为空时关闭并 `stopped`（`reason = "replaced"`），否则等下一轮。
3. **失败处理**：`failures + 1`，置 `failed`，`next_restart_at = now + min(backoff_base_s * 2^(failures-1), backoff_max_s)`，写 `instance.failed`；`failures >= restart_max` 时改为 `crashed`，写 `instance.crashed`，`WakeQueue.file("instance", f"instance:{工位}", {"instance": id, "code": code})`。`crashed` 不再自动重启，只能由 `factory instance restart` 恢复。
4. **健康检查**：距上次检查 `>= health_every_s`（第一轮总是检查），对 `ready` 与 `busy` 实例：`sandbox.alive()` 为假 → 失败（`code = INSTANCE_DIED`）；有 `health` 时在沙箱里执行，非 0 或超时 → 失败（`code = INSTANCE_UNHEALTHY`）。失败的实例先关闭沙箱。`busy` 实例失败时，借用它的 job 由 M1 规则自然失败（exec 报错或超时）。
5. **到期重启**：`failed` 且 `next_restart_at <= now` → 关闭旧沙箱，旧行置 `stopped`（`reason = "restarted"`），按第 1 步新建（新 id，继承 `failures`）。
6. **清零**：`ready` 或 `busy` 的实例自 `started_at` 起已超过 `backoff_max_s` 且本轮健康检查通过 → `failures = 0`。

然后：

- 工位不在 `desired` 里的 `resident` 实例：`job` 为空时关闭，`stopped`（`reason = "removed"`）。
- `pooled` 实例 `ready` 且 `now - idle_since >= idle_ttl_s`：关闭，`stopped`（`reason = "idle"`）。

最后，本对象持有的沙箱中，行已被别的进程置为 `stopped` 的（例如 `factory instance stop`），在本轮关闭。

`reconcile` 用 `ctx.clock`，不等待。一个进程里只应有一个对象调用 `reconcile`（`factory serve` 的 daemon）；`acquire`、`release`、`stop` 可以在任何进程里调用，靠数据库事务互斥。

### 5.6 恢复：`recover`

`factory serve` 启动时（取得租约之后、第一轮 `tick` 之前）调用一次。对每个未停止的实例：

- `job` 不为空且该 job 已终态，或不再是该实例的占用者：清掉 `job`。
- `pooled`：一律销毁（`stopped`，`reason = "recovered"`）。不知道上一个 job 留下了什么。
- `resident`：`attach` 成功且 `alive()` → 保留，置 `ready`，并启动它的控制通道（5.7）；否则按 5.5 第 3 步记一次失败。

### 5.7 emit

`emit(runner, station=S, port=P, source=D, instance_id=I, labels=L, trigger=T)`。控制通道的 handler 传入实例行的 `station`，`runner` 是 active 快照上的 `Runner`：

1. `I` 不为 `None` 时：实例存在、未停止、`station == S`；否则 `EMIT_INVALID`，`reason = "instance"`。
2. `S` 在快照里且工位 `role == "source"`；否则 `EMIT_INVALID`，`reason = "role"`。
3. `P` 是工位配方 `outputs` 里的端口，且端口的类型不是 `list<...>`；否则 `EMIT_INVALID`，`reason = "port"`。
4. `D` 是目录。`I` 不为 `None` 时它的宿主路径必须在 `instances/<I>/main/out/` 之下。目录内文件总大小 `<=` 64 MiB。否则 `EMIT_INVALID`，`reason = "dir"`。
5. 速率（仅 `I` 不为 `None`）：`emits` 表中该工位最近 60 秒的行数 `>= emit_per_min` → `EMIT_RATE_LIMITED`。
6. `job = runner.adopt(S, out_dir, labels=L, by=...)`，其中 `out_dir` 是新建的临时目录，`out_dir/<P>/` 是 `D` 的拷贝。`Runner.adopt`：
   - 插入 job 行：`id = ids.new("j")`，`state = GATING`，`attempt = 1`，`inputs = {}`，`params = {}`，`epoch` 为快照 epoch；写 `job.adopted` 事件 `{job, station, by}`。
   - 把 `out_dir` 拷到 attempt 目录的 `out/`，调用 `self.gate.inspect(...)`，与 `_finish` 中收货之后的部分相同（`inputs` 为空）。
   - `pass` → 与 `_seal` 相同地封存，job 的终态与普通 source job 封存后相同；之后对每个产出 Bean 调用 `warehouse.label(bean, K, V, by=...)`。
   - `reject` 或 `error` → job `DEAD`（不重试），`error` 为检验结果。
7. job 不是封存成功 → `EMIT_INVALID`，`reason = "gate"`，`detail.feedback` 是检验的反馈。
8. 成功：插入 `emits` 行（`trigger` 为 `T`，缺省 `{"on": "emit", "instance": I}`），写 `instance.emit`，删除 `D`（`I` 为 `None` 时由调用方清理），返回 Bean id。jobs 表没有 trigger 列，emit 的来由只记在 `emits`。之后的 bean 触发器照常处理这个 Bean（`lines.md` 5.6）。

`by` 是 `f"instance:{I}"`，`I` 为 `None` 时是 `trigger["on"]`。

控制通道的 handler 先换算 `dir`：以 `/factory/instance/` 开头的虚拟路径换成 `instances/<id>/main/` 下的宿主路径（Docker 下主进程看到的是虚拟路径）；其余当作宿主路径（LocalProvider 下主进程看到的就是宿主路径）。然后才做第 4 步的检查。

实例主进程的控制通道：实例进入 `starting` 时，在维护实例的进程里启动 `CtlServer(instances/<id>/main/ctl, handler)`，只接受 op `emit`，其他 op 回 `CTL_OP_INVALID`；实例停止时停止它。`emit` 抛 `FactoryError` 时返回 `{"ok": false, "code", "message", "detail"}`（`detail` 打码）。`Instances` 的构造参数增加 `runner_for: Callable[[], Runner] | None`，handler 用它取 active 快照上的 `Runner`；为 `None` 时 emit 回 `EMIT_INVALID`（`reason = "runner"`）。

### 5.8 出网与秘密

- 实例的沙箱 env 按 `capabilities.md` 5.0 用工位策略构造（秘密在建容器时注入）。
- 主体名是 `inst-<id>`：建实例时用新令牌调用 `register_check_egress`，实例停止时 `close_egress`。主进程的流量用这个主体。
- 每个 job 的 `exec` 用 job 自己的主体：`Runner` 照常登记 attempt 令牌，凭据放在 `job_env` 里，经 exec 的 env 传入，覆盖容器级的代理变量。
- 令牌与秘密不写进 `instances` 表、事件和日志。

### 5.9 `Daemon` 的接线

- `Daemon` 有 `epochs` 时在构造时建 `Instances(ctx, active 快照, runner_for=...)`，保存为公开属性 `daemon.instances`；`runner_for` 返回 active epoch 在组件缓存里的 `Runner`。没有 `epochs`（单快照模式）时用那个快照。
- `start()`：取得租约之后调用 `instances.recover()`。
- `tick` 在第 3 步（活跃 epoch）之后、第 4 步（调度）之前调用 `instances.reconcile(active 快照)`，结果记入 `TickReport` 的新字段 `instances: dict[str, list[str]]`。
- 调度器与执行器里的 `Runner` 都由 `open_runner` 装配，共用同一张 `instances` 表；daemon 自己的 `Scheduler` 构造时传入 `daemon.instances`。
- `shutdown`：调用 `instances.close()`。**不停止**常驻实例：它们不属于 daemon 进程，重启后由 `recover` 接上。

---

## 6. 错误

新码都要登记到 `mao.errors`。

| 码 | 何时 | detail |
|---|---|---|
| `INSTANCE_INVALID` | 4.1 的规则 | `machine, reason` |
| `INSTANCE_UNSUPPORTED` | 工位不是 `once`，但 `Runner` 没有 `Instances` | `station` |
| `INSTANCE_BUSY` | `pooled` 没有空闲实例且已到 `pool_size` | `station` |
| `INSTANCE_NOT_READY` | `resident` 实例不是 `ready` | `station` |
| `INSTANCE_LOST` | `attach` 时容器不存在或没有运行 | `sandbox` |
| `INSTANCE_UNHEALTHY` | 健康检查失败 | `instance, exit_code` |
| `INSTANCE_DIED` | 主进程退出 | `instance` |
| `INSTANCE_NOT_FOUND` | `restart`、`stop` 找不到 | `ref` |
| `EMIT_INVALID` | 5.7 第 1、2、3、4、7 步 | `reason`；`gate` 时另有 `feedback` |
| `EMIT_RATE_LIMITED` | 5.7 第 4 步 | `station, per_min` |

命令行退出码：`INSTANCE_NOT_FOUND` 为 1。

---

## 7. 验收测试

夹具：`tests/acceptance/m12/instance_world.py`。`World(tmp)` 建 `$FACTORY_ROOT`、带治理的 Registry（`governance.md`）与 `LocalProvider`。机器主进程与操作工都是标准库脚本，经 `factoryctl` 交互。时钟是 `FakeClock`。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m12/test_m12_01_command.py` | `test_run_argv_with_command` | 有 `command` 时 argv 末尾是 `/bin/sh -c 'mkdir -p /tmp/home && exec "$@"' sh ...`；没有时与 `sandbox.md` 5.2 相同 |
| | `test_instance_config_rules` | 4.1 表中每条规则都报 `INSTANCE_INVALID` 和对应的 `reason`；`per_job` 当作 `once` |
| | `test_local_command_alive` | LocalProvider 启动主进程，`alive()` 为真；主进程退出后为假；`close` 终止仍在运行的主进程 |
| | `test_invalid_instance_job_dead` | 机器 `pooled` 却没有 `reset`：job 在静态检查中 `DEAD`，`INSTANCE_INVALID` |
| `tests/acceptance/m12/test_m12_02_pooled.py` | `test_pooled_reuses_instance` | 同一工位先后两个 job 用同一个实例：沙箱只建一次；两次之间执行了 `reset`；第二个 job 看不见第一个 job 的输出 |
| | `test_pooled_reset_failure_destroys` | `reset` 失败：实例 `stopped`（`reason = reset`），第一个 job 仍是 `DONE`；下一个 job 得到新实例 |
| | `test_pooled_stage_in_out` | 输入出现在 `/factory/in`；`factoryctl` 请求经实例的 `job/ctl` 得到响应；`/factory/out` 的文件被收货封存 |
| | `test_pooled_admission_cap` | `pool_size = 1` 时，一个 job 占用中，`admit` 不选同工位的其余 job；`pool_size = 2` 时选一个 |
| | `test_pooled_idle_ttl` | 空闲超过 `idle_ttl_s` 后 `reconcile` 关闭实例（`reason = idle`） |
| | `test_exec_timeout_destroys_instance` | 操作工超时：job 按 M1 规则失败，实例 `stopped`（`reason = timeout`） |
| `tests/acceptance/m12/test_m12_03_resident.py` | `test_reconcile_starts_resident` | 第一次 `reconcile` 启动常驻实例并置 `ready`，主进程在运行；再次 `reconcile` 不新建 |
| | `test_resident_restart_backoff` | 主进程退出：记为失败，退避期内不重建，推进时钟后重建；连续失败到 `restart.max` 后 `crashed`，开出 `instance` 唤醒票 |
| | `test_resident_replaced_on_change` | 推广改了工位的 epoch：建新实例；旧实例无人借用时 `stopped`（`reason = replaced`） |
| | `test_resident_state_persists` | 第一个实例写进 `/factory/state` 的文件，重建后的实例能读到 |
| | `test_resident_job_runs_in_instance` | 常驻工位的 job 在常驻实例里执行，能读到主进程写的状态；实例不是 `ready` 时 `admit` 不选它 |
| | `test_recover_on_start` | `recover`：活着的常驻实例保留为 `ready`；`pooled` 实例被销毁（`reason = recovered`） |
| | `test_instance_cli` | `factory instance list --json`、`restart`、`stop` 的输出与状态变化；`stop` 后 `reconcile` 不重建 |
| | `test_serve_tick_reconciles` | `factory serve` 的 `tick` 报告里有 `instances`，常驻实例被启动 |
| `tests/acceptance/m12/test_m12_04_emit.py` | `test_emit_seals_bean` | 主进程 `factoryctl emit`：Bean 被封存，来源是 `DONE` 的 emit job，`emits` 行的 `trigger.on = emit`；bean 触发器为它建 run |
| | `test_emit_rejects_invalid` | 未知端口、`out` 之外的目录、出厂检验不通过都报 `EMIT_INVALID`（`port`、`dir`、`gate`），不封存 |
| | `test_emit_rate_limit` | 超过 `emit_per_min`：`EMIT_RATE_LIMITED`；推进 60 秒后恢复 |
| | `test_emit_requires_source_role` | `role: transform` 的常驻工位 emit：`EMIT_INVALID`（`reason = role`） |

---

## 8. 错误示范与正确示范

```python
# ✖ 错误：pooled 实例在 job 结束后不清目录，只靠 reset
# ✔ 正确：release 先清空 job/in、job/out、job/ctl，再执行 reset；reset 失败就销毁

# ✖ 错误：按机器键共用实例，两个工位的秘密进了同一个容器
# ✔ 正确：实例按工位键区分（D-02）

# ✖ 错误：常驻实例挂掉后在 tick 里 sleep 等它重启
# ✔ 正确：记 next_restart_at，下一轮 reconcile 到期才重建；crashed 交给厂长和人

# ✖ 错误：emit 直接写 beans 表，没有来源 job
# ✔ 正确：每次 emit 生成 DONE 的 emit job，Bean 的来源证明指向它
```

---

## 9. 风险与注意事项

- **Docker 的 exec 超时会 kill 整个容器**（`sandbox.md` 5.8）。所以超时必须销毁实例，不能当作普通失败继续借出。
- **LocalProvider 不能跨进程判断主进程**。`attach` 得到的对象 `alive()` 恒为真，生产的健康判断只在 Docker 下可靠；需要时用 `runtime.health`。
- **常驻实例的 `/factory/state` 按工位名保留**，升级版本后新实例接着用。不要把它放进 `reset` 或 `release` 清理的目录。
- **emit 风暴**：`emit_per_min` 只限一个工位；下游 run 的限流仍靠调度准入。

---

## 10. 任务拆分

| 任务 | 内容 | 依赖 | 必读 | 产出 | 验收 |
|---|---|---|---|---|---|
| M12-01 | `SandboxSpec.command`、`alive`、`attach`、`instance_config`、静态检查 | M11 | 3.1、4.1、5.1 | `mao.sandbox`、`mao.instances`、`mao.runner` | `test_m12_01_command.py` |
| M12-02 | 租借、`reset`、拷入拷出、准入、`Runner` 与网关接线 | M12-01 | 3.2、5.2–5.4 | `mao.instances`、`mao.runner`、`mao.scheduler`、`mao.gateway` | `test_m12_02_pooled.py` |
| M12-03 | `reconcile`、`recover`、命令行、`Daemon` 接线、MCP 与控制台 | M12-02 | 3.4、3.5、5.5、5.6、5.8、5.9 | `mao.instances`、`mao.daemon`、`mao.commands.instance` | `test_m12_03_resident.py` |
| M12-04 | `Runner.adopt`、`emit`、`factoryctl emit` | M12-03 | 3.3、5.7 | `mao.runner`、`mao.instances`、`mao.ctl.factoryctl` | `test_m12_04_emit.py` |
