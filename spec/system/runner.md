# runner：Job 状态机与 Runner（mao.jobs / mao.runner）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M1-08**。
> 模块：`mao.jobs`、`mao.runner`。迁移：`src/mao/migrations/002_jobs.sql`。
> 依据：README D4、D9、D10、D12、D14、第 4.7–4.9 节、第 5 节。架构文档 6.5 的状态名已经定了。
> 状态表 `GATING` 行增加 `DEAD`，只用于 Sink 的回执核验，见 `wiring.md` 5.1（M10）。
> 审批接线见 `delegation.md` 5.9；出网与秘密的静态拒绝作废、每个 attempt 的出网令牌与沙箱资源见 `capabilities.md`；ctl op `ask`、`handoff`、`egress` 见 `delegation.md`、`inbox.md`、`capabilities.md`（M11），冲突时以它们为准。
> **M12/M13 增补**：`open_runner` 构造 `Instances`（`runner.instances`）、`pooled` 与 `resident` 工位经租借执行、实例的静态检查、`Runner.adopt` 见 `instances.md` 3.3、5.3（M12）。冲突时以新规范为准。

---

## 1. 目的与范围

给定一个通过检查的快照和已封存的输入 Bean，跑**一个**工位：准备沙箱、执行操作工、收货、出厂检验、封存、重试、运行中审批、事后预算。

不做的事（M1 明确不做，碰到就拒绝，不要做一半）：

| 不做 | 碰到时 | 归属 |
|---|---|---|
| 产线、缓存、Source、Sink | 角色是 `source`/`sink`/`judge`/`planner` → `ROLE_UNSUPPORTED` | M2 |
| 出网代理、秘密 | 策略不是纯 `none`，或声明了秘密 → 拒绝 | M2 |
| 两阶段工位 | `phases` 含 `propose` → `PHASE_UNSUPPORTED` | M2 `approvals.md` |
| `form: service`、compose | `FORM_UNSUPPORTED` / `COMPOSE_UNSUPPORTED` | M4 / 不做 |
| judge | 忽略 `gate.judge`，不调用模型 | M2 以后 |
| Web UI | 不存在 | — |

`seal` 只发生在出厂检验 `verdict == "pass"` 之后。未封存的 Bean 不能当输入（R9）。预算超出不封存、不重试（D14）。

M2 会在本组件上**追加**，不会改 README 4.9 的签名：`Runner.submit` 增加可选关键字 `run=`；`jobs` 表增加 `idempotency_key`、`run_value`；`events` 表增加 `run_id`；状态表增加 `PENDING → SEALED`（只给 Sink 的重复发布）。M1 的建表语句不要预先加上这些列。

---

## 2. 依赖

只用 README 第 4 节已经列出的接口。

| 依赖 | 用到的接口 |
|---|---|
| `mao.snapshot.Snapshot` | `key`、`spec`、`compiled`、`type_key` |
| `mao.values` | `bean_value`、`params_value`、`approval_env`、`recipe_env` |
| `mao.warehouse.Warehouse` | `get`、`materialize`、`seal_many`、`record_lineage` |
| `mao.gate.Gate` | `check_inputs`、`inspect` |
| `mao.sandbox` | `SandboxProvider.open`、`prepare_inject_volume`、`remove_volume` 不在热路径里调用 |
| `mao.operator` / `mao.brief` | `render_brief`、`build_invocation`、`parse_transcript` |
| `mao.ctl.CtlServer` | `start` / `stop`；handler 由本组件实现 |
| `mao.base` | `canonical_json`、`utc_iso`、`parse_duration`、`Clock`、`IdSource` |
| `mao.db.Database` | `tx`、`query`。迁移 `002_jobs.sql` |

禁止在事务里调用 `sandbox.exec` 或 `docker`（S7）。禁止 `datetime.now`、`uuid`、`random`。

---

## 3. 接口

README 4.9 照抄。`mao.jobs` 定义状态和 `Job`，`mao.runner` 定义 `Runner` 并再导出这两个名字。

```python
class JobState(StrEnum):
    PENDING = "PENDING"
    PROVISIONING = "PROVISIONING"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    GATING = "GATING"
    SEALED = "SEALED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    DEAD = "DEAD"

TERMINAL = {JobState.SEALED, JobState.DEAD}

@dataclass(frozen=True)
class Job:
    id: str
    station: str               # Spec 键
    epoch: str
    state: JobState
    attempt: int               # 从 1 开始
    inputs: dict[str, list[str]]
    params: dict[str, Any]
    outputs: dict[str, list[str]]    # 只含已封存的 Bean ID
    cost_usd: float
    error: dict[str, Any] | None     # {"code","message","detail"} 或 None
    feedback: str | None
    created_at: str
    updated_at: str

class Runner:
    def __init__(self, ctx: Context, snapshot: Snapshot) -> None
    def submit(self, station_ref: str, inputs: Mapping[str, str | Sequence[str]],
               params: Mapping[str, Any]) -> Job
    def run(self, job_id: str) -> Job
    def decide(self, approval_id: str, decision: Literal["approve", "deny"], *,
               by: str, note: str = "") -> None
```

`submit` 先做来料检验，再插入 `PENDING` job。检验失败不插入行。`run` 阻塞到 `SEALED` 或 `DEAD`。已经是终态时直接返回，不重新执行。

`mao.jobs` 另外提供：

```python
def load_job(db: Database, job_id: str) -> Job    # 不存在 → JOB_NOT_FOUND
def dump_job(job: Job) -> dict[str, Any]          # 状态用字符串，供 CLI --json
```

---

## 4. 数据格式

### 4.1 迁移 `src/mao/migrations/002_jobs.sql`

文件名必须匹配 `foundation.md` 4.1。内容禁止含 `BEGIN`、`COMMIT`、`ROLLBACK`、`PRAGMA user_version`。

```sql
CREATE TABLE jobs (
  id          TEXT PRIMARY KEY,
  station     TEXT NOT NULL,
  epoch       TEXT NOT NULL,
  state       TEXT NOT NULL,
  attempt     INTEGER NOT NULL,
  inputs      TEXT NOT NULL,
  params      TEXT NOT NULL,
  outputs     TEXT NOT NULL DEFAULT '{}',
  cost_usd    REAL NOT NULL DEFAULT 0,
  error       TEXT,
  feedback    TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE TABLE events (
  seq     INTEGER PRIMARY KEY AUTOINCREMENT,
  id      TEXT NOT NULL UNIQUE,
  at      TEXT NOT NULL,
  kind    TEXT NOT NULL,
  job_id  TEXT,
  data    TEXT NOT NULL
);
CREATE INDEX events_job ON events(job_id, seq);
CREATE TABLE approvals (
  id          TEXT PRIMARY KEY,
  job_id      TEXT NOT NULL,
  attempt     INTEGER NOT NULL,
  request_id  TEXT NOT NULL,
  phase       TEXT NOT NULL,
  kind        TEXT NOT NULL,
  summary     TEXT NOT NULL,
  cost_usd    REAL NOT NULL,
  status      TEXT NOT NULL,
  by          TEXT,
  note        TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  decided_at  TEXT
);
CREATE INDEX approvals_job ON approvals(job_id);
CREATE UNIQUE INDEX approvals_request ON approvals(request_id);
```

没有 `run_id`（`lines.md` 的 `010_runs.sql` 会 `ALTER TABLE events ADD COLUMN`）。没有 `idempotency_key`、`run_value`（`connectors.md` 的 `012_receipts.sql` 会加）。

`inputs` / `params` / `outputs` / `error` / `events.data` 都是规范 JSON 字符串。`inputs` 的值永远是数组，即使端口只有一个 Bean。

### 4.2 attempt 目录

```text
$FACTORY_ROOT/jobs/<job_id>/<attempt>/
  in/  out/  ctl/  workspace/  logs/  tmp/
```

`workspace/` 只被 `LocalProvider` 当作 `/workspace`。Docker 的工作区是命名卷，这个目录仍然创建，但不挂进容器。

卷名：

| 情况 | 名字 |
|---|---|
| attempt 为 1，或重试模式是 `resume` | `mao-<job_id>-ws` |
| `fresh` 且 attempt > 1 | `mao-<job_id>-ws-<attempt>` |

容器名：`mao-<job_id>-<attempt>`，例如 `mao-j-0001-1`。

### 4.3 事件

每次状态变化写一行。`data` 是规范 JSON。`at` 是 `utc_iso(clock.now())`。`id` 是 `ids.new("ev")`。

| kind | 何时 | data |
|---|---|---|
| `job.created` | `submit` 插入行 | `{station, epoch, inputs, params}` |
| `job.state` | 每次状态变化 | `{from, to, attempt, reason}` |
| `job.approval` | 审批有了决定 | `{approval, decision, by}` |
| `job.sealed` | 进入 SEALED | `{outputs, report}`，`report` 是 `GateReport.digest()` |
| `job.failed` | 进入 DEAD | `{code, message}` |
| `bean.sealed` | 每个封存的产物一行 | `{bean, job, type, station}` |

`bean.sealed` 的 `bean` 是 Bean ID。M2 的 `TriggerEngine` 按这个字段取 Bean。它和 `job.sealed`、`seal_many`、`record_lineage` 在同一个事务里。

### 4.4 控制通道文件

`ctl/job.json`、`ctl/contract.json`、`ctl/brief.md`、`ctl/harvest.json` 的格式见 `factoryctl.md` 第 4 章。`brief.md` 是 `Brief.markdown`。script 工位也写 `brief.md`（内容可以是空任务的契约），方便排查。

`harvest.json` 只包含配方里写了 `harvest` 的端口。`place_as` 取类型文档里恰好一个 required、且路径不含 `*` 的 payload 路径。不是恰好一个时，`place_as` 用匹配文件的 basename，并在结果里让检验自己判断。夹具 `note.text@1` 的 `place_as` 是 `note.txt`。

### 4.5 审批行

`status` 是 `pending`、`approved`、`denied`、`timed_out` 之一。`phase` 在 M1 只有 `in_run`。`decision` 参数 `approve` 对应状态 `approved`，`deny` 对应 `denied`。

---

## 5. 行为

### 5.1 状态机

```text
PENDING → PROVISIONING → RUNNING ⇄ AWAITING_APPROVAL
                            ↓
                         GATING → SEALED
                            ↓
              REJECTED / FAILED → 未超过上限 → PENDING（attempt+1）
                                → 超过上限，或预算超出 → DEAD
```

合法转换（其他转换是编程错误，抛 `JOB_STATE_INVALID`，不要悄悄改写）：

| 从 | 到 |
|---|---|
| `PENDING` | `PROVISIONING` |
| `PROVISIONING` | `RUNNING`、`FAILED`、`DEAD` |
| `RUNNING` | `AWAITING_APPROVAL`、`GATING`、`FAILED`、`DEAD` |
| `AWAITING_APPROVAL` | `RUNNING`、`FAILED` |
| `GATING` | `SEALED`、`REJECTED`、`FAILED` |
| `REJECTED` | `PENDING`、`DEAD` |
| `FAILED` | `PENDING`、`DEAD` |

`SEALED` 和 `DEAD` 没有出边。M2 会追加 `PENDING → SEALED`，M1 不要写这条。

`run` 在内存里循环，直到终态。每次转换都先落库再继续，所以另一个进程能看见中间状态。

重试上限来自 `limits.retries`，缺省 `rejected=0`、`failed=0`、`mode=fresh`。计数是本 job 的 `job.state` 事件里 `to` 等于 `REJECTED` 或 `FAILED` 的次数（含刚刚这次）。

- 刚刚进入 `REJECTED`，且该次数 `<= retries.rejected`：转到 `PENDING`，`attempt += 1`，`feedback` 保留，`reason` 为 `retry`。
- 刚刚进入 `FAILED`，且该次数 `<= retries.failed`，且错误码不是 `BUDGET_EXCEEDED`：同样回到 `PENDING`。
- 否则转到 `DEAD`，写 `job.failed`。
- `BUDGET_EXCEEDED` 直接 `DEAD`，不回到 `PENDING`（D14）。即使 `retries.failed > 0` 也不重试。

`upper` 的 `failed: 1` 表示第一次失败后还能再试一次，所以操作工失败时 `attempt` 最终是 2，状态是 `DEAD`。`rejected: 0` 表示检验退回一次就 `DEAD`，`attempt` 仍是 1。

### 5.2 `submit`

1. `station_key = snapshot.key("Station", station_ref)`。快照不是当前 `Runner` 持有的那个：编程错误（调用方保证同一个 `Snapshot`）。
2. 规范化输入：每个端口的值收成 `list[str]`，保持顺序。空字符串的 id：`INTAKE_REJECTED`。
3. **在事务外**调用 `Gate.check_inputs(station_key, inputs)`。它检查存在、已封存、类型兼容、`requires`。失败抛 `INTAKE_REJECTED`，本方法不捕获、不插行。
4. `params = params_value(...)`。失败不插行。
5. 一个事务里：插入 `jobs`（`state=PENDING`，`attempt=1`，`outputs={}`，`cost_usd=0`，`error=null`），写 `job.created`。`id = ids.new("j")`，时间用 `clock`。
6. 返回 `load_job`。

`check_inputs` 返回的 Bean 不在 `Job` 里保存。`Job.inputs` 是调用方传入的 ID 列表。

### 5.3 `run` 的准备与拒绝

进入循环前，若状态已是终态，返回。若状态不是 `PENDING` 也不是中间态，`JOB_STATE_INVALID`。中间态（进程崩溃后留下的 `PROVISIONING` 等）从该状态继续，不增加 attempt：`PROVISIONING` 重新准备，`RUNNING` 重新执行，`GATING` 重新检验。

对工位文档做一次静态拒绝（在第一次 `PROVISIONING` 之前，失败直接 `DEAD`，不重试）：

| 条件 | 错误码 |
|---|---|
| `role` 不是 `transform` 或 `converter` | `ROLE_UNSUPPORTED` |
| `phases` 存在且不等于 `["execute"]` | `PHASE_UNSUPPORTED` |
| 机器 `form` 不是 `tool` 或 `workspace` | `FORM_UNSUPPORTED`（与 `connectors.md` 同一个码；detail 含 `station`、`machine`、`form`） |
| 机器 runtime 有 `compose` 且没有 `image` | `COMPOSE_UNSUPPORTED` |
| 策略 `egress` 的键集合不是 `{"none"}` | `EGRESS_UNSUPPORTED`（M11 起作废，见 `capabilities.md` 5.0） |
| 策略 `secrets` 是非空列表，或是 `from-machine` 且机器 `needs.secrets` 非空 | `SECRETS_UNSUPPORTED`（M11 起作废，见 `capabilities.md` 5.0） |

`gate.judge` 存在时不报错，也不运行。M2 再接。

`EGRESS_UNSUPPORTED` 这里是策略等级，不是 `SandboxSpec.network`。M1 不把 `allow_unproxied_egress` 用在 Runner 上：策略不是 `none` 就拒绝。开发开关只存在于 `DockerProvider`，给将来的调用方，Runner 不传 `network="egress"`。

### 5.4 准备沙箱

`PENDING → PROVISIONING` 之后：

1. 创建 4.2 的目录。
2. 对每个输入端口、每个 Bean，`warehouse.materialize(bean_id, dest, layout="port")`。单 Bean 的 `dest` 是 `in/<port>/`；`list` 是 `in/<port>/<i>/`。`i` 从 0 开始。物化结果必须满足 README 第 5 节：payload 按 `path` 放置，另有 `bean.json`。
3. 写 `ctl/job.json`、`ctl/contract.json`、`ctl/brief.md`。agent 工位的 brief 用 `render_brief(..., attempt=job.attempt, feedback=job.feedback, budget_usd=工位预算)`。输入 Bean 用 `warehouse.get`。
4. 组装 `SandboxSpec`：
   - `name` 见 4.2；
   - `image` 是机器 `runtime.image`，必须是 `repo@sha256:64hex`，否则 `IMAGE_REF_INVALID`；
   - 绑定挂载：`in` → `/factory/in` 只读，`out` → `/factory/out` 读写，`ctl` → `/factory/ctl` 读写，`$FACTORY_ROOT/tools/factory` → `/opt/factory` 只读；
   - `env` 含 `FACTORY_JOB_ID`、`FACTORY_ATTEMPT`（十进制字符串）；
   - `network="none"`；
   - `cpus`、`memory_mb` 来自机器 `runtime.resources`。`cpus` 是数。`memory` 匹配 `^([0-9]+)(Mi|M|MB|Gi|G|GB)$`：前三个单位是该数的 MB，后三个是该数乘 1024。解析失败：`SANDBOX_SPEC_INVALID`。没有 `resources` 时两个都是 `None`。
   - Docker 且 `form == workspace`：`workspace_volume` 用 4.2 的卷名，不把宿主 `workspace/` 挂进去。
   - Local：不设 `workspace_volume`，改为把 `attempt/workspace` 挂到 `/workspace`。`resume` 时先把上一 attempt 的 `workspace/` 用 `shutil.copytree` 拷过来（目录已存在则先删再拷）。
5. agent-cli 且 runtime 有 `inject.volume`：卷名 `mao-op-` + digest 的前 12 位 hex。`provider.prepare_inject_volume(image, volume)`，再挂 `Mount("volume:"+volume, inject.mount, readonly=True)`。Local 上这个方法是空操作，命令映射由测试传入的 `LocalProvider` 负责。
6. `provider.open(spec)`。失败：`FAILED`，错误码用沙箱抛出的那个（`DOCKER_UNAVAILABLE`、`IMAGE_NOT_FOUND`、`DOCKER_FAILED` 等），计入 failed 重试。
7. `PROVISIONING → RUNNING`。

### 5.5 执行

1. `inv = build_invocation(...)`。
2. `CtlServer(ctl_dir, handler, clock).start(0.2)`。handler 见 5.6。
3. `sandbox.exec(inv.argv, timeout_s=inv.timeout_s, stdout=logs/operator.stdout, stderr=logs/operator.stderr, env=inv.env, workdir="/workspace")`。
4. `finally` 里 `CtlServer.stop()`。不要在事务里做第 3 步。
5. 若 `inv.transcript == "stream-json"`：`summary = parse_transcript(logs/operator.stdout, "stream-json")`，`cost = summary.cost_usd`。否则 `cost = 0.0`。`job.cost_usd = round(cost, 6)` 立即落库。
6. 若 `cost > budget`（等于上限不算超，README 3.2）：`DEAD`，`BUDGET_EXCEEDED`，`detail` 含 `cost_usd` 与 `budget_usd`。不收货，不检验，不封存，不重试。然后 `close` 沙箱并返回。
7. 否则若超时：`FAILED` / `OPERATOR_TIMEOUT`。
8. 否则若存在 `ctl/fail.json`：`FAILED` / `OPERATOR_FAILED`，`detail.reason` 为文件里的 `reason`。
9. 否则若 transcript 的 `is_error`：`FAILED` / `AGENT_ERROR`，`detail.result` 为 summary.result。
10. 否则若 `exit_code != 0`：`FAILED` / `OPERATOR_FAILED`，`detail.exit_code`。
11. 否则进入收货和检验（5.7）。

第 7–10 步计入 failed 重试。第 6 步不计入可重试失败。

### 5.6 运行中审批

handler 只处理 `check` 和 `approval`。其他 `op`：返回 `ok: false`，`error.code` 为 `CTL_REQUEST_INVALID`。

**check**：把 `args.path` 映射到宿主路径（按沙箱 mount 反查），复制到 `tmp/check/<port>/`，调用 `Gate.inspect` 只作为预览。预览**不**封存、不改变 job 状态。返回 `ok: true`，`result` 为 `{"verdict", "feedback"}`。设施故障时 `verdict` 为 `error`。M1 的预览允许和正式检验一样调用 `inspect`；正式检验在 5.7 再做一次。不要把预览的候选 Bean 留在仓库里当输入。若 `inspect` 会 `warehouse.add`，预览用的 Bean 保持未封存，且不写进 `job.outputs`。

**approval**（M11 起由 `delegation.md` 5.9 取代：经 `Approvals.open_request`，响应用 `approvals.md` 4.3 的格式）：

1. 按 `request_id` 查找。没有则插入 `pending` 行，`id = ids.new("a")`，`phase=in_run`，`kind/summary/cost_usd` 来自 args。并把 job 改为 `AWAITING_APPROVAL`（若当前是 `RUNNING`）。
2. 读工位 `approval.in_run`。没有这条规则：立刻 `denied`，响应 `APPROVAL_DENIED`，job 回到 `RUNNING`。
3. 规则是 `"auto"`，或 `{auto_if, else}` 且 `snapshot.compiled(f"{station_key}/approval.in_run").evaluate(approval_env("in_run", payload, inputs, params))` 为 true：`approved`，`by="auto"`。`payload` 是 `{"kind", "summary", "cost_usd"}`。
4. 规则是 `"foreman"` 或 `"review"`，或 `auto_if` 为 false 且 `else` 不是 `"auto"`：不写响应（handler 返回 `None`），直到 `decide` 或超时。M1 没有厂长，也没有复核模型（D10）。`review` 与 `foreman` 一样，只等 `decide`。
5. 超时：`clock.now() - 解析(created_at) >= timeout_s`（`approval.timeout`，缺省 600 秒）。状态改 `timed_out`，响应 `ok: false`、`error.code` 为 `APPROVAL_TIMEOUT`。`exec` 返回后，Runner 若看到本次 attempt 有 `timed_out` 的审批，即使退出码是 0 也 `FAILED` / `APPROVAL_TIMEOUT`，计入 failed 重试，不封存。
6. 决定写入后，handler 下一轮返回响应，并把 job 从 `AWAITING_APPROVAL` 改回 `RUNNING`，写 `job.approval`。

`decide(approval_id, decision, *, by, note="")`：

- 找不到行：`APPROVAL_NOT_FOUND`。
- `status != "pending"`：`APPROVAL_STATE_INVALID`。
- `decision` 不是 `approve` 或 `deny`：`APPROVAL_STATE_INVALID`。
- 一个事务里更新 `status`、`by`、`note`、`decided_at`。不写响应文件；正在跑的 handler 会写。因此另一个进程里的 `factory job decide` 也能用。

`auto_if` 求值抛 `EvalError`：当作不通过，走 `else`。不要当成 true。

### 5.7 收货、检验、封存

操作工成功且没超预算：

1. `RUNNING → GATING`。
2. 若有需要 harvest 的端口：写 `harvest.json`，`sandbox.exec(["/opt/factory/bin/factoryctl", "harvest"], timeout_s=60, ...)`。退出码不是 0，或 `harvest-result.json` 缺失，或顶层 `ok` 不是 true：`FAILED` / `HARVEST_FAILED`，`detail` 含结果 JSON。显式 `submissions/<port>.json` 的端口不在 harvest 结果里，算已经交过。
3. 没有 harvest 的端口（夹具 `upper` 的 `run` 直接写 `/factory/out/note/note.txt`）保持出口目录的现状。
4. **事务外** `report = gate.inspect(station_key, job_id=..., attempt=..., out_dir=attempt/out, inputs=..., params=...)`。
5. `verdict == "error"`：`FAILED` / `GATE_ERROR`，`detail.feedback` 为报告的 feedback。这是设施故障，计入 failed 重试，不封存。
6. `verdict == "reject"`：`REJECTED`，`feedback = report.feedback`，`error` 为 `{"code": "GATE_REJECTED", "message": feedback, "detail": {}}`。计入 rejected 重试。不封存。`job.outputs` 保持 `{}`。
7. `verdict == "pass"`：一个事务里：
   - `warehouse.seal_many([(bean_id, facts) for ...], job_id=job.id, report_digest=report.digest())`，facts 用报告里的 `report.facts`；
   - `warehouse.record_lineage(job.id, inputs, outputs)`，outputs 是报告的 `report.outputs`；
   - 每个产物写 `bean.sealed`；
   - 更新 job：`state=SEALED`，`outputs=report.outputs`，`error=null`，写 `job.sealed`。
8. `finally`：`sandbox.close()`。不删除工作区卷。

报告里的 Bean 在 `inspect` 期间是未封存候选。在第 7 步之前它们不是输入，也不能出现在 `job.outputs` 里。

### 5.8 重试与工作区

回到 `PENDING` 之后，循环立即开始下一次 attempt。`fresh`：新的工作区卷名（4.2），不删除旧卷（留给排查）。`resume`：Docker 复用 `mao-<job_id>-ws`；Local 拷贝上一份 `workspace/`。输入目录每次都重新物化，不复用 `in/`。

`close` 失败只记日志，不把已经 `SEALED` 的 job 改成失败。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `JOB_NOT_FOUND` | `run` / `load_job` 的 id 不存在 | `job_id` |
| `JOB_STATE_INVALID` | 当前状态不允许这个转换，或 `run` 看见不认识的状态 | `job_id`、`state` |
| `INTAKE_REJECTED` | `check_inputs` 拒绝。本组件不包装，原样冒泡。未封存、类型不符、requires 不满足都是这个码 | `problems`（由 Gate 填） |
| `ROLE_UNSUPPORTED` | 角色不是 transform / converter | `station`、`role` |
| `PHASE_UNSUPPORTED` | 两阶段工位 | `station`、`phases` |
| `FORM_UNSUPPORTED` | `form` 不是 tool / workspace | `station`、`machine`、`form` |
| `COMPOSE_UNSUPPORTED` | 机器用 compose、没有 image | `station`、`machine` |
| `SECRETS_UNSUPPORTED` | 策略或机器声明了秘密。detail 只含名字，不含值 | `station`、`secrets` |
| `EGRESS_UNSUPPORTED` | 策略出网不是 `none` | `station`、`egress` |
| `BUDGET_EXCEEDED` | `cost_usd > limits.budget_usd`。不封存，不重试 | `job`、`cost_usd`、`budget_usd` |
| `OPERATOR_FAILED` | 退出码不是 0，或存在 `fail.json` | `exit_code` 或 `reason` |
| `OPERATOR_TIMEOUT` | `ExecResult.timed_out` | `timeout_s` |
| `AGENT_ERROR` | transcript `is_error` 且退出码是 0 | `result` |
| `APPROVAL_NOT_FOUND` | `decide` 的 id 不存在 | `approval_id` |
| `APPROVAL_STATE_INVALID` | 不是 pending，或 decision 不合法 | `approval_id`、`status` |
| `APPROVAL_TIMEOUT` | 审批等待超过 `approval.timeout` | `approval_id` |
| `APPROVAL_DENIED` | 没有 in_run 规则，或决定是 deny。出现在响应里；job 是否失败取决于 agent 接下来怎么做 | `approval_id` |
| `HARVEST_FAILED` | harvest 退出码不是 0，或结果 `ok` 不是 true | `job`、`result` |
| `GATE_ERROR` | `GateReport.verdict == "error"` | `job`、`feedback` |
| `GATE_REJECTED` | `verdict == "reject"`。job 状态是 `REJECTED`，不是立刻 `DEAD` | `job` |

沙箱和镜像的错误码（`DOCKER_UNAVAILABLE`、`IMAGE_NOT_FOUND`、`IMAGE_REF_INVALID`、`DOCKER_FAILED`、`SANDBOX_SPEC_INVALID`）原样写入 `job.error`，不改名。

---

## 7. 验收测试

文件：`tests/acceptance/m1/test_m1_08_runner.py`。开头 `pytest.importorskip("mao.runner")`。用 `LocalProvider`、`tests/fakes/fakelib.py` 的 `local_commands()` 和 `tests/fixtures/registry-m1`。时钟用 `FakeClock`，ID 用 `CounterIds`，所以第一个 job 是 `j-0001`。

| 测试 | 验证 |
|---|---|
| `test_terminal_states` | `SEALED`、`DEAD` 在 `TERMINAL` 里；`FAILED`、`REJECTED` 不在 |
| `test_submit_rejects_unsealed` | 5.2：未封存 Bean → `INTAKE_REJECTED`，`jobs` 表 0 行 |
| `test_upper_seals` | 5.7：`upper@1` 把 `hello` 收成已封存的大写 `note.txt`，状态 `SEALED`，`outputs["note"]` 非空 |
| `test_operator_fail_retries_then_dead` | 5.1：输入含 `#fail`，`failed: 1`，最终 `DEAD`，`attempt == 2`，错误码 `OPERATOR_FAILED` |
| `test_gate_reject_records_feedback` | 5.7 第 6 步：产物不满足 `shouting` → `REJECTED`，`rejected: 0` 所以 `DEAD`、`attempt == 1`、`GATE_REJECTED`，`feedback` 提到 `shouting` |
| `test_writer_harvests_and_seals` | 5.4–5.7：agent 工位按 `drafts/*.txt` 收货、封存；`brief.md` 含渲染后的任务与工厂契约；`cost_usd` 来自 transcript |
| `test_budget_exceeded_does_not_seal_or_retry` | D14：writer 的脚本报告成本 2.0，预算 1.0，`DEAD` / `BUDGET_EXCEEDED`，`attempt == 1`，`outputs == {}` |
| `test_in_run_approval_decide` | 5.6：成本 1.0 的审批不自动通过；另一个线程 `decide(..., "approve")` 之后 job 能封存 |

Docker 下的同一条成功路径在 `test_m1_10_e2e.py`，不在本文件。

---

## 8. 错误示范与正确示范

**8.1 未封存的 Bean 当输入**

```python
# ✖ 跳过来料检验，候选 Bean 的 facts 还是空的
job = insert_job(inputs)
warehouse.materialize(bean_id, dest, layout="port")
# ✔ 先 check_inputs。失败就不插行
beans = gate.check_inputs(station_key, inputs)
```

**8.2 检验还没过就封存**

```python
# ✖ 操作工退出码 0 不等于契约满足
warehouse.seal_many(...)
# ✔ 只有 GateReport.verdict == "pass" 才 seal_many，并且和血缘、事件同一个事务
```

**8.3 超预算还重试**

```python
# ✖ 超预算记成 FAILED，retries.failed 又把它跑一遍，钱花两次
if cost > budget:
    fail("FAILED")
# ✔ 直接 DEAD，错误码 BUDGET_EXCEEDED，outputs 保持空
```

**8.4 在事务里跑容器**

```python
# ✖ 持有 BEGIN IMMEDIATE 时 docker run 会堵住所有读者（S7）
with db.tx():
    sandbox.exec(...)
# ✔ exec 在事务外；只有封存那一下进事务
```

**8.5 为了两阶段工位先把 propose 当 execute 跑**

```python
# ✖ M1 不实现 propose。默默跑 execute 会跳过审批
if "propose" in phases:
    phases = ["execute"]
# ✔ PHASE_UNSUPPORTED，job DEAD，不创建沙箱
```

---

## 9. 风险与注意事项

- **D14 与 D10。** 成本只来自 transcript 的 `total_cost_usd`。控制面不调用模型，也不估算 token。
- **审批线程。** `CtlServer` 的间隔是真实 0.2 秒。`decide` 只写数据库。测试必须等审批行出现再 `decide`，不要用 `FakeClock.sleep` 去推动后台线程。
- **重试与卷泄漏。** `fresh` 留下旧卷。M1 不回收。不要在失败时 `remove_volume`，否则没法排查（架构文档 6.4）。
- **Windows。** attempt 目录用 `pathlib`。给 Docker 的 source 用 `Path.resolve()` 的字符串，并且不含逗号。
- **崩溃恢复。** `run` 对中间态会重做该阶段。`GATING` 重做可能再次 `warehouse.add`。`add` 对相同内容是幂等的（README 4.4）。`seal_many` 也必须能对已经封存的同一 Bean 再调用而不产生第二套血缘；若仓库做不到，Runner 在封存前先看 `warehouse.get(id).sealed`，已封存的跳过 `seal_many` 但仍写事件（事件不重复：先查 `events` 里是否已有该 job 的 `bean.sealed`）。
- **角色扩展。** M2 在 `run` 里按角色分派，不改 `submit` 的位置参数。`ROLE_UNSUPPORTED` 的分支会被 Sink/Source 分支替换，错误码留给 `judge`/`planner` 继续用。
