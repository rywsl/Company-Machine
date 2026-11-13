# admit：工位的跨端口来料约束（M14）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M14-01、M14-02**。设计说明见 `plan/design-admit.md`。
> 现有的来料检验逐个端口看：Bean 已封存、类型是端口类型的子类型、满足端口的 `requires`。它表达不了“两个输入之间要相互匹配”。
> 例如刷写工位同时收一台车的构建记录和一份固件，固件必须是这台车的车型、这台车的平台。车型有很多，不可能每个车型造一个类型或一个属性。
> 现在只能在出厂 `accept` 里事后检查：工位跑完才发现不匹配，这一单白做。
>
> 本文件给工位加一段 `admit`：若干条 MRL 布尔表达式，可以同时引用全部输入端口和工位参数，在来料检验时、建 job 之前求值。任一条不成立就拒收，不建 job，不启动操作工。

---

## 1. 目的与范围

- **静态（M14-01）**：`station.schema.json` 增加 `spec.admit`；抽取映射产出 `station_admit(K, n)` 和表达式位置 `admit`；kernel 对“没有输入端口却写了 admit”报 `E-PORT`；宿主阶段对“admit 引用输入 Bean 的 meta”报新的警告码 `W-ADMIT-META`。
- **运行期（M14-02）**：`Gate.check_inputs` 在逐端口检查全部通过之后求值 `admit`；`Runner.submit` 把参数交给它。产线不改：节点提交被拒时按 `lines.md` 已有的规则 `FAILED`。

不做：

- 带参数的命名属性、参数化类型（`fw<model>`）、并集类型、`bundle` / `ref` 的检验。理由与触发条件见 `plan/design-admit.md`。
- 产线接口级的 admit。接口输入仍只做类型与 `requires`（`lines.md`）；跨输入的约束写在消费它们的工位上。
- 把 `admit` 当成类型：它不参与子类型、转换解析、`E-CONTRACT` 的覆盖检查，也不进缓存键。
- 不新增错误码。拒收仍是 `INTAKE_REJECTED`，只多一个 `reason`。

## 2. 依赖

| 依赖 | 用到什么 |
|---|---|
| `gate.md` 5.3 | `check_inputs` 的逐端口检查、问题对象、`INTAKE_REJECTED` |
| `runner.md` 5.2 | `submit` 先来料检验再插入 job；检验失败不插入行 |
| `lines.md` 5.3 | 节点提交时 `Runner.submit` 抛 `FactoryError` → 节点 `FAILED`，`error` 记 `{code, message, detail}` |
| `cache.md` 3、5.3 | 缓存键含 `station_digest`、输入 Bean ID、规范化后的参数；命中时不提交 job |
| `factory-profile.md` 2.3、3.6、4.2、7.1、7.3 | 主体、抽取映射、表达式位置的类型环境、诊断码 |
| `mao.values` | `inputs_value`、`params_value` |
| `mao.snapshot` | `Snapshot.compiled(subject)`：所有表达式位置在快照加载时编译 |

## 3. 接口

### 3.1 工位的 `admit` 段（`station.schema.json`）

```yaml
spec:
  inputs:
    car:      {type: vehicle.build@1}
    firmware: {type: fw.image@1, requires: [signed]}
  admit:
    - inputs.firmware.facts.target_model == inputs.car.facts.model
    - inputs.firmware.facts.platform == inputs.car.facts.platform
    - inputs.firmware.facts.size_kb / inputs.car.facts.ecu_count <= 4096.0
```

```json
"admit": {"type": "array", "minItems": 1, "items": {"$ref": "common.schema.json#/$defs/expr"}}
```

- 放在 `spec` 下，与 `inputs`、`outputs` 平级。可以省略；写了就至少一条。
- 每条是一个 MRL 表达式，期望类型 `bool`。以 `!` 开头的表达式在 YAML 里必须加引号（否则是 YAML 标签）。
- 条目的序号就是诊断主体与问题对象里的 `i`。

### 3.2 `Gate.check_inputs`

```python
def check_inputs(self, station_key: str, inputs: Mapping[str, str | Sequence[str]],
                 params: Mapping[str, Any] | None = None) -> dict[str, "Bean | list[Bean]"]
```

- 新增的 `params` 是**未规范化**的工位参数（与 `Runner.submit` 收到的相同）。`None` 等同 `{}`。
- 没有 `admit` 的工位不读 `params`，行为与 M1 完全相同（`tests/acceptance/m1/test_m1_06_gate.py` 不传它）。

### 3.3 `Runner.submit`

签名不变。来料检验改为 `self.gate.check_inputs(station_key, checked, params)`。其余顺序不变：Source 检查、策略防线、拆分输入、`reject_expired`、来料检验、`params_value`、插入 job。

## 4. 数据格式

### 4.1 抽取映射（`factory-profile.md` 3.6）

| 事实 / 位置 | 来源 |
|---|---|
| `station_admit(K, n)` | `spec.admit` 的条数；没有 `admit` 时不产出 |
| 表达式位置 `admit`，主体 `K/admit[i]` | `spec.admit[i]`；位置（`locs`）指向 `spec.admit[i]` |
| 主体 `K/admit` | 位置指向 `spec.admit`，供 `E-PORT` 定位 |

`kernel/schema.mrl` 声明 `@input rel station_admit(key: string, n: int).`。

### 4.2 类型环境（`factory-profile.md` 4.2）

| 根名字 | 类型 |
|---|---|
| `inputs` | 工位输入端口组成的记录，与 `accept` 的 `inputs` 相同：每个端口是 `bean(端口类型)`，列表端口是 `list<bean(元素类型)>`；可选端口是可选字段 |
| `params` | 工位参数组成的记录，与 `accept` 的 `params` 相同 |

期望类型 `bool`。没有 `out`：admit 在产出之前求值。

### 4.3 运行期的求值环境

```python
{"inputs": inputs_value(beans), "params": params_value(snapshot, station_key, params or {})}
```

`beans` 是逐端口检查返回的 dict（标量端口一个 `Bean`，列表端口 `list[Bean]`，缺席的可选端口不出现）。Bean 的 `facts` 是仓库里封存时写入的值。

### 4.4 问题对象

沿用 `gate.md` 5.3 的形状，每条不成立的 admit 一个：

```python
{"port": "", "index": i, "reason": "admit", "detail": "admit[i]", "bean_id": ""}
```

- `port` 是空串（端口名都是非空 ident，所以不会混淆）。
- 求值抛 `EvalError` 时 `detail` 是 `f"admit[{i}]: {exc.code}"`，例如 `"admit[2]: MRL-DIV-ZERO"`。结果是 `False` 时 `detail` 只有 `"admit[i]"`。
- 按 `i` 升序。

### 4.5 诊断

| 码 | 严重度 | 主体 | 消息 | 产生位置 |
|---|---|---|---|---|
| `E-EXPR` / `W-EXPR` | error / warning | `Station:K/admit[i]` | 与其他表达式位置相同 | 宿主阶段（`sites.py`） |
| `W-ADMIT-META` | warning | `Station:K/admit[i]` | `admit 引用了输入 Bean 自报的 meta；准入应当建立在 validator 实测的 facts 上` | 宿主阶段（`sites.py`） |
| `E-PORT` | error | `Station:K/admit` | `没有输入端口的工位不能写 admit` | `kernel/stations.mrl` |

`kernel/codes.mrl` 登记：`code("W-ADMIT-META", "warning", "工位 admit 引用了输入 Bean 自报的 meta，而不是实测的 facts").`。`mrl explain W-ADMIT-META` 把它列为宿主阶段的码（`src/mrl/cli.py` 的前缀表）。

### 4.6 一致性用例

`tests/acceptance/m14/admit.conformance.yaml` 是本里程碑的一致性用例（base 为示例 Registry，改 `dub@2` 与 `intake-brief@1`）。M14-01 把它**原样**复制为 `spec/mrl/conformance/factory/admit.yaml`，不改一个字。验收会比较两份文件的内容。

## 5. 行为

### 5.1 静态检查（宿主阶段）

1. 每条 `admit[i]` 和其他表达式位置一样，在第一遍求值之后按 4.2 的环境编译。静态错误报 `E-EXPR`，警告报 `W-EXPR`（例如可选端口没有用 `has()` 保护时的 `MRL-MAYBE-ABSENT`）。
2. 编译通过后，若 `compiled.paths` 中有一条匹配 `^inputs\.<ident>\.meta(\.|$)`，报一条 `W-ADMIT-META`。判断只看 `compiled.paths`，与 `W-PROPERTY-META` 对 `bean.meta` 的做法相同。
3. 快照加载时 `_compile_sites` 照常编译全部位置，`Snapshot.compiled(f"{station_key}/admit[{i}]")` 可以取到编译结果。

### 5.2 kernel 规则

```
diag("E-PORT", S + "/admit", "没有输入端口的工位不能写 admit") :- station_admit(S, _), !has_in(S).
```

写在 `kernel/stations.mrl` 的“角色要求的端口”一节。`has_in` 已有。Source 工位写了 admit 时，除了 `source` 的端口规则，还会得到这一条。

### 5.3 `check_inputs` 的顺序

1. 逐端口检查，与 `gate.md` 5.3 完全相同。收集问题，不在第一个问题时抛出。
2. **逐端口检查有任何问题时，不求值 admit**，直接抛 `INTAKE_REJECTED`，`problems` 里只有端口问题。admit 的环境需要每个端口都已就绪，带着缺失或错类型的 Bean 去求值没有意义。
3. 端口全部通过、且工位有 `admit` 时：
   1. `normalized = params_value(snapshot, station_key, params or {})`。参数不合法时 `PARAM_INVALID` 原样冒泡（`Runner.submit` 接下来本来也会抛它）。
   2. 按 4.3 构造环境。对每个 `i`，`snapshot.compiled(f"{station_key}/admit[{i}]").evaluate(env)`。结果 `is True` 才算成立。`False` 与 `EvalError` 都按 4.4 记一个问题（失败即关闭）。不要在第一条失败时停下：每一条都求值。
   3. 有问题 → `FactoryError("INTAKE_REJECTED", "来料检验未通过", problems=problems)`。
4. 全部成立时返回值与 M1 相同。

### 5.4 `Runner.submit`

按 3.3 把 `params` 传给 `check_inputs`。被拒时不插入 job 行，不写 `job.created`，不启动沙箱。`factory job run` 因此以退出码 1 结束，`--json` 时 stdout 是 `{"error": {"code": "INTAKE_REJECTED", "message": ..., "detail": {"problems": [...]}}}`（`commands.md`）。

### 5.5 产线

`LineRunner` 不需要改动：

- 节点提交时 `Runner.submit` 抛 `INTAKE_REJECTED` → 节点 `FAILED`，`job_id` 为空，`error` 是 `{"code": "INTAKE_REJECTED", "message": ..., "detail": {"problems": [...]}}`；run 按 `lines.md` 的规则结束为 `FAILED`，`factory line run` 退出码 1。
- 接口输入只做类型与 `requires`。接口里一对不匹配的 Bean 会通过 `start_run`，在消费它们的工位提交时被拒。
- 上游是 Source 时，Source 照常执行、封存；它的产物与接口里的 Bean 在下游工位汇合，admit 在这时核对两者。Source 已封存的产物不因下游拒收而回滚。
- `map` 展开的每个元素各自提交，各自求值 admit。
- **缓存**：命中检查在提交之前，命中时不再求值 admit。这是安全的：缓存键含 `station_digest`（`admit` 在 spec 里）、输入 Bean ID 和规范化后的参数，Bean 的 facts 封存后不变，所以命中的那个 job 当初已经以同样的输入通过了 admit。

### 5.6 与其他检查的关系

| 检查 | 时机 | 看什么 |
|---|---|---|
| 端口类型、`requires` | 来料，逐端口 | 单个 Bean |
| `admit` | 来料，端口全部通过之后 | 全部输入 + 参数 |
| 输出 `guarantees`、`accept` | 出厂 | 产物（`accept` 还能看输入与参数） |

`admit` 不是 guarantee，下游不能把它当作上游的保证；也不进入 `E-CONTRACT` 的覆盖检查。

## 6. 错误

不新增错误码。

| 错误码 | 何时 | detail |
|---|---|---|
| `INTAKE_REJECTED` | 新增 `reason: "admit"`：端口全部通过后，某条 admit 不成立或求值出错 | `problems`：4.4 的对象列表 |
| `PARAM_INVALID` | 工位有 admit、端口全部通过，参数不合法（`params_value`） | 与 `runner.md` 相同 |

诊断码见 4.5。

## 7. 验收测试

夹具：`tests/acceptance/m14/admit_world.py`。在 `tests/fixtures/registry-m2` 的副本上增加车辆构建记录、固件、刷写结果三种类型（facts 由 validator `json-facts` 从 JSON 载荷实测），模拟外部拉取固件的 Source `pull-fw@1`，带 admit 的 `flash@1`、`flash-fixed@1`、`flash-many@1`，以及产线 `vehicle-flash@1`（接口里有车辆与固件）和 `vehicle-pull-flash@1`（接口里只有车辆，固件由 Source 现拉）。物料经 `factory bean import` 入库。静态检查经 `mrl registry check ROOT --json`。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m14/test_m14_01_admit_static.py` | `test_registry_with_admit_is_clean` | 夹具 Registry 在 `--strict` 下退出 0，没有 error 与 warning |
| | `test_admit_field_errors_are_e_expr` | 拼错的 fact、不存在的端口报 `E-EXPR`（`MRL-NO-FIELD`），不是 bool 的表达式报 `E-EXPR`；主体 `Station:flash@1/admit[i]`，`file` 是 `stations/flash.yaml`，`path` 是 `spec.admit[i]` |
| | `test_admit_sees_params_and_list_ports` | 环境里有 `params`（拼错参数名报 `E-EXPR`）；列表端口是 `list<bean>`，`all` 宏里拼错的 fact 报 `E-EXPR` |
| | `test_admit_on_meta_warns` | 引用 `inputs.firmware.meta.*` 报一条 `W-ADMIT-META`（warning），非 strict 退出 0，`--strict` 退出 1 |
| | `test_admit_optional_port_needs_has` | 可选端口不用 `has()` 保护报 `W-EXPR`（`MRL-MAYBE-ABSENT`），保护了的那条不报 |
| | `test_admit_without_inputs_is_e_port` | Source 写 admit：`E-PORT`，主体 `Station:pull-fw@1/admit`，消息与 4.5 相同，`path` 是 `spec.admit` |
| | `test_admit_schema` | `admit: []`、`admit: [1]`、`admit` 写成字符串都报 `E-SCHEMA`，位置在 `admit` 下 |
| | `test_conformance_cases_installed` | `spec/mrl/conformance/factory/admit.yaml` 与 `tests/acceptance/m14/admit.conformance.yaml` 内容相同，每个用例经 `mrl.conformance.run_case` 通过 |
| `tests/acceptance/m14/test_m14_02_admit_runtime.py` | `test_job_run_admits_matching_pair` | 车型、平台一致：`factory job run flash@1` 得到 `SEALED`，产物 facts 正确 |
| | `test_job_run_rejects_mismatch_without_job` | 车型不一致：`job run` 退出 1，`INTAKE_REJECTED`，`problems` 恰好是 `admit[0]` 一项；`jobs` 表为空，没有 `job.created` |
| | `test_all_admit_failures_reported` | 车型、平台都不一致：`admit[0]`、`admit[1]` 两项，按序 |
| | `test_admit_eval_error_fails_closed` | `ecu_count` 为 0：`admit[2]: MRL-DIV-ZERO`；配额超限：`admit[2]` |
| | `test_port_problems_skip_admit` | 未签名的固件只报端口 `requires`；错类型的 Bean 只报端口 `type`；都不求值 admit |
| | `test_admit_reads_params` | `flash-fixed@1` 用参数默认值 `X1` 放行；显式 `model: Y2` 时 X1 的车与固件被拒（两项）；Y2 的一对放行 |
| | `test_admit_over_list_port` | 列表端口：同平台放行；混入异平台的固件 → `admit[0]`；固件份数超过 ECU 数 → `admit[1]` |
| | `test_line_node_fails_on_admit` | `factory line run vehicle-flash@1` 不匹配：run `FAILED`，退出 1，节点 `flash` `FAILED`、`job_id` 为空、`error.detail.problems` 是 `admit[0]`，`jobs` 表为空；匹配的一对 `SUCCEEDED` |
| | `test_source_material_joins_existing_bean` | `vehicle-pull-flash@1`：Source 按车辆 facts 现拉固件，与接口里的车辆在 `flash` 汇合并 `SUCCEEDED`；平台不一致的车：`pull` 仍 `DONE`，`flash` `FAILED`（`admit[1]`），没有 job |

验收命令：`uv run python tools/harness.py pytest <文件>`，断言 `out.failed == 0 && out.errors == 0 && out.skipped == 0`，`passed` 不少于上表的行数。

---

## 8. 错误示范与正确示范

```python
# ✖ 错误：先建 job，开工时再检查 admit，不通过就把 job 置 DEAD
#   jobs 表里留下一行从未该存在的 job，也写了 job.created；产线节点有了 job_id
# ✔ 正确：admit 是来料检验的一部分，在 Runner.submit 插入 job 之前求值，被拒时什么都不写
```

```python
# ✖ 错误：端口有问题时也去求值 admit，把缺失的端口当成空 Bean
# ✔ 正确：逐端口检查有任何问题就直接抛 INTAKE_REJECTED；admit 只在全部端口就绪后求值
```

```python
# ✖ 错误：EvalError 当作成立，或当作设施故障
# ✔ 正确：False 与 EvalError 都是不成立，detail 带 MRL 错误码；失败即关闭
```

```python
# ✖ 错误：每次求值时 compile_expr(spec["admit"][i], ...)，或用 eval
# ✔ 正确：snapshot.compiled(f"{station_key}/admit[{i}]").evaluate(env)
```

```yaml
# ✖ 错误：用自报的 meta 做准入，生产者写什么就是什么（会报 W-ADMIT-META）
admit:
  - inputs.firmware.meta.target_model == inputs.car.facts.model
# ✔ 正确：用 validator 实测的 facts
admit:
  - inputs.firmware.facts.target_model == inputs.car.facts.model
```

```yaml
# ✖ 错误：以 ! 开头不加引号，YAML 把 !has(...) 当成标签
admit:
  - !has(inputs.firmware) || inputs.firmware.facts.signed
# ✔ 正确
admit:
  - "!has(inputs.firmware) || inputs.firmware.facts.signed"
```

## 9. 风险与注意事项

- **admit 只在运行期成立**。它不是类型，也不是 guarantee；kernel 只检查它能编译。上游产物能否满足下游的 admit，要到提交时才知道。需要静态保证的约束仍用类型与 `requires`。
- **facts 才可信**。meta 由生产者自报；用 meta 做准入等于没设防，所以有 `W-ADMIT-META`。宪法可以把它升级为错误。
- **不要在实现里使用夹具的工位名、类型名、fact 名**作为常量或分支条件。
- **不要改 `check_inputs` 的端口检查**，包括问题对象的顺序与 `reason` 词表（只追加 `admit`）。
- **缓存命中不重新求值**（5.5）。如果以后 facts 允许在封存后改写，这条推理就不成立，届时要把 admit 的结果写进缓存键或在命中时重新求值。
