# commands：补齐 M2–M4 的 `factory` 命令行（M5-02）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M5-02**。
> M2–M4 各组件规范的“接口”章都列了命令行，实现只做了 `backend migrate`（`plan/drift.md` D-19）。厂长、人和守护进程都靠这些命令操作工厂，所以必须补齐。
> 每条命令的参数与语义**以所在组件规范为准**。本文件只规定：命令清单、全局选项、快照的选择、JSON 输出的形状、退出码的映射和代码组织。

---

## 1. 目的与范围

**做什么（M5-02）**

- 实现第 3.2 节表中的全部子命令。
- 统一 `--json` 的输出、错误体与退出码（第 4、5 章）。
- 实现 `ForemanQuery.registry_diff`（`foreman.md` 3.2，D-21）。
- `factory line run` 在配置了治理时，用 `EpochRouter.choose` 选择 epoch（D-23 的命令行一侧）。

**不做什么**

| 不做 | 归属 |
|---|---|
| `factory mcp` | M5-05，`mcp.md`。本任务只要求 `factory mcp --help` 能解析 |
| `factory serve`、`factory foreman supervise` | M5-04、M5-06 |
| 改变 M1 命令（`check`、`bean import`、`bean lineage`、`job run`、`job show`、`run`）的行为 | 禁止。M1 验收锁定了它们 |
| 新的业务语义 | 禁止。命令只调用组件的公开接口 |

---

## 2. 依赖

所有组件的公开接口（README 第 4 节与各组件规范第 3 章）。命令行不得读写组件的表，只能调用接口。唯一的例外是第 5.4 节的只读列表查询，见那里的说明。

---

## 3. 接口

### 3.1 代码组织

```python
# mao.cli（入口不变：factory = "mao.cli:main"）
def main(argv: list[str] | None = None) -> int
def build_parser() -> argparse.ArgumentParser

# mao.commands（新包）
# 每组命令一个模块：mao/commands/<group>.py，组名见 3.2 表第一列
def register(sub: argparse._SubParsersAction) -> None      # 每个模块各一个，给 build_parser 调用

# mao.commands.common
def open_context(args: argparse.Namespace) -> Context
def open_snapshot(ctx: Context, args: argparse.Namespace) -> Snapshot
def to_jsonable(obj: Any) -> Any
def emit(args: argparse.Namespace, obj: Any, text: str | None = None) -> None
def exit_code(exc: FactoryError) -> int
```

`mao.cli` 只负责解析与分派。业务处理写在 `mao/commands/*.py`。`print` 只允许出现在 `cli.py` 与 `commands/` 包内（静态规则的 `PRINT_FILES`、`PRINT_PREFIXES`），其他模块是库代码，输出走返回值、事件或 logging。

### 3.2 命令清单

| 组 | 命令 | 规范 | 调用 |
|---|---|---|---|
| `line` | `line plan LINE@N` | `lines.md` 3.4 | `compile_run_plan(...).to_json()` |
| | `line run LINE@N --in PORT=BEAN ... [--budget USD] [--variant STEP=V ...] [--no-cache]` | 同上 | `LineRunner.start_run` + `run_to_completion(InlineExecutor)` |
| | `line ls [--state S]`、`line show RUN`、`line nodes RUN` | 同上 | `LineRunner.get` / `nodes`；`ls` 见 5.4 |
| | `line cancel RUN`、`line retry RUN NODE`、`line rebuild RUN` | 同上 | `cancel` / `retry_node` / `rebuild` |
| `bean` | `bean label BEAN KEY=VALUE` | `lines.md` 3.4 | `Warehouse` 的标签接口（`lines.md` 3.3） |
| `trigger` | `trigger process` | `lines.md` 3.4 | `TriggerEngine.process` |
| `cache` | `cache ls [--station S@N]`、`cache explain JOB`、`cache invalidate (--key K\|--job J\|--station S@N\|--bean B) --reason TEXT` | `cache.md` | `JobCache` |
| `approval` | `approval ls [--job J] [--status S]`、`approval show ID`、`approval decide ID approve\|deny --by NAME [--note TEXT]` | `approvals.md` | `Approvals` |
| `secret` | `secret ls` | `egress.md` 3.4 | `SecretStore`。只输出名字与分类 |
| `egress` | `egress check STATION@N` | `egress.md` 3.4 | `build_egress` |
| `contract` | `contract validator TYPE VALIDATOR --timeout S`、`contract golden G@N [--baseline B@N] --timeout S`、`contract equivalence S@N [--timeout S]`、`contract diff S@N [--timeout S]`、`contract sink-dry-run S@N --in PORT=BEAN ... [--param K=V ...] [--timeout S]`、`contract ls [--kind K]` | `contract-tests.md` | `ContractRunner` |
| `schedule` | `schedule tick`、`schedule dlq ls`、`schedule quarantine BEAN --reason TEXT`、`schedule release BEAN` | `scheduler.md` 3.2 | `Scheduler` |
| `epoch` | `epoch init COMMIT`、`epoch ls`、`epoch show [COMMIT]`、`epoch rollback [COMMIT]`、`epoch adopt COMMIT` | `governance.md` 3.4 | `EpochStore` |
| `changeset` | `changeset propose FILE --commit C --by ACTOR`、`changeset ls [--state S]`、`changeset show ID`、`changeset withdraw ID --by ACTOR`、`changeset advance ID` | 同上 | `ChangePipeline` |
| `governance` | `governance tick`、`governance safe-mode` | 同上 | `ChangePipeline.tick` / `safe_mode` |
| `audit` | `audit once`、`audit ls [--state S]` | `reconciler.md` 3.1 | `Reconciler` |
| `drift` | `drift ls` | 同上 | `Reconciler.findings(state=None)` 中 `invariant == "drift"` 的项 |
| `foreman` | `foreman next`、`foreman done WAKE`、`foreman get KEY`、`foreman list KIND`、`foreman diff BASE COMMIT`、`foreman bean get BEAN`、`foreman bean lineage BEAN up\|down`、`foreman job get JOB`、`foreman job transcript JOB`、`foreman dlq ls`、`foreman drift ls`、`foreman invariant ls`、`foreman metrics STATION METRIC`、`foreman approval ls`、`foreman changeset propose --doc FILE --commit SHA`、`foreman changeset status ID`、`foreman changeset withdraw ID`、`foreman review ID --runtime R --verdict approve\|deny --note TEXT`、`foreman conversion ls [--status S]` | `foreman.md` 3.6 | `WakeQueue`、`ForemanQuery`、`ForemanSubmit`、`ConversionStore` |
| `maintain` | `maintain bootstrap` | `foreman.md` 3.6 | `bootstrap_check` |
| `gateway` | `gateway manifest MACHINE@V` | `gateway.md` | 清单生成 |
| `backend` | `backend status`、`backend migrate ...`（已有）、`backend verify` | `backends.md` 3.1 | `open_context` 与迁移 |
| `mcp` | `mcp` | `mcp.md` | M5-05 实现；本任务只注册子命令，未实现时退出码 4 并说明 |

`foreman changeset propose` 与 `foreman review` 的 `by` 不能由命令行参数给出：命令行里的厂长身份是 `$FACTORY_ACTOR`（必须是 OperatorRuntime 引用，例如 `claude-code@4`）。没有该环境变量 → 退出码 2。`changeset propose`（人的命令）用 `--by`。

### 3.3 全局选项

`--root`、`--registry`、`--json`、`--provider`、`--local-commands`、`--epoch COMMIT` 可以写在子命令**之前或之后**，效果相同。M1 的写法（全部写在前面）继续有效。

`--epoch` 只对需要快照的命令有意义，见 5.2 节。

---

## 4. 数据格式

### 4.1 `--json` 输出

stdout 只有一个规范 JSON 值（`canonical_json`），后面跟一个换行。`to_jsonable` 的转换规则，按顺序取第一条适用的：

1. 有 `to_json()` 方法的对象：用它的返回值。
2. dataclass：`dataclasses.fields` 逐字段转换，键为字段名。
3. `StrEnum`：取 `.value`。
4. `tuple`、`list`：转成数组，保持顺序。`set`、`frozenset`：排序后转成数组。
5. `dict`：键必须是字符串，值递归转换。
6. `Path`：`as_posix()`。`datetime`：`utc_iso`。
7. `None`、`bool`、`int`、`float`、`str`：原样。
8. 其他：内部错误（退出码 5）。不要用 `str(obj)` 兜底。

接口返回 `None` 的命令（`cancel`、`quarantine`、`decide` 等），`--json` 时输出 `{"ok": true}`。接口返回对象的，输出该对象。列表命令输出数组，顺序与接口一致。

容易有歧义的几条，输出固定如下：

| 命令 | `--json` 输出 |
|---|---|
| `bean import` | `{"id": ...}`（M1，不变） |
| `line run`、`line show`、`line cancel`、`line rebuild` | `Run` |
| `line nodes` | `RunNode` 数组 |
| `line plan` | `RunPlan.to_json()` |
| `epoch show [COMMIT]` | 该 commit 的 `Epoch`（缺省 active），另加键 `pointers`：指向它的指针名数组 |
| `epoch ls` | `Epoch` 数组 |
| `changeset propose`、`show`、`advance`、`withdraw` | `ChangeSet.to_json()` |
| `governance tick` | 状态有变化的 `ChangeSet` 数组 |
| `foreman next` | `Wake`；没有票时为 `null` |
| `foreman get KEY` | `ForemanQuery.registry_get` 的返回值（文档） |
| `foreman list KIND` | Spec 键数组 |
| `foreman diff` | `{"files": [...], "stat": "..."}` |
| `secret ls` | `[{"name": ..., "class": ...}]`，按名字升序 |
| `contract *` | `ContractResult` |

### 4.2 错误体

`FactoryError` 且带 `--json`：stdout 输出 `{"error": exc.to_json()}`，退出码按 5.3 节。不带 `--json`：stderr 一行 `<code>: <message>`。两种情况都先经 `redact_obj` 打码（`egress.md`）。

用法错误：退出码 2。带 `--json` 时 stdout 输出 `{"error": {"code": "CLI_USAGE", "message": ..., "detail": {"argv": [...]}}}`（`cli.md` 第 6 章）。

### 4.3 非 `--json` 输出

每条命令给人看的一到几行，格式不锁定，验收只测 `--json`。不得打印秘密值。

---

## 5. 行为

### 5.1 打开 Context

`open_context(args)`：

1. 根目录取 `--root`、`$FACTORY_ROOT`、`./.factory`（README 3.1）。
2. provider：`--provider docker`（缺省）构造 `DockerProvider`，**构造时不得调用 docker**。只读命令因此在没有 Docker 的机器上也能运行。`local` 的 `--local-commands` 可以省略，省略时命令表为空 `{}`（与 M1 的实现一致）；只读命令和不执行工位的命令这样就够了。
3. `Context.open(root, provider=...)`。命令结束时 `close`。

### 5.2 选择快照

`open_snapshot(ctx, args)`：

1. 给了 `--epoch C`：要求 `factory.json` 有 `governance.registry`（否则 `REGISTRY_NOT_CONFIGURED`），返回 `EpochStore(ctx).materialize(C)`。
2. `factory.json` 有 `governance.registry`：返回 `EpochStore(ctx).materialize(EpochStore(ctx).current())`。
3. 否则：`Snapshot.load(--registry 或 $FACTORY_REGISTRY 或 ./registry)`。

**新建 run 的命令**（`line run`）在第 2 种情况下改用路由：`key = line + ":" + canonical_json(inputs)`，`commit = EpochRouter(ctx, epochs).choose(key)`，快照取 `materialize(commit)`（`governance.md` 5.9）。给了 `--epoch` 时不路由。

**操作已有对象的命令**（`line show`、`line retry`、`line rebuild` 等）用该对象记录的 epoch 物化快照，不用 active。`rebuild` 例外：它按 `lines.md` 5.5 在当前快照上新建 run，当前快照按本节第 1–3 条选。

### 5.3 退出码

| 情况 | 退出码 |
|---|---|
| 成功 | 0 |
| `line run` 的 run 终态为 `FAILED` 或 `CANCELLED` | 1 |
| `contract *` 返回 `verdict == "fail"` | 1 |
| `audit once` 后存在 `open` 或 `wake` 的发现 | 1（`reconciler.md` 3.1） |
| 业务 `FactoryError` | 1 |
| 用法错误 | 2 |
| `REGISTRY_INVALID` | 3 |
| 环境与设施：`DOCKER_UNAVAILABLE`、`DOCKER_FAILED`、`IMAGE_NOT_FOUND`、`GIT_FAILED`、`REGISTRY_NOT_CONFIGURED`、`BACKEND_UNAVAILABLE`、`MODEL_PROXY_BIND_FAILED`、`EGRESS_PROXY_UNAVAILABLE`、`RESTORE_FAILED` | 4 |
| 其他异常 | 5（stderr 只写类型名） |

`exit_code(exc)` 实现这张表。新的设施类码要加进这张表，不要在各命令里单独判断。

### 5.4 列表命令

`line ls`、`approval ls`、`changeset ls`、`audit ls`、`schedule dlq ls`、`contract ls` 调用组件已有的列表接口。组件没有列表接口的（`line ls`）：在 `mao.runs` 增加公开方法 `LineRunner.list(*, state: str | None = None) -> list[Run]`，按 `id` 升序。命令行本身不写 SQL。

### 5.5 `registry_diff`

`ForemanQuery.registry_diff(base, commit, *, timeout_s)`：

1. 两个参数都用 `git rev-parse --verify <x>^{commit}` 解析成 40 位，失败 → `COMMIT_NOT_FOUND`。
2. `files`：`git diff --name-only --no-renames -z <base> <commit>` 的结果，升序。
3. `stat`：`git diff --stat <base> <commit>` 的 stdout 原文。
4. git 调用经 `mao.governance` 的同一个 git 辅助函数，超时用 `min(timeout_s, governance.git_timeout_s)`。命令行的 `timeout_s` 取 `governance.git_timeout_s`。

### 5.6 身份

- `$FACTORY_ACTOR` 只被 `foreman changeset propose`、`foreman review`、`foreman done` 读取。
- `foreman review` 的 `--runtime` 必须等于 `$FACTORY_ACTOR`，否则退出码 2。命令行不允许替别的 runtime 投票，这与 MCP 的身份规则一致（`mcp.md` 5.3）。
- 人的命令（`epoch init|adopt|rollback`、`changeset propose`、`approval decide`）用 `--by` 或 `human:<登录名>`。不读 `$FACTORY_ACTOR`。

---

## 6. 错误

本文件不新增错误码。命令行把下层的 `FactoryError` 原样放进错误体。`CLI_USAGE` 已在 `cli.md` 定义。

---

## 7. 验收测试

夹具：
- `tests/fixtures/registry-m2`，`--provider local --local-commands` 指向测试写出的 `tests.fakes.fakelib.local_commands()`；
- 治理相关的测试用 `tests/acceptance/m3/gov_world.py` 建好的根目录。

测试在进程内调用 `mao.cli.main(argv)`，用 `capsys` 读输出。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m5/test_m5_02_commands.py` | `test_every_command_parses` | 3.2 表中每条命令加 `--help` 都退出 0（命令存在） |
| | `test_options_before_or_after` | `--json` 写在子命令前后，`line plan` 的输出逐字节相同 |
| | `test_line_run_show_nodes` | `bean import` 任务脚本后 `line run --json` 退出 0、`state == "SUCCEEDED"`；`line show`、`line nodes`、`line ls` 都能找到这个 run |
| | `test_error_body_and_exit_code` | 没有 `scheduler.json` 时 `schedule tick --json` 退出 1，stdout 是 `{"error": {"code": "SCHEDULER_CONFIG_MISSING", ...}}` |
| | `test_contract_validator_json` | `contract validator note.text@1 count-text --timeout 30 --json` 退出 0，`verdict == "pass"` |
| | `test_secret_ls_has_no_values` | `secret ls --json` 列出名字与分类，stdout 不含秘密值 |
| | `test_epoch_and_changeset` | 治理根目录下：`epoch show --json` 是 active；`changeset propose` 得到 `draft`；`changeset advance` 之后 `changeset show` 的状态是 `verified` 或 `rejected` |
| | `test_line_run_uses_active_epoch` | 治理根目录下 `line run --json` 的 `epoch` 等于 active commit（没有金丝雀时路由回 active） |
| | `test_foreman_readonly_queries` | `foreman next --json` 为 `null`；`foreman get`、`foreman list` 返回夹具文档；`foreman diff` 的 `files` 含改动的文件 |
| | `test_foreman_identity_from_env` | 没有 `$FACTORY_ACTOR` 时 `foreman changeset propose` 退出 2；`foreman review --runtime` 与 `$FACTORY_ACTOR` 不同时退出 2 |

```
uv run python tools/harness.py pytest tests/acceptance/m5/test_m5_02_commands.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 10`。

---

## 8. 错误示范与正确示范

```python
# ✖ 命令行自己查表
rows = ctx.db.query("SELECT * FROM runs")
# ✔ 只调用组件接口；组件缺列表接口就按 5.4 节加公开方法
runs = LineRunner(ctx, snapshot, runner).list(state=args.state)
```

```python
# ✖ 无法转换的对象用 str 兜底，输出看似成功
json.dumps(obj, default=str)
# ✔ to_jsonable 遇到未知类型就是内部错误
emit(args, to_jsonable(obj))
```

```python
# ✖ 厂长的身份来自命令行参数
pipeline.propose(doc, commit, by=args.by, timeout_s=t)
# ✔ 厂长命令的身份只来自环境（由值守进程设置）
pipeline.propose(doc, commit, by=os.environ["FACTORY_ACTOR"], timeout_s=t)
```

```python
# ✖ 有治理时 line run 仍然用 --registry 目录
snapshot = Snapshot.load(args.registry)
# ✔ 有治理时按路由选择 epoch
snapshot = epochs.materialize(EpochRouter(ctx, epochs).choose(key))
```

---

## 9. 风险与注意事项

- **命令很多**：先写 `to_jsonable`、`emit`、`exit_code`、`open_context`、`open_snapshot`，再逐组接线。每组一个模块，模块之间不互相导入。
- **长命令**：`line run` 同步执行，可能运行很久。它不是守护进程；长期运行用 `factory serve`（`daemon.md`）。
- **并发打开**：命令行与守护进程可以同时打开同一个 `factory.db`（WAL）。写操作都经组件接口的事务，命令行不持有长事务。
- **Windows**：stdout 用 UTF-8（`cli.md` 第 9 章已有约定）。
