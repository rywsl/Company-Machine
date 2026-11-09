# 动态结构（dynamic.md）

> 规范性文档，M4。模块：`mao.dynamic`（map / switch / until / subline 的展开，以及 Plan 在执行前的编译检查）。
> 依据：架构文档 10.2、10.3；`spec/mrl/kernel/lines.mrl`；`spec/mrl/factory-profile.md` 第 3.7、4.2、5 章；`spec/schemas/line.schema.json` 与 `records.schema.json` 的 `plan`。
> `lines.md` 第 5.1 步对这四种构造抛 `LINE_UNSUPPORTED`。那个函数保持不变，M2 验收继续依赖它。本文件是这些构造成为合法运行计划的地方。
> 跨组件约定见 `README.md`。本文件只增加接口，不改变已列出的签名与语义。

---

## 1. 目的与范围

**做什么（M4）**

- 用与编译期相同的规则展开 `map`、`switch`、`until`、`subline`。内核已经检查过的约束（类型、无环、预算倍数、`max_items`）在运行期不另写一套。
- planner 产出的 Plan 在封存前做 V1（schema）、V2（与产线相同的编译）和 `plan_bounds`。失败则 job `REJECTED`，反馈回到下一次 attempt。通过后把子 DAG 挂到**同一个 epoch** 的当前 run 上。
- 运行期才出现的边，按 factory-profile 第 5 章的 `resolve` 追加到该 epoch 的解析表。已有行不改（I14）。

**不做什么**

| 不做 | 归属 |
|---|---|
| 让 `mao.runplan.compile_run_plan` 不再抛 `LINE_UNSUPPORTED` | 禁止。M2 测试依赖该错误码 |
| 在 Plan 里定义新的 Type、Station、Line、Policy | 禁止。Plan 只能引用当前 epoch 已有的工位 |
| 控制面调用 LLM 来“修”编译错误 | 禁止（D10）。错误文本作为 Gate 反馈交给沙箱里的 planner CLI |
| 为展开次数发明全局上限 | 上限只来自步骤上的 `max_items` / `max_iter` / `max_parallel`，以及 planner 工位的 `plan_bounds` |
| B 形态机器、对象存储 | `gateway.md`、`backends.md` |

---

## 2. 依赖

| 依赖 | 用到的接口 |
|---|---|
| `mao.runplan` | 无动态构造时**调用** `compile_run_plan`，摘要必须一致。有动态构造时不调用它（它会抛 `LINE_UNSUPPORTED`） |
| `Snapshot` | `key`、`spec`、`doc`、`compiled`、`is_subtype`、`result.resolutions` |
| `mrl` | `compile_expr` 已由 `Snapshot.compiled` 完成；解析用与内核相同的 `resolve` |
| `LineRunner`、`Runner` | 提交展开后的节点。子产线用 `start_run` |
| `mao.values` | `bean_value`、`inputs_value` |
| `ConversionStore`（`foreman.md`） | 运行期 `no-converter` 时 `open_or_merge` |
| `EpochStore`（`governance.md`） | Plan 与 run 必须同一 commit。缺失转换时把 run 标 `BLOCKED` 的规则见 `governance.md` 5.8；Plan 编译失败不走 `BLOCKED`，走 job `REJECTED` |

---

## 3. 接口

```python
def compile_dynamic(snapshot: Snapshot, line_ref: str) -> "RunPlan"
    # 产线没有 map/switch/until/subline：结果与 compile_run_plan 的 to_json() 逐字节相同。
    # 有这些构造：返回展开后的 RunPlan（字面 map 在此时展开；动态 map 留下 kind == "map" 的占位节点）。
    # 快照有 error → 不在这里吞掉，调用方不应把未通过的快照传进来。

@dataclass(frozen=True)
class PlanCheck:
    ok: bool
    diags: tuple[dict[str, Any], ...]    # 每项 {code, subject, message}
    plan: "RunPlan | None"               # ok 时为可挂接的子计划
    feedback: str                        # 给 planner 下一次 attempt 的 Markdown

def compile_plan(snapshot: Snapshot, document: Mapping[str, Any], *,
                 planner_key: str, run: "Run") -> PlanCheck
    # 不抛 FactoryError 来表示 Plan 不合格（那是产物问题，不是设施故障）。
    # 设施故障（schema 文件读不到、MRL 求值崩溃）仍抛 FactoryError。

class DynamicRunner:
    def __init__(self, ctx: Context, snapshot: Snapshot, runner: "Runner",
                 lines: "LineRunner") -> None
    def expand(self, run_id: str) -> list[str]
        # 在 tick 提交节点之前调用。把占位节点展开成实例节点。返回新节点 id（升序）。
        # 幂等：同一输入重复调用不新增节点。
```

`RunPlan` 与 `PlanNode` 增加的 `kind` 值（在 M4 的计划里；M2 的 `compile_run_plan` 仍只产生 `station` 与 `converter`）：

| kind | 含义 | 是否提交 job |
|---|---|---|
| `station` | 与 M2 相同 | 是 |
| `converter` | 与 M2 相同 | 是 |
| `map` | 动态列表尚未展开的占位 | 否 |
| `switch` | 分支控制 | 否 |
| `until` | 迭代控制 | 否 |
| `subline` | 子产线 | 否（子 run 自己提交） |
| `aggregate` | 把 map 实例的输出按序号收成 list | 否 |

节点 id：

| 构造 | id |
|---|---|
| 字面或已展开的 map 第 i 项（从 0） | `{step}#{i}` |
| map 的聚合 | `{step}` |
| until 第 n 次（从 1） | `{step}@{n}` |
| switch / subline 控制节点 | `{step}` |
| 隐式转换 | 与 `lines.md` 相同：`{step}.in.{port}~{converter_id}`，实例上再加 `#i` 或 `@{n}` 后缀 |

---

## 4. 数据格式

不新开迁移文件。展开出的节点写入已有的 `run_nodes`（`lines.md` 的 `010_runs.sql`）。`params` 列保存渲染后的参数。map 的序号写在节点 id 里，不另造列。

Plan 文档必须符合 `records.schema.json` 的 `plan`。`metadata.generated_by` 是 planner 工位键，`metadata.run` 是当前 run id。`spec.steps` 的每一项再用 `line.schema.json` 的 step 对象校验。多出来的字段失败。

`plan_bounds` 以 `station.schema.json` 为准：`budget_usd` 是数，不是模板。架构 13.3 示例里的 `"{{ run.budget.remaining_usd }}"` 不符合 schema，本文件不实现那条模板。剩余预算在运行期另比（5.5）。

解析表追加行写在该 epoch 的侧车文件 `$FACTORY_ROOT/epochs/<commit>/resolution-runtime.json`（规范 JSON 数组）。行的形状与 `Snapshot.result.resolutions` 的一项相同，并带 `origin: "runtime"`。已有 `origin: "precomputed"` 的键禁止出现在这个文件里被改写。

---

## 5. 行为

### 5.1 与 M2 编译器的关系

`compile_dynamic`：

1. 读产线 spec。任一步有 `map`、`switch`、`until` 或 `subline` 时，走 5.2–5.4，不调用 `compile_run_plan`。
2. 都没有时，`return compile_run_plan(snapshot, line_ref)`。验收要求两次 `to_json()` 逐字节相同。

内核已经在零 error 的快照里排除了环、未绑定端口、以及“动态 map 有预算却没有 `max_items`”。本模块不再次发明这些规则。若快照零 error 而这里编不出计划，抛 `RUN_PLAN_INVALID`，`detail.reason` 说明哪一步与内核不符。不要“修”内核结果。

### 5.2 map

字段以 schema 为准：`over`、`as`（字面列表必填）、`max_parallel`（默认 1）、`max_items`、`overflow`（`reject` 或 `truncate`）。

**字面列表**（编译期就知道长度）：

- 长度是 N。每个元素一个节点 `{step}#{i}`，`params` 在提交时用 `Snapshot.compiled` 渲染，环境在 `lines.md` 的 step-param 环境上增加 `as` 的名字，值是该元素（string / 数 / bool）。禁止 `str.format`（D3）。
- 聚合节点 `{step}` 依赖全部实例。它的输出端口类型是 `list<工位输出类型>`，与 `lines.mrl` 的 `step_out_port` 一致。Bean id 列表按 i 升序拼接。
- 预算：每个实例预留工位的 `budget_usd`。实例个数必须等于内核 `multiplicity` 用的 items。不等 → `RUN_PLAN_INVALID`。

**动态来源**（`over` 是来源写法，类型必须是 `list<T>`）：

- 编译期放一个 `kind == "map"` 的占位节点，依赖来源节点。
- `expand` 在来源 `DONE` 之后：
  - 令 L 为来源列表长度，M 为 `max_items`（没有则只有在工位预算为 0 时才允许；否则快照本应已有 `E-BUDGET`，这里再见到就 `RUN_PLAN_INVALID`）。
  - L ≤ M，或没有 M 且预算为 0：展开 L 个实例。
  - L > M 且 `overflow == "reject"`：占位节点 `FAILED`，错误码 `DYNAMIC_OVERFLOW`。run 按 `lines.md` 进入 `FAILING`。
  - L > M 且 `overflow == "truncate"`：只取下标 `[0, M)`，保持上游给出的顺序。不按分数重排。排名若需要，由上游工位在封存列表之前完成。
  - 未写 `overflow` 且 L > M：与 `reject` 相同。
- `max_parallel`：一次 `tick` 最多新提交这么多个该 map 的 `WAITING` 实例，按序号升序。其余保持 `WAITING`。这不依赖调度器。
- `$item` 绑定到当前元素的 Bean。`as` 若存在，模板环境里的值是该 Bean 的 `bean_value`（factory-profile：动态来源的 `as` 是元素 Bean）。

### 5.3 switch

schema 要求 `on`、`cases`、`default`。控制节点不提交 job。

1. `on` 指向的来源 `DONE` 之后，按 `cases` 的列表顺序求值。表达式用 `snapshot.compiled` 在 `switch` 位置取出的形式，环境是 `{bean: bean_value(来源)}`。
2. 第一条结果为真的 `goto` 胜出。全假则用 `default`。
3. 求值抛 MRL 错误 → 控制节点 `FAILED`，错误码 `SWITCH_EVAL`，`detail.mrl` 为 MRL 错误码。不要选 default 来掩盖错误。
4. 没被选中的 `goto` 目标，以及只依赖这些目标的下游，标 `SKIPPED`。被选中的目标仍要等它自己的其他依赖 `DONE`。
5. 目标步骤名必须已在计划里（内核 `E-PORT` 已保证）。运行期找不到 → `RUN_PLAN_INVALID`，不要临时造步骤。

### 5.4 until 与 subline

**until**。schema 要求 `cond` 与 `max_iter`（1 到 10，schema 已限制）。执行是串行的：

1. 第一次提交 `{step}@1`，输入按步骤绑定，与普通工位相同。until 不把上一次输出接回自己的输入（那会成环，内核已禁止）。每次迭代的输入绑定相同。
2. 该次 job `SEALED` 后，用 `until` 位置的已编译表达式求值。环境：`gate` 为 `{attempt: n, judge_score: ...}`，工位没有 judge 时不放 `judge_score`；`out` 为这次输出端口的 Bean 值。
3. 结果为真：聚合节点 `{step}` 的输出等于这一次的输出，后续迭代不创建。
4. 结果为假且 n `< max_iter`：创建 `{step}@{n+1}`。
5. 结果为假且 n `== max_iter`：节点 `FAILED`，错误码 `UNTIL_EXHAUSTED`。不把最后一次输出交给下游。
6. 预算预留按内核倍数：`max_iter` 次的工位预算之和在第一次提交前就检查是否会超过 run 上限。会超过 → 与 `lines.md` 相同，`RUN_BUDGET_EXHAUSTED`，一次都不提交。

**subline**。目标必须是声明了 `interface` 的产线（内核已查引用）。

1. 控制节点就绪后，`lines.start_run(目标, 绑定好的输入, budget_usd=子产线 cap, trigger={"on": "subline", "parent": run_id, "step": step})`。子 run 的 epoch 等于父 run 的 epoch。用父 run 的快照，禁止换 commit。
2. 父节点预留的金额等于子产线 `cap_usd`（与内核 `step_cost` 一致）。子 run 自己的上限就是这笔预留。子 run 结束时，父 run 的 `spent_usd` 加上子 run 的 `spent_usd`，并释放预留。
3. 子 run `SUCCEEDED`：父节点 `DONE`，输出等于子 run 的接口输出。
4. 子 run `FAILED` 或 `CANCELLED`：父节点 `FAILED`，错误码 `SUBLINE_FAILED`，`detail.child` 为子 run id。父 run 进入 `FAILING`。
5. 子产线步骤不接受参数（内核 `E-PARAM`）。这里不再渲染参数。

### 5.5 Plan

`compile_plan` 的步骤。任一步失败则 `ok` 为假，`plan` 为 `None`，`feedback` 列出全部诊断（不要只留第一条）。

1. **V1**。文档符合 `plan` schema；每个 step 符合 line step schema。失败码记为 `E-SCHEMA`，本模块抛出的设施错误除外。
2. **不能定义新 Spec**。step 里出现的 `station` / `subline` 引用必须在当前 epoch 解析成功。出现 `kind: Type` 或其他文档 → `PLAN_INVALID`。
3. **plan_bounds**（都来自 planner 工位的 spec，缺字段则该项不查，不补默认数字）：
   - 每个非转换工位必须出现在 `allowed_stations`。不在 → `PLAN_BOUNDS`。
   - 角色为 `sink` 的还必须出现在 `allowed_sinks`。隐式转换工位不必出现在这两个列表里（架构 13.3：转换由转换库插入）。
   - 展开后的步骤数（字面 map 按元素个数，动态 map 按 `max_items`，until 按 `max_iter`，含隐式转换）`>` `max_steps` → `PLAN_BOUNDS`。
   - 任一 map 的展开个数 `>` `max_fanout` → `PLAN_BOUNDS`。`max_items` 与 `max_fanout` 都存在时，有效上限是二者中较小的那个，不另造第三个数。
   - 嵌套：Plan 本身深度为 1。Plan 里的 `subline` 再加 1。出现 planner 角色的步骤 → `PLAN_BOUNDS`（禁止 Plan 再产出 Plan）。深度 `>` `max_depth` → `PLAN_BOUNDS`。
4. **预算**。用与 `compile_run_plan` 相同的 `cost_item` 规则求和（含转换、含 map/until 倍数）。和 `>` `plan_bounds.budget_usd`（若声明了）→ `PLAN_BUDGET`。和 `>` `run.value()["budget"]["remaining_usd"]` → `PLAN_BUDGET`。等于允许。
5. **V2**。把 Plan 的 steps 当作一条临时产线做与 5.2–5.4 相同的绑定、解析与环检查。解析只调用 factory-profile 的 `resolve`，并与“若这条边已在解析表里则必须得到同一条链”核对。表里没有的键可以追加（`origin: runtime`）。`no-converter` → 诊断 `E-NO-CONVERTER`，并 `ConversionStore.open_or_merge`。这使 `ok` 为假，job 走 `REJECTED`，**不**把父 run 标成 `BLOCKED`。planner 可以换一个不需要该转换的 Plan。
6. 父产线里若有普通工位的输入绑到 planner 的 plan 端口：`compile_dynamic` 在编译父产线时就失败，错误码 `PLAN_SHAPE`。plan 端口不是下游物料。子 DAG 由本模块挂上，不通过 Bean 传递给另一个工位。

执行：

- planner job 的出厂检验调用 `compile_plan`。`ok` 为假：`GateReport.verdict = "reject"`，`feedback` 用 `PlanCheck.feedback`。不封存。
- `ok` 为真：封存 plan Bean（类型以工位输出端口为准），把子计划的节点插入父 run，依赖接在 planner 节点之后。子节点的 epoch 与父 run 相同。写事件 `run.plan`，`data` 含 plan Bean id 与子计划摘要。
- 子节点失败：父 run `FAILING`，与普通节点相同（`NODE_FAILED`）。

### 5.6 追加解析表

追加必须在一个事务里完成：读侧车文件，若键已存在且链不同 → `RUN_PLAN_INVALID`，不覆盖。键不存在则追加并写回。同一 epoch 的两次追加，输入相同则链相同，因为 `resolve` 是纯函数。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `DYNAMIC_OVERFLOW` | 动态 map 长度超过 `max_items` 且 overflow 不是 `truncate`（写入节点 error） | `step, length, max_items` |
| `UNTIL_EXHAUSTED` | 达到 `max_iter` 时条件仍为假（写入节点 error） | `step, max_iter` |
| `SWITCH_EVAL` | switch 条件求值失败（写入节点 error） | `step, index, mrl` |
| `SUBLINE_FAILED` | 子 run 不是 `SUCCEEDED`（写入节点 error） | `step, child, child_state` |
| `PLAN_INVALID` | Plan 含新 Spec，或 step 不是产线步骤形状 | `subject, reason` |
| `PLAN_BOUNDS` | 工位不在允许列表、步数、展开、深度超过该工位声明的界 | `bound, actual, limit` |
| `PLAN_BUDGET` | 子计划预算和超过 `plan_bounds.budget_usd` 或 run 剩余 | `need, remaining, cap` |
| `PLAN_SHAPE` | 父产线把 plan 端口当成普通工位输入 | `step, port` |
| `RUN_PLAN_INVALID` | 与内核倍数或已有解析表不一致 | `line, reason` |

`LINE_UNSUPPORTED` 仍只由 `mao.runplan.compile_run_plan` 抛出。`compile_dynamic` 不抛它。

Plan 不合格不是上表的抛出路径：`compile_plan` 返回 `ok=False`，诊断码用 `E-SCHEMA`、`E-NO-CONVERTER`、`E-BUDGET`、`PLAN_BOUNDS`、`PLAN_BUDGET`、`PLAN_INVALID`。设施故障才抛 `FactoryError`。

---

## 7. 验收测试

夹具在 `tests/fixtures/registry-m2/` 上打补丁，或在 `tmp_path` 建最小 Registry。`LocalProvider`、`FakeClock`、`CounterIds`。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m4/test_m4_01_dynamic.py` | `test_m2_compiler_still_rejects` | `compile_run_plan` 对含 map 的产线仍抛 `LINE_UNSUPPORTED` |
| | `test_plain_line_digest_matches_m2` | 无动态构造时，`compile_dynamic(...).to_json()` 与 M2 编译器逐字节相同 |
| | `test_literal_map_order` | `over: [en, ja]` 产生 `dubs#0`、`dubs#1`，聚合输出的 Bean 顺序与下标一致 |
| | `test_dynamic_overflow_reject` | 列表长度大于 `max_items` 且 `overflow: reject` → 节点 `DYNAMIC_OVERFLOW`，下游 `SKIPPED` |
| | `test_truncate_keeps_prefix` | `overflow: truncate` 只保留下标 `[0, max_items)`，不按内容重排 |
| | `test_switch_first_match` | 两条 `when` 都为真时走列表里的第一条，另一目标 `SKIPPED` |
| | `test_switch_default` | 全假时走 `default` |
| | `test_switch_eval_error` | 表达式类型错误 → `SWITCH_EVAL`，不走 default |
| | `test_until_stops` | 条件在第 2 次为真时不再创建 `@3`，且预留按 `max_iter` 计算 |
| | `test_until_exhausted` | `max_iter` 用尽仍为假 → `UNTIL_EXHAUSTED`，下游拿不到最后一次输出 |
| | `test_subline_same_epoch` | 子 run 的 epoch 等于父 run；子 run 失败 → `SUBLINE_FAILED` |
| `tests/acceptance/m4/test_m4_02_plan.py` | `test_plan_feedback_on_bounds` | 步骤数超过该工位 `max_steps` 时 job `REJECTED`，`feedback` 含 `PLAN_BOUNDS`，Bean 未封存 |
| | `test_plan_cannot_define_specs` | Plan 文档夹带 Type 文档 → `ok` 为假，`PLAN_INVALID` |
| | `test_budget_number_not_template` | `plan_bounds.budget_usd` 写成模板字符串 → schema 失败，不按模板求值 |
| | `test_budget_remaining` | 子计划和大于 run 剩余 → `PLAN_BUDGET`；等于剩余则通过 |
| | `test_missing_converter_rejects_plan` | 边无转换 → `E-NO-CONVERTER`，转换需求被 `open_or_merge`，父 run 不是 `BLOCKED` |
| | `test_sealed_plan_same_epoch` | 通过后 plan Bean 已封存，子节点出现在父 run 上，epoch 相同 |
| | `test_runtime_resolution_append_only` | 同一键第二次追加若链不同则 `RUN_PLAN_INVALID`，文件中的旧链不变 |

```
uv run python tools/harness.py pytest tests/acceptance/m4/test_m4_01_dynamic.py
uv run python tools/harness.py pytest tests/acceptance/m4/test_m4_02_plan.py
```

零失败、零跳过。

---

## 8. 错误示范与正确示范

**8.1 不要改 M2 编译器的拒绝行为**

```python
# ✖ 错误：在 compile_run_plan 里删掉 LINE_UNSUPPORTED，让 M2 测试开始展开 map
# ✔ 正确：M2 函数仍抛 LINE_UNSUPPORTED。M4 走 compile_dynamic
```

**8.2 运行期解析不能改已有链**

```python
# ✖ 错误：动态边上重新 resolve 之后写回预计算表，覆盖 epoch 构建时的链
# ✔ 正确：键已存在则必须相同，否则 RUN_PLAN_INVALID。新键才追加，origin 为 runtime
```

**8.3 截断不发明排序**

```python
# ✖ 错误：overflow truncate 时按 judge 分数或文件名重排，取“前 20 名”
# ✔ 正确：保持上游列表顺序，取下标 [0, max_items)
```

**8.4 Plan 失败是退回，不是改 run 状态机去硬跑**

```python
# ✖ 错误：编译失败仍封存 plan Bean，并在 tick 里跳过非法工位
# ✔ 正确：verdict reject，feedback 含诊断，不封存。planner 的下一次 attempt 能读到 feedback
```

**8.5 模板仍用已编译形式**

```python
# ✖ 错误：map 的 "{{ lang }}" 用 str.replace 替换
# ✔ 正确：snapshot.compiled(...).render({..., as_name: element})
```

---

## 9. 风险与注意事项

- **占位节点与 tick 幂等**。`expand` 必须先看 `run_nodes` 里是否已有 `{step}#0`。进程重启后不得再插一遍。
- **预留与 map**。动态 map 在展开前不知道实际长度时，按 `max_items` 预留（与内核倍数一致）。实际展开更少时，在展开的同一事务里把多预留的部分释放。不要等到全部 DONE 才发现预算账对不上。
- **子 run 与父 run 的预算**。子 run 的花费已经含在父 run 的 `spent_usd` 里之后，报表不要再加一次。验收可以核对父 spent 等于各子 spent 之和加上父自身节点花费。
- **switch 的共享下游**。一个步骤既是某个 `goto` 又被别的步骤绑定：只有当它的每一条入边都来自未选中的分支时才 `SKIPPED`。不要因为它出现在 `cases` 里就无条件跳过。
- **Windows 路径**。侧车 JSON 用 `pathlib` 写在 epoch 目录旁，不写进 Git 工作区，避免把 Registry 弄脏。
---

## 10. 任务拆分

第 7 章已经把测试分成两个文件。本表只照这个分法，不另拆。

| ID | 标题 | 依赖 | 规范章节 | 允许修改的路径 | 验收 | 规模 |
|---|---|---|---|---|---|---|
| M4-01 | `compile_dynamic` 与动态构造展开 | M2-03 | 3、5.1–5.4、7 的 `test_m4_01_dynamic.py` | `src/mao/dynamic.py`、`src/mao/runs.py`（只增展开前的 `expand` 调用）、`src/mao/errors.py`、`tests/system/test_dynamic_*.py` | `test_m4_01_dynamic.py`（第 7 章该文件全部测试通过） | L |
| M4-02 | Plan 编译、预算与运行期解析表追加 | M4-01、M3-05 | 3、4、5.5、7 的 `test_m4_02_plan.py` | `src/mao/dynamic.py`、`src/mao/errors.py`、`tests/system/test_plan_*.py` | `test_m4_02_plan.py`（第 7 章该文件全部测试通过） | M |

`compile_run_plan` 对 map / switch / until / subline 仍抛 `LINE_UNSUPPORTED`。本任务不改那个函数的拒绝行为。`tests/acceptance/m4/` 属于预先写好的验收材料。任务执行者不得修改它们。
