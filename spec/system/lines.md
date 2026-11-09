# 产线执行（lines.md）

> 规范性文档，M2。模块：`mao.runplan`（运行计划编译，纯函数）、`mao.runs`（run 存储、状态机、推进、触发）。
> 依据：架构文档 3.7、6.5（run 的状态）、9.3（隐式转换步骤）、10.1（epoch）；`spec/mrl/kernel/lines.mrl`；factory-profile 第 4、5 章。
> 跨组件约定见 `README.md`。本文件只能增加 README 第 4 节的接口，不能改变已列出的签名与语义。

---

## 1. 目的与范围

**做什么（M2）**

- 把一条 Line（在一个已通过检查的快照上）编译成**运行计划** `RunPlan`：业务步骤节点 + 按转换解析表插入的**隐式转换节点**、依赖关系、端口绑定、预算项。编译是纯函数。
- 创建 **run**，绑定快照的 epoch，按计划逐个提交 job（经 M1 `Runner`），收集产物，沿边把产物交给下游，直到 run 结束。
- run 级预算：每个节点提交前预留它的预算上限，结算后按实际花费记账；总额不超过 run 的上限（不变量 I10 的 run 部分）。
- 失败传播（fail-fast）、取消、单节点重试（`retry_node`）、以新快照重建（`rebuild`）。
- 触发器：`manual` 与 `bean`（物料到达即触发）；Bean 标签（`label`）。

**不做什么**

| 不做 | 归属 |
|---|---|
| `map` / `switch` / `until` / `subline` / `plan` 的运行期展开 | M4（`dynamic.md`）；M2 编译时遇到就报 `LINE_UNSUPPORTED` |
| 并发派发、资源队列、熔断、背压、cron 触发 | `scheduler.md`（实现本文件的 `Executor` 接口） |
| 缓存键与命中 | `cache.md`（本文件只规定在哪里调用） |
| Sink 幂等与回执、Source 的摄入 | `connectors.md` |
| 两阶段工位与审批路由 | `approvals.md` |
| epoch 存储、多 epoch 并存、ChangeSet、`BLOCKED` 后自动以新 epoch 重建 | M3（`governance.md`）；M2 只提供手动的 `rebuild` |

M2 的 run 只在**一个快照**上执行。`LineRunner` 持有一个 `Snapshot`，只能推进 `epoch` 与之相同的 run（否则 `EPOCH_MISMATCH`）。

---

## 2. 依赖

| 依赖 | 用到的接口（README 第 4 节） |
|---|---|
| `mao.snapshot.Snapshot` | `key`、`spec`、`doc`、`compiled`、`properties`、`is_subtype`、`type_key`、`result`（取 `resolutions` 与内核关系 `cost_item`） |
| `mao.values` | `bean_value`、`inputs_value`、`property_env` |
| `mao.runner.Runner`、`mao.jobs.Job` | `submit`、`run`；读取 job 的 `state`、`outputs`、`cost_usd`、`error` |
| `mao.warehouse.Warehouse` | `get`（检查输入 Bean 已封存、类型） |
| `mao.cache.JobCache`（`cache.md`） | `material`、`lookup`、`bind`（可选注入；不注入即不使用缓存） |
| `mao.connectors`（`connectors.md`） | Sink 节点的 job 由 `Runner` 按角色处理，本文件不直接调用 |
| `mao.base` | `Clock`、`IdSource`、`canonical_json`、`digest_bytes` |
| `mao.db.Database` | 事务；迁移 `010_runs.sql` |

---

## 3. 接口

### 3.1 `mao.runplan`（纯函数，不读数据库、不读时间）

```python
@dataclass(frozen=True)
class Binding:
    port: str                                # 本节点的输入端口
    source: Literal["input", "node"]         # 来源：产线接口输入 / 上游节点
    node: str                                # source == "node" 时为上游节点 id；否则 ""
    out_port: str                            # 接口输入名，或上游节点的输出端口名

@dataclass(frozen=True)
class PlanNode:
    id: str                                  # 业务步骤：步骤名；隐式转换：f"{step}.in.{port}~{converter_id}"
    kind: Literal["station", "converter"]
    step: str                                # 所属业务步骤（隐式转换节点为下游步骤名）
    station: str                             # Spec 键，如 "Station:dub@2"
    role: str                                # 工位角色：source/transform/converter/judge/planner/sink
    params: dict[str, Any]                   # 步骤中书写的参数原值（模板保留为字符串，提交时渲染）
    templated: tuple[str, ...]               # params 中是模板的参数名（升序）
    inputs: tuple[Binding, ...]              # 按 port 升序
    deps: tuple[str, ...]                    # 直接依赖的节点 id（升序、去重）
    budget_usd: float                        # 工位 limits.budget_usd；缺省 0.0
    implicit: bool                           # 隐式转换节点为 True
    resolution: dict[str, Any] | None        # 隐式转换节点：所在边在解析表中的结果（原样复制）

@dataclass(frozen=True)
class RunPlan:
    line: str                                # "Line:explainer-multilang@5"
    epoch: str
    nodes: tuple[PlanNode, ...]              # 拓扑序；同一层内按 id 升序（见 5.1 第 6 步）
    outputs: dict[str, tuple[str, str]]      # 接口输出名 → (节点 id, 端口)
    keep: tuple[tuple[str, str], ...]        # (节点 id, 端口)，升序
    inputs: dict[str, str]                   # 接口输入名 → 类型表达式（原样）
    input_requires: dict[str, tuple[str, ...]]
    budget_cap_usd: float | None             # Line 的 budget.cap_usd
    budget_items: tuple[tuple[str, float], ...]   # 与内核 cost_item 一一对应：(item, usd)，按 item 升序
    def node(self, node_id: str) -> PlanNode            # 不存在 → FactoryError("NODE_NOT_FOUND")
    def to_json(self) -> dict[str, Any]
    def digest(self) -> str                             # digest_bytes(canonical_bytes(to_json()))

def compile_run_plan(snapshot: Snapshot, line_ref: str) -> RunPlan
```

### 3.2 `mao.runs`

```python
class RunState(StrEnum):
    PENDING = "PENDING"          # 已创建、计划已编译，尚未推进
    RUNNING = "RUNNING"
    FAILING = "FAILING"          # 有节点失败：不再提交新节点，等待在途 job 结束
    CANCELLING = "CANCELLING"    # 已请求取消：同上
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
RUN_TERMINAL = {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}

class NodeState(StrEnum):
    WAITING = "WAITING"          # 依赖未完成
    SUBMITTED = "SUBMITTED"      # 已有 job，job 尚未进入终态
    DONE = "DONE"                # job SEALED，或缓存命中
    FAILED = "FAILED"            # job DEAD，或无法构造输入
    SKIPPED = "SKIPPED"          # run 失败或取消，节点不再执行

@dataclass(frozen=True)
class RunNode:
    run_id: str; node_id: str; state: NodeState; job_id: str | None; cache_hit: bool
    outputs: dict[str, list[str]]; params: dict[str, Any] | None
    reserved_usd: float; error: dict[str, Any] | None; updated_at: str

@dataclass(frozen=True)
class Run:
    id: str                      # "r-0001"
    line: str; epoch: str; state: RunState
    plan_digest: str
    inputs: dict[str, list[str]]             # 接口输入名 → Bean ID 列表（非 list 类型时恰好 1 个）
    variants: dict[str, str]                 # 业务步骤名 → variant（见 cache.md）
    budget_cap_usd: float; spent_usd: float; reserved_usd: float
    outputs: dict[str, list[str]]            # 接口输出名 → Bean ID 列表
    error: dict[str, Any] | None             # {"code","message","node"}
    trigger: dict[str, Any]                  # {"on": "manual", "by": "..."} | {"on": "bean", "bean": "...", "label": ...}
    rebuilt_from: str | None
    use_cache: bool                          # False：不查缓存（仍然 bind，见 cache.md 5.5）
    created_at: str; updated_at: str
    def value(self) -> dict[str, Any]        # MRL 的 run 值：{id, epoch, line, budget{cap_usd, spent_usd, remaining_usd}}

class Executor(Protocol):
    def dispatch(self, job_ids: Sequence[str]) -> None   # 把 PENDING 的 job 交给执行者
    def wait(self, timeout_s: float) -> None             # 等到“有 job 状态变化”或超时

class InlineExecutor:                                    # 同步：dispatch 时按 job id 升序逐个 runner.run(job_id)
    def __init__(self, runner: Runner) -> None

class LineRunner:
    def __init__(self, ctx: Context, snapshot: Snapshot, runner: Runner, *,
                 cache: "JobCache | None" = None) -> None
    def start_run(self, line_ref: str, inputs: Mapping[str, str | Sequence[str]], *,
                  budget_usd: float | None = None, variants: Mapping[str, str] | None = None,
                  trigger: Mapping[str, Any] | None = None, use_cache: bool = True) -> Run   # 创建 PENDING run
    def tick(self, run_id: str) -> list[str]             # 推进一步（第 5.3 节）；返回本次新提交的 job id（升序）
    def run_to_completion(self, run_id: str, executor: Executor, *,
                          max_ticks: int = 10_000, wait_s: float = 1.0) -> Run
    def cancel(self, run_id: str, *, by: str) -> Run
    def retry_node(self, run_id: str, node_id: str, *, by: str) -> Run
    def rebuild(self, run_id: str, *, by: str) -> Run    # 在本 LineRunner 的快照上新建 run（同样的输入、预算、variants）
    def get(self, run_id: str) -> Run
    def nodes(self, run_id: str) -> list[RunNode]        # 按计划中的节点顺序
    def plan(self, run_id: str) -> RunPlan               # 从存储的规范 JSON 还原

class TriggerEngine:
    def __init__(self, ctx: Context, snapshot: Snapshot, lines: LineRunner) -> None
    def process(self) -> list[str]                       # 处理游标之后的 bean.sealed 事件；返回新建的 run id（升序）
```

### 3.3 对 M1 接口的增补（只增加，不改变）

| 组件 | 增补 | 用途 |
|---|---|---|
| `Warehouse` | `label(bean_id, key, value, *, by) -> None`、`labels(bean_id) -> dict[str, str]` | Bean 标签（`bean` 触发器的 `label` 过滤） |
| `factoryctl submit` | 可重复的 `--label k=v` | 产物在封存时带上标签（由 `Runner` 在封存后调用 `Warehouse.label`） |
| `events` 表 | 新列 `run_id`（可空）与索引 | run 级事件 |
| `factory` CLI | `factory line run/ls/show/nodes/cancel/retry/rebuild/plan`、`factory bean label` | 第 3.4 节 |

### 3.4 CLI

```
factory line plan LINE@N [--json]                         # 打印运行计划（不创建 run）
factory line run LINE@N --in PORT=BEAN_ID ... [--budget USD] [--variant STEP=V ...]
                 [--provider docker|local] [--no-cache] [--json]   # 创建并同步执行到结束（InlineExecutor）
factory line ls [--state S] [--json]
factory line show RUN_ID [--json]       factory line nodes RUN_ID [--json]
factory line cancel RUN_ID              factory line retry RUN_ID NODE_ID
factory line rebuild RUN_ID             factory bean label BEAN_ID KEY=VALUE
factory trigger process                                   # 处理一次 bean 触发器
```

退出码沿用 README 4.10：run `SUCCEEDED` 为 0；`FAILED`/`CANCELLED` 为 1。

---

## 4. 数据格式

### 4.1 迁移 `src/mao/migrations/010_runs.sql`

```sql
CREATE TABLE runs (
  id             TEXT PRIMARY KEY,
  line           TEXT NOT NULL,
  epoch          TEXT NOT NULL,
  state          TEXT NOT NULL,
  plan           TEXT NOT NULL,              -- RunPlan.to_json() 的规范 JSON
  plan_digest    TEXT NOT NULL,
  inputs         TEXT NOT NULL,              -- {"port": ["sha256:…"]}
  variants       TEXT NOT NULL DEFAULT '{}',
  budget_cap_usd REAL NOT NULL,
  spent_usd      REAL NOT NULL DEFAULT 0,
  reserved_usd   REAL NOT NULL DEFAULT 0,
  outputs        TEXT NOT NULL DEFAULT '{}',
  error          TEXT,
  trigger        TEXT NOT NULL,
  rebuilt_from   TEXT REFERENCES runs(id),
  use_cache      INTEGER NOT NULL DEFAULT 1,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE TABLE run_nodes (
  run_id       TEXT NOT NULL REFERENCES runs(id),
  node_id      TEXT NOT NULL,
  ord          INTEGER NOT NULL,             -- 节点在计划中的序号
  state        TEXT NOT NULL,
  job_id       TEXT,
  cache_hit    INTEGER NOT NULL DEFAULT 0,
  outputs      TEXT NOT NULL DEFAULT '{}',
  params       TEXT,                         -- 渲染后的参数（规范 JSON）
  reserved_usd REAL NOT NULL DEFAULT 0,
  error        TEXT,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (run_id, node_id)
);
CREATE TABLE run_node_jobs (                -- 节点的全部 job（retry_node 会新增）
  run_id TEXT NOT NULL, node_id TEXT NOT NULL, job_id TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, node_id, job_id)
);
CREATE TABLE bean_labels (
  bean_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, set_by TEXT NOT NULL, at TEXT NOT NULL,
  PRIMARY KEY (bean_id, key)
);
CREATE TABLE trigger_fires (                -- 每个 (产线, Bean) 最多触发一次
  line TEXT NOT NULL, bean_id TEXT NOT NULL, run_id TEXT NOT NULL, at TEXT NOT NULL,
  PRIMARY KEY (line, bean_id)
);
CREATE TABLE cursors (name TEXT PRIMARY KEY, seq INTEGER NOT NULL);
ALTER TABLE events ADD COLUMN run_id TEXT;
CREATE INDEX events_run ON events(run_id, seq);
```

### 4.2 `RunPlan.to_json()`

```json
{
  "line": "Line:explainer-multilang@5",
  "epoch": "e-3f9a…",
  "inputs": {"brief": "brief.video@1"},
  "input_requires": {"brief": []},
  "budget_cap_usd": 12.0,
  "budget_items": [["dub_en", 3.0], ["explainer", 5.0], ["to_github", 0.0], …],
  "nodes": [
    {"id": "explainer", "kind": "station", "step": "explainer", "station": "Station:explainer@3",
     "role": "transform", "params": {}, "templated": [], "deps": [],
     "inputs": [{"port": "brief", "source": "input", "node": "", "out_port": "brief"}],
     "budget_usd": 5.0, "implicit": false, "resolution": null},
    {"id": "dub_en.in.subtitle~subs-convert@1", "kind": "converter", "step": "dub_en",
     "station": "Station:subs-convert@1", "role": "converter", "params": {}, "templated": [],
     "deps": ["explainer"],
     "inputs": [{"port": "in", "source": "node", "node": "explainer", "out_port": "subtitle"}],
     "budget_usd": 0.0, "implicit": true,
     "resolution": {"status": "convert", "chain": ["subs-convert@1"], "type": "subtitle.srt@1", …}}
  ],
  "outputs": {},
  "keep": []
}
```

所有键都写出（值为空也写），按规范 JSON 序列化；`digest()` 对它求摘要。

### 4.3 事件种类（写入 `events`，`run_id` 列有值）

| kind | data |
|---|---|
| `run.created` | `{line, epoch, plan_digest, inputs, budget_cap_usd, trigger}` |
| `run.state` | `{from, to, reason}` |
| `run.node` | `{node, from, to, job, cache_hit}` |
| `run.budget` | `{node, reserved_usd, spent_usd, run_spent_usd}`（预留、结算各一条） |
| `run.trigger` | `{line, bean, label}` |

---

## 5. 行为

### 5.1 编译运行计划 `compile_run_plan`

输入：快照（已零 error）与 `line_ref`。步骤：

1. `key = snapshot.key("Line", line_ref)`，`doc = snapshot.spec(key)`。
2. **构造检查**：任何步骤含 `subline`、`switch`、`map`、`until` → `FactoryError("LINE_UNSUPPORTED", step=…, construct=…)`。M2 只执行工位步骤。
3. **业务节点**：每个步骤一个节点，`id = 步骤名`。
   - `station` 为 `snapshot.key("Station", 步骤.station)`；`role` 取该工位的 `spec.role`；`budget_usd` 取 `limits.budget_usd`，缺省为 0.0。
   - `params` 原样保存；值是含 `{{` 的字符串时记入 `templated`。
4. **绑定与隐式转换**：对每个步骤 S 的每个输入 P（`in` 中的每一项）：
   - 来源写法 `$inputs.x` → `Binding(P, "input", "", "x")`；`步骤.端口` → `Binding(P, "node", 步骤, 端口)`。`$item` 只会出现在 map 中，第 2 步已经拒绝。
   - 在 `snapshot.result.resolutions` 中找 `line == key、step == S、port == P` 的解析结果 R。找不到时：`FactoryError("RUN_PLAN_INVALID")`。快照零 error 时，每条边都有解析结果。
   - `R.status == "direct"`：直接使用上面的绑定。
   - `R.status == "convert"`：对 `R.chain` 中的第 i 个转换 c（`converter_id` 形如 `subs-convert@1`）建立隐式节点：
     - `id = f"{S}.in.{P}~{c}"`，`kind = "converter"`，`implicit = True`，`resolution = R`；
     - `station = snapshot.key("Station", c)`；`params = {}`，转换工位的参数一律取默认值；
     - 输入 `in`：i = 0 时来自原来源，否则来自上一个隐式节点的 `out`。
     - 最后，S 的端口 P 改为绑定到链上最后一个节点的 `out`。
     - 链中出现重复的转换 id 时，`FactoryError("RUN_PLAN_INVALID")`。原因：节点 id 会冲突，内核的 `cost_item` 也会少算预算。
     - 转换工位有没有默认值的必填参数时，`FactoryError("RUN_PLAN_INVALID")`。
   - 其他 status：`FactoryError("RUN_PLAN_INVALID")`。它们都是 error，零 error 的快照里不会出现。
5. `deps` = 绑定中出现的上游节点 id，升序去重。
6. **排序**：按 Kahn 算法分层，每次取出所有入度为 0 的节点，层内按 `id` 的 Unicode 码点升序。有环时报 `RUN_PLAN_INVALID`；内核已经排除了环。
7. `outputs` 来自 `interface.outputs`（`from: 步骤.端口`）。来源端口所在的边如果插入了转换，接口输出仍取**业务节点**的原始端口，因为转换只服务于下游输入。`keep` 来自 `spec.keep`。
8. `budget_items`：每个节点一项。业务节点的 item 是步骤名，隐式节点的 item 是节点 id，usd 是 `budget_usd`。
   - 结果必须与内核关系 `cost_item` 中该产线的行**完全相同**（验收测试比较两者）。
   - 不一致时报 `RUN_PLAN_INVALID`，说明实现与内核语义不一致。不要“修正”其中一边。
9. `budget_cap_usd` 取 `spec.budget.cap_usd`，没有时为 `None`。

### 5.2 创建 run `start_run`

1. `plan = compile_run_plan(snapshot, line_ref)`。
2. **预算上限**：
   - `budget_usd` 给出时必须 > 0。
   - 产线有 `cap_usd` 时，`budget_usd` 只能更小，不能更大，否则 `RUN_BUDGET_INVALID`；不给时取 `cap_usd`。
   - 产线没有 `cap_usd` 且没有给出 `budget_usd` 时，`RUN_BUDGET_REQUIRED`：每个 run 都必须有有限的上限（I10）。
3. **输入检查**（run 级来料检验）：
   - 接口的每个输入都必须给出，且不能给出接口里没有的输入，否则 `RUN_INPUT_INVALID`。
   - 给出的每个 Bean 都必须已封存（`Warehouse.get(...).sealed`）。
   - Bean 的类型 id（`type` 去掉 minor）必须是接口类型的子类型（`snapshot.is_subtype`）。`list<T>` 接口接受一个或多个 Bean，每个都要检查；非 list 接口恰好接受一个。
   - 接口 `requires` 中的每个属性 p，都必须满足 `snapshot.properties(tid)[p].evaluate(property_env(bean)) is True`。
   - 任何一项不满足：`RUN_INPUT_INVALID`，`detail.problems` 逐项列出。
4. **variants** 的每个键必须是业务步骤名，值是非空字符串，否则 `RUN_INPUT_INVALID`。
5. **写入**：在一个事务里写 `runs`（`PENDING`）和全部 `run_nodes`（`WAITING`，`ord` 为计划序号），并写事件 `run.created`。
6. 返回 `Run`。`start_run` 不提交任何 job。

### 5.3 推进一步 `tick`

`tick` 是幂等的。它只读写数据库，同样的数据库状态得到同样的结果，每次调用在**一个事务**里完成。

1. 读取 run、全部节点、各节点当前 job 的状态。
   - run 已经是终态：返回 `[]`。
   - `run.epoch != snapshot.epoch`：报 `EPOCH_MISMATCH`。
   - run 为 `PENDING`：先转为 `RUNNING`。
2. **收割**：对每个 `SUBMITTED` 节点：
   - job 为 `SEALED` → 节点 `DONE`，`outputs = job.outputs`。
   - job 为 `DEAD` → 节点 `FAILED`，`error = job.error`。
   - 其他状态不变。
   - 每个节点 `DONE` 或 `FAILED` 时，都**结算**一次：
     - `run.reserved_usd -= node.reserved_usd`，`node.reserved_usd = 0`；
     - `run.spent_usd += job.cost_usd`（缓存命中为 0）；
     - 写一条 `run.budget` 事件。
3. **失败**：有节点 `FAILED` 且 run 为 `RUNNING` 时：
   - run 转为 `FAILING`；
   - `run.error = {code: "NODE_FAILED", message, node: 第一个失败节点的 id（按 ord）}`。
4. **提交**：仅当 run 为 `RUNNING` 时，按 `ord` 升序检查每个 `WAITING` 节点 N。N 的全部 `deps` 都已 `DONE` 时：
   1. **构造输入**：每个绑定取 Bean ID 列表。
      - 来源是接口输入时，取 `run.inputs[x]`；来源是上游节点时，取该节点 `outputs[out_port]`。
      - 端口类型不是 list，但列表长度不是 1 时：
        - 长度为 0（上游的可选输出没有产出）且端口是可选的 → 不传这个端口；
        - 否则节点 `FAILED`，错误码 `INPUT_MISSING`，然后回到第 3 步。
   2. **渲染参数**：对 `templated` 中的每个参数 X：
      - 取 `snapshot.compiled(f"{run.line}/steps.{N.step}.params.{X}")`；
      - 以 `{"run": run.value(), "inputs": 接口输入组成的记录}` 渲染（接口输入的 Bean 值由 `inputs_value` 构造），得到字符串。
      - 隐式节点没有参数。
   3. **缓存**：注入了 `cache`，并且节点角色在 `CACHEABLE_ROLES` 中（`cache.md` 5.1）时：
      - `material = cache.material(snapshot, N.station, 输入, 参数, variant=run.variants.get(N.step))`。
      - `run.use_cache` 为真时，`job = cache.lookup(material.key())`。命中时：
        - 节点立即 `DONE`，`cache_hit = 1`，`job_id = job.id`，`outputs = job.outputs`，不预留预算；
        - 写 `run.node` 事件，带 `cache_hit: true`；
        - 继续检查下一个节点。就绪判断一律使用第 4 步开始时的节点状态，所以本次 tick 中刚命中的节点，它的后继要到下一次 tick 才会就绪。这样更简单、更确定。
   4. **预留**：如果 `run.spent_usd + run.reserved_usd + N.budget_usd > run.budget_cap_usd`：
      - run 转为 `FAILING`，`error.code = "RUN_BUDGET_EXHAUSTED"`；
      - 停止提交。已提交的节点继续运行。
   5. **提交**：
      - `job = runner.submit(N.station, 输入, 参数)`。`submit` 内部做来料检验，失败时抛出 `INTAKE_REJECTED`；此时节点 `FAILED`，并保留原错误码。
      - 成功时：节点 `SUBMITTED`，`job_id = job.id`，`reserved_usd = N.budget_usd`，`run.reserved_usd += N.budget_usd`；写入 `run_node_jobs`，并写 `run.node`、`run.budget` 事件。
      - 第 3 步算出了 `material` 时，在同一事务里调用 `cache.bind(job.id, material)`。`use_cache` 为假时也要绑定。
5. **收尾**：
   - run 为 `FAILING` 或 `CANCELLING`，且没有 `SUBMITTED` 节点时：所有 `WAITING` 节点改为 `SKIPPED`，run 转为 `FAILED` 或 `CANCELLED`。
   - run 为 `RUNNING`，且全部节点都已 `DONE` 时：
     - `run.outputs[o]` 取 `outputs` 指向的节点端口的 Bean ID；
     - run 转为 `SUCCEEDED`。
6. 返回本次新提交的 job id，升序。

### 5.4 执行到结束 `run_to_completion`

```
for _ in range(max_ticks):
    before = (get(run_id).state, [n.state for n in nodes(run_id)])
    new = tick(run_id)
    if new: executor.dispatch(new)
    run = get(run_id)
    if run.state in RUN_TERMINAL: return run
    after = (run.state, [n.state for n in nodes(run_id)])
    if not new and after == before: executor.wait(wait_s)   # 没有任何进展时才等待
raise FactoryError("RUN_STUCK", run=run_id)
```

`InlineExecutor` 的行为：
- `dispatch(ids)`：按 id 升序逐个调用 `runner.run(job_id)`，同步执行到 `SEALED` 或 `DEAD`。job 等待审批时，阻塞在 `runner.run` 内部。
- `wait`：什么都不做。

因此，用 `InlineExecutor` 时，run 的执行顺序完全确定。

### 5.5 取消、重试、重建

- **cancel**：
  - run 为 `PENDING` 或 `RUNNING` → `CANCELLING`，`error.code = "RUN_CANCELLED"`，记录 `by`。
  - M2 不中断在途 job，等它们结束后，在下一次 `tick` 中转为 `CANCELLED`。
  - 其他状态 → `RUN_STATE_INVALID`。
- **retry_node**：
  - 只允许在 run 为 `FAILED` 或 `FAILING`，且该节点为 `FAILED` 时调用，否则 `RUN_STATE_INVALID`。
  - 节点改回 `WAITING`，清空 `error`。所有 `SKIPPED` 节点改回 `WAITING`。
  - run 改为 `RUNNING`，清空 `run.error`。如果还有其他 `FAILED` 节点，下一次 `tick` 会再次进入 `FAILING`。
  - 下一次 `tick` 会为该节点提交新 job。
  - 写 `run.state`（reason `retry_node by=<by>`）。
- **rebuild**：
  - 用原 run 的 `inputs`、`budget_cap_usd`、`variants`，在本 LineRunner 的快照上调用 `start_run(原 line 的名字@版本, …)`，并设置 `rebuilt_from`。
  - 原 run 必须是终态，否则 `RUN_STATE_INVALID`。
  - 已经完成的工位靠缓存（`cache.md`）避免重复执行。

### 5.6 触发器 `TriggerEngine.process`

1. 游标 `cursors.name = "trigger.bean"`，初值 0。
2. 读取 `seq > 游标` 且 `kind = "bean.sealed"` 的事件，按 `seq` 升序处理。
3. 对每个事件中的 Bean B，遍历快照中所有 `lifecycle == active` 的 Line L（按键升序）。对 L 的每条 `on: bean` 触发器：
   - **类型**：B 的类型 id 必须是触发器 `type` 的子类型。
   - **标签**：写了 `label: "k=v"` 时，`Warehouse.labels(B)` 中 `k` 的值必须等于 `v`。`label` 不是 `k=v` 形式时，报 `TRIGGER_INVALID`。
   - **接口**：L 必须恰好有一个接口输入，且该输入类型接受 B，否则 `TRIGGER_INVALID`，写事件后跳过这条触发器。
   - **去重**：`trigger_fires` 中已有 `(L, B)` → 跳过。
   - **启动**：`start_run(L, {该输入: B}, trigger={"on": "bean", "bean": B, "label": label})`，写入 `trigger_fires`，写 `run.trigger` 事件。
4. 把游标推进到最后处理的 `seq`，与第 3 步在同一事务里完成。
5. 返回新建的 run id，升序。
6. `TriggerEngine` 只创建 run，不推进它们；推进由调用方或调度器负责。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `LINE_UNSUPPORTED` | 产线含 `map` / `switch` / `until` / `subline`（M2 不支持） | `line, step, construct` |
| `RUN_PLAN_INVALID` | 解析结果缺失或不是 direct/convert；链中有重复转换；转换有无默认值的必填参数；预算项与内核 `cost_item` 不一致；有环 | `line, reason` |
| `RUN_INPUT_INVALID` | 接口输入缺失或多余；Bean 未封存、类型不兼容、不满足 requires；variants 非法 | `problems: [{port, bean, reason}]` |
| `RUN_BUDGET_REQUIRED` | 产线没有 `cap_usd`，也没有给出 `budget_usd` | `line` |
| `RUN_BUDGET_INVALID` | `budget_usd` ≤ 0，或大于产线的 `cap_usd` | `line, given, cap` |
| `RUN_BUDGET_EXHAUSTED` | 提交节点时预留会超出 run 上限（写入 `run.error.code`，不抛出） | `node, spent, reserved, need, cap` |
| `RUN_NOT_FOUND` | run id 不存在 | `run` |
| `NODE_NOT_FOUND` | 节点 id 不在计划中 | `run, node` |
| `RUN_STATE_INVALID` | 当前状态不允许该操作 | `run, state, op` |
| `EPOCH_MISMATCH` | run 的 epoch 与 LineRunner 的快照不同 | `run, run_epoch, snapshot_epoch` |
| `INPUT_MISSING` | 上游可选输出没有产出，而下游端口必填（写入节点 error） | `node, port, source` |
| `NODE_FAILED` | 节点的 job 进入 DEAD（写入 `run.error.code`） | `node, job` |
| `RUN_CANCELLED` | run 被取消（写入 `run.error.code`） | `by` |
| `RUN_STUCK` | `run_to_completion` 超过 `max_ticks` | `run, state` |
| `TRIGGER_INVALID` | 触发器的 label 格式错误，或产线接口不是恰好一个可接受该 Bean 的输入 | `line, reason` |

---

## 7. 验收测试

### 7.1 夹具

- **计划编译**的测试使用示例 Registry `spec/examples/registry`，以及通过补丁得到的变体：沿用 `mao.conformance` 的补丁格式，写到 `tmp_path`。
- **执行**的测试使用 `tests/fixtures/registry-m2/`。它在 M1 夹具 Registry（`testing.md`）的基础上增加：
  - 产线 `note-pipeline@1`：`write`（agent 工位，fake agent）→ `upper`（脚本工位）→ `publish-local@1`（Sink，见 `connectors.md`）。另有一条边需要隐式转换 `note-to-text@1`。
  - 产线 `note-dead@1`：中间工位必定失败（fake tool 退出码 3），用来验证 fail-fast。
  - 一条带 `on: bean` 触发器的产线。
  - 全部用 `LocalProvider`，确定性时钟与 ID（`FakeClock`、`CounterIds`）。

### 7.2 测试清单

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m2/test_m2_01_runplan.py` | `test_example_plan_nodes_and_order` | 示例产线的节点集合与拓扑序（逐项断言 id 顺序） |
| | `test_budget_items_equal_kernel_cost_item` | `budget_items` 与内核 `cost_item` 行完全一致 |
| | `test_implicit_converter_node` | 把 explainer 的字幕改为 VTT（演进场景 13.2 的补丁）后，出现 `dub_en.in.subtitle~subs-convert@1` 节点，依赖与绑定正确，`resolution` 与解析表一致 |
| | `test_two_step_chain` | 两步转换链生成两个串联的隐式节点 |
| | `test_unsupported_constructs` | map / switch / until / subline 各报 `LINE_UNSUPPORTED`，detail 指明步骤与构造 |
| | `test_plan_is_deterministic` | 两次编译的 `to_json()` 逐字节相同；打乱 Registry 文件读取顺序后摘要不变 |
| | `test_templated_params_recorded` | `to_s3.params.prefix` 记入 `templated` |
| `tests/acceptance/m2/test_m2_02_runs_store.py` | `test_start_run_creates_pending` | run 与节点行、`run.created` 事件 |
| | `test_input_validation` | 缺输入、多输入、未封存、类型不符、requires 不满足，分别报 `RUN_INPUT_INVALID`，`problems` 内容正确 |
| | `test_budget_rules` | `RUN_BUDGET_REQUIRED`、`RUN_BUDGET_INVALID`、override 只能更小 |
| | `test_run_value_shape` | `Run.value()` 符合 `mrl.factory.sites.RUN` 类型（用 MRL 编译一个读取全部字段的表达式并求值） |
| `tests/acceptance/m2/test_m2_03_line_runner.py` | `test_happy_path_inline` | `note-pipeline@1` 执行到 `SUCCEEDED`；节点顺序、job 序列、输出 Bean、`spent_usd` |
| | `test_implicit_converter_job_runs` | 隐式转换节点作为普通 job 执行，下游拿到转换后的 Bean |
| | `test_fail_fast` | `note-dead@1`：失败节点之后的节点 `SKIPPED`，run `FAILED`，`error.code == "NODE_FAILED"` |
| | `test_budget_reservation` | run 上限设为小于两个节点预算之和时，第二个节点不提交，run `FAILED`（`RUN_BUDGET_EXHAUSTED`） |
| | `test_retry_node` | 修复夹具后 `retry_node`，run 从 `FAILED` 回到 `RUNNING` 并最终 `SUCCEEDED` |
| | `test_cancel` | `cancel` 后在途 job 完成，未开始的节点 `SKIPPED`，run `CANCELLED` |
| | `test_tick_idempotent` | 在两次 `tick` 之间不改变数据库时，第二次返回 `[]`、数据库不变 |
| | `test_epoch_mismatch` | 用另一个快照的 LineRunner 推进 → `EPOCH_MISMATCH` |
| `tests/acceptance/m2/test_m2_04_triggers.py` | `test_bean_trigger_starts_run_once` | 封存一个匹配的 Bean 后 `process()` 新建一个 run；再次 `process()` 不重复 |
| | `test_label_filter` | 标签不匹配时不触发；`factoryctl submit --label` 设置的标签可触发 |
| | `test_trigger_invalid_interface` | 两个接口输入的产线 → 事件中记录 `TRIGGER_INVALID`，不建 run |
| | `test_cursor_survives_restart` | 新建 `TriggerEngine` 实例后从游标继续 |

任务验收命令（每个文件）：

```
uv run python tools/harness.py pytest tests/acceptance/m2/test_m2_01_runplan.py
# 断言：out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 7
```

---

## 8. 错误示范与正确示范

**8.1 只按解析表插入转换（I14）**

```python
# ✖ 错误：运行期自己调用 resolve 重新解析——同一个 epoch 内结果可能和编译期不同（例如转换库读取顺序不同）
chain = resolve(conv_lib, subs, always, u, g, d, r)["chain"]
# ✔ 正确：只使用快照的解析表（snapshot.result.resolutions），原样复制到计划里
r = next(x for x in snapshot.result.resolutions if (x["line"], x["step"], x["port"]) == (key, step, port))
```

**8.2 预算项与内核一致，不各算各的**

```python
# ✖ 错误：只累加业务步骤，忘了隐式转换的预算（内核的 cost_item 包含它们）
items = [(s, budget(s)) for s in steps]
# ✔ 正确：每个计划节点一项，并与内核 cost_item 比对；不一致就报 RUN_PLAN_INVALID，绝不“调整”
assert sorted(items) == sorted((i, u) for (l, i, u) in snapshot.result.model.rows("cost_item") if l == key)
```

**8.3 tick 必须幂等、可重入**

```python
# ✖ 错误：在内存里记“已经提交过”，进程重启后重复提交同一个节点
if node_id not in self._submitted: runner.submit(...)
# ✔ 正确：一切以数据库为准；在同一个事务里检查节点状态并写入 SUBMITTED 与 job_id
with db.tx() as c:
    if node_state(c, run_id, node_id) == "WAITING": ...
```

**8.4 失败时不要继续花钱**

```python
# ✖ 错误：某个节点 DEAD 后，继续提交其他“不受影响”的分支（可能是昂贵的工位或对外发布）
# ✔ 正确：fail-fast——进入 FAILING，不再提交新节点；在途 job 跑完后 FAILED；需要时人或厂长 retry_node
```

**8.5 参数模板用编译好的形式渲染**

```python
# ✖ 错误：用 str.replace / format 渲染 "{{ run.id }}"（D3 禁止）
# ✔ 正确：snapshot.compiled(f"{line}/steps.{step}.params.{name}").render({"run": run.value(), "inputs": ...})
```

---

## 9. 风险与注意事项

- **InlineExecutor 会阻塞**：审批等待期间 `runner.run` 不返回，整条 run 也停在那里。并发由 `scheduler.md` 解决。测试里，审批要用预先写好的决定或 `auto_if`。
- **预留是保守的**：节点预留的是它的预算上限，不是估计值，所以 run 上限偏紧时会提前停止。这是有意的设计：宁可不跑，也不超支（I10）。
- **重试与预算**：本文件假设 M1 `Runner` 的工位预算覆盖一个 job 的**全部 attempt**（README D14）。如果 `runner.md` 规定按 attempt 计，预留额必须改成 `budget × (1 + retries.rejected + retries.failed)`，同时更新本节和验收测试。
- **触发风暴**：一个 Source 一次产出大量 Bean 时，`bean` 触发器会一次建很多 run。M2 不限流；限流和背压见 `scheduler.md`（run 准入）。
- **跨 epoch**：M2 的 LineRunner 只服务一个快照。换快照之前要让在途 run 跑完，或者用 `rebuild`。M3 引入 epoch 存储后，按 run 的 epoch 加载快照。
- **接口输出与转换**：接口输出取业务节点的原始端口，不取转换后的结果。需要转换后的结果时，应当在产线里显式写一个转换步骤，或者在下游声明需要的类型。

---

## 10. 任务拆分

| ID | 标题 | 依赖 | 规范章节 | 允许修改的路径 | 验收 | 规模 |
|---|---|---|---|---|---|---|
| M2-01 | 运行计划编译（纯函数） | M1 全部 | 3.1、4.2、5.1、6 | `src/mao/runplan.py`、`src/mao/errors.py`、`tests/system/test_runplan_*.py` | `test_m2_01_runplan.py`（≥ 7 通过） | M |
| M2-02 | run 存储、状态机与输入检查 | M2-01 | 3.2、4.1、4.3、5.2 | `src/mao/runs.py`、`src/mao/migrations/010_runs.sql`、`src/mao/errors.py`、`tests/system/test_runs_*.py` | `test_m2_02_runs_store.py`（≥ 4 通过） | M |
| M2-03 | LineRunner 推进、InlineExecutor、取消、重试、重建、CLI | M2-02 | 3.2–3.4、5.3–5.5（缓存一步留空：`cache=None`） | `src/mao/runs.py`、`src/mao/cli.py`、`src/mao/errors.py`、`tests/system/test_line_runner_*.py` | `test_m2_03_line_runner.py`（≥ 8 通过） | L |
| M2-04 | Bean 标签与 bean 触发器 | M2-03 | 3.3、5.6 | `src/mao/runs.py`、`src/mao/warehouse.py`（只增 `label`/`labels`）、`src/mao/ctl/factoryctl.py`（只增 `--label`）、`src/mao/runner.py`（封存后写标签）、`src/mao/cli.py` | `test_m2_04_triggers.py`（≥ 4 通过） | M |

每个任务的通用验收（`plan/gates.yaml` 中的 `check` 列表）：ruff、格式、`harness lock --verify`、全部既有测试不回归。

`tests/fixtures/registry-m2/` 与 `tests/acceptance/m2/` 属于预先写好的验收材料，受保护（README 第 8 节）。任务执行者**不得**修改它们；认为夹具或测试有错时，按 `plan/README.md` 写记录并阻塞任务。
