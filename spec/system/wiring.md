# wiring：生产路径接线——Sink 回执、命令补全、缓存与网关（M10）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M10-01 至 M10-03**。
> 阶段门 M9 通过之后，按文档演练实际运行产线时发现（`plan/review-wiring.md`）：Sink 的回执在生产路径上只被占用、从不发布和核验，`connectors.verify` 是永远返回通过的桩；`factoryctl receipt`、`factory receipt …`、`factory source run`、`factory run` 没有实现；生产路径上没有一处产线运行器带缓存；几处 `Runner` 没有装网关。本文件规定这些能力怎样接上，并补上 `connectors.md`、`cache.md`、`commands.md`、`governance.md`、`corrections.md`、`closure.md` 写错或漏写的部分。
> 本文件与上述规范冲突时，以本文件为准；其余以原文为准。
> 回执核验沙箱注入秘密、登记 `check` 出网主体、使用机器的沙箱资源，见 `capabilities.md` 5.5、5.6（M11）。

---

## 1. 目的与范围

**做什么**

| 编号 | 内容 | 任务 | 性质 |
|---|---|---|---|
| W-01 | Sink 的 execute 阶段按 `connectors.md` 5.3 取得回执、发布、核验、结算；`connectors.verify` 在沙箱里真实运行核验配方 | M10-01 | 实现缺失 + 桩 |
| W-02 | Sink 操作工超时、`factoryctl fail`、agent 报错、退出码非 0 都直接 `SINK_OUTCOME_UNKNOWN`，不重试 | M10-01 | 实现错误 |
| W-03 | `factoryctl receipt`（`connectors.md` 3.2） | M10-01 | 实现缺失 |
| W-04 | `factory receipt ls/show/verify/void`、`factory source run`；Source 产物的来源证明带 `external: true` | M10-02 | 实现缺失 + 设计缺口 |
| W-05 | `factory run STATION@N`：单工位运行，可选查缓存（`cache.md` 第 3 章） | M10-02 | 实现缺失 |
| W-06 | 生产路径的产线运行器都带 `JobCache`（影子运行除外） | M10-03 | 实现缺失 |
| W-07 | 生产代码只经 `open_runner` / `open_line_runner` 构造运行器，网关与缓存因此一处装好 | M10-03 | 实现缺失 + 设计缺口 |
| W-08 | job 状态表补 `GATING → DEAD`；默认构造写法改用工厂函数；补命令清单 | 本文件 | 设计缺口 |

**不做什么**

| 不做 | 原因 |
|---|---|
| `sink.verify_failed` 唤醒厂长 | 现有信号泵不含该信号；另行设计 |
| `RUNNING` 状态的 Sink job 崩溃恢复不重跑操作工 | 需要改 `durable.md` 的恢复语义；本里程碑不动 |
| 重写 `test_m2_10_sink.py`、`test_m2_11_source.py` | 它们只验证 `ReceiptStore` 与 `Runner` 的直接调用，结论仍然成立；运行期行为由 `tests/acceptance/m10/` 覆盖 |
| 新的回执字段或核验协议 | 沿用 `connectors.md` 第 3–4 章 |

---

## 2. 依赖

| 依赖 | 用途 |
|---|---|
| `connectors.md` | `ReceiptStore`、`Claim`、`receipt.json`、核验配方约定 4.3、事件 4.4、错误码 |
| `runner.md`、`jobs` | job 状态机、`_materialize` 的输入布局、`job.json` |
| `factoryctl.md` | `factoryctl` 子命令约定（标准库、Python 3.8、退出码 2） |
| `egress.md` | Sink 执行与核验共用的出网计划与秘密注入 |
| `cache.md` | `JobCache`、`CACHEABLE_ROLES`、`material` / `lookup` / `bind` |
| `closure.md` 5.3 | `open_runner` 装网关 |
| `commands.md` | CLI 的 JSON 输出与错误输出约定 |

---

## 3. 接口

```python
# mao.runs（新增）
def open_line_runner(ctx: Context, snapshot: Snapshot, runner: Runner | None = None, *,
                     cache: bool = True) -> LineRunner
    # runner 缺省为 open_runner(ctx, snapshot)；cache 为真时 LineRunner(..., cache=JobCache(ctx))，否则 cache=None

# mao.runner（已有，不变）
def open_runner(ctx: Context, snapshot: Snapshot) -> Runner          # Runner(ctx, snapshot, gateway=ServiceGateway(...))

# mao.connectors（签名不变，行为见 5.1）
def verify(ctx: Context, snapshot: Snapshot, receipt: Receipt) -> tuple[bool, str]
class ReceiptStore:
    def settle(self, receipt_id: str, *, ok: bool, detail: str) -> Receipt
        # 允许的起始状态：published、verified、failed（connectors.md 5.4 第 4 条的复核）；claimed、void → RECEIPT_STATE_INVALID

# mao.jobs
EDGES[JobState.GATING] = {SEALED, REJECTED, FAILED, DEAD}           # 新增 DEAD，只用于 5.1 第 5 步
```

```
factoryctl receipt --target TARGET [--locator KEY=VALUE]... [--item BEAN=LOCATOR[@DIGEST]]... [--note TEXT]

factory receipt ls [--station NAME | NAME@N] [--status STATUS]
factory receipt show RC_ID
factory receipt verify RC_ID
factory receipt void RC_ID --by ACTOR --reason TEXT
factory source run STATION@N [--param NAME=VALUE]...
factory run STATION@N [--input PORT=BEAN]... [--param NAME=VALUE]... [--cache] [--variant V]
```

---

## 4. 数据格式

### 4.1 `factoryctl receipt` 写出的 `receipt.json`

字段与 `connectors.md` 4.2 相同：`{"target", "locator", "items", "note"}`。

- `--locator KEY=VALUE`：按第一个 `=` 切分；没有 `=` → 退出码 2。重复的键以最后一次为准。
- `--item BEAN=LOCATOR[@DIGEST]`：按第一个 `=` 切出 `bean`；其余部分最后一个 `@` 之后若以 `sha256:` 开头，就是 `digest`，否则整段都是 `locator`（定位信息里可能本来就有 `@`，例如 `git@host:repo`）。没有 `=` → 退出码 2。
- 缺少 `--target` 或参数格式错误时退出码 2，并且不写文件。成功时原子写入（临时文件 + rename）`$FACTORY_CTL/receipt.json`，退出码 0。

### 4.2 核验沙箱

| 项 | 规定 |
|---|---|
| 名字 | `mao-<job>-verify-<n>`，`n` 从 1 起，同一 job 每次核验加 1（执行时核验与 `factory receipt verify` 共用计数） |
| 工作目录 | `$FACTORY_ROOT/jobs/<job>/verify-<n>/{in,out,ctl,workspace}` |
| 镜像、资源 | Sink 工位所在机器的镜像与资源 |
| `/factory/in` | 只读；Sink job 的输入，布局与执行时的物化相同（`runner.md`） |
| `/factory/ctl` | 只读；`receipt.json`（回执记录的 `target`、`locator`、`items`、`note`）与 `job.json`（Sink job 最后一次 attempt 的那一份） |
| `/factory/out`、`/workspace` | 可写、初始为空；内容忽略 |
| `/opt/factory` | 只读；与执行时相同 |
| 网络与秘密 | 与 Sink 执行时相同（`egress.md` 的出网计划） |
| argv | 核验配方的 `run`，含 `{{` 的项按 `recipe_env(Sink job 的输入 Bean, Sink job 的参数)` 渲染 |
| 超时 | 工位 `limits.timeout` 与 300 秒中较小者；没有 `limits.timeout` 时 300 秒 |
| 结果 | 退出码 0 → 通过；1 → 不通过；其他或超时 → `RECEIPT_VERIFY_ERROR`。stdout 是含 `detail` 的 JSON 对象时取其 `detail`，否则取去掉首尾空白的 stdout |

### 4.3 `SINK_OUTCOME_UNKNOWN` 的 `detail.cause`

| 情形 | `cause` |
|---|---|
| 操作工超时 | `{"timeout_s": <秒数>}` |
| 操作工调用了 `factoryctl fail` | `{"reason": <fail.json 的 reason>}` |
| agent transcript 报告 `is_error` | `{"result": <结果文本>}` |
| 退出码非 0 | `{"exit_code": <退出码>}` |

多种情形同时成立时，按上表从上到下取第一个。

### 4.4 Source 产物的来源证明

Source 工位封存的每个 Bean，其 `bean.sealed` 事件的 `data` 在 `{bean, job, type, station}` 之外增加 `"external": true` 与 `"params": <job 参数>`（`connectors.md` 5.5 第 3 条）。

### 4.5 命令输出

| 命令 | 输出（`--json` 与否都是 JSON） | 退出码 |
|---|---|---|
| `receipt ls` | `Receipt` 数组，按 `created_at`、`id` 升序；`status` 为字符串 | 0 |
| `receipt show` / `void` | 该 `Receipt` | 0 |
| `receipt verify` | 结算后的 `Receipt` | 结算为 `verified` 时 0，`failed` 时 1 |
| `source run` | `dump_job(job)` | `SEALED` 时 0，否则 1 |
| `run` | `{"job": dump_job(job), "cache_hit": bool}` | job `SEALED` 时 0，否则 1 |

错误按现有约定输出：`--json` 时 stdout 上的 `{"error": {...}}`，否则 stderr 上的 `CODE: message`；退出码按 README 4.10（`CLI_USAGE` 为 2，业务错误为 1）。argparse 层面的用法错误（缺少必需参数）退出码 2。

---

## 5. 行为

### 5.1 Sink 的回执（W-01、W-02、W-03）

适用于有 `idempotency_key` 的 Sink 工位，`sink_mode` 为 `execute` 时（影子运行的 `dry-run` 不变：不占用、不发布）。

1. **占用**：`connectors.md` 5.2，不变。
2. **操作工结束后**，预算停止与 `BUDGET_EXCEEDED` 的检查保持原有位置与优先级；之后，第 4.3 节任一情形成立 → job 直接 `DEAD`（`RUNNING → DEAD`），错误码 `SINK_OUTCOME_UNKNOWN`，`detail = {job, cause}`，**不论 `limits.retries` 是多少都不重试**；回执保持 `claimed`。
3. **取得回执**：
   - 有 `ctl/receipt.json`：读取。不是合法 JSON 或不是对象 → 当作 `problems == ["json"]` 的格式错误，走第 4 步的 `RECEIPT_INVALID` 分支。
   - 否则配方有 `receipt` 模板：`target` 与 `locator` 的每个值，含 `{{` 的按表达式位置 `<Machine 键>/recipes.<配方>.receipt.target`、`<Machine 键>/recipes.<配方>.receipt.locator.<名字>` 以 `recipe_env(输入 Bean, 参数)` 渲染，不含的原样使用；`items` 为空数组。
   - 两者都没有 → `DEAD`，`RECEIPT_MISSING`，`detail = {job}`；回执保持 `claimed`。
4. **发布**：`ReceiptStore.publish(回执, 载荷)`。`RECEIPT_INVALID` 时：回执的 `verify_detail` 写成 `"receipt.json 格式错误：" + ", ".join(problems)`，job `DEAD`，`detail = {job, problems}`；回执保持 `claimed`。
5. **核验**：`RUNNING → GATING`（原因 `verify`），调用 `verify(ctx, snapshot, 回执)`：
   - 抛出 `RECEIPT_VERIFY_ERROR` → `GATING → DEAD`，`detail = {receipt, cause}`；回执保持 `published`，不结算。
   - 返回不通过 → `settle(ok=False)`（写 `sink.verify_failed`），然后 `GATING → DEAD`，`RECEIPT_UNVERIFIED`，`detail = {receipt, detail}`。
   - 返回通过 → `settle(ok=True)`（写 `sink.verified`），然后封存：`outputs == {}`，记录输入血缘，写 `job.state` 与 `job.sealed`。
   Sink 不收货、不会进入 `REJECTED`；封存时是否附带空的出厂检验报告由实现决定。
6. **崩溃恢复**：处于 `GATING` 的 Sink job，回执为 `published` 时从第 5 步重新核验；回执已是 `verified` 时直接封存。
7. **`verify` 的实现**按第 4.2 节，在新沙箱里运行，不得读取执行沙箱的文件系统，不得根据回执内容或工位名在宿主上自行判断。回执状态不是 `published`、`verified`、`failed` 时抛 `RECEIPT_STATE_INVALID`（`op = "verify"`）。
8. **`factoryctl receipt`**按第 4.1 节；它是沙箱内 `factoryctl` 的子命令，只用标准库、兼容 Python 3.8。

### 5.2 命令（W-04、W-05）

1. **快照**：`receipt verify` 用 Sink job 的 epoch——有治理时 `EpochStore(ctx).materialize(job.epoch)`，否则加载 `--registry`。`source run`、`run` 与 `line run` 一样用 `open_snapshot`。
2. **`receipt ls`**：`--station` 不含 `@` 时按 `station_name` 过滤，含 `@` 时按 Spec 键 `Station:<值>` 过滤；`--status` 按状态过滤。
3. **`receipt verify`**：取回执 → `verify`（5.1 第 7 条）→ `settle`。`failed → verified` 与 `verified → failed` 都允许（复核）。回执不存在 → `RECEIPT_NOT_FOUND`。
4. **`receipt void`**：`ReceiptStore.void`，写 `receipt.voided {receipt, by, reason}`。作废后同一幂等键可以再次发布（`connectors.md` 5.2）。
5. **`source run`**：工位角色不是 `source` → `ROLE_MISMATCH`，`detail = {station, role, expected: "source"}`。否则 `submit(工位, {}, 参数)` 后 `run`，按第 4.5 节输出。
6. **`run`**：缺少工位 → 退出码 2。`--input`、`--param` 没有 `=` → `CLI_USAGE`。参数值按工位声明的类型转换：`string` 原样；`int`、`float`、`num`、`bool` 按 JSON 解析，失败 → `CLI_USAGE`。工位角色在 `CACHEABLE_ROLES` 中时：
   - 计算 `material`（含 `--variant`）；
   - 有 `--cache` 时先 `lookup`，命中则**不创建 job**，输出命中的 job，`cache_hit: true`；
   - 未命中或没有 `--cache`：`submit` 后立即 `bind`，再 `run`。
   角色不可缓存时忽略 `--cache`，照常运行，`cache_hit: false`。
7. **Source 的来源证明**按第 4.4 节。
8. `commands.md` 3.2 的命令表增加本节的三组命令；`test_every_command_parses` 不变。

### 5.3 构造点（W-06、W-07）

1. `src/mao` 中，`Runner(...)` 只出现在 `mao.runner.open_runner` 里，`LineRunner(...)` 只出现在 `mao.runs.open_line_runner` 里。其余所有地方（命令、守护进程、控制台、调度、调和器、契约测试、治理流水线的默认 `contracts` 与 `rebuild`、影子运行、planner 检查）都调用这两个函数。
2. 生产路径的产线运行器一律带缓存：`open_line_runner(ctx, snapshot[, runner])`。唯一例外是影子运行：`open_line_runner(ctx, candidate, cache=False)`，并照旧 `use_cache=False`、`sink_mode="dry-run"`（`corrections.md` 第 177 行的其余参数不变）。planner 检查只读 run，可以用 `cache=False`。
3. 带缓存的运行器在提交可缓存节点后总是 `bind`；是否 `lookup` 由 run 的 `use_cache` 决定（`line run --no-cache` 为假）。
4. `governance.md` 与 `corrections.md` 中 `rebuild` 缺省写法里的 `LineRunner(ctx, snap, Runner(ctx, snap))`，以及 `contracts` 缺省写法里的 `Runner(ctx, snap)`，改读为 `open_line_runner(ctx, snap)` 与 `open_runner(ctx, snap)`。`closure.md` 5.3 第 1 条的列举由本节第 1 条取代。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `ROLE_MISMATCH`（新增） | `factory source run` 的工位不是 Source | `station, role, expected` |
| `SINK_OUTCOME_UNKNOWN` | 第 4.3 节任一情形 | `job, cause` |
| `RECEIPT_MISSING` | 没有 `receipt.json` 也没有 `receipt` 模板 | `job` |
| `RECEIPT_INVALID` | 载荷不符合 `connectors.md` 4.2，或不是 JSON 对象 | `job, problems` |
| `RECEIPT_UNVERIFIED` | 核验配方退出码 1 | `receipt, detail` |
| `RECEIPT_VERIFY_ERROR` | 核验退出码非 0/1、超时、沙箱无法创建 | `receipt, cause` |
| `RECEIPT_STATE_INVALID` | 对 `claimed`/`void` 回执核验或结算 | `receipt, status, op` |
| `RECEIPT_NOT_FOUND` | 回执 id 不存在 | `receipt` |
| `CLI_USAGE` | `--input`、`--param` 格式或类型错误 | — |

---

## 7. 验收测试

夹具：`tests/acceptance/m10/wiring_world.py` 在 `registry-m2` 的副本上增加 8 个 Sink 工位与 2 条以测试目录为发布目标的产线；Sink 的操作工是测试运行时写出的标准库脚本，经 `LocalProvider` 的命令映射执行。`World` 是进程内工厂（`FakeClock`、`CounterIds`，`Runner` 由 `open_runner` 构造），`Cli` 经 `mao.cli.main` 操作工厂目录。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m10/test_m10_01_sink_receipts.py` | `test_sink_publishes_and_verifies` | `publish-local@1` 经 `Runner.run`：job `SEALED`、`outputs == {}`；回执 `verified`，`target`/`locator` 来自 `receipt.json`；事件依次是 `sink.claimed`、`sink.published`、`sink.verified` |
| | `test_duplicate_publish_is_suppressed` | 同样输入与参数再运行：`sink.duplicate`，不再执行操作工（目标文件 mtime 不变），只有一条回执 |
| | `test_line_rerun_succeeds` | `factory line run` 同一产线两次都 `SUCCEEDED`；第二次的 publish 节点是重复发布 |
| | `test_receipt_from_recipe_template` | 操作工不写 `receipt.json`：回执由配方的 `receipt` 模板渲染，核验通过 |
| | `test_receipt_missing_is_dead` | 既没有 `receipt.json` 也没有模板 → `RECEIPT_MISSING`，回执保持 `claimed` |
| | `test_receipt_invalid_is_dead` | `receipt.json` 缺少 `target` → `RECEIPT_INVALID`，`problems` 含 `target`，`verify_detail` 以 `receipt.json 格式错误` 开头 |
| | `test_unverified_receipt_is_dead` | 回执指向不存在的文件 → 核验配方不通过：`RECEIPT_UNVERIFIED`，回执 `failed`，事件 `sink.verify_failed` |
| | `test_verify_facility_error_keeps_published` | 核验配方退出码 3 → `RECEIPT_VERIFY_ERROR`，回执保持 `published`，没有结算事件 |
| | `test_sink_timeout_and_refusal_are_outcome_unknown` | 超时与 `factoryctl fail` 都是第 1 次 attempt 就 `SINK_OUTCOME_UNKNOWN`（`retries.failed: 2` 也不重试），`cause` 符合第 4.3 节 |
| | `test_factoryctl_receipt` | 操作工用 `factoryctl receipt --target --locator --item` 留回执 → `items` 含输入 Bean 与摘要，核验通过；缺 `--target` 或 `--locator` 无 `=` → 退出码 2 且不写文件 |
| `tests/acceptance/m10/test_m10_02_commands.py` | `test_receipt_ls_and_show` | `receipt ls` 列出回执，`--status`、`--station NAME`、`--station NAME@N` 过滤；`receipt show` 返回同一条 |
| | `test_receipt_verify_detects_deleted_target` | 删除目标文件后 `receipt verify` 退出码 1、状态 `failed`、`sink.verify_failed`；恢复文件后再核验 → `verified` |
| | `test_receipt_verify_rejects_claimed` | `factory run` 一个缺回执的 Sink 留下 `claimed` 回执；`receipt verify` → 退出码 1、`RECEIPT_STATE_INVALID` |
| | `test_receipt_void_allows_republish` | `receipt void` 写 `receipt.voided`；同一产线再运行重新发布，回执状态为 `void` 与 `verified` 各一 |
| | `test_receipt_command_errors` | 不存在的回执 → `RECEIPT_NOT_FOUND`（退出码 1）；`void` 缺 `--by` → 退出码 2 |
| | `test_source_run_ingests` | `source run pull-notes@1 --param from_dir=…` 封存 Bean；`source.ingested`；`bean.sealed` 带 `external: true`；再摄入同样内容得到同一个 Bean |
| | `test_source_run_requires_source_role` | 非 Source 工位 → `ROLE_MISMATCH`（退出码 1）；`--param` 无 `=` → 退出码 2 |
| | `test_run_reuses_cache_only_when_asked` | `run upper@1` 不查缓存；加 `--cache` 命中同一个 job；不加时再建新 job；`--variant` 不同则不命中 |
| | `test_run_params_and_exit_codes` | `--param` 传给 Sink 并发布；Sink 上的 `--cache` 被忽略；`DEAD` 退出码 1；缺工位或 `--param` 格式错误退出码 2 |
| `tests/acceptance/m10/test_m10_03_wiring.py` | `test_line_run_reuses_cached_jobs` | `factory line run` 第二次：可缓存节点全部 `cache_hit`、复用第一次的 job，`spent_usd == 0`，run `SUCCEEDED` |
| | `test_no_cache_flag_runs_fresh` | `--no-cache` 不查缓存、重新花费，但仍登记键：之后的普通运行命中 |
| | `test_serve_tick_binds_cache_keys` | 守护进程按触发器执行的 run 登记了缓存键：随后 `factory line run` 同样输入命中守护进程那次的 job |
| | `test_constructions_go_through_factories` | `src/mao` 里 `Runner(...)` 只在 `open_runner` 中、`LineRunner(...)` 只在 `open_line_runner` 中 |

验收命令：`uv run python tools/harness.py pytest <文件>`，断言 `out.failed == 0 && out.errors == 0 && out.skipped == 0`，`passed` 不少于上表的行数。

---

## 8. 错误示范与正确示范

```python
# ✖ 错误：相信操作工退出码 0 就结算成功，或在宿主上检查文件代替核验配方
store.settle(receipt.id, ok=True, detail="exit 0")
# ✔ 正确：在新的核验沙箱里运行工位的 verify_receipt 配方，按退出码结算
ok, detail = verify(ctx, snapshot, store.publish(receipt.id, payload))
store.settle(receipt.id, ok=ok, detail=detail)
```

```python
# ✖ 错误：Sink 超时或 factoryctl fail 走普通的 FAILED → 重试；第二次 attempt 因回执仍是 claimed 而撞上 IDEMPOTENCY_CONFLICT
return self._fail(job, "OPERATOR_TIMEOUT", ...)
# ✔ 正确：发布可能已经部分发生，直接 DEAD，留给厂长核实后作废回执
return self._dead(job, {"code": "SINK_OUTCOME_UNKNOWN", "detail": {"job": job.id, "cause": {"timeout_s": t}}})
```

```python
# ✖ 错误：在调用处各自构造，漏掉网关或缓存
lines = LineRunner(ctx, snapshot, Runner(ctx, snapshot))
# ✔ 正确：只经工厂函数
lines = open_line_runner(ctx, snapshot)
```

```python
# ✖ 错误：factory run 命中缓存时仍提交一个新 job 再标记为命中
job = runner.submit(...); return {"job": hit, "cache_hit": True}
# ✔ 正确：命中时不创建 job；未命中时 submit 后立即 bind
```

---

## 9. 风险与注意事项

- 本文件的验收对 M9 结束时的实现全部失败，失败原因正是审查列出的问题。M10-03 的缓存验收依赖 M10-01：Sink 不结算时第二次产线运行先因 `IDEMPOTENCY_CONFLICT` 失败。
- 设计方在一个原型上验证过：按本文件实现 5.1 与 5.3 后，M9 阶段门的全部 1448 个测试仍然通过。若某条既有验收与本文件冲突，按 `plan/README.md` 第 5 节写日志并阻塞，不要改测试。
- Docker 下核验在新容器里运行。发布目标若在执行容器自己的文件系统里，核验必然失败——这是正确行为，真实 Sink 发布到外部系统。
- Windows 上回执中的路径含反斜杠，`receipt.json` 必须按 JSON 正确转义；模板渲染的 `{{ params.dir }}/note.txt` 会得到混合分隔符，核验配方用 `pathlib` 处理即可。
- 核验计数 `n` 与 `jobs/<job>/verify-<n>` 目录对应；并发核验同一回执不是本里程碑的场景，不需要加锁。
- `factory run --cache` 与 `line run` 共用 `job_keys`；两者登记的键可以互相命中，这是期望的行为。
