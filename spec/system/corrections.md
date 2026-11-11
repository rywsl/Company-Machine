# corrections：M1–M4 纠偏（M5-01）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M5-01**。
> 偏移清单与来龙去脉见 `plan/drift.md`（D-01 至 D-24）。本文件只写纠偏后**必须成立**的接口与行为，以及验收测试。
> 各组件规范已经同步修改的部分（`egress.md`、`governance.md`、`reconciler.md`、`snapshot.md`、README 4.2），以那里为准；本文件不重复它们的全文。

---

## 1. 目的与范围

**做什么（M5-01）**

- 删除 `plan/drift.md` 2.1 节列出的全部面向测试的特例（D-01 至 D-11）。
- 补齐 2.2 节的缺省路径与不变量（D-12 至 D-18）：
  - 治理流水线的三个缺省组件，包括新写的 `LineShadowRunner`；
  - 调和器的 `contracts` 注入点与 I4–I13；
  - 漂移只读 active 快照；
  - 去掉前缀比较、吞异常和跨模块私有访问。
- 新增本文件第 3 章的四个小接口：
  - `Snapshot.keys`；
  - `register_job_cap` 与 `model_caps`；
  - `Reconciler(..., contracts=...)`；
  - 产线运行的 `sink_mode`（影子运行不发布）。
- 让修正后的 M1–M4 受保护验收全部通过（`plan/drift.md` 4.2 节列出现有实现预期失败的测试）。

**不做什么**

| 不做 | 归属 |
|---|---|
| 补齐命令行（D-19、D-21） | M5-02，`commands.md` |
| `factory mcp`（D-20） | M5-05，`mcp.md` |
| `EpochRouter` 的调用方、唤醒票的生产者（D-23、D-24） | M5-04，`daemon.md` |
| 修改任何受保护的测试、夹具或规范 | 禁止。觉得它们错了，走 `plan/README.md` 第 5 节 |

---

## 2. 依赖

| 依赖 | 用到的接口 |
|---|---|
| `Snapshot`（`snapshot.md`） | 新增 `keys`；其余不变 |
| `Runner`（`runner.md`） | PROVISIONING 时调用 `register_job_cap` |
| `ModelProxy`（`egress.md` 5.5） | 读 `model_caps` |
| `ChangePipeline`、`ShadowRunner`、`ShadowReport`（`governance.md` 3.3） | 缺省组件 |
| `LineRunner`、`InlineExecutor`（`lines.md`） | 影子运行；新增 `sink_mode` |
| `ContractRunner`（`contract-tests.md`） | `run_validator`、`run_golden`、`run_sink_dry_run` |
| `Reconciler`（`reconciler.md`） | `contracts` 注入点 |
| `WakeQueue`（`foreman.md`） | 不变 |

不新增第三方依赖（D2）。

---

## 3. 接口

### 3.1 `mao.snapshot`

```python
class Snapshot:
    def keys(self, kind: str | None = None) -> list[str]
        # 本快照全部 Spec 键，升序。kind 给出时只列该 kind（"Station"、"Baseline" 等）。
        # 未知 kind 返回 []，不抛错。返回新列表，调用方修改它不影响快照
```

其他模块要遍历 Spec，只能用 `keys` 加 `doc` / `spec`。**禁止**读 `Snapshot` 的私有成员，包括 `getattr(snapshot, "_docs", ...)`（静态规则检查）。

### 3.2 `mao.egress`

```python
def register_job_cap(db: Database, job_id: str, station_key: str, cap_usd: float | None) -> None
```

签名与语义见 `egress.md` 3.3。迁移 `src/mao/migrations/024_model_caps.sql` 见 `egress.md` 4.2。

### 3.3 `mao.reconcile`

```python
class Reconciler:
    def __init__(self, ctx: Context, epochs: "EpochStore", *,
                 contracts: Callable[[Snapshot], "ContractRunner"] | None = None) -> None
```

见 `reconciler.md` 第 3 章、5.1 节 I8。

### 3.4 `mao.governance`

```python
class LineShadowRunner:                      # 实现 governance.md 3.3 的 ShadowRunner 协议
    def __init__(self, ctx: Context) -> None
    def run(self, cs: ChangeSet, *, candidate: Snapshot, active: Snapshot,
            rollout: Mapping[str, Any], golden_only: bool) -> ShadowReport
```

`mao.governance` 导出 `LineShadowRunner`。`ChangePipeline.__init__` 的三个可选参数为 `None` 时，按 `governance.md` 3.3 装上缺省实现。实例属性 `contracts`、`shadow`、`rebuild` 保存装好的对象，永远不是 `None`。

### 3.5 `mao.runs`：`sink_mode`

```python
class LineRunner:
    def start_run(self, line_ref: str, inputs: Mapping[str, str | Sequence[str]], *,
                  budget_usd: float | None = None, variants: Mapping[str, str] | None = None,
                  trigger: Mapping[str, Any] | None = None, use_cache: bool = True,
                  sink_mode: str = "execute") -> Run
        # sink_mode: "execute"（缺省，行为不变）| "dry-run"（影子运行）。其他值 → RUN_INPUT_INVALID

@dataclass(frozen=True)
class Run:
    ...                                       # 原有字段不变
    sink_mode: str                            # 新增字段，放在最后
```

迁移 `src/mao/migrations/025_run_sink_mode.sql`：

```sql
ALTER TABLE runs ADD COLUMN sink_mode TEXT NOT NULL DEFAULT 'execute';
```

---

## 4. 数据格式

### 4.1 影子运行的 regression 项

`ShadowReport.regressions` 的每一项：

| `kind` | 何时 | `subject` | `detail` |
|---|---|---|---|
| `node_failed` | 同一输入、同一节点：基线 run 中 `DONE`，候选 run 中 `FAILED` | 节点 id | `{"base_run", "candidate_run", "error"}`，`error` 是候选节点的 `error.code` |
| `type_changed` | 两边都 `DONE`，同一输出端口的 Bean 类型集合不同 | `节点 id/端口` | `{"base": [...], "candidate": [...]}` |
| `sink_dry_run` | 两阶段 Sink 的 dry-run 结果不是 `pass` | 节点 id | `{"result": contract_runs 的 id}` |
| `golden` | `golden_only` 时某个 GoldenSet 的结果不是 `pass` | GoldenSet 键 | `{"result": contract_runs 的 id}` |

列表按 `(kind, subject)` 升序。

### 4.2 事件

| kind | data | 何时 |
|---|---|---|
| `run.sink_skipped` | `{run, node, station}` | `dry-run` 的 run 跳过一个单阶段 Sink |
| `run.sink_dry_run` | `{run, node, station, result}` | `dry-run` 的 run 对两阶段 Sink 做了 dry-run |

---

## 5. 行为

### 5.1 删除特例（D-01 至 D-11）

逐条删除 `plan/drift.md` 2.1 节列出的代码。删除之后：

1. `src/mao` 里没有任何模块级代码修改标准库对象。
2. `ContractRunner` 不往快照写任何东西；GoldenSet、Baseline、converter 只从快照的公开接口读。
3. `ModelProxy` 不读磁盘上的 Registry。
4. `compile_dynamic` 产出的 `RunPlan.inputs` 与产线 `interface.inputs` 一致。缺少接口输入时，`start_run` 一律 `RUN_INPUT_INVALID`，不看产线名。
5. 沙箱不修改任何子进程的 stdout / stderr 字节。
6. 控制面不往 Source 的外部目录写文件。
7. 审批路由只来自工位规则（`approvals.md` 5.2）。复核 deny → job `FAILED`，`error.code = APPROVAL_REVIEW_SPLIT`。法定人数缺失 → job `FAILED`，`REVIEW_QUORUM_UNSET`。
8. `Database.query` 原样执行 SQL。所有用到 `commit` 列的 SQL 都写 `"commit"`。

### 5.2 治理流水线的缺省组件（D-12）

- `contracts` 缺省：`lambda snap: ContractRunner(ctx, snap, Runner(ctx, snap))`。V4（`governance.md` 5.7 第 8 步）的超时：工位有 `limits.timeout` 时用工位的；validator 与黄金样例用 `governance.git_timeout_s` 作上限传入 `timeout_s`。没有任何可用超时 → 该项 `V4_ERROR`，`detail.cause = "CONTRACT_TIMEOUT_REQUIRED"`。不要静默跳过。
- `shadow` 缺省：`LineShadowRunner(ctx)`，算法见 5.3。
- `rebuild` 缺省：`lambda run_id, snap: LineRunner(ctx, snap, Runner(ctx, snap)).rebuild(run_id, by="governance").id`。

### 5.3 `LineShadowRunner.run`

**`golden_only` 为真**：

1. 被改工位 = `cs.report.impact.stations`（`governance.md` 5.7 第 9 步）。没有报告时从候选与基线的差异重算。
2. 取候选快照里 `spec.station` 的名字与被改工位同名的全部 GoldenSet，按键升序。
3. 每个调用 `ContractRunner(ctx, candidate, Runner(ctx, candidate)).run_golden(key, None, timeout_s=None)`。结果不是 `pass` → 一条 `golden` regression。
4. 返回 `ShadowReport(ok=True, runs=GoldenSet 个数, spent_usd=各结果的观测 cost_usd 之和, regressions=...)`。没有相关 GoldenSet 时 `runs=0`，`ok=True`。

**否则**：

1. `n = rollout["shadow"]["runs"]`。取 `runs` 表中 `epoch == active.epoch`、`state == "SUCCEEDED"`、`sink_mode == "execute"` 的 run，按 `created_at` 降序、`id` 降序取前 `n` 个。
2. 每个基线 run：在候选快照上 `LineRunner(ctx, candidate, Runner(ctx, candidate), cache=None).start_run(run.line, run.inputs, budget_usd=run.budget_cap_usd, variants=run.variants, trigger={"on": "shadow", "changeset": cs.id, "base_run": run.id}, use_cache=False, sink_mode="dry-run")`，再 `run_to_completion(..., InlineExecutor(...))`。
3. 候选里不存在该产线（被退役或改名）：一条 `node_failed` regression，`subject = "*"`，`detail.error = "LINE_NOT_FOUND"`。
4. 按 4.1 节比较两边节点，生成 regression。
5. 返回 `ShadowReport(ok=True, runs=候选 run 数, spent_usd=候选 run 的 spent_usd 之和, regressions=...)`。

`ok=False` 只用于影子运行本身无法进行（例如候选快照加载失败）。这时 `regressions` 为空，`governance.md` 5.9 照样判失败。

### 5.4 `sink_mode = "dry-run"`

`LineRunner.tick` 推进到 `role == sink` 的节点时：

- 工位 `phases` 含 `propose`：调用 `ContractRunner(...).run_sink_dry_run(station, inputs=..., params=..., timeout_s=None)`，写 `run.sink_dry_run`。结果 `pass` → 节点 `DONE`，`outputs = {}`。其他 → 节点 `FAILED`，`error.code = "SHADOW_SINK_REJECTED"`。
- 否则：不提交 job，节点 `DONE`，`outputs = {}`，写 `run.sink_skipped`。

任何情况下都不调用 Sink 配方的 `run`，不写 `receipts`。`sink_mode = "execute"` 的行为与 M2 完全相同。

### 5.5 调和器（D-13 至 D-15）

按 `reconciler.md` 修改后的第 3 章、5.1、5.3 节实现。补充两点：

- **I11 的起点**：`refs/factory/active` 第一父链上，`epoch init` 登记的 commit（`epochs` 表中 `built_at` 最早的那一行）及其祖先不检查。`init` 是人的操作，不经过 ChangeSet。
- **I8 的超时**：用 `audit_once(timeout_s=...)` 传入的值。

### 5.6 其余（D-16 至 D-18）

- `base_epoch` 与 `current()` 用相等比较。
- `warehouse._receipt_ids` 去掉 `try`。
- 跨模块用到的方法改成公开名字（去掉下划线），写在该模块内。它们不是规范接口，可以自由命名，但不能和规范接口重名。
- 迁移编号按 `plan/drift.md` D-33：本任务新增 `024_model_caps.sql`、`025_run_sink_mode.sql`，并补上 `backends.md` 5.x 要求而实现缺失的写锁表 `026_backend_lock.sql`（`factory backend migrate` 的 `MIGRATE_IN_PROGRESS` 依赖它）。新迁移的编号必须大于已有最大编号。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `SHADOW_SINK_REJECTED` | `dry-run` 的 run 里，两阶段 Sink 的 dry-run 结果不是 `pass` | `run, node, result` |

其余沿用各组件规范已有的码：
- `RUN_INPUT_INVALID`：`sink_mode` 非法；
- `EGRESS_JOB_INVALID`：`model_caps` 无行；
- `V4_ERROR`：V4 设施故障。

---

## 7. 验收测试

共享夹具：`tests/acceptance/m2/world.py`、`tests/acceptance/m3/gov_world.py`。全部 `LocalProvider`、`FakeClock`、`CounterIds`。修正后的 M1–M4 验收也是本任务的验收项（见 `plan/tasks.yaml`）。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m5/test_m5_01_corrections.py` | `test_snapshot_keys` | `keys("Station")` 升序且含夹具工位；`keys()` 含各 kind；未知 kind 为 `[]`；改返回值不影响下次调用 |
| | `test_proxy_requires_registered_cap` | 只插 job 行、不登记上限时，模型代理返回 403 `EGRESS_JOB_INVALID`，不转发 |
| | `test_runner_registers_cap` | 经 `Runner` 跑完的 job 在 `model_caps` 有一行，`cap_usd` 等于工位的 `limits.budget_usd` |
| | `test_pipeline_defaults_installed` | 不注入任何替身时，`contracts`、`rebuild` 可调用，`shadow` 是 `LineShadowRunner` |
| | `test_v4_runs_by_default` | 新增一个 equivalence 恒假的 converter，缺省流水线 `advance` 后 `rejected`，`report.code == "V4_FAILED"` |
| | `test_shadow_dry_run_does_not_publish` | 基线 run 发布过一次之后，`LineShadowRunner` 在候选上重放：`runs == 1`，`receipts` 行数不变，发布目录没有新文件，事件有 `run.sink_skipped` |
| | `test_reconciler_i8_not_permanent` | 缺省 `Reconciler` 审计一次，没有 `invariant == "I8"` 的发现 |
| | `test_reconciler_i10_i12` | `spent_usd > budget_cap_usd` 的 run 产生 I10；绑定到未登记 epoch 的未终态 run 产生 I12 |
| | `test_drift_ignores_working_tree` | 只写在 Registry 工作区（未提交）的 Baseline 不产生漂移发现 |

```
uv run python tools/harness.py pytest tests/acceptance/m5/test_m5_01_corrections.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 9`。

---

## 8. 错误示范与正确示范

```python
# ✖ 验收夹具调用 mkdir 时没写 exist_ok，于是改标准库
Path.mkdir = _mkdir_parents
# ✔ 测试写错就阻塞任务，由设计方修正测试（plan/README.md 第 5 节）
```

```python
# ✖ 测试引用的 GoldenSet 夹具里没有，就在实现里内置一份
_BUILTIN = {("GoldenSet", "golden", "1"): {...}}
# ✔ 只读快照里真实存在的文档；没有就是 CONTRACT_NOT_FOUND
key = snapshot.key("GoldenSet", ref)
```

```python
# ✖ 替身没注入时把安全网当成“通过”
if pipeline.shadow is None:
    return ShadowReport(True, 0, 0.0, [])
# ✔ 构造时装上缺省实现，None 只表示“用缺省”
self.shadow = shadow if shadow is not None else LineShadowRunner(ctx)
```

```python
# ✖ 遍历 Spec 时读私有成员
for key, doc in snapshot._docs.items(): ...
# ✔
for key in snapshot.keys("Line"):
    doc = snapshot.doc(key)
```

```python
# ✖ 影子运行沿用正常路径，Sink 真的发布了
lines.start_run(line, inputs)
# ✔
lines.start_run(line, inputs, sink_mode="dry-run", use_cache=False)
```

---

## 9. 风险与注意事项

- **改动面大**：删除特例后，若干 M2–M4 测试会先失败（`plan/drift.md` 4.2 节）。按那张表逐项修，不要把特例换个写法加回来。静态规则会拦截常见写法，但规则拦不住的写法同样禁止。
- **影子运行的花费**：影子 run 走真实工位，会花钱。`governance.md` 5.9 用 `rollout.shadow.budget_usd` 兜底。`LineShadowRunner` 不另设上限，也不跳过 agent 工位。
- **I8 的耗时**：每轮审计重跑全部 validator 的样例。`daemon.md` 用独立的审计间隔控制频率。不要为了省时间只跑一部分。
- **并发**：影子 run 与正常 run 共用调度准入与资源队列（`scheduler.md`）。`dry-run` 的 run 也受背压约束。
