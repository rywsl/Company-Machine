# 契约测试（contract-tests.md）

> 规范性文档，M2。模块：`mao.contract`。V4。
> 依据：架构文档 7.5（契约测试）、8.6（黄金样例与基线）、9.6（差分）。与示例工位、factory-profile 冲突时，以已有 Spec 为准：equivalence 的环境是 `src` / `dst` / `params`，不是架构草稿里的 `in` / `out`。
> 本文件不实现 V3。演进校验不在这四个 M2 组件里。

---

## 1. 目的与范围

**做什么（M2）**

- validator 的正样例必须被接受，反样例必须被拒绝（I8 的运行部分）。
- 按 `GoldenSet` 跑工位，用 `Baseline` **已经写出的**门槛键判定，不另定数字。
- converter：正样例上 equivalence 成立；反样例不得封存；与上一版本做差分。
- 两阶段 Sink 的 dry-run：只跑 propose，不产生回执。

**不做什么**

| 不做 | 原因 |
|---|---|
| V3 兼容规则、影响面、引用计数 | 不在本文件。不要在这里重写一套演进检查 |
| PSI、滑动窗口漂移、熔断 | 架构 8.7 / 10.6 的控制器。M2 不发明 0.2 这类距离门槛 |
| judge 抽样 | judge 归出厂检验。本文件不调用模型（D10） |
| 为黄金样例或差分发明容差 | 容差只来自 Baseline 里已有的 `max` / `min` 键。差分用精确相等 |
| 查缓存 | `cache.md`：canary 与契约测试不查缓存。直接 `Runner.submit` + `Runner.run` |
| 写验收测试代码 | 第 7 章只列文件、测试名和断言 |

跑契约测试的进程禁止调用模型代理，禁止把秘密写进契约报告。

---

## 2. 依赖

| 依赖 | 用到的接口 |
|---|---|
| `Snapshot` | `key`、`spec`、`doc`、`validators`、`compiled`、`type_key` |
| `SandboxProvider` | validator 沙箱。网络 `none`。挂载见 README 第 5 节 |
| `Gate.admit_external` / `Gate.inspect` | 正样例入库、黄金样例与转换产物的出厂检验。本文件不重写检验阶段 |
| `Runner` | `submit`、`run`。默认不查缓存 |
| `Warehouse` | 上一版本用过的已封存输入 Bean |
| `Approvals`（`approvals.md`） | Sink dry-run 只走 propose |
| `ReceiptStore` | dry-run 之后断言没有回执 |
| `SecretStore.redact` | 报告与异常消息 |
| `mao.base` | `Clock`、`IdSource`、`canonical_json`、`digest_bytes` |
| `mao.db` | 迁移 `015_contract.sql` |

`timeout_s` 没有默认值。调用方不传、工位也没有 `limits.timeout` 时，抛 `CONTRACT_TIMEOUT_REQUIRED`，不要填 30 或 300。

---

## 3. 接口

```python
class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"

@dataclass(frozen=True)
class ContractResult:
    id: str                                  # "ct-0001"
    kind: str                                # validator | golden | equivalence | diff | sink_dry_run | fixture_set
    subject: str                             # Spec 键或 validator 名
    verdict: Verdict
    detail: dict[str, Any]                   # 已脱敏。字段见第 4.2 节
    at: str

class ContractRunner:
    def __init__(self, ctx: Context, snapshot: Snapshot, runner: Runner) -> None
    def run_validator(self, type_id: str, validator: str, *, timeout_s: float) -> ContractResult
        # 该类型该 validator 的全部 accept / reject 样例
    def run_golden(self, golden_key: str, baseline_key: str | None, *,
                   timeout_s: float | None = None) -> ContractResult
    def run_equivalence(self, station_key: str, *, timeout_s: float | None = None) -> ContractResult
    def run_diff(self, station_key: str, *, timeout_s: float | None = None) -> ContractResult
        # 没有上一版本：verdict PASS，detail.reason == "no_previous"。这不是失败
    def run_sink_dry_run(self, station_key: str, *, inputs: Mapping[str, str],
                         params: Mapping[str, Any],
                         timeout_s: float | None = None) -> ContractResult
    def check_fixture_set(self, old: Snapshot, new: Snapshot, type_id: str, validator: str) -> ContractResult
        # 反样例路径集合只增不减。没有数字门槛
    def get(self, result_id: str) -> ContractResult
    def list(self, *, kind: str | None = None, subject: str | None = None) -> list[ContractResult]
```

CLI：

```
factory contract validator TYPE@M.m VALIDATOR --timeout S
factory contract golden GOLDEN@N [--baseline BASELINE@N] --timeout S
factory contract equivalence STATION@N [--timeout S]
factory contract diff STATION@N [--timeout S]
factory contract sink-dry-run STATION@N --in PORT=BEAN ... [--timeout S]
factory contract ls [--kind K] [--json]
```

退出码：verdict `pass` 为 0；`fail` 为 1；用法错误为 2。`--timeout` 是调用方的期限，不是本规范规定的门槛。

---

## 4. 数据格式

### 4.1 迁移 `src/mao/migrations/015_contract.sql`

```sql
CREATE TABLE contract_runs (
  id       TEXT PRIMARY KEY,
  kind     TEXT NOT NULL,
  subject  TEXT NOT NULL,
  verdict  TEXT NOT NULL CHECK (verdict IN ('pass', 'fail')),
  detail   TEXT NOT NULL,
  at       TEXT NOT NULL
);
CREATE INDEX contract_runs_subject ON contract_runs(subject, kind);
```

`detail` 是规范 JSON。

### 4.2 detail 字段

| kind | 必有的键 |
|---|---|
| `validator` | `accept: [{path, ok}]`，`reject: [{path, ok}]`。`ok` 对正样例表示“被接受”，对反样例表示“被拒绝” |
| `golden` | `cases: [{name, job, sealed}]`，`observed: {指标名: 数}`，`gates: [{metric, key, limit, observed, ok}]` |
| `equivalence` | `accept: [{path, sealed, failed_expr}]`，`reject: [{path, sealed}]` |
| `diff` | `reason`（可空），`cases: [{input, old_sealed, new_sealed, facts_equal}]` |
| `sink_dry_run` | `approval`，`receipt`（没有回执时为 `null`），`executed_run`（bool） |
| `fixture_set` | `removed: [path]` |

失败时可以多一个 `code`，值是第 6 章的错误码。报告里不放秘密、不放沙箱环境。

### 4.3 样例路径

validator 与 converter 的 `fixtures.accept` / `fixtures.reject` 是相对 **Registry 根**的路径（与示例 `fixtures/video/ok-1920x1080.mp4` 一致）。文件不是 Spec，检查期被忽略；运行期必须能读到。路径越出 Registry 根：该样例失败，`code=CONTRACT_FIXTURE_PATH`。

`GoldenSet.spec.cases[].inputs` 的值：以 `sha256:` 开头则是已封存 Bean ID；否则是 Registry 内的文件，用 `Gate.admit_external` 入库后再提交。`GoldenSet` 没有 `budget_usd` 时抛 `CONTRACT_BUDGET_REQUIRED`，不要填 0 或 6。

### 4.4 Baseline 门槛

`Baseline.spec.metrics` 的每个指标是一个数值对象。只有这些键是门槛：

| 键 | 失败条件 |
|---|---|
| `max`，或以 `max_` 开头 | 观测值 `>` 该数（等于不算超过，与 README 的金额比较一致） |
| `min`，或以 `min_` 开头 | 观测值 `<` 该数 |

`mean`、`p50`、`p90` 以及其他不以 `max` / `min` 为门槛前缀的键只是基线记录，**不**参与判定。没有门槛键的指标记入 `observed`，不产生失败。不要把 `mean` 当成必须相等，也不要另加 PSI。

能计算的观测值只有：

| 指标名 | 观测值 |
|---|---|
| `cost_usd` | 各 case 的 `job.cost_usd` 之和，`round(..., 6)` |
| `gate_reject_rate` | 未封存的 case 数 / case 数 |

Baseline 列出了别的名字（如 `judge_score`、`duration_error`）而本模块不会算：`CONTRACT_METRIC_MISSING`，verdict `fail`。不要当成通过，也不要现编公式。

---

## 5. 行为

### 5.1 validator 正反样例

对 `snapshot.validators(type_id)` 里名为 `validator` 的那一项。找不到：`CONTRACT_NOT_FOUND`。

每个样例文件单独开一个 `network=none` 的沙箱：

| 虚拟路径 | 内容 |
|---|---|
| `/bean/payload/<文件名>` | 样例文件。`文件名` 是路径的最后一段 |
| `/bean/bean.json` | 最小对象：`type` 为该类型 id，`facts` 为 `{}`，`meta` 为 `{}`，`payload` 含这一份文件的摘要与大小 |

argv 是 validator 的 `run`，超时是调用方的 `timeout_s`。退出码与 stdout 的解释：

- **接受**：退出码 0，且 stdout 是一个 JSON 对象，包含每一个 `emits` 名字。值的 JSON 类型必须与该 fact 的声明相容：`string`→字符串，`int`→整数（bool 不算），`float` / `num`→数，`bool`→布尔。多出来的键不算失败。
- **拒绝**：不是上面的“接受”。退出码非 0、不是 JSON、缺 fact、类型不对，都算拒绝。

正样例必须被接受，否则该样例 `ok=false`，总 verdict `fail`，`code=CONTRACT_FIXTURE_REJECTED`。反样例必须被拒绝，否则 `code=CONTRACT_FIXTURE_ACCEPTED`。不比较 fact 的具体数值：正样例只要求“能测出声明过的 fact”。

### 5.2 反样例集合只增不减

`check_fixture_set` 比较两个快照里同一 validator 的 `fixtures.reject` 路径集合。`old` 有而 `new` 没有的路径列入 `removed`。`removed` 非空则 `fail`，`code=CONTRACT_FIXTURE_REMOVED`。只增加、或完全相同：`pass`。不比较文件字节，不规定“至少 N 个”反样例。删除反样例是宪法级操作（I8）；本函数只报告，不修改 Registry。

### 5.3 黄金样例

1. `golden_key = snapshot.key("GoldenSet", golden_key)`。`station = snapshot.key("Station", spec.station)`。
2. 没有 `budget_usd`：`CONTRACT_BUDGET_REQUIRED`。
3. 超时：参数优先，否则工位 `limits.timeout`。两者都没有：`CONTRACT_TIMEOUT_REQUIRED`。
4. 按 `cases` 的书写顺序逐个 `Runner.submit` + `Runner.run`。不调用 `JobCache.lookup`。花费累计超过 `budget_usd`（`>`）时停止后续 case，`CONTRACT_BUDGET_EXCEEDED`，已跑完的 case 仍写入 detail。
5. 给了 `baseline_key` 时，按第 4.4 节填 `gates`。任一门槛 `ok=false`：`fail`，`code=CONTRACT_BASELINE_EXCEEDED`。没给 baseline：只记录是否封存，不发明门槛；全部封存则 `pass`。
6. 观测值算不出来的指标：`fail`，`CONTRACT_METRIC_MISSING`。

黄金样例使用夹具里的脚本工位。本模块不为了“跑通 explainer”去调用模型。

### 5.4 converter 的 equivalence

只接受 `role=converter` 的工位，否则 `CONTRACT_NOT_FOUND`。

正样例：`admit_external` 得到输入 Bean（入库失败则该样例 `sealed=false`），`Runner.submit` 该工位，`run` 到终态。

- 未 `SEALED`：`failed_expr` 为空，该样例失败，`code=CONTRACT_FIXTURE_REJECTED`。
- 已 `SEALED`：对 `spec.equivalence` 的每一条，`snapshot.compiled(f"{station_key}/equivalence[{i}]").evaluate({"src": bean_value(输入), "dst": bean_value(输出), "params": params_value(...)})`。结果不是 `True`，或求值抛错：把该下标记入 `failed_expr`，`code=CONTRACT_EQUIVALENCE_FAILED`。禁止把异常当成成立。环境名字是 `src` 与 `dst`，与示例 `subs-convert@1` 和 factory-profile 一致。

反样例：不要求入库成功。无论失败发生在入库、操作工还是出厂检验，只要没有新的已封存输出，该反样例就算被拒绝（`sealed=false` 为通过）。产生了已封存输出：`code=CONTRACT_FIXTURE_ACCEPTED`。

### 5.5 差分

上一版本：同一 `metadata.name`、版本号更小、且存在于本快照中的最大版本。没有：`PASS`，`detail.reason="no_previous"`，不跑沙箱。

输入集合是下面两者的并，按 Bean ID 升序，去重：

1. 当前版本的 `fixtures.accept` 所入库的 Bean；
2. Warehouse 里，上一版本已 `SEALED` 的 job 的全部输入 Bean。

不抽样，不设“最近 N 个”。集合可以很大，见第 9 章。

对每个输入分别跑旧版本与新版本（都不查缓存）：

| 旧 | 新 | 结果 |
|---|---|---|
| 封存 | 未封存 | `fail`，`CONTRACT_DIFF_REGRESSION`（旧通过、新失败） |
| 未封存 | 封存或未封存 | 不算回归 |
| 都封存 | 两边 facts 的**共有键**用规范 JSON 精确比较 | 任一键不同：`fail`，`CONTRACT_DIFF_FACTS` |

精确比较不用 epsilon。Baseline 的门槛**不**套用到差分上。新版本仍须通过第 5.4 节的 equivalence；差分函数不重复发明关系，调用方可以接着调用 `run_equivalence`。

### 5.6 Sink dry-run

工位不是 `sink`，或 `phases` 不含 `propose`：抛 `CONTRACT_SINK_NO_PROPOSE`。不要改跑 execute 来“凑一个通过”。

1. `Runner.submit` 后只执行 `approvals.md` 第 5.4 节的 propose。
2. 不调用配方的 `run`，不调用 `ReceiptStore.claim`。
3. propose 未得到 `approved`：verdict `fail`，detail 记下审批 id 与状态。这不是本模块的新错误码，沿用审批行里的码。
4. propose 已批准：仍然**不**自动 execute。检查 `receipts` 中没有该 job 的行。有行：`fail`，`CONTRACT_SINK_PUBLISHED`。没有行：`pass`，`executed_run=false`。
5. 真正的发布由调用方另一次显式 execute 完成，不在 dry-run 里。验收测试的本地 Sink 若要验证“批准后才能复制文件”，那是 `approvals.md` 的测试，不是本函数的默认步骤。

### 5.7 定时 canary

`ContractRunner.canary(station_key)` 选择引用该工位的、Spec 键最小的一个 `GoldenSet`，调用 `run_golden`（带同工位的 Baseline，若有多个则取键最小的一个）。没有 GoldenSet：`CONTRACT_NOT_FOUND`。不查缓存。多久跑一次由 `scheduler.md` 的配置决定，本文件不规定周期。

### 5.8 写入

每个公开方法在一个事务里插入 `contract_runs` 并返回。`detail` 先 `redact_obj`。方法不修改 Registry，不封存契约报告本身（报告是表里的一行，不是 Bean）。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `CONTRACT_NOT_FOUND` | 类型、validator、工位、GoldenSet 不存在，或工位不是 converter | `subject` |
| `CONTRACT_TIMEOUT_REQUIRED` | 调用方与工位都没有超时 | `subject` |
| `CONTRACT_BUDGET_REQUIRED` | GoldenSet 没有 `budget_usd` | `golden` |
| `CONTRACT_BUDGET_EXCEEDED` | 黄金样例累计花费超过其 `budget_usd` | `golden, spent, cap` |
| `CONTRACT_FIXTURE_PATH` | 样例路径越出 Registry 根 | `path` |
| `CONTRACT_FIXTURE_REJECTED` | 正样例未被接受或未封存 | `path` |
| `CONTRACT_FIXTURE_ACCEPTED` | 反样例被接受或被封存 | `path` |
| `CONTRACT_FIXTURE_REMOVED` | 新快照删了旧快照有的反样例路径 | `removed` |
| `CONTRACT_METRIC_MISSING` | Baseline 列出了本模块不会算的指标 | `metric` |
| `CONTRACT_METRIC_UNKNOWN` | 门槛键不是数 | `metric, key` |
| `CONTRACT_BASELINE_EXCEEDED` | 观测值越过 Baseline 已写的门槛 | `metric, key, limit, observed` |
| `CONTRACT_EQUIVALENCE_FAILED` | equivalence 不为真或求值失败 | `station, index` |
| `CONTRACT_DIFF_REGRESSION` | 旧版本封存、新版本没有 | `input` |
| `CONTRACT_DIFF_FACTS` | 共有 fact 不完全相同 | `input, fact` |
| `CONTRACT_SINK_NO_PROPOSE` | 对非两阶段 Sink 调用 dry-run | `station` |
| `CONTRACT_SINK_PUBLISHED` | dry-run 期间出现了回执行 | `job, receipt` |

verdict 为 `fail` 时，上表的码写入 `detail.code`，同时抛 `FactoryError` 的只有“无法开始”的那些：`CONTRACT_NOT_FOUND`、`CONTRACT_TIMEOUT_REQUIRED`、`CONTRACT_BUDGET_REQUIRED`、`CONTRACT_SINK_NO_PROPOSE`、`CONTRACT_FIXTURE_PATH`、`CONTRACT_METRIC_UNKNOWN`。样例没过、门槛没过、差分没过：不抛，返回 `FAIL` 的 `ContractResult`，CLI 退出码 1。

---

## 7. 验收测试

夹具：`tests/fixtures/registry-m2/` 增加一个带 validator 的类型（正反样例各至少一）、一个脚本 converter（equivalence 比较一个整数 fact）、一个 `GoldenSet` 与一个只含 `cost_usd.max` 的 `Baseline`、一个两阶段本地 Sink。反样例文件是零字节或明显坏掉的文本。全部 `LocalProvider`，不调用模型。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m2/test_m2_08_contract.py` | `test_validator_accept_and_reject` | 正样例 `ok=true`（被接受），反样例 `ok=true`（被拒绝），verdict `pass` |
| | `test_reject_fixture_that_passes_fails` | 把反样例换成正样例的副本后，verdict `fail`，`code=CONTRACT_FIXTURE_ACCEPTED` |
| | `test_fixture_set_removed` | 新快照少一条 reject 路径 → `CONTRACT_FIXTURE_REMOVED`；只增加路径 → `pass` |
| | `test_golden_within_baseline` | 观测 `cost_usd` `<=` Baseline 写出的 `max` 时 `gates[].ok` 为真 |
| | `test_golden_exceeds_written_max` | 把 Baseline 的 `max` 改成小于观测值（测试自己写的数）后 `CONTRACT_BASELINE_EXCEEDED`。规范不规定这个数 |
| | `test_baseline_record_keys_are_not_gates` | 只有 `p50`、没有 `max_*` / `min_*` 时不因数值失败 |
| | `test_unknown_metric_fails` | Baseline 含 `judge_score` 时 `CONTRACT_METRIC_MISSING`，而不是当成 0 通过 |
| | `test_golden_budget_required` | 去掉 `budget_usd` → `CONTRACT_BUDGET_REQUIRED` |
| | `test_equivalence_holds` | 正样例封存且 equivalence 为真；把表达式改成恒假的补丁后 `CONTRACT_EQUIVALENCE_FAILED` |
| | `test_converter_reject_not_sealed` | 反样例没有已封存输出 |
| | `test_diff_regression` | 旧版本能封存、新版本的配方恒失败时 `CONTRACT_DIFF_REGRESSION` |
| | `test_diff_facts_exact` | 共有 fact 差 1 即 `CONTRACT_DIFF_FACTS`；没有 epsilon |
| | `test_diff_skipped_without_previous` | 只有一个版本时 `pass` 且 `reason=no_previous` |
| | `test_sink_dry_run_no_receipt` | propose 批准后 `receipts` 仍无该 job，`executed_run` 为假 |
| | `test_sink_without_propose_refuses` | 单阶段 Sink → `CONTRACT_SINK_NO_PROPOSE`，不执行 `run` |
| | `test_timeout_required` | 工位无 `limits.timeout` 且调用方不传 → `CONTRACT_TIMEOUT_REQUIRED` |

验收命令：

```
uv run python tools/harness.py pytest tests/acceptance/m2/test_m2_08_contract.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 16`。

---

## 8. 错误示范与正确示范

**8.1 反样例被接受就是失败，不是“validator 更宽容了”**

```python
# ✖ 错误：退出码 0 就记 pass，不管这是 reject 目录里的文件
# ✔ 正确：reject 样例一旦满足第 5.1 节的“接受”条件，code = CONTRACT_FIXTURE_ACCEPTED
```

**8.2 不要发明门槛**

```python
# ✖ 错误：架构正文里有 PSI 0.2，于是黄金样例差 0.2 就失败
if psi(observed, baseline) > 0.2: fail()
# ✖ 错误：差分允许时长相差 0.05
if abs(a - b) <= 0.05: ok = True
# ✔ 正确：只比较 Baseline 已有的 max / min 键；差分用规范 JSON 精确相等
#         不会算的指标 CONTRACT_METRIC_MISSING
```

**8.3 equivalence 用 src / dst，失败即关闭**

```python
# ✖ 错误：照架构草稿写 out.facts，示例工位的表达式读的是 dst
env = {"in": src, "out": dst}
# ✖ 错误：求值异常时跳过这条 equivalence
# ✔ 正确：snapshot.compiled(f"{station}/equivalence[{i}]").evaluate({"src": ..., "dst": ..., "params": ...})
#         异常或非 True → CONTRACT_EQUIVALENCE_FAILED
```

**8.4 dry-run 不是小规模真发布**

```python
# ✖ 错误：dry-run 时调用配方 run，再删掉目标文件
# ✔ 正确：只走 propose；出现 receipts 行就是 CONTRACT_SINK_PUBLISHED
```

**8.5 没有上一版不是回归**

```python
# ✖ 错误：新 converter 没有历史 job，就判差分失败
# ✔ 正确：reason = no_previous，verdict pass，不跑沙箱
```

---

## 9. 风险与注意事项

- **差分不做抽样。** 上一版本的全部已封存输入都会再跑一遍。Warehouse 很大时，这是一次昂贵的全量比较。M2 不引入“最近 N 个”来省钱；要限流，调用方少调用，而不是在这里写一个 N。
- **正样例不锁定 fact 数值。** 锁定数值会把 validator 的输出口径写成第二份规范。数值关系由 equivalence 与 Baseline 的已写门槛负责。
- **黄金样例会花钱。** 上限只取 GoldenSet 自己的 `budget_usd`。超过就停，不自动改跑更便宜的子集。
- **canary 不查缓存**，否则外部 provider 的漂移会被缓存盖住（`cache.md` 第 9 章）。
- **报告不是 Bean**，也不进血缘。不要为了“可追溯”把报告封存成物料。
- **Windows**：样例路径用 `pathlib`，相对 Registry 根解析后再检查是否仍在根下。复制用 `shutil.copyfile`，不要把大样例读进内存。
---

## 10. 任务拆分

| ID | 标题 | 依赖 | 规范章节 | 允许修改的路径 | 验收 | 规模 |
|---|---|---|---|---|---|---|
| M2-08 | 契约运行：validator、黄金样例、等价、差分与 Sink 干跑 | M2-06、M2-10 | 3–7 | `src/mao/contract.py`、`src/mao/migrations/015_contract.sql`、`src/mao/cli.py`、`src/mao/errors.py`、`tests/system/test_contract_*.py` | `test_m2_08_contract.py`（≥ 16 通过） | L |

`tests/acceptance/m2/` 属于预先写好的验收材料。任务执行者不得修改它们。
