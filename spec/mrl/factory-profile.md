# MRL 工厂 profile 1.0（规范）

> 本文档规定如何用 MRL（见 [mrl-1.0.md](mrl-1.0.md)）检查一个工厂 Registry：哪些文件是 Spec、从 Spec 抽取哪些事实、
> 嵌入的表达式在什么类型环境下编译、转换怎样解析、诊断怎样产生和输出。
> 参考实现：`src/mrl/factory/`；规范性规则：`spec/mrl/kernel/*.mrl`；一致性用例：`spec/mrl/conformance/factory/`。
>
> 本文档中的“必须”“不得”是规范要求。实现与本文档不一致时，以一致性用例为准，并且必须修正其中一方。

## 目录

1. 概述
2. Registry 布局
3. 抽取映射
4. 表达式位置与类型环境
5. 转换解析
6. 检查流程与输出
7. 诊断码
8. 一致性用例（kind: factory）
9. 内核与宪法的边界
10. 演进规则

---

## 1. 概述

### 1.1 分工

```
Registry（Git 目录）
  │  YAML 1.2 core 读取、目录布局（E-YAML、E-LAYOUT）
  ▼
Spec 文档 ──JSON Schema（spec/schemas/）──▶ 未通过：E-SCHEMA，文档不参与后续步骤
  │
  ▼  抽取（第 3 章，确定性的纯函数）
@input 事实（kernel/schema.mrl 声明）
  │
  ▼  内核规则（kernel/*.mrl） + 宪法（constitution/*.mrl）：MRL-R 求值
diag(code, subject, message)  ◀── 嵌入表达式的静态检查（第 4 章）以 host_diag 的形式汇入
  │
  ▼  码表校验、位置映射、排序（第 6 章）
诊断报告 + 转换解析表
```

| 部分 | 性质 | 谁可以改 |
|---|---|---|
| JSON Schema（`spec/schemas/`） | 结构约束 | 维护者（随 profile 版本演进） |
| 抽取映射（本文档第 3 章，`extract.py`） | 规范性的纯函数 | 维护者 |
| 内核规则（`spec/mrl/kernel/`） | 规范性的边界规则，可执行 | 维护者；必须同步更新一致性用例 |
| 宪法（每个 Registry 的 `constitution/`） | L0 不变量，只能追加诊断 | **只有人** |
| 嵌入表达式的类型环境（第 4 章，`sites.py`） | 规范性 | 维护者 |

### 1.2 两条保证

1. **确定性**：同一个 Registry（同样的文件内容）总是得到同样的诊断、同样的顺序、同样的转换解析表。抽取、求值、解析都是纯函数，不读取时间、网络或环境变量（`MRL_SPEC_DIR` 只决定 schema 与内核从哪里读取）。
2. **单调性**：宪法模块只能向 `diag` 追加行，不能为内核的关系写规则，也不能读取 `diag`（MRL-R 的归属规则，见 mrl-1.0.md 第 8 章）。因此**加上宪法只会让检查更严，永远不会放松内核的检查**。

---

## 2. Registry 布局

### 2.1 目录布局

只有下列位置的 YAML 文件是 Spec。`*` 只匹配一级目录或文件名，不跨越 `/`。`.yml` 与 `.yaml` 等价。

| 位置 | kind |
|---|---|
| `types/*.yaml` | Type |
| `machines/*/machine.yaml` | Machine |
| `runtimes/*.yaml` | OperatorRuntime |
| `stations/*.yaml`、`stations/*/station.yaml`、`converters/*/station.yaml` | Station |
| `lines/*.yaml` | Line |
| `policies/*.yaml` | Policy |
| `goldens/*.yaml` | GoldenSet |
| `baselines/*.yaml` | Baseline |
| `constitution/*.mrl` | 宪法模块（MRL-R 源文件，不是 YAML） |

- 其他文件（样例、Dockerfile、脚本、`stations/x/fixtures/*.yaml` 等）**不是** Spec，检查时忽略。
- 一个文件可以包含多个 YAML 文档（`---` 分隔）；空文档被跳过，但文档序号仍按出现顺序计数（从 0 开始）。
- 文件按相对路径的字典序处理；宪法模块也按文件名字典序加载。

### 2.2 读取规则

- 采用 YAML 1.2 core 的标量规则：布尔值只有 `true/false`（及 `True/TRUE/False/FALSE`），`on/off/yes/no` 是字符串；不做时间戳解析；整数只有十进制。
- 重复的键、非字符串的键都是 `E-YAML`；语法错误也是 `E-YAML`。出错的文件整个不参与后续步骤。
- 文档的 `kind` 必须与所在位置一致，否则报 `E-LAYOUT`，该文档不参与后续步骤。记录类 kind（Plan、ChangeSet、ConversionRequest）放进 Spec 目录同样是 `E-LAYOUT`。
- 每个文档按其 kind 的 JSON Schema 校验（`spec/schemas/`，Draft 2020-12）。每个错误一条 `E-SCHEMA`（每个文档最多 20 条），**未通过的文档不参与后续步骤**。因此引用它的地方会报 `E-REF`——这是预期行为。
- 类型表达式无法解析（例如缺少 `>`、使用保留名 `any`/`list`/`ref` 作为类型名）时报 `E-SCHEMA`，该文档同样整体不参与后续步骤。

### 2.3 Spec 键与主体

**Spec 键**：`<kind>:<name>@<version>`。

| kind | version 写法 | 例子 |
|---|---|---|
| Type | 字符串 `"M.m"` | `Type:video.mp4@1.3` |
| 其他 | 正整数 | `Station:explainer@3`、`Line:explainer-multilang@5` |

**类型 id**：`name@major`，例如 `video.mp4@1`。所有语义关系（子类型、属性、facts）都用类型 id；同一个 major 内的 minor 双向兼容，`name@major` 总是解析到该 major 下**最新的非 retired minor**。

**诊断主体（subject）**：Spec 键，或 Spec 键 + `/` + 子路径。主体是诊断的稳定标识，一致性用例按主体匹配。

| 主体写法 | 指向 |
|---|---|
| `Station:K/inputs.P`、`Station:K/outputs.P` | 端口 |
| `Station:K/outputs.P.accept[i]` | 验收标准 |
| `Station:K/inputs`、`Station:K/outputs` | 端口集合（缺端口、多端口） |
| `Station:K/machine`、`/recipe`、`/operator`、`/policy`、`/limits`、`/params.X` | 对应字段 |
| `Station:K/converts`、`/equivalence`、`/equivalence[i]`、`/fixtures` | converter 字段 |
| `Station:K/idempotency_key`、`/approval.propose`、`/approval.in_run`、`/gate.judge` | 对应字段 |
| `Station:K/monitors.<name>`（M11） | `spec.monitors.<name>.when` |
| `Type:K/extends`、`/fields.F`、`/facts.F`、`/properties.P`、`/always`、`/validators.V` | 类型字段 |
| `Machine:K/source`、`/recipes.R.<路径>` | 机器字段与配方模板 |
| `OperatorRuntime:K/invoke.argv[i]`、`/env.NAME` | 运行时模板 |
| `Line:L/steps.S` | 步骤 |
| `Line:L/steps.S.in`、`/steps.S.in.P`、`/steps.S.in.P.via[i]` | 绑定 |
| `Line:L/steps.S.params`、`/steps.S.params.X` | 步骤参数 |
| `Line:L/steps.S.map`、`/steps.S.switch`、`/steps.S.switch.cases[i]`、`/steps.S.until` | 动态构造 |
| `Line:L/interface.inputs.P`、`/interface.outputs.P`、`/budget`、`/keep` | 产线字段 |
| `Policy:K` | 策略 |
| `File:<相对路径>`、`File:<相对路径>#<序号>` | 无法确定 Spec 键时（YAML 错误、信封不完整） |
| `Registry` | 整个 Registry（例如缺少宪法） |

---

## 3. 抽取映射

抽取是从“已通过 schema 校验的文档”到 `kernel/schema.mrl` 中 `@input` 关系的**确定性纯函数**。任何实现都必须对同一组文档产出完全相同的事实集合。下表中 `K` 是文档的 Spec 键；“默认”指字段缺省时使用的值；“引用”指同时产出一条 `ref(主体, kind, 目标)` 事实（目标按书写原样，可能缺版本）。

### 3.1 类型表达式

```
texpr ::= "any" | name [ "@" major [ "." minor ] ] | "list<" texpr ">" | "ref<" texpr ">"
```

- **规范串（id）**去掉 minor：`video.mp4@1.2` 的 id 是 `video.mp4@1`；`list<video.mp4@1.2>` 的 id 是 `list<video.mp4@1>`。钉住的 minor 只用于引用检查。
- Registry 中出现的每个类型表达式及其全部子表达式都登记为 `texpr(e, form, arg)`：

| form | e | arg |
|---|---|---|
| `named` | 类型 id，例如 `video.mp4@1` | 同 e（类型 id） |
| `any` | `any` | 空串 |
| `list` / `ref` | 例如 `list<video.mp4@1>` | 子表达式的 id |

- `any` 总是登记。每个 Station 输出端口的类型 T，额外登记 `list<T>`（带 map 的步骤把输出聚合为 `list<T>`，见 `kernel/lines.mrl`）。
- 每个 `named` 子表达式产出一条 `ref(主体, "Type", 原样书写)`。

### 3.2 所有 kind 共有

| 事实 | 来源 |
|---|---|
| `spec(K, kind, name, version, lifecycle)` | version 一律转为字符串；lifecycle 默认 `active` |
| `spec_doc(K, file, doc)` | 文件相对路径与文档序号（同一个 K 出现多次时有多行 → `E-DUPLICATE`） |
| `loc(subject, file, doc, path)` | 每个登记过的主体及其 JSON 路径（用于定位，不参与内核规则） |

### 3.3 Type

| 事实 | 来源与默认 |
|---|---|
| `type_version(K, id, minor, lifecycle)` | id = `name@M`，minor 为整数 |
| `type_kind(K, kind)` | `spec.kind`，默认 `atomic` |
| `type_extends_decl(K, parent_id)` | `spec.extends` 解析为 named 时；引用（主体 `K/extends`） |
| `type_field(K, F, texpr)` | `spec.fields`；引用（主体 `K/fields.F`） |
| `type_item(K, texpr)`、`type_target(K, texpr)` | `spec.item`、`spec.target` |
| `type_fact_decl(K, F, ftype)` | `spec.facts`（ftype 为 MRL 类型串，如 `float`、`list<string>`） |
| `type_validator(K, V, image)` | `spec.validators[i]` |
| `validator_emits(K, V, F)` | `validators[i].emits` 的每一项 |
| `validator_fixture(K, V, polarity, path)` | `validators[i].fixtures.accept/reject` 的每一项 |
| `type_property(K, P)` | `spec.properties` 的每个键；同时产出一个 `property` 表达式位置 |
| `type_always(K, P)` | `spec.always` 的每一项 |

`spec.meta_schema` 不产出事实，只用于构造类型环境（第 4 章）。

### 3.4 Machine

| 事实 | 来源与默认 |
|---|---|
| `machine(K, form)` | `spec.form` |
| `machine_image(K, image)` | `spec.runtime.image`（使用 compose 时没有这条） |
| `machine_source(K, repo, commit)` | `spec.source` |
| `recipe(K, R)` | `spec.recipes` 的每个键 |
| `recipe_in(K, R, P, texpr, optional)`、`recipe_out(...)` | `recipes.R.inputs/outputs.P`；optional 默认 false |
| `recipe_param(K, R, X, ptype)` | `recipes.R.params.X.type` |
| `recipe_run(K, R)` | 配方有 `run`（M2-09） |

表达式位置（`recipe-template`）：`recipes.R.task_template`、`recipes.R.call` 中的字符串、`recipes.R.run` 中的字符串、`recipes.R.outputs.P.harvest`、`recipes.R.receipt.target`、`recipes.R.receipt.locator.*`（后两项自 M2-09）——只有含 `{{` 的字符串才是模板。

### 3.5 OperatorRuntime

| 事实 | 来源 |
|---|---|
| `runtime(K, kind)` | `spec.kind`（`agent-cli` / `script`） |
| `runtime_model(K, M)` | `spec.models_allowed` 的每一项 |
| `runtime_volume(K, image)` | `spec.inject.volume` |

表达式位置（`runtime-template`）：`spec.invoke.argv[i]`、`spec.invoke.resume[i]`（M13）、`spec.env.NAME` 中含 `{{` 的字符串。

### 3.6 Station

| 事实 | 来源与默认 |
|---|---|
| `station(K, role)` | `spec.role` |
| `station_machine(K, machine, recipe)` | 引用 Machine（主体 `K/machine`） |
| `station_operator(K, runtime, model)` | model 默认空串；引用 OperatorRuntime（主体 `K/operator`） |
| `station_policy(K, policy)` | 引用 Policy（主体 `K/policy`） |
| `station_param(K, X, ptype, has_default)` | `spec.params.X` |
| `in_port(K, P, texpr, optional, requires)`、`in_require(K, P, prop)` | `spec.inputs.P`；optional 默认 false，requires 默认 `[]` |
| `out_port(K, P, texpr, optional, guarantees)`、`out_guarantee(K, P, prop)` | `spec.outputs.P`；每条 `accept[i]` 是一个 `accept` 表达式位置 |
| `station_budget(K, usd)` | `spec.limits.budget_usd`（缺省时没有这条） |
| `station_phases(K, phases)` | 默认 `["execute"]` |
| `converts(K, from, to, lossless, preserves)`、`conv_preserve(K, p)` | `spec.converts`；preserves 默认 `[]` |
| `conv_cost(K, usd, p50)` | `spec.cost.usd_est`、`spec.cost.p50_s`，默认 0、0（只在有 converts 时产出） |
| `equivalence_count(K, n)` | `spec.equivalence` 的条数（总是产出）；每条是一个 `equivalence` 表达式位置 |
| `station_fixture(K, polarity, path)` | `spec.fixtures.accept/reject` |
| `sink_idempotency(K, template)` | `spec.idempotency_key`；同时是一个 `idempotency` 模板位置 |
| `sink_verify(K, method)` | `spec.verify_receipt` |
| `approval(K, phase, mode)` | `spec.approval.propose/in_run`；mode 为 `auto/foreman/review`，写成 `{auto_if, else}` 时为 `conditional`（`auto_if` 是一个 `approval` 表达式位置） |
| （只有引用） | `spec.gate.judge.runtime`（主体 `K/gate.judge`）；`spec.plan_bounds.allowed_stations[i]`、`allowed_sinks[i]` |

### 3.7 Line

| 事实 | 来源与默认 |
|---|---|
| `line(K)` | — |
| `line_input(K, P, texpr, requires)`、`line_input_require(K, P, prop)` | `spec.interface.inputs.P`；requires 默认 `[]` |
| `line_output(K, P, step, port)` | `spec.interface.outputs.P.from`（`步骤.端口`） |
| `line_output_type(K, P, texpr)` | `spec.interface.outputs.P.type`（可选） |
| `line_budget(K, usd)` | `spec.budget.cap_usd` |
| `step(K, S, kind, target)` | kind 为 `station` / `subline` / `switch`；target 为原样引用，switch 为空串；引用 Station 或 Line（主体 `K/steps.S`） |
| `bind(K, S, P, src_kind, src_step, src_port)` | `steps.S.in.P`；来源写法见下表 |
| `bind_via(K, S, P, mode, chain)` | 没有 via：`auto`、`[]`；`via: none`：`none`、`[]`；`via: [...]`：`explicit`、该列表（每项引用 Station，主体 `K/steps.S.in.P.via[i]`） |
| `step_param(K, S, X, vtype)` | vtype：含 `{{` 的字符串为 `template`（同时是 `step-param` 模板位置），否则按字面值为 `string/int/float/bool` |
| `step_map(K, S, items, max_parallel)` | items：字面列表的长度；动态来源时为 `max_items`，没有则 -1；max_parallel 默认 1 |
| `map_over(K, S, src_kind, src_step, src_port)` | 动态来源（`map.over` 是来源写法而不是列表）时 |
| `step_until(K, S, max_iter)` | `until.cond` 是一个 `until` 表达式位置 |
| `switch_on(K, S, src_kind, src_step, src_port)`、`switch_target(K, S, T)` | 每个 `cases[i].goto` 与 `default` 各一条 target；每个 `cases[i].when` 是一个 `switch` 表达式位置 |
| `keep(K, step, port)` | `spec.keep` 的每一项 |
| （只有引用） | `spec.trigger[i].type` |

**来源写法**：

| 写法 | src_kind | src_step | src_port |
|---|---|---|---|
| `$inputs.x` | `input` | 空串 | `x` |
| `步骤.端口` | `step` | 步骤 | 端口 |
| `$item` | `item` | 空串 | 空串 |

### 3.8 Policy、GoldenSet、Baseline

| 事实 | 来源 |
|---|---|
| `policy(K)` | — |
| `policy_applies(K, role)` | `spec.applies_to` 的每一项 |
| `policy_egress(K, tier)` | `spec.egress` 的每个键（`none/providers/web_read/publish/ingress`） |
| `policy_secret(K, name)` | `spec.secrets` 是列表时的每一项（`from-machine` 不产出） |
| （只有引用） | GoldenSet、Baseline 的 `spec.station`（主体 `K/station`） |

### 3.9 宿主诊断

宿主阶段产生的诊断以 `host_diag(code, subject, message)` 事实进入求值，由 `kernel/host.mrl` 转为 `diag`。宪法可以读取 `host_diag`（例如把 `W-EXPR` 升级为错误）。

---

## 4. 表达式位置与类型环境

Spec 中的每一处嵌入表达式或模板都在规定的类型环境下编译（MRL-E 的静态检查）。静态错误报 `E-EXPR`，警告（如可选字段没有用 `has()` 保护）报 `W-EXPR`；消息中带有 MRL 错误码和表达式内的行列号。类型环境需要类型信息，所以表达式检查在第一遍求值之后进行（第 6 章）。

### 4.1 Bean 的类型

类型表达式 E 的 Bean 类型 `bean(E)`：

| E | bean(E) |
|---|---|
| 命名类型 T（能解析） | 封闭记录 `{id: string, type: string, facts: F(T), meta: M(T), payload: list<{path: string, digest: string, size: int, media_type: string}>}` |
| `list<E'>` | `list<bean(E')>` |
| `ref<E'>` | `bean(E')` 再加一个必需字段 `uri: string` |
| `any`，或命名类型无法解析 | `{id: string, type: string, facts: map<dyn>, meta: map<dyn>, payload: ...}` |

- **F(T)**：T 的全部 facts（含继承）组成的**封闭**记录，字段都是必需的。因此 `bean.facts.audio_stream`（拼错）是静态错误 `MRL-NO-FIELD`。
- **M(T)**：由 `meta_schema` 推导；T 自己没有 `meta_schema` 时沿 extends 链向上找；都没有时为空的开放记录（任何字段都是 `dyn`）。

**meta_schema → MRL 类型**：

| JSON Schema | MRL 类型 |
|---|---|
| `type: string` / `integer` / `number` / `boolean` / `null` | `string` / `int` / `num` / `bool` / `null` |
| `enum` / `const` | 各字面值类型的最小上界（全是字符串时为 `string`） |
| `type: array` | `list<items 的类型>`，没有 items 时为 `list<dyn>` |
| `type: object` 且有 `properties` | 记录：`required` 中的字段必需，其余可选；`additionalProperties: false` 时封闭，否则开放 |
| `type: object` 且没有 `properties` | `map<additionalProperties 的类型>` 或 `map<dyn>` |
| 其他 | `dyn` |

> 注意：JSON Schema 默认 `additionalProperties` 为 true，所以 meta 记录默认是**开放**的，`bean.meta.拼错的字段` 不会报错（类型为 `dyn`）。需要拼写检查时，在 meta_schema 中写 `additionalProperties: false`。可选的 meta 字段必须先用 `has()` 判断，否则报 `W-EXPR`。

### 4.2 各位置的类型环境

| 位置（kind） | 根名字与类型 | 期望类型 |
|---|---|---|
| `property`（Type 的命名属性） | `bean`：该 Type 文档（具体到 minor）的 Bean 类型 | `bool` |
| `accept`（输出端口的验收标准） | `out`：该端口的 Bean；`inputs`：工位输入端口组成的记录（可选端口为可选字段）；`params`：工位参数组成的记录 | `bool` |
| `equivalence`（converter） | `src`：`converts.from` 的 Bean；`dst`：`converts.to` 的 Bean；`params` | `bool` |
| `approval`，phase = propose | `proposal`：`{summary: string, cost_estimate_usd: num, time_estimate_s: num}`；`inputs`；`params` | `bool` |
| `approval`，phase = in_run | `request`：`{kind: string, summary: string, cost_usd: num}`；`inputs`；`params` | `bool` |
| `monitor`（M11，`capabilities.md` 5.4） | 与 `approval`、phase = in_run 相同 | `bool` |
| `idempotency`（Sink 的幂等键，模板） | `inputs`；`params`；`run` | 模板 |
| `step-param`（步骤参数中的模板） | `run`；`inputs`：产线接口输入组成的记录；map 的 `as` 名字（见下） | 模板 |
| `switch`（`cases[i].when`） | `bean`：被观察来源的 Bean | `bool` |
| `until`（`until.cond`） | `gate`：`{attempt: int, judge_score: float}`，工位没有配置 judge 时 `judge_score` 是可选字段；`out`：工位输出端口组成的记录 | `bool` |
| `recipe-template` | `inputs`：配方输入组成的记录；`params`：配方参数组成的记录 | 模板 |
| `runtime-template` | `brief: {task: string, factory_contract: string}`；`limits: {max_turns: int, timeout_s: int, budget_usd: float}`；`operator: {model: string, runtime: string}`；`session: {id: string}`（M13，`invoke.resume` 用） | 模板 |

- `run` 的类型：`{id: string, epoch: string, line: string, budget: {cap_usd: float, spent_usd: float, remaining_usd: float}}`。
- 参数类型：`string/int/float/num/bool` 分别对应同名的 MRL 类型。
- **map 的 `as` 名字**：字面列表时，类型为各元素字面值类型的最小上界（如 `[en, ja]` 为 `string`）；动态来源时，为来源 `list<T>` 的元素 Bean `bean(T)`。`as` 名字与环境中已有的名字（如 `run`、`inputs`）冲突时报 `E-EXPR`。
- 模板插值的类型必须是 `string`、数或 `bool`（MRL-T 规则）。
- 命名属性引用了 `bean.meta`（生产者自报、未经实测）时，额外报 `W-PROPERTY-META`：属性应当建立在 validator 实测的 facts 上。

### 4.3 正确与错误示范

```yaml
# ✔ 正确：int 与 float 混合运算会提升（brief 的 duration_s 是 JSON 整数 60 也没问题）
accept: ["out.facts.duration_s >= inputs.brief.meta.duration_s * 0.9"]

# ✖ 错误：in 是关键字，验收标准里引用输入要写 inputs
accept: ["out.facts.duration_s >= in.brief.meta.duration_s * 0.9"]          # E-EXPR（MRL-SYNTAX）

# ✖ 警告：audience 是可选字段，没有判断是否存在
accept: ["inputs.brief.meta.audience != 'kids'"]                             # W-EXPR（MRL-MAYBE-ABSENT）
# ✔ 正确
accept: ["!has(inputs.brief.meta.audience) || inputs.brief.meta.audience != 'kids'"]

# ✖ 错误：模板中的拼写错误在检查时就会发现，而不是等到运行时
idempotency_key: "{{ inputs.artifact.idd }}:{{ params.bucket }}"           # E-EXPR（MRL-NO-FIELD）
```

---

## 5. 转换解析

转换解析由两个 profile 函数完成（`src/mrl/factory/functions.py`），内核在 `resolution` 关系中调用它们：边的 `bind_via` 为 `auto` 时用 `resolve(conv_lib, ...)`，为 `none` 时用 `resolve([], ...)`，为 `explicit` 时用 `resolveVia(conv_lib_all, ..., chain)`。两个函数都是纯函数，所以**同一个 epoch 内，同样的边总是解析到同样的转换链**（不变量 I14）。只有两端类型都能解析（`texpr_ok`）的边才参与解析。

### 5.1 输入

| 参数 | 含义 |
|---|---|
| `lib` | 转换库：每个非 retired 的 converter 一项 `{id, from, to, lossless, requires, guarantees, preserves, cost, p50}`。`conv_lib` 只含 active 的转换；`conv_lib_all` 还含 deprecated 的（只能显式使用） |
| `subs` | 子类型对 `[a, b]`（`a <: b`），即 `kernel.types` 的 `tsub` |
| `always` | 恒真属性对 `[e, p]`，即 `texpr_always` |
| `u`、`g` | 上游类型与保证 |
| `d`、`r` | 下游类型与要求 |
| `chain` | 显式转换链（`resolveVia`） |

记 `props(t, base) = base ∪ always(t)`；经过转换 c 后的属性为 `step(c, P) = c.guarantees ∪ (P ∩ c.preserves) ∪ always(c.to)`。

### 5.2 resolve（自动解析）

```
P0 = props(u, g)
1. 若 u <: d 且 r ⊆ P0：返回 direct。
2. 在转换图上枚举路径（长度 1..MAX_CHAIN，MAX_CHAIN = 4；同一个转换在一条路径中最多出现一次）：
     转换 c 在当前类型 t、当前属性 P 下可用 ⇔ t <: c.from 且 c.requires ⊆ P；
     经过 c 后：t ← c.to，P ← step(c, P)。
   满足 “终点类型 <: d 且 r ⊆ 终点属性” 的路径是候选。
   探索的路径数超过 MAX_PATHS（100000）时报 MRL-COST。
3. 候选按以下键取最小值（逐项比较）：
     (不是全部无损, 步数, 成本之和, 耗时之和, id 序列的字典序)
   即：无损优先 → 步数最少 → 估计成本最低 → 估计耗时最短 → 名称字典序。返回 convert。
4. 没有候选：若 u <: d，返回 contract（类型兼容，但要求没有被覆盖）；否则返回 no-converter。
```

### 5.3 resolveVia（显式转换链）

```
t = u，P = props(u, g)
对 chain 中的第 i 个 id：
    不在 lib 中（不存在、不是 converter、已退役）→ via-invalid
    t 不是 c.from 的子类型                       → via-invalid
    c.requires ⊄ P                               → via-invalid
    t ← c.to，P ← step(c, P)
终点 t 不是 d 的子类型 → via-invalid
r ⊄ P               → contract
否则                 → convert
```

### 5.4 结果

| 字段 | 含义 |
|---|---|
| `status` | `direct` / `convert` / `contract` / `no-converter` / `via-invalid` |
| `chain` | 插入的转换 id 列表；direct、contract（自动）、no-converter 为空 |
| `type` | 经过转换链后的类型（没有转换时为 u） |
| `props` | 到达下游时可提供的属性（升序、去重） |
| `missing` | r 中没有被覆盖的属性（升序） |
| `lossless` | 链上的转换是否都无损；direct 为 true；contract、no-converter、via-invalid 为 false |
| `detail` | via-invalid 的原因；其他情况为空串 |

**status → 诊断**（`kernel/lines.mrl`）：

| status | auto | none | explicit |
|---|---|---|---|
| direct | — | — | — |
| convert | `I-CONVERT` | （不会出现） | `I-CONVERT` |
| contract | `E-CONTRACT` | `E-CONTRACT` | `E-CONTRACT` |
| no-converter | `E-NO-CONVERTER`（生成转换需求） | `E-TYPE-MISMATCH` | （不会出现） |
| via-invalid | （不会出现） | （不会出现） | `E-VIA` |

### 5.5 例子（示例 Registry）

```text
$ mrl resolve spec/examples/registry --from subtitle.vtt@1 --to subtitle.srt@1
status    convert
chain     ['subs-convert@1']
...
$ mrl resolve spec/examples/registry --from subtitle.vtt@1 --to subtitle.srt@1 --requires non_empty
status    no-converter          # subs-convert 没有声明 preserves: [non_empty]，属性在转换后丢失
missing   ['non_empty']
```

### 5.6 不做的事

- 不对 `list<T>` 自动逐元素转换：转换只作用于整个类型。需要逐元素处理时，用 map 展开后再连接。
- 不考虑运行期才知道的子类型；运行期遇到新组合时按同样的规则解析并追加到解析表（架构文档 9.3）。

---

## 6. 检查流程与输出

### 6.1 流程

1. 按第 2 章读取 Registry：`E-YAML`、`E-LAYOUT`、`E-SCHEMA`；未通过的文档排除。
2. 按第 3 章抽取事实。类型表达式无法解析的文档排除后重新抽取。
3. 构建程序：`kernel/*.mrl` + `constitution/*.mrl`，函数表为标准库 + `resolve`、`resolveVia`。
   - 没有宪法文件：`E-CONSTITUTION`（主体 `Registry`），只用内核继续。
   - 宪法无法通过 MRL 静态检查（语法、归属、分层等）：每个 MRL 诊断一条 `E-CONSTITUTION`，只用内核继续。
   - 内核本身无法通过静态检查：内部错误（退出码 5），不产出报告。
4. 第一遍求值。宪法在求值时出错（如除以零）：`E-CONSTITUTION`，只用内核重新求值；内核求值出错：内部错误。
5. 用第一遍的结果构造类型环境，检查第 4 章的全部表达式位置。
6. 第二遍求值：把第 5 步的诊断加入 `host_diag`，得到最终的 `diag`。
7. 码表校验：`diag` 的每个 code 都必须在 `kernel/codes.mrl` 的 `code` 关系中登记；没有登记的转为 `E-CONSTITUTION`，消息中保留原码与原消息。
8. 位置映射：宿主诊断使用自带的位置；其他诊断按主体查 `loc`，找不到时逐级去掉主体 `/` 之后的最后一段（以 `.`、`/`、`[` 分隔），直到 Spec 键。路径再映射到 YAML 节点的行列号。
9. 排序：严重度（error → warning → info）、文件、行、码、主体、消息。

### 6.2 结果判定与退出码

| 情况 | `mrl registry check` 的退出码 |
|---|---|
| 没有 error | 0（加 `--strict` 时，有 warning 也算未通过） |
| 有 error | 1 |
| 用法错误、目录不存在 | 2 |
| 内部错误（内核规则本身有缺陷） | 5 |

`info`（如 `I-CONVERT`）从不影响结果。

### 6.3 文本输出

```text
✖ E-CONTRACT       lines/explainer-multilang.yaml:32  Line:explainer-multilang@5/steps.to_shorts.in.video
    要求 {vertical, hd, has_audio, not_silent}，可提供 {h264, hd, vertical}，缺失 {has_audio, not_silent}
ℹ I-CONVERT        lines/explainer-multilang.yaml:18  Line:explainer-multilang@5/steps.dub_en.in.subtitle
    subtitle.vtt@1 → subtitle.srt@1：插入 [subs-convert@1]（无损）
✖ 未通过：1 个错误，0 个警告，1 条信息 · 33 个 Spec · 9 条边 · 1 处转换
```

### 6.4 JSON 输出（`--json`）

```json
{
  "root": "spec/examples/registry",
  "ok": true,
  "counts": {"error": 0, "warning": 0, "info": 0},
  "stats": {"kind:Station": 9, "edges": 9, "conversions": 0},
  "diagnostics": [
    {"code": "E-CONTRACT", "severity": "error", "subject": "Line:…/steps.to_shorts.in.video",
     "message": "…", "file": "lines/explainer-multilang.yaml", "line": 32, "col": 7, "path": "spec.steps.to_shorts.in.video"}
  ],
  "resolutions": [
    {"line": "Line:explainer-multilang@5", "step": "dub_en", "port": "subtitle",
     "from": "subtitle.vtt@1", "guarantees": [], "to": "subtitle.srt@1", "requires": [],
     "status": "convert", "chain": ["subs-convert@1"], "type": "subtitle.srt@1",
     "props": [], "missing": [], "lossless": true, "detail": ""}
  ]
}
```

`resolutions` 就是本 epoch 的转换解析表（架构文档 9.3、A.13）。

### 6.5 其他命令

| 命令 | 用途 |
|---|---|
| `mrl registry facts ROOT [--rel NAME]...` | 不带 `--rel` 时列出每个输入关系的事实数；带 `--rel` 时输出该关系（输入或派生）的全部行 |
| `mrl registry resolutions ROOT` | 输出转换解析表 |
| `mrl resolve ROOT --from U --to D [--guarantees ..] [--requires ..] [--via ..]` | 对一条假想的边做解析（U、D 不必在 Registry 中出现过）；direct/convert 时退出码 0，否则 1 |
| `mrl explain CODE` | 解释诊断码，并列出产生它的内核规则位置 |
| `mrl coverage` | factory 用例的覆盖情况（第 8.4 节） |

---

## 7. 诊断码

码表的唯一来源是 `spec/mrl/kernel/codes.mrl`。`mrl explain <码>` 会列出产生它的规则位置。

### 7.1 宿主阶段

| 码 | 严重度 | 含义 | 常见原因 → 修法 |
|---|---|---|---|
| `E-YAML` | error | YAML 无法解析、重复的键、非字符串的键 | 改正 YAML；注意 YAML 1.2 规则 |
| `E-SCHEMA` | error | 不符合 kind 的 JSON Schema；类型表达式无法解析 | 按消息中的 JSON 路径修正字段；对照 `spec/schemas/` |
| `E-LAYOUT` | error | 文件位置与 kind 不符；记录类 kind 放进了 Spec 目录 | 把文件移到 2.1 表中对应的位置 |
| `E-EXPR` | error | 嵌入表达式或模板的静态错误 | 看消息中的 MRL 码与行列号；对照第 4.2 节的类型环境 |
| `W-EXPR` | warning | 嵌入表达式的警告（主要是可选字段没有用 `has()` 保护） | 加 `has()` 判断 |
| `W-PROPERTY-META` | warning | 命名属性引用了自报的 `bean.meta` | 改用 validator 实测的 facts |
| `E-CONSTITUTION` | error | 宪法缺失、无法编译、求值出错，或使用了未登记的诊断码 | 交给人处理：宪法只由人修改 |

### 7.2 引用与类型（`kernel/refs.mrl`、`kernel/types.mrl`）

| 码 | 严重度 | 含义 | 常见原因 → 修法 |
|---|---|---|---|
| `E-DUPLICATE` | error | 同一个 Spec 键定义了多次 | 升版本号，不要原地复制 |
| `E-REF` | error | 引用的 Spec 不存在或已退役；工位的配方在机器中不存在 | 修正名字或版本；改用未退役的版本 |
| `E-UNPINNED` | error | 引用没有版本；镜像没有 64 位小写十六进制的 sha256 digest；上游源码没有 7–40 位小写十六进制 commit | 写全 `name@version`、`@sha256:<digest>`、commit |
| `W-DEPRECATED` | warning | 引用了 deprecated 的 Spec | 迁移到新版本 |
| `E-CYCLE` | error | 类型继承成环（产线依赖成环也用这个码） | 去掉环上的一条 extends / 依赖 |
| `E-EXTENDS` | error | 子类型与父类型的种类不同、缺少或放宽了字段、fact 类型冲突、重新定义了父类型的属性 | 子类型只能收窄；换一个属性名表达新语义 |
| `E-FACT` | error | validator 产出了未声明的 fact；具体类型的某个 fact 没有任何 validator 产出 | 补 `facts` 声明或 `emits` |
| `E-PROPERTY` | error | guarantees / requires / preserves / always / 接口 requires 中的名字不是该类型的命名属性 | 改正属性名，或先在类型上定义属性 |

### 7.3 工位（`kernel/stations.mrl`）

| 码 | 严重度 | 含义 | 常见原因 → 修法 |
|---|---|---|---|
| `E-PORT` | error | 角色要求的端口缺失（source 没有输出、sink 没有输入等）；工位端口与配方不一致；产线绑定、map、switch、keep、接口输出引用了不存在的端口 | 按消息修正端口名或类型 |
| `E-PARAM` | error | 参数没有在配方 / 工位中声明；类型不一致；必填参数没有提供；值的类型不被参数类型接受；给子产线传参数 | 对齐参数声明 |
| `E-CONVERTER` | error | converter 结构不合要求：没有 converts、端口不是唯一的 `in`/`out`、converts 与端口类型不一致、没有 equivalence、没有正样例、输出可选；非 converter 声明了 converts | 按 9.2 节的 converter 定义修正 |
| `E-PERMISSION` | error | 策略不适用于工位角色；模型不在 `models_allowed` 中；agent-cli 工位没有指定 model；（宪法）出网或凭据越级 | 换策略或模型；越级的问题交给人 |
| `W-NO-BUDGET` | warning | agent 驱动的工位没有 `budget_usd`；产线没有 `budget.cap_usd` | 设置预算上限 |
| `E-RECEIPT` | error | Sink 的 `verify_receipt` 不是其机器的配方、该配方没有 `run`，或声明了 `outputs`（M2-09 登记） | 在机器里补核验配方：有 `run`、没有 `outputs` |
| `E-APPROVAL` | error | 两阶段工位的 `phases` 与 `approval.propose` 不一致（M2-09 登记） | 两者同时写或同时去掉 |

### 7.4 产线（`kernel/lines.mrl`）

| 码 | 严重度 | 含义 | 常见原因 → 修法 |
|---|---|---|---|
| `E-UNBOUND` | error | 必填输入没有连接 | 绑定该输入，或在工位与配方中把它设为可选 |
| `E-TYPE-MISMATCH` | error | 类型不兼容，且这条边是 `via: none` | 去掉 `via: none`，或改正类型 |
| `E-NO-CONVERTER` | error | 类型不兼容（或要求经转换后无法满足），转换库中没有满足条件的路径 | 交给厂长编写 converter（转换需求）；或检查 preserves / guarantees |
| `E-CONTRACT` | error | 下游的 requires 没有被覆盖 | 上游补 guarantee（要真的由出厂检验强制），或下游放宽 requires |
| `E-VIA` | error | 显式转换链不可用 | 按 detail 修正 via 列表 |
| `E-ORPHAN` | error | 步骤的输出既不被消费，也没有 keep，也不是接口输出 | 连接下游、加 keep，或作为接口输出 |
| `E-BUDGET` | error | 各步骤预算上限之和超过产线预算。求和时计入 map、until 的倍数，也计入解析时自动插入的转换工位的预算（转换工位的 `limits.budget_usd` × 下游步骤的倍数）；动态 map 没有 `max_items` | 调整预算、设置 `max_items`，或换用更便宜的转换 |
| `I-CONVERT` | info | 边上插入了转换 | 无需处理；可在解析表中查看 |

### 7.5 宪法

| 码 | 严重度 | 含义 |
|---|---|---|
| `E-INVARIANT` | error | 违反宪法不变量（I7 Sink 幂等与回执、I8 validator 正反样例、预算上限等） |

宪法也可以使用其他已登记的码（参考宪法用 `E-PERMISSION` 表达 I6）。宪法**不能**新增码：`code` 关系归内核所有。

---

## 8. 一致性用例（kind: factory）

### 8.1 格式

```yaml
suite: factory.lines              # 全局唯一
kind: factory
base: example                     # example = spec/examples/registry；empty = 空 Registry；否则为相对套件文件的目录
cases:
  - id: contract-violation        # 套件内唯一
    description: 架构文档 7.3：vertical 删掉了 has_audio、not_silent 两个保证
    base: example                 # 可选：覆盖套件级的 base
    patch:                        # 按顺序应用；可以嵌套列表（便于用 YAML 锚点复用一组补丁）
      - {file: stations/vertical.yaml, set: {spec.outputs.video.guarantees: [vertical, hd, h264]}}
    expect:
      codes: [E-CONTRACT]
      diags:
        - {code: E-CONTRACT, subject: "Line:explainer-multilang@5/steps.to_shorts.in.video", message: "缺失 {has_audio, not_silent}"}
      resolutions: [{step: to_shorts, port: video, status: contract}]
```

### 8.2 补丁操作

| 操作 | 写法 | 说明 |
|---|---|---|
| 写入整个文件 | `{file: F, content: "…"}` | 文件不存在时创建；可以是 YAML 或 `.mrl` |
| 删除文件 | `{file: F, delete: true}` | 文件必须存在 |
| 设置字段 | `{file: F, doc: N, set: {路径: 值, …}}` | doc 默认 0；中间的映射不存在时自动创建；`a[n]` 中 n 等于列表长度时追加 |
| 删除字段 | `{file: F, doc: N, unset: [路径, …]}` | 路径必须存在 |
| 追加文档 | `{file: F, append: {…}}` | 在文件末尾追加一个文档 |
| 删除文档 | `{file: F, doc: N, remove_doc: true}` | |

路径写法：`a.b.c`、`a.b[0].c`；键中含 `.` 时写成 `a["x.y"].c`。被 set/unset/append 修改过的文件会重写为 JSON 文档（JSON 是 YAML 1.2 的子集，不会有标量歧义），未修改的文件保持原样。补丁无法应用时用例失败。

### 8.3 期望

| 字段 | 含义 |
|---|---|
| `clean: true` | 没有 error，也没有 warning（info 不限） |
| `ok: true` | 没有 error |
| `codes: [...]` | error 与 warning 的诊断码（多重集合）必须**恰好**等于此列表；info 不计入 |
| `diags: [{code, subject, message, severity}]` | 每项都必须匹配至少一条诊断；省略的字段不比较；message 按子串匹配 |
| `absent: [{...}]` | 不得匹配任何诊断 |
| `resolutions: [{line, step, port, status, chain, type, props, missing, lossless}]` | 每项都必须匹配至少一条解析结果；`line` 可以写完整键或去掉 `Line:` 前缀 |

**规则**：每个负面用例都必须写 `codes`（精确断言）。只写 `diags` 会放过多余的连带诊断——连带诊断的变化往往正是回归。

### 8.4 覆盖要求

`mrl coverage` 必须报告：

1. `kernel/codes.mrl` 中的每个诊断码，至少被一个用例在 `codes` 或 `diags` 中期望；
2. 内核中每条写 `diag` 的规则，至少在一个用例中触发过一次（求值器记录每条规则新增的元组数）。

修改内核时，新增的每条 `diag` 规则都必须配一个让它触发的用例（第 10 章）。

### 8.5 正确与错误示范

```yaml
# ✔ 正确：一个用例只验证一件事；codes 精确；主体与消息都有断言
- id: model-not-allowed
  patch: [{file: stations/explainer.yaml, set: {spec.operator.model: gpt-5}}]
  expect:
    codes: [E-PERMISSION]
    diags: [{code: E-PERMISSION, subject: "Station:explainer@3/operator", message: "gpt-5"}]

# ✖ 错误：只断言“有 E-PERMISSION”，多出来的连带诊断不会被发现
- id: model-not-allowed
  patch: [{file: stations/explainer.yaml, set: {spec.operator.model: gpt-5}}]
  expect: {diags: [{code: E-PERMISSION}]}

# ✖ 错误：为了让用例通过而修改期望，却没有确认新的诊断是正确的
#    （先用 mrl registry check 查看完整输出，逐条确认每条诊断都是补丁的直接后果，再写 codes）
```

---

## 9. 内核与宪法的边界

| | 内核（`spec/mrl/kernel/`） | 宪法（`<Registry>/constitution/`） |
|---|---|---|
| 内容 | 元模型的结构规则（V1、V2 的关系部分） | 这个工厂的 L0 不变量与数值边界 |
| 可以读 | 全部 `@input` 关系 | 全部 `@input` 关系、内核的全部派生关系、`host_diag` |
| 可以写 | 自己声明的关系；`diag` | 自己声明的关系；`diag` |
| 不能做 | — | 为内核的关系写规则（MRL-OWNERSHIP）；读取 `diag`（MRL-OWNERSHIP）；新增诊断码 |
| 谁修改 | 维护者，经过一致性用例与覆盖检查 | **只有人**（受保护路径，见架构文档 9.1、I13） |

**为什么这样就能“约束整个系统的边界”**：

- 内核与宪法都是可执行的：Registry 的每次变更都要跑一遍检查，任何 error 都会拒绝变更（V1、V2）。
- 归属规则保证：任何新加的模块（包括厂长写错的、或者恶意的）都只能**增加**诊断，不能让已有的诊断消失。参考用例：`factory.constitution/constitution-cannot-suppress-kernel`、`factory.host/constitution-cannot-write-kernel-relations`。
- 宪法缺失本身就是 error（`E-CONSTITUTION`），所以“删掉宪法”不是绕过检查的办法。

**示例：人收紧宪法**

```
module constitution.invariants.
rel limit(name: string, value: float).
limit("station_budget_usd", 4.0).          // 从 20.0 收紧到 4.0
diag("E-INVARIANT", S + "/limits", "单个工位的预算上限 " + string(B) + " USD 超过宪法上限 " + string(M) + " USD") :-
    station_budget(S, B), limit("station_budget_usd", M), B > M.
```

**示例：宪法读取宿主诊断，把表达式警告升级为错误**

```
module constitution.strict.
diag("E-INVARIANT", S, "本工厂把表达式警告视为错误：" + M) :- host_diag("W-EXPR", S, M).
```

---

## 10. 演进规则

修改本 profile（schema、抽取、内核、类型环境、解析算法）时：

1. **先写用例**：在 `spec/mrl/conformance/factory/` 中写出新行为的正面与负面用例（负面用例必须有精确的 `codes`）。
2. 修改实现与内核规则。新增的诊断码先登记到 `kernel/codes.mrl`，再在本文档第 7 章补一行。
3. 运行并确认全部通过：

   ```bash
   uv run mrl conformance            # 全部一致性用例
   uv run mrl coverage               # 诊断码与内核规则的覆盖，必须没有缺口
   uv run mrl registry check spec/examples/registry --strict
   uv run pytest
   ```

4. 更新本文档对应的章节；抽取映射（第 3 章）与类型环境（第 4 章）的变化必须逐条写明。
5. 不兼容的变化（已有 Registry 会出现新的 error）在变更说明中列出，并给出迁移方法。

**不得**：为了让 Registry 通过而删除或放宽内核规则；为了让用例通过而修改期望却不复核诊断；在内核中引用宪法的关系（内核必须独立成立）。
