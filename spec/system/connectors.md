# Source 与 Sink（connectors.md）

> 规范性文档，M2。模块：`mao.connectors`（幂等键、回执、核验、Source 的来源证明）。
> 依据：架构文档 5.4（Source 与 Sink 的连接机制）、6.8（Sink 幂等）、7.6（回执核验）、12.2（权限矩阵）、12.3（I3、I6、I7）。
> 跨组件约定见 `README.md`。

---

## 1. 目的与范围

**做什么（M2）**

- **Sink**：
  - 执行前按工位的 `idempotency_key` 模板算出幂等键，用回执记录去重：同一个键已经有核验通过的回执，就不再发布。
  - 执行后读取回执，用 `verify_receipt` 指定的**核验配方**在沙箱内核实结果确实存在。
  - 回执记录进入血缘的“向下”视图，但它不是 Bean，不能被下游引用。
- **Source**：
  - 没有输入的工位以 job 的形式运行，它从外部拉取物料。产物照常经过出厂检验，外部输入一律视为不可信。
  - 来源证明标记 `external: true`。
- **凭据边界**（I6 的运行时部分）：工位的网络与秘密只按它的策略和角色注入。发布类秘密只给 Sink，摄入类秘密只给 Source，在 `Runner` 中二次检查（纵深防御）。
- 为上述功能扩展 `factoryctl`（`receipt` 命令）、机器配方（`receipt` 模板）和 MRL profile（核验配方的静态检查）。

**不做什么**

| 不做 | 归属 |
|---|---|
| 推送式 Source（长期运行、接收 webhook 的服务型机器，`form: service`） | M4（`gateway.md`）。M2 运行 `form: service` 的工位时报 `FORM_UNSUPPORTED` |
| 两阶段 Sink 的 propose（dry-run）与审批 | `approvals.md`；本文件只规定 execute 阶段 |
| 出网代理、秘密的存储与注入机制 | `egress.md`；本文件只规定“给谁、不给谁” |
| V4 的 Sink dry-run 测试 | `contract-tests.md` |

---

## 2. 依赖

| 依赖 | 接口 |
|---|---|
| `Snapshot` | `key`、`spec`、`compiled`（`Station:K/idempotency_key` 模板、配方 `receipt` 模板） |
| `mao.values` | `inputs_value`、`params_value`、`recipe_env` |
| `Runner`（M1） | 按角色分派执行流程（第 3.3 节的增补） |
| `SandboxProvider` | 运行核验配方 |
| `mao.egress`（`egress.md`） | `SecretStore`、按策略构造出网配置 |
| `Warehouse` | `get`；Source 产物的封存走 M1 的 Gate 与 `seal_many` |
| `mao.db` | 迁移 `012_receipts.sql` |

---

## 3. 接口

### 3.1 `mao.connectors`

```python
class ReceiptStatus(StrEnum):
    CLAIMED = "claimed"          # 已占用幂等键，发布进行中
    PUBLISHED = "published"      # 操作工已结束并留下回执，等待核验
    VERIFIED = "verified"        # 核验通过
    FAILED = "failed"            # 核验不通过
    VOID = "void"                # 被人或厂长作废，允许以同一个键再次发布

@dataclass(frozen=True)
class Receipt:
    id: str                      # "rc-0001"
    station_name: str            # 幂等范围按工位名，不含版本（第 5.1 节）
    station: str                 # Spec 键
    idempotency_key: str
    job_id: str
    run_id: str | None
    inputs: dict[str, list[str]]
    status: ReceiptStatus
    target: str | None
    locator: dict[str, str]
    items: list[dict[str, str]]  # [{"bean", "locator", "digest"}]，可为空
    verify_method: str
    verify_detail: str | None
    verified_at: str | None
    created_at: str; updated_at: str

def idempotency_key(snapshot: Snapshot, station_key: str, inputs: Mapping[str, "Bean | Sequence[Bean]"],
                    params: Mapping[str, Any], run: Mapping[str, Any]) -> str
    # 渲染 snapshot.compiled(f"{station_key}/idempotency_key")；结果为空串 → FactoryError("IDEMPOTENCY_KEY_EMPTY")

class ReceiptStore:
    def __init__(self, ctx: Context) -> None
    def claim(self, *, station_key: str, key: str, job_id: str, run_id: str | None,
              inputs: Mapping[str, Sequence[str]], verify_method: str) -> "Claim"
        # 第 5.2 节；返回 Claim(kind="new"|"duplicate", receipt=...)，冲突时抛错
    def publish(self, receipt_id: str, payload: Mapping[str, Any]) -> Receipt   # CLAIMED → PUBLISHED
    def settle(self, receipt_id: str, *, ok: bool, detail: str) -> Receipt      # PUBLISHED → VERIFIED | FAILED
    def void(self, receipt_id: str, *, by: str, reason: str) -> Receipt         # 任意非 VOID → VOID
    def get(self, receipt_id: str) -> Receipt
    def list(self, *, station: str | None = None, status: str | None = None) -> list[Receipt]
    def for_bean(self, bean_id: str) -> list[Receipt]                           # inputs 中含该 Bean 的回执

def verify(ctx: Context, snapshot: Snapshot, receipt: Receipt) -> tuple[bool, str]
    # 在沙箱内运行核验配方（第 5.4 节）；返回 (是否通过, detail)；设施故障 → FactoryError("RECEIPT_VERIFY_ERROR")
```

### 3.2 `factoryctl receipt`（对 M1 `factoryctl` 的增补）

```
factoryctl receipt --target TARGET [--locator KEY=VALUE]... [--item BEAN_ID=LOCATOR[@DIGEST]]... [--note TEXT]
```

- 原子地写入 `/factory/ctl/receipt.json`：先写临时文件，再 rename。重复调用时，以最后一次为准。
- 缺少 `--target`，或参数格式不对，退出码 2；成功退出码 0。
- 只用标准库，兼容 Python 3.8（README D7）。

### 3.3 对 M1 接口的增补

| 组件 | 增补 |
|---|---|
| `Runner.submit` | 新增可选关键字参数 `run: Mapping[str, Any] \| None = None`（MRL 的 run 值；产线中由 `LineRunner` 传入）。Sink 的幂等键在提交时渲染并保存。不传时用合成值：`{"id": "", "epoch": snapshot.epoch, "line": "", "budget": {"cap_usd": B, "spent_usd": 0.0, "remaining_usd": B}}`，B 为工位预算，没有则为 0.0 |
| `Runner.run` | 按工位角色分派：`sink` 走第 5.3 节，`source` 走第 5.5 节，其他角色不变 |
| `JobState` 转换表 | 增加 `PENDING → SEALED`，只用于 Sink 的重复发布（第 5.2 节） |
| `jobs` 表 | 新列 `idempotency_key TEXT`、`run_value TEXT`（迁移 `012`） |
| Machine schema（配方） | 新字段 `receipt: {target: 模板, locator: {名字: 模板}}`（第 5.3 节第 5 步） |
| `factory` CLI | `factory receipt ls/show/verify/void`、`factory source run`；`factory bean lineage --down` 列出回执 |

---

## 4. 数据格式

### 4.1 迁移 `src/mao/migrations/012_receipts.sql`

```sql
CREATE TABLE receipts (
  id              TEXT PRIMARY KEY,
  station_name    TEXT NOT NULL,
  station         TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  job_id          TEXT NOT NULL,
  run_id          TEXT,
  inputs          TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('claimed','published','verified','failed','void')),
  target          TEXT,
  locator         TEXT NOT NULL DEFAULT '{}',
  items           TEXT NOT NULL DEFAULT '[]',
  note            TEXT,
  verify_method   TEXT NOT NULL,
  verify_detail   TEXT,
  verified_at     TEXT,
  voided_by       TEXT,
  void_reason     TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
-- 同一个工位名下，同一个幂等键最多只有一条非 void 的回执
CREATE UNIQUE INDEX receipts_idem ON receipts(station_name, idempotency_key) WHERE status != 'void';
CREATE INDEX receipts_job ON receipts(job_id);
ALTER TABLE jobs ADD COLUMN idempotency_key TEXT;
ALTER TABLE jobs ADD COLUMN run_value TEXT;
```

### 4.2 `/factory/ctl/receipt.json`

```json
{
  "target": "s3://media-archive/explainers/r-0042/",
  "locator": {"object_key": "explainers/r-0042/video.mp4", "etag": "9b2c…"},
  "items": [{"bean": "sha256:77e0…", "locator": "s3://media-archive/explainers/r-0042/video.mp4", "digest": "sha256:b3a9…"}],
  "note": ""
}
```

| 字段 | 类型 | 必需 | 说明 |
|---|---|---|---|
| `target` | string | ✔ | 发布目标（桶、仓库、频道） |
| `locator` | object（值为 string） | ✔，可为空对象 | 定位信息：URL、对象键、commit |
| `items` | array | ✘ | 每个输入 Bean 落到了哪里；`digest` 可省略 |
| `note` | string | ✘ | 备注 |

不符合上表时：回执记录写入 `verify_detail = "receipt.json 格式错误：…"`，job 进入 `DEAD`（`RECEIPT_INVALID`）。

### 4.3 核验配方的约定

- 核验配方是 Sink 所在机器中的一个普通配方，名字就是工位的 `verify_receipt`。它必须有 `run`，不能有 `outputs`；这两条由 MRL 静态检查保证，见第 5.7 节。
- 核验配方的沙箱与 Sink 的执行沙箱使用**同一个镜像、同一个策略**（出网与秘密），输入同样挂在 `/factory/in/<port>/`，`/factory/ctl/receipt.json` 只读提供。
- 退出码：
  - `0`：核验通过；
  - `1`：核验不通过，stdout 可以是 `{"detail": "…"}`；
  - 其他退出码或超时：设施故障。
- 超时：取工位 `limits.timeout` 与 300 秒中较小的一个。

### 4.4 事件

| kind | data |
|---|---|
| `sink.claimed` | `{receipt, station, key, job}` |
| `sink.duplicate` | `{receipt, station, key, job}` |
| `sink.published` | `{receipt, target, locator}` |
| `sink.verified` / `sink.verify_failed` | `{receipt, method, detail}` |
| `receipt.voided` | `{receipt, by, reason}` |
| `source.ingested` | `{job, station, outputs}` |

---

## 5. 行为

### 5.1 幂等键

- 渲染：`snapshot.compiled(f"{station_key}/idempotency_key")` 以 `{"inputs": inputs_value(...), "params": params_value(...), "run": run}` 渲染。类型环境见 factory-profile 4.2 的 `idempotency` 一行，编译期已经检查过。
- 渲染在 `Runner.submit` 时完成，结果存入 `jobs.idempotency_key`。之后的重试和续跑都用这个值，不重新渲染，因为 `run.budget.*` 这类字段会随时间变化。
- **范围**：唯一性按 `(工位名, 键)`，不含工位版本。`publish-s3@2` 升级为 `@3` 后，同样的键不会重复发布。确实需要重新发布时，先作废旧回执（`void`）。

### 5.2 占用 `claim`

在 `Runner.run` 处理 Sink job 的第一步执行，此时还没有 `PROVISIONING`。在一个事务里，按 `(station_name, key)` 查找非 void 的回执：

| 已有回执 | 结果 |
|---|---|
| 没有 | 插入一条 `claimed` 回执，`Claim(kind="new")`，写 `sink.claimed`，继续执行 |
| `verified` | `Claim(kind="duplicate")`：job 直接 `PENDING → SEALED`，`outputs = {}`，不创建沙箱；写 `sink.duplicate` |
| `claimed` 或 `published` | 抛出 `IDEMPOTENCY_CONFLICT`（另一个 job 正在发布，或者上次发布中断），job 进入 `DEAD`，`detail.receipt` 指向那条回执 |
| `failed` | 抛出 `RECEIPT_UNVERIFIED_EXISTS`，job 进入 `DEAD` |

部分唯一索引保证：并发的两个 job 只有一个能插入成功，另一个得到 `IDEMPOTENCY_CONFLICT`。

### 5.3 Sink 的执行流程（execute 阶段）

1. `claim`（第 5.2 节）。
2. `PROVISIONING`：物化输入，创建沙箱。
   - 网络与秘密按第 5.6 节构造。
   - 这一步失败按 `limits.retries.failed` 重试，因为此时还没有任何对外操作。
3. `RUNNING`：运行操作工。脚本运行配方的 `run`，agent 运行 Brief。Brief 中的输出契约部分改为“发布要求”：目标参数，以及用 `factoryctl receipt` 留下回执的说明。
4. 操作工退出码不是 0、超时，或者调用了 `factoryctl fail` 时：job 直接 `DEAD`，错误码 `SINK_OUTCOME_UNKNOWN`，原因放在 `detail.cause`。**不自动重试**，因为发布可能已经部分发生；由厂长核实后作废回执、再重试。
5. **取得回执**：
   - 有 `/factory/ctl/receipt.json` 时，按第 4.2 节校验。
   - 没有时，如果配方有 `receipt` 模板，以 `recipe_env` 渲染 `target` 与 `locator` 的每个值。
   - 两者都没有：`DEAD`，错误码 `RECEIPT_MISSING`。
   - 拿到回执后，`publish(...)`，回执变为 `published`，写 `sink.published`。
6. `GATING`：对 Sink 来说，这一步就是**回执核验**。`verify(...)`：
   - 通过 → `settle(ok=True)` → job `SEALED`，`outputs = {}`。
   - 不通过 → `settle(ok=False)` → job `DEAD`，错误码 `RECEIPT_UNVERIFIED`。
   - 设施故障 → 回执保持 `published`，job `DEAD`，错误码 `RECEIPT_VERIFY_ERROR`。可以稍后用 `factory receipt verify` 重新核验。
7. Sink 永远不会进入 `REJECTED`：它没有产物，也就没有出厂检验。

### 5.4 核验 `verify`

1. 机器为工位的 `machine`，配方为工位的 `verify_receipt`。
2. 在新的沙箱里运行，沙箱名为 `mao-<job>-verify-<n>`。
   - 挂载：输入物化到 `/factory/in`；回执写入 `/factory/ctl/receipt.json`，同时提供 `job.json`。
   - 网络与秘密与执行时相同。
3. 按第 4.3 节解释退出码。
4. `factory receipt verify RC_ID`（审计抽查，I7）走同一个函数：
   - 只能用于 `published`、`verified`、`failed` 状态的回执；
   - 结果写回 `verify_detail` 与状态。`verified` 也可能变成 `failed`，这时写 `sink.verify_failed`，并在 M3 唤醒厂长。

### 5.5 Source 的执行流程

1. Source 工位没有输入。`Runner.submit` 的 `inputs` 必须为空，否则 `INTAKE_REJECTED`。
2. 其余流程与 transform 相同：沙箱、操作工、收货、出厂检验、封存。网络与秘密按第 5.6 节构造（`ingress` 等级）。
3. 封存时，来源证明（job 记录与 `bean.sealed` 事件）带 `external: true`，以及 Source 工位与参数。
4. 写 `source.ingested` 事件。
5. 摄入的内容相同就得到相同的 Bean ID，内容寻址天然去重。`bean` 触发器按 `(产线, Bean)` 去重（`lines.md` 5.6），所以同一份物料不会重复触发产线。
6. 定时拉取：把 Source 作为产线的第一个步骤，产线配置 `trigger: [{on: cron, schedule: …}]`。cron 由 `scheduler.md` 处理。
7. 机器 `form: service` 的 Source：`FORM_UNSUPPORTED`（M4）。

### 5.6 凭据与网络的边界（纵深防御）

`Runner` 在 `PROVISIONING` 时，根据工位的角色与策略做如下检查。快照已经通过了宪法检查，这里**再检查一次**：

| 条件 | 结果 |
|---|---|
| 策略出网含 `publish`，但角色不是 `sink` | `POLICY_VIOLATION`，job `DEAD` |
| 策略出网含 `ingress`，但角色不是 `source` | `POLICY_VIOLATION` |
| 要注入的秘密被 `SecretStore` 标为 `publish` 类，但角色不是 `sink` | `POLICY_VIOLATION` |
| 要注入的秘密被标为 `ingress` 类，但角色不是 `source` | `POLICY_VIOLATION` |

秘密的分类与注入方式见 `egress.md`。要注入哪些秘密：
- 策略 `secrets` 是列表时，就是这个列表；
- 是 `from-machine` 时，取机器 `needs.secrets`。

### 5.7 MRL 与 schema 的改动（由 M2-09 实现）

| 改动 | 文件 | 内容 |
|---|---|---|
| Machine schema | `spec/schemas/machine.schema.json` | 配方新增可选字段 `receipt: {target: string, locator: {string: string}}`，`additionalProperties: false` |
| 抽取 | `src/mrl/factory/extract.py`、factory-profile 3.4 | `recipes.R.receipt.target`、`recipes.R.receipt.locator.*` 中含 `{{` 的字符串是 `recipe-template` 位置；新增事实 `recipe_run(M, R)`（配方有 `run`） |
| 内核 | `spec/mrl/kernel/stations.mrl` | 新诊断（码 `E-RECEIPT`）：(a) sink 的 `verify_receipt` 不是其机器的配方；(b) 该配方没有 `run`；(c) 该配方声明了 `outputs` |
| 码表 | `spec/mrl/kernel/codes.mrl`、factory-profile 7.3 | `code("E-RECEIPT", "error", "Sink 的回执核验配方不存在、没有 run，或声明了输出")` |
| 示例 Registry | `spec/examples/registry/machines/{rclone,git,yt-upload}/machine.yaml` | 补上核验配方 `s3-head-and-hash`、`git-commit-visible`、`url-reachable`，给 rclone 的 `copy` 配方加上 `receipt` 模板，使示例保持 clean |
| 一致性用例 | `spec/mrl/conformance/factory/stations.yaml`（追加） | `receipt-recipe-missing`、`receipt-recipe-no-run`、`receipt-recipe-has-outputs`、`receipt-template-typo`（E-EXPR） |

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `IDEMPOTENCY_KEY_EMPTY` | 幂等键渲染结果为空串 | `station` |
| `IDEMPOTENCY_CONFLICT` | 同键已有 `claimed`/`published` 回执 | `station, key, receipt` |
| `RECEIPT_UNVERIFIED_EXISTS` | 同键已有 `failed` 回执 | `station, key, receipt` |
| `SINK_OUTCOME_UNKNOWN` | Sink 的操作工启动后失败（退出码非 0、超时、fail） | `job, cause` |
| `RECEIPT_MISSING` | 既没有 receipt.json 也没有 `receipt` 模板 | `job` |
| `RECEIPT_INVALID` | receipt.json 不符合第 4.2 节 | `job, problems` |
| `RECEIPT_UNVERIFIED` | 核验配方退出码 1 | `receipt, detail` |
| `RECEIPT_VERIFY_ERROR` | 核验设施故障（退出码非 0/1、超时、镜像缺失） | `receipt, cause` |
| `RECEIPT_NOT_FOUND` | 回执 id 不存在 | `receipt` |
| `RECEIPT_STATE_INVALID` | 当前状态不允许该操作（例如核验 `claimed` 的回执） | `receipt, status, op` |
| `POLICY_VIOLATION` | 第 5.6 节的任一条件 | `station, role, reason` |
| `FORM_UNSUPPORTED` | M2 运行 `form: service` 的工位 | `station, machine, form` |

---

## 7. 验收测试

夹具：`tests/fixtures/registry-m2/`（受保护）在 M1 夹具的基础上增加：

- Sink `publish-local@1`：脚本操作工，把输入复制到参数 `dir` 指定的目录（测试给出 `tmp_path`），用 `factoryctl receipt` 留下回执；核验配方 `local-file-check` 检查文件存在、摘要一致。
- Sink `publish-flaky@1`：替身工具在复制后退出码 1，用来验证 `SINK_OUTCOME_UNKNOWN`。
- Sink `publish-templated@1`：不写 receipt.json，靠配方的 `receipt` 模板生成回执。
- Source `pull-notes@1`：从参数 `from_dir` 读取文件，产出 `note.text@1`。

全部用 `LocalProvider`。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m2/test_m2_09_profile_receipts.py` | `test_conformance_cases_pass` | 第 5.7 节新增的一致性用例全部通过（`mrl conformance` 的 JSON 输出中这些 id 为 pass） |
| | `test_example_registry_still_clean` | `mrl registry check spec/examples/registry --strict` 退出码 0 |
| | `test_coverage_complete` | `mrl coverage --json` 没有缺口（含 `E-RECEIPT`） |
| `tests/acceptance/m2/test_m2_10_sink.py` | `test_publish_and_verify` | 首次发布：回执 `verified`，job `SEALED`，`outputs == {}`，目标目录有文件 |
| | `test_duplicate_suppressed` | 同样的输入与参数再提交：job `PENDING → SEALED`，`sink.duplicate` 事件，不创建沙箱（目录内容与 mtime 不变） |
| | `test_key_scope_is_station_name` | 换成同名工位的新版本：仍判为重复 |
| | `test_void_allows_republish` | `void` 后再次发布成功，新回执 `verified` |
| | `test_outcome_unknown_no_retry` | `publish-flaky@1`：job 直接 `DEAD`（`SINK_OUTCOME_UNKNOWN`），即使 `retries.failed > 0` 也不重试；回执保持 `claimed` |
| | `test_conflict_on_claimed` | 已有 `claimed` 回执时提交 → `IDEMPOTENCY_CONFLICT` |
| | `test_receipt_template` | `publish-templated@1` 的回执来自模板渲染 |
| | `test_receipt_invalid` | 替身写出缺少 `target` 的 receipt.json → `RECEIPT_INVALID` |
| | `test_verify_failed` | 测试删除目标文件后 `factory receipt verify` → 状态 `failed`，事件 `sink.verify_failed` |
| | `test_lineage_down_shows_receipt` | `factory bean lineage --down` 的 JSON 输出中有该回执 |
| `tests/acceptance/m2/test_m2_11_source.py` | `test_source_ingests_and_seals` | `factory source run pull-notes@1` 产出并封存 Bean；`external: true`；`source.ingested` 事件 |
| | `test_source_rejects_inputs` | 给 Source 传输入 → `INTAKE_REJECTED` |
| | `test_untrusted_input_gated` | 外部文件不满足类型 validator → job 走 `REJECTED`/重试/`DEAD`，不封存 |
| | `test_same_content_same_bean` | 两次摄入同样的内容 → 同一个 Bean ID；`bean` 触发器只触发一次 |
| | `test_service_form_unsupported` | `form: service` 的 Source → `FORM_UNSUPPORTED` |
| | `test_policy_violation_defense` | 用补丁让快照里出现越权绑定（绕过宪法的夹具 Registry 使用空宪法 + 直接构造快照对象），`Runner` 仍然拒绝：`POLICY_VIOLATION` |

验收命令：`uv run python tools/harness.py pytest <文件>`，断言 `out.failed == 0 && out.errors == 0 && out.skipped == 0`，`passed` 不少于上表的行数。

---

## 8. 错误示范与正确示范

**8.1 幂等检查必须在任何对外操作之前，并且原子**

```python
# ✖ 错误：先发布、后查重；或者查重与插入分两个事务——两个并发 job 都会发布
run_operator(); if not exists(key): insert(key)
# ✔ 正确：占用在 PROVISIONING 之前，依靠部分唯一索引在同一个事务里完成“查 + 占”
with db.tx() as c:
    c.execute("INSERT INTO receipts (...) VALUES (...)")   # 冲突 → IDEMPOTENCY_CONFLICT
```

**8.2 发布中途失败不要自动重试**

```python
# ✖ 错误：Sink 的操作工退出码 1 → 按 retries.failed 重来一次 —— 可能已经上传了一半，重来就重复发布
# ✔ 正确：进入 RUNNING 之后的任何失败都直接 DEAD（SINK_OUTCOME_UNKNOWN），由厂长核实后 void 再重试
```

**8.3 回执核验要独立、确定**

```python
# ✖ 错误：相信操作工自己说“上传成功”就把回执记为 verified
# ✖ 错误：在控制面宿主上直接发 HTTP 请求核验（宿主没有、也不应该有发布凭据；还违反出网边界）
# ✔ 正确：在沙箱里运行机器的核验配方（同样的镜像与策略），按退出码判定
```

**8.4 幂等键的范围按工位名**

```python
# ✖ 错误：唯一约束是 (station_key, key) —— 工位从 @2 升到 @3，同一个视频会被再发布一次
# ✔ 正确：唯一约束是 (station_name, key)，并且只在 status != 'void' 的行上
```

**8.5 Source 的产物同样要过出厂检验**

```python
# ✖ 错误：Source 摄入的是“外部数据”，直接 seal 省事
# ✔ 正确：走与 transform 完全相同的 Gate.inspect；外部输入一律视为不可信
```

---

## 9. 风险与注意事项

- **“外部世界”无法回滚**：核验通过只说明“当时存在”。后来目标被删除，要靠定期抽查（`factory receipt verify`）发现。M3 的审计器会按周期抽查。
- **幂等键写得太宽或太窄**：
  - 太宽，比如只用 `params.bucket`，会把不同的内容误判为重复；
  - 太窄，比如包含 `run.id`，同一个内容每个 run 都会再发布一次。
  - 这是 Spec 作者的责任。示例写法见架构文档 13.1；V4 的 Sink dry-run 测试会暴露明显错误。
- **核验配方依赖出网**：核验与发布使用同样的出网策略。M2 出网代理就绪之前（`egress.md`），真实目标的 Sink 只能在开发开关下运行，验收测试只用本地 Sink。
- **`docker inspect` 能看到注入的秘密**：见 `egress.md` 第 9 节。
- **回执不是 Bean**：下游禁止引用回执。内核保证 Sink 没有输出端口，所以产线里也无法绑定回执。

---

## 10. 任务拆分

| ID | 标题 | 依赖 | 规范章节 | 允许修改的路径 | 验收 | 规模 |
|---|---|---|---|---|---|---|
| M2-09 | MRL profile：核验配方的静态检查、配方 `receipt` 模板；两阶段工位的静态检查（见 `approvals.md` 5.7） | M1 全部 | 5.7；`approvals.md` 5.7 | `spec/schemas/machine.schema.json`、`spec/schemas/station.schema.json`、`src/mrl/factory/extract.py`、`spec/mrl/kernel/stations.mrl`、`spec/mrl/kernel/codes.mrl`、`spec/mrl/factory-profile.md`、`spec/examples/registry/machines/**`、`spec/mrl/conformance/factory/*.yaml`（只追加） | `test_m2_09_profile_receipts.py`；`mrl conformance`、`mrl coverage` 全通过 | M |
| M2-10 | Sink：幂等键、占用、回执、核验、作废、CLI | M2-03、M2-09 | 3、4、5.1–5.4、5.6 | `src/mao/connectors.py`、`src/mao/runner.py`（只增 sink 分支）、`src/mao/jobs.py`（只增转换）、`src/mao/ctl/factoryctl.py`（只增 `receipt`）、`src/mao/migrations/012_receipts.sql`、`src/mao/cli.py`、`src/mao/errors.py`、`tests/system/test_connectors_*.py` | `test_m2_10_sink.py`（≥ 10 通过） | L |
| M2-11 | Source：执行流程、来源证明、`factory source run`、凭据边界二次检查 | M2-10 | 5.5、5.6 | `src/mao/connectors.py`、`src/mao/runner.py`（只增 source 分支与 5.6 检查）、`src/mao/cli.py`、`src/mao/errors.py`、`tests/system/test_source_*.py` | `test_m2_11_source.py`（≥ 6 通过） | M |
