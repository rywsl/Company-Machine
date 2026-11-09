# 审批路由（approvals.md）

> 规范性文档，M2。模块：`mao.approvals`。
> 依据：架构文档 6.3（审批人）、6.6（两阶段工位）；factory-profile 4.2 的 `approval` 类型环境。
> 跨组件约定见 `README.md`。M1 的 `JobState.AWAITING_APPROVAL` 与 `Runner.decide` 签名不改。本文件只在其上增加路由。
> 两阶段 Sink 的 execute、占用幂等键、回执核验仍归 `connectors.md`。本文件只规定 propose，以及何时才可以进入 execute。

---

## 1. 目的与范围

**做什么（M2）**

- 两阶段工位：`phases` 含 `propose` 时，先跑 propose，得到方案，按 `approval.propose` 路由；批准之后才进入 execute。
- 运行中审批：`factoryctl approval request` 按 `approval.in_run` 路由。
- 路由只有三条：`auto`、`foreman`、`review`。`auto_if` 为真走 `auto`，否则走 `else`。
- 超时：Spec 写了 `approval.timeout` 时，到期视为拒绝，job 进入 `FAILED`。
- 全部决定写入审批记录，并进入来源证明（事件，不是 Bean ID 的一部分）。

**不做什么**

| 不做 | 归属 |
|---|---|
| 在控制面里调用 LLM、启动复核用的 OperatorRuntime、实现厂长委托范围 | M3（`foreman.md`）。本文件只收票、计票 |
| 改 `Runner.decide` 的签名，或新增 Job 状态 | M1 已有 `AWAITING_APPROVAL`；阶段记在 job 的 `phase` 列，不进状态枚举 |
| Sink 的 `claim`、回执、核验 | `connectors.md` 的 execute。propose 期间禁止调用 |
| 出网、秘密注入 | `egress.md` |
| 为没写的超时或法定人数发明一个默认数字 | 见第 5.3、5.5 节：缺省表示该控制未武装，或无法结案 |

控制面路由只做三件事：读快照、用已编译的 MRL 表达式求值、写数据库。**禁止**在 `mao.approvals` 里发 HTTP、import 模型 SDK、或根据方案文本“自行判断”。

---

## 2. 依赖

| 依赖 | 用到的接口 |
|---|---|
| `Snapshot` | `key`、`spec`、`compiled`（`Station:K/approval.propose` 与 `/approval.in_run`） |
| `mao.values.approval_env` | 构造求值环境的**唯一**入口。禁止手写 `proposal` / `request` 字典去求值 |
| `Runner.decide` | 签名不变：`decide(approval_id, decision, *, by, note="")`。M2 的实现转到本模块的 `apply_decision` |
| `CtlHandler.handle` | 返回 `None` 表示暂不答复（README 4.6） |
| `SecretStore.redact`（`egress.md`） | 写入事件、note、异常消息之前脱敏 |
| `mao.base` | `Clock`、`IdSource`、`parse_duration`、`utc_iso`、`canonical_json` |
| `mao.db` | 迁移 `013_approvals.sql`；不改 `002` |

`InlineExecutor` 在审批等待期间不返回（`lines.md` 第 9 章）。测试要走 `auto_if`、预先写入的决定，或直接调用本模块，不要在 `runner.run` 阻塞时另起线程去 `decide`。

---

## 3. 接口

### 3.1 `mao.approvals`

```python
class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"          # 含超时：decision 为 deny，decided_by 为 "timeout"

class ApprovalPhase(StrEnum):
    PROPOSE = "propose"
    IN_RUN = "in_run"

@dataclass(frozen=True)
class Approval:
    id: str                                # "a-0001"，前缀 a（README 3.2）
    job_id: str
    attempt: int
    phase: ApprovalPhase
    status: ApprovalStatus
    route: str                             # auto | foreman | review；条件规则解析后的最终路由
    payload: dict[str, Any]                # 已脱敏。propose / in_run 的字段见第 4.2 节
    deadline: str | None                   # 没有 timeout 时为 None
    decision: str | None                   # approve | deny | None
    decided_by: str | None                 # auto | timeout | 调用方传入的 by
    note: str
    created_at: str
    decided_at: str | None

@dataclass(frozen=True)
class Vote:
    approval_id: str
    runtime: str                           # OperatorRuntime 的名字，不含版本
    by: str
    decision: str                          # approve | deny
    note: str
    at: str

class Approvals:
    def __init__(self, ctx: Context, snapshot: Snapshot) -> None
    def rule(self, station_key: str, phase: ApprovalPhase) -> dict[str, Any]
        # 第 5.1 节。没有规则时不抛错，返回 {"mode": "missing"}
    def open_proposal(self, *, job_id: str, attempt: int, station_key: str,
                      payload: Mapping[str, Any],
                      inputs: Mapping[str, Any], params: Mapping[str, Any]) -> Approval
    def open_request(self, *, job_id: str, attempt: int, station_key: str,
                     payload: Mapping[str, Any],
                     inputs: Mapping[str, Any], params: Mapping[str, Any]) -> Approval
    def apply_decision(self, approval_id: str, decision: Literal["approve", "deny"], *,
                       by: str, note: str = "") -> Approval
        # Runner.decide 的唯一实现。签名与 README 4.9 一致
    def expire_due(self) -> list[str]      # 返回本次超时的 approval id（升序）；幂等
    def preset(self, *, station_key: str, phase: ApprovalPhase,
               decision: Literal["approve", "deny"], by: str, note: str = "") -> None
        # 测试用：下一次该工位该阶段的 open_* 在同一线程里立刻消费，不停车
    def get(self, approval_id: str) -> Approval
    def list(self, *, job_id: str | None = None, status: str | None = None) -> list[Approval]
    def votes(self, approval_id: str) -> list[Vote]     # 按 runtime 名升序
    def for_provenance(self, job_id: str) -> list[dict[str, Any]]
        # 封存时挂到来源证明的列表，字段见第 4.4 节。不含 payload 原文以外的秘密
```

`open_proposal` / `open_request` 在同一个事务里完成：插入行、求值、能结案就结案、写事件。`auto` 不把 job 留在 `AWAITING_APPROVAL`。

### 3.2 对 M1 的增补（只增加，不改变签名）

| 组件 | 增补 |
|---|---|
| `Runner.decide` | 转到 `Approvals.apply_decision`。`review` 路由下一次 `decide` 是一张票，不一定结案 |
| `Runner.run` | 两阶段工位先走第 5.4 节的 propose，批准后才走原来的 execute（Sink 则才进入 `connectors.md` 5.3） |
| `jobs` | 新列 `phase`（`propose` 或 `execute`，默认 `execute`） |
| `factoryctl` | `proposal` 与 `approval request`（第 3.3 节） |
| `CtlHandler` | `op == "approval"` 时调用 `open_request`；未结案返回 `None` |
| `factory` CLI | `factory approval ls/show/decide` |

### 3.3 CLI 与 factoryctl

```
factory approval ls [--job J] [--status pending|approved|denied] [--json]
factory approval show APPROVAL_ID [--json]
factory approval decide APPROVAL_ID approve|deny --by NAME [--note TEXT]
```

`decide` 只调用 `Runner.decide`。退出码：成功 0；审批不存在或状态不对为 1；缺少 `--by` 为 2。

沙箱内（只用标准库，兼容 Python ≥ 3.8，禁止 import `mao`）：

```
factoryctl proposal --summary TEXT --cost-usd N --time-s N [--plan PATH]
factoryctl approval request --kind KIND --summary TEXT [--cost-usd N]
```

- `proposal`：原子写入 `/factory/ctl/proposal.json`（临时文件后 rename）。缺任何一个必填参数，退出码 2，不写文件。成功退出码 0。重复调用以最后一次为准。
- `approval request`：写 ctl 请求并阻塞到有响应或宿主关闭了响应文件。`--cost-usd` 省略时请求里的 `cost_usd` 为 `0.0`（可选参数的数据默认，不是预算门槛）。退出码 0，stdout 是第 4.3 节的响应 JSON。用法错误退出码 2。

---

## 4. 数据格式

### 4.1 迁移 `src/mao/migrations/013_approvals.sql`

迁移 `002`（`runner.md`）拥有 `approvals` 基础表。本文件**不**重建它，也**不**改 `002`。013 只追加列。002 必须已有这些列，缺列时停下来对齐，不要在 013 里 `CREATE TABLE approvals`：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PRIMARY KEY | `a-…` |
| `job_id` | TEXT NOT NULL | |
| `status` | TEXT NOT NULL | `pending` / `approved` / `denied` |
| `decision` | TEXT | `approve` / `deny` |
| `decided_by` | TEXT | |
| `note` | TEXT | |
| `created_at` | TEXT NOT NULL | |
| `decided_at` | TEXT | |

```sql
ALTER TABLE approvals ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1;
ALTER TABLE approvals ADD COLUMN phase TEXT NOT NULL DEFAULT 'in_run';
ALTER TABLE approvals ADD COLUMN route TEXT NOT NULL DEFAULT 'foreman';
ALTER TABLE approvals ADD COLUMN payload TEXT NOT NULL DEFAULT '{}';
ALTER TABLE approvals ADD COLUMN deadline TEXT;
ALTER TABLE jobs ADD COLUMN phase TEXT NOT NULL DEFAULT 'execute';
CREATE TABLE approval_votes (
  approval_id TEXT NOT NULL REFERENCES approvals(id),
  runtime    TEXT NOT NULL,
  by         TEXT NOT NULL,
  decision   TEXT NOT NULL CHECK (decision IN ('approve', 'deny')),
  note       TEXT NOT NULL DEFAULT '',
  at         TEXT NOT NULL,
  PRIMARY KEY (approval_id, runtime)
);
CREATE TABLE approval_presets (             -- 只给测试；生产调用方不要写
  station_key TEXT NOT NULL,
  phase       TEXT NOT NULL,
  decision    TEXT NOT NULL,
  by          TEXT NOT NULL,
  note        TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (station_key, phase)
);
```

SQLite 的 `ADD COLUMN` 不能带非常量默认值。`attempt` / `phase` / `route` 的默认值只为迁旧行；M2 插入时必须显式写出每一列。

### 4.2 载荷

propose 写入 `proposal.json` 的对象：

| 字段 | 类型 | 必需 | 说明 |
|---|---|---|---|
| `summary` | string | ✔ | 方案摘要 |
| `cost_estimate_usd` | number | ✔ | 交给 `approval_env` 的数 |
| `time_estimate_s` | number | ✔ | 秒 |
| `plan` | object | ✘ | 只存档。Sink 的 dry-run 把将要发布的目标写在这里。**不**进入 MRL 环境 |

交给 `approval_env(phase, payload, inputs, params)` 的 `payload` 只有三个必需字段。`plan` 留在数据库的 `payload` 列里，求值前剥掉。多出来的字段同样剥掉，避免封闭记录报错。

in_run 请求：

| 字段 | 类型 | 必需 |
|---|---|---|
| `kind` | string | ✔ |
| `summary` | string | ✔ |
| `cost_usd` | number | ✔（省略旗标时为 0.0） |

类型环境就是 factory-profile 4.2 的两行。不要增加字段去“让表达式更好写”。

### 4.3 ctl 响应

```json
{"decision": "approve", "note": "", "code": ""}
```

| decision | 何时 | `code` |
|---|---|---|
| `approve` | 结案为批准 | `""` |
| `deny` | 人、厂长、复核或 `auto_if` 的 else 拒绝 | `APPROVAL_DENIED`，或复核分裂时的 `APPROVAL_REVIEW_SPLIT` |
| `timeout` | 到期 | `APPROVAL_TIMEOUT` |

响应里的 `note` 必须先经过 `SecretStore.redact`。

### 4.4 事件与来源证明

| kind | data |
|---|---|
| `approval.requested` | `{id, job, phase, route, deadline}` |
| `approval.decided` | `{id, decision, by, route}` |
| `approval.expired` | `{id, job, phase}` |
| `approval.vote` | `{id, runtime, decision}` |

`for_provenance` 的每一项：`{id, phase, decision, decided_by, route}`，再加上 propose 的 `cost_estimate_usd` 或 in_run 的 `cost_usd`（数，没有则为 `null`）。不放 `summary`、`plan`、`note`：它们是操作工写的自由文本，可能复述秘密。封存时由 `Runner` 把这个列表写进 `job.approvals` 事件。它不参与 Bean ID。

---

## 5. 行为

### 5.1 解析规则

`spec.approval.<phase>` 缺省时，`rule()` 返回 `{"mode": "missing"}`。

| Spec 写法 | 解析结果 |
|---|---|
| `auto` / `foreman` / `review` | `{"mode": <该值>}` |
| `{auto_if, else}` | `{"mode": "conditional", "else": <else>}`。`auto_if` 不在这里求值 |

`else` 只能是 `auto`、`foreman`、`review`（schema 已限制）。不要把 `conditional` 当成一种审批人。

### 5.2 求值与路由

`open_proposal` 与 `open_request` 共用下面的算法。`phase` 不同，编译主体不同。

1. 用第 4.2 节校验载荷。缺字段、类型不对：抛 `APPROVAL_PAYLOAD_INVALID`，不插入行。
2. `payload` 的每个字符串值先 `redact`。
3. 读规则。
   - `missing` 且 phase 是 `propose`：抛 `APPROVAL_RULE_MISSING`（快照零 error 时，第 5.7 节已经挡住；这里是纵深防御）。
   - `missing` 且 phase 是 `in_run`：不插入 pending 行。ctl 响应 `decision=deny`、`code=APPROVAL_RULE_MISSING`。job **保持** `RUNNING`。这不是预算默认，而是“没委托就不准”。
4. 插入 `pending` 行。`deadline`：工位写了 `approval.timeout` 时，`created_at + parse_duration(timeout)`，格式 `utc_iso`。没写则为 `None`（**不**填 2 小时或任何其他数）。
5. 若有匹配的 `preset`，在同一事务里消费并删除 preset，然后走第 5.3 节的结案，`by` 用 preset 的 `by`。preset 优先于表达式，只为测试。
6. 否则解析最终路由：
   - `conditional`：`expr = snapshot.compiled(f"{station_key}/approval.{phase}")`，`env = approval_env(phase, 剥离后的 payload, inputs, params)`，`ok = expr.evaluate(env)`。
     - 求值抛错，或结果不是 `True`：`ok` 视为假。**禁止**把异常当成通过。异常时路由仍取 `else`，并在 note 里记下 `APPROVAL_EVAL_ERROR`（已脱敏的异常类型名，不放表达式环境）。
     - `ok is True`：最终路由 `auto`。
     - 否则：最终路由 = `else`。
   - 非条件规则：最终路由 = `mode`。
7. 写 `approval.requested`（`route` 是最终路由）。
8. 按最终路由：
   - `auto`：同一事务结案为 `approved`，`decided_by="auto"`。job 不进入 `AWAITING_APPROVAL`。
   - `foreman`：保持 `pending`。job 进入 `AWAITING_APPROVAL`。ctl 返回 `None`。
   - `review`：先读法定人数（第 5.5 节）。读不到合格的数：不停车。propose 时 job `FAILED`，错误码 `REVIEW_QUORUM_UNSET`；in_run 时响应 `deny` + 该错误码，job 保持 `RUNNING`。读到了就保持 `pending`，job 进入 `AWAITING_APPROVAL`。

`auto` 的 `else` 也走上面的 `auto` 分支。无条件 `auto` 不求值。

### 5.3 结案 `apply_decision` 与超时

`by` 必须是非空字符串，且不是 `auto` 或 `timeout`（这两个只由本模块写入）。否则 `APPROVAL_BY_INVALID`。

| 当前路由 | `decide` 的效果 |
|---|---|
| 已不是 `pending` | `APPROVAL_STATE_INVALID` |
| `foreman` | 一次调用即结案。`approve` → `approved`；`deny` → `denied` |
| `review` | 记一票，不立刻把 foreman 式的单票当成全票。见第 5.5 节 |
| `auto`（已经结案） | `APPROVAL_STATE_INVALID` |

propose 结案为 `denied`：job `FAILED`，`error.code = APPROVAL_DENIED`，**不**进入 execute。in_run 结案为 `denied`：写 ctl 响应，job 回到 `RUNNING`，由操作工决定是否 `factoryctl fail`。propose 结案为 `approved`：`jobs.phase = execute`，job 回到 `RUNNING`，继续第 5.4 节。

超时只由 `expire_due` 处理，不经过 `decide`：

1. `status = pending` 且 `deadline` 不是 `None` 且 `clock.now()` 已到 `deadline`。
2. 结案为 `denied`，`decided_by = "timeout"`，写 `approval.expired`。
3. 写 ctl 响应 `decision=timeout`，然后 job `FAILED`，`error.code = APPROVAL_TIMEOUT`。先写响应，再改 job，避免沙箱里的 `factoryctl` 永远堵在没有响应的请求上。
4. `deadline` 为 `None` 的行永远不会被这条扫到。

等待循环用注入的 `clock`：剩余时间 `<= 0` 就调用 `expire_due`；否则 `clock.sleep(剩余秒数)` 一次。不要用 `time.sleep`，也不要轮询一个自定的小间隔。`FakeClock` 下一次 `sleep` 就推进到期限。

`FAILED` 之后是否重试，沿用 M1 的 `limits.retries.failed`。本文件不把审批失败排除在重试之外。Sink 一旦进入 execute 并 `claim`，重试规则改由 `connectors.md` 决定。

### 5.4 两阶段执行顺序

`jobs.phase` 默认 `execute`。只有快照里该工位的 `phases` 含 `propose` 时，`Runner.run` 才把本 attempt 的阶段先设为 `propose`。

1. **propose**
   - 创建沙箱（网络与秘密按 `egress.md`，与 execute 相同的策略）。
   - **不**调用 `ReceiptStore.claim`。两阶段 Sink 的 `claim` 推迟到 execute，也就是 `connectors.md` 5.3 的第 1 步。单阶段 Sink 仍在 execute 一开始就 `claim`，本文件不介入。
   - 脚本操作工：只运行配方的 `propose` argv（第 5.7 节的可选字段）。没有这段 argv：job `FAILED`，`PROPOSE_UNSUPPORTED`。**禁止**把配方的 `run` 当作 dry-run 来跑。
   - agent 操作工：运行 Brief。`render_brief` 的签名不改。Runner 在它返回的 `markdown` 末尾拼接下面这段固定说明（普通字符串连接，不是 Spec 模板，不用 `str.format` 渲染 Spec）：

     ```
     # 方案阶段
     现在是 propose，不是 execute。不要发布、不要上传、不要调用有副作用的配方。
     写完方案后执行 factoryctl proposal --summary "…" --cost-usd N --time-s N。
     批准之前不要向 /factory/out 提交最终产物。
     ```
   - 操作工退出码不是 0，或超时：按 M1 的失败处理，不读方案，不进入 execute。
   - 读 `proposal.json`。没有文件：`FAILED`，`PROPOSAL_MISSING`。文件不是第 4.2 节的对象：`FAILED`，`PROPOSAL_INVALID`。
   - `open_proposal`。未批准则停。批准则进入下一步，**同一个**沙箱、**同一个** attempt。
2. **execute**
   - `jobs.phase = execute`。
   - 角色是 `sink`：从此才进入 `connectors.md` 5.3（先 `claim`，再跑配方的 `run`）。
   - 其他角色：走 M1 原来的执行路径。
   - 本 attempt 已经有 `approved` 的 propose 行时，重入 `Runner.run`（例如进程重启）不要再跑 propose。

in_run 请求可以发生在 execute 的 `RUNNING` 期间，也可以发生在 propose 的 `RUNNING` 期间。阶段列不因 in_run 而改变。

### 5.5 复核计票

法定人数来自宪法关系 `limit`，名字 `approval_review_quorum`。读取：

```text
rows = snapshot.result.model.rows("limit")    # (name, value)
```

合格值：有限、`int(value) == value`、且 `int(value) >= 1`。没有这行、`model` 为空、值不合格、或同名多行：视为**未设置**。本文件不把未设置换成 2 或任何其他数。

`review` 的 `by` 必须匹配 `^runtime:[a-z][a-z0-9-]*@[0-9]+$`。runtime 名字是 `runtime:` 与 `@` 之间的部分。同名不同版本算**同一个** runtime。格式不对：`APPROVAL_BY_INVALID`，不记票。

计票：

1. `(approval_id, runtime)` 已有票：`APPROVAL_STATE_INVALID`。不覆盖第一票。
2. 插入 `approval_votes`，写 `approval.vote`。
3. 已有任一 `deny`：立刻结案 `denied`。propose 的 job `FAILED`，`error.code = APPROVAL_REVIEW_SPLIT`（若唯一的票就是 deny，同样用这个码，表示复核没有形成一致通过）。
4. 不同 runtime 的 `approve` 张数 `>=` 法定人数，且没有 `deny`：结案 `approved`，`decided_by` 为这些 `by` 按 runtime 名升序用逗号连接。
5. 还不够：保持 `pending`，ctl 继续返回 `None`。

M2 不启动第二个沙箱去要票。票只来自对 `Runner.decide` 的外部调用。谁来调用是 M3 的事。

### 5.6 来源证明

job 进入 `SEALED` 之前，`Runner` 写一条 `job.approvals` 事件，data 是 `for_provenance(job_id)`。没有审批时写空数组。不要把审批行改写进 Bean 的 `meta`（那会改变 Bean ID）。

### 5.7 静态检查（由 M2-09 实现，本文件不另开任务）

`connectors.md` 第 10 章的 M2-09 已把本节列入该任务。只追加，不改已有一致性用例。

示例 Registry 里的 `explainer@3` 与 `publish-shorts@1` 没有 `approval.timeout`，机器配方也还没有 `propose` argv。因此内核**不**把“缺超时”或“缺 propose argv”做成 error 或 warning：`--strict` 会因 warning 失败，示例必须保持干净。缺超时的运行期含义见第 5.2 节（未武装）。缺 argv 的运行期含义见第 5.4 节（`PROPOSE_UNSUPPORTED`）。

| 改动 | 文件 | 内容 |
|---|---|---|
| 诊断码 | `spec/mrl/kernel/codes.mrl` | `code("E-APPROVAL", "error", "两阶段工位的 phases 与 approval.propose 不一致")` |
| 内核 | `spec/mrl/kernel/stations.mrl` | 下面两条规则 |
| 码表说明 | `spec/mrl/factory-profile.md` 7.3 | 增加 `E-APPROVAL` 行，说明里带反引号码 |
| 一致性用例 | `spec/mrl/conformance/factory/stations.yaml`（只追加） | `phases-propose-without-rule`、`approval-propose-without-phases` |

规则（`station_phases` 与 `approval` 已由抽取产出）：

```text
diag("E-APPROVAL", S + "/approval.propose", "phases 含 propose，但没有 approval.propose") :-
    station_phases(S, P), "propose" in P, !approval(S, "propose", _).
diag("E-APPROVAL", S + "/phases", "写了 approval.propose，但 phases 不含 propose") :-
    approval(S, "propose", _), station_phases(S, P), !("propose" in P).
```

配方可选字段 `propose`（字符串数组，与 `run` 相同）由 M2-09 加进 `machine.schema.json`。抽取不需要新事实：运行期直接读配方文档。没有这个字段时，第 5.4 节拒绝脚本两阶段，而不是在检查期把示例判脏。

用例期望：

| id | 补丁 | 期望 |
|---|---|---|
| `phases-propose-without-rule` | 去掉 `stations/explainer.yaml` 的 `spec.approval` | `E-APPROVAL`，主体 `Station:explainer@3/approval.propose` |
| `approval-propose-without-phases` | 去掉该文件的 `spec.phases`，保留 `approval.propose` | `E-APPROVAL`，主体 `Station:explainer@3/phases` |

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `APPROVAL_NOT_FOUND` | id 不存在 | `approval` |
| `APPROVAL_STATE_INVALID` | 当前状态或路由不允许这次 `decide`；复核重复投票 | `approval, status, op` |
| `APPROVAL_RULE_MISSING` | propose 没有规则（抛出）；in_run 没有规则（写入响应的 `code`，不抛） | `station, phase` |
| `APPROVAL_EVAL_ERROR` | `auto_if` 求值失败（记入 note 与事件，路由取 `else`，一般不单独抛） | `station, phase` |
| `APPROVAL_PAYLOAD_INVALID` | 载荷缺字段或类型不对 | `phase, problems` |
| `APPROVAL_BY_INVALID` | `by` 为空、为保留字，或复核格式不对 | `by` |
| `APPROVAL_DENIED` | propose 被拒绝（写入 `job.error`） | `approval, by` |
| `APPROVAL_TIMEOUT` | 到期（写入 `job.error`） | `approval, deadline` |
| `APPROVAL_REVIEW_SPLIT` | 复核出现 deny，或未能一致通过（写入 `job.error` 或响应 `code`） | `approval, votes` |
| `REVIEW_QUORUM_UNSET` | 路由到 `review` 但宪法没有合格的 `approval_review_quorum` | `station` |
| `PROPOSAL_MISSING` | propose 结束时没有 `proposal.json`（写入 `job.error`） | `job` |
| `PROPOSAL_INVALID` | `proposal.json` 不符合第 4.2 节（写入 `job.error`） | `job, problems` |
| `PROPOSE_UNSUPPORTED` | 脚本两阶段工位的配方没有 `propose` argv（写入 `job.error`） | `station, recipe` |
| `PHASE_INVALID` | `jobs.phase` 不是 `propose` 或 `execute` | `job, phase` |

抛出的错误用 `FactoryError`。写入 `job.error` 的码同样要登记，消息里不得含秘密值。

---

## 7. 验收测试

夹具：`tests/fixtures/registry-m2/`（受保护）在 M1 夹具上增加：

- 脚本两阶段工位 `quote@1`：配方有 `propose` argv，把固定方案写入 `proposal.json`（`cost_estimate_usd` 由参数决定）；`approval.propose` 为 `{auto_if: "proposal.cost_estimate_usd <= 1.0", else: foreman}`；`approval.timeout` 为 `1s`；`approval.in_run` 为 `{auto_if: "request.cost_usd <= 0.5", else: foreman}`。
- 同一工位的变体（补丁，不改受保护文件的原件）：`else: review`、无 `in_run`、无 `propose` argv、无 `timeout`。
- 两阶段本地 Sink：`propose` argv 只写方案，不复制文件；`run` 才复制。用来证明 propose 不产生回执。
- 宪法补丁把 `limit("approval_review_quorum", 2.0)` 写进夹具的副本。规范本身不规定这个 2。

全部 `LocalProvider`、`FakeClock`、`CounterIds`。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m2/test_m2_06_approvals.py` | `test_conformance_approval_cases` | 第 5.7 节两个用例 id 在 `mrl conformance` 的 JSON 里为 pass |
| | `test_auto_if_approves_without_parking` | 估价不超过表达式阈值时，同一事务 `approved`、`decided_by=auto`，job 不停留在 `AWAITING_APPROVAL`，随后 execute 跑完 |
| | `test_auto_if_false_parks_for_foreman` | 估价高于表达式阈值时 `route=foreman`、`pending`，job 为 `AWAITING_APPROVAL`；`preset` 批准后进入 execute |
| | `test_deny_propose_does_not_execute` | propose 被 `deny` 后 job `FAILED`（`APPROVAL_DENIED`），配方的 `run` 没有执行（输出目录为空） |
| | `test_in_run_deny_returns_to_running` | in_run 被拒绝后 ctl 响应 `deny`，job 回到 `RUNNING` 而不是 `FAILED` |
| | `test_in_run_missing_rule_rejects` | 没有 `in_run` 规则时响应 `APPROVAL_RULE_MISSING`，不插入 `pending` 行 |
| | `test_timeout_uses_spec_not_default` | 写了 `1s` 时，`FakeClock.sleep` 推进后 `expire_due` 使 job `FAILED`（`APPROVAL_TIMEOUT`）；删掉 `timeout` 的变体在任意推进后仍是 `pending` |
| | `test_review_quorum_unset` | 宪法没有该 `limit` 时，`review` 路由得到 `REVIEW_QUORUM_UNSET`，不停车 |
| | `test_review_counts_distinct_runtimes` | 夹具把法定人数设为 2 时：同一 runtime 的第二票 `APPROVAL_STATE_INVALID`；两个不同 runtime 都 `approve` 才结案；中途一张 `deny` 即 `APPROVAL_REVIEW_SPLIT` |
| | `test_eval_error_is_not_true` | `auto_if` 求值失败时走 `else`，不自动批准 |
| | `test_sink_propose_does_not_claim` | 两阶段 Sink 在批准前 `receipts` 无行；批准后才出现 `claimed` |
| | `test_proposal_missing` | 不写 `proposal.json` → `PROPOSAL_MISSING`，不 execute |
| | `test_secret_not_in_approval_records` | 方案摘要里含有 `SecretStore` 的测试值时，事件、`note`、`str(FactoryError)`、来源证明列表都没有该值 |

验收命令：

```
uv run python tools/harness.py pytest tests/acceptance/m2/test_m2_06_approvals.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 13`。

---

## 8. 错误示范与正确示范

**8.1 控制面不调用模型**

```python
# ✖ 错误：复核就是在控制面里再问一次模型
decision = urllib.request.urlopen(model_url, data=proposal).read()
# ✔ 正确：auto_if 只走 snapshot.compiled(...).evaluate(approval_env(...))
#         review 只收 Runner.decide 送来的票。M2 不启动复核沙箱
```

**8.2 没写的数不要补**

```python
# ✖ 错误：架构示例里有 timeout: 2h，于是没写超时的工位也用 2 小时
deadline = now + timedelta(hours=2)
# ✖ 错误：复核“至少两个 runtime”，于是法定人数写死 2
quorum = 2
# ✔ 正确：timeout 缺省则 deadline is None，expire_due 跳过
#         法定人数只读宪法 limit("approval_review_quorum", _)；没有就 REVIEW_QUORUM_UNSET
```

**8.3 propose 失败不能当成通过，也不能先发布**

```python
# ✖ 错误：表达式抛错时当作 True，免得产线卡住
ok = True
try:
    ok = expr.evaluate(env)
except EvalError:
    ok = True
# ✖ 错误：Sink 一进入 Runner.run 就 claim，propose 被拒绝后幂等键仍被占用
# ✔ 正确：求值失败走 else（失败即关闭）。两阶段 Sink 在 propose 批准之后才进入 connectors 的 claim
```

**8.4 不要手写求值环境，也不要改 decide 的签名**

```python
# ✖ 错误：自己拼一个 dict，字段名与 factory-profile 差一个字，运行期才爆
env = {"proposal": {"cost_usd": payload["cost_estimate_usd"]}}
# ✖ 错误：为了复核给 decide 加一个 runtime 参数——README 4.9 的签名不能改
def decide(self, approval_id, decision, *, by, note="", runtime): ...
# ✔ 正确：approval_env(phase, payload, inputs, params)
#         runtime 编码在 by 里：runtime:claude-code@4
```

**8.5 超时不是批准**

```python
# ✖ 错误：等不及就自动批准，避免 job 停在 AWAITING_APPROVAL
# ✔ 正确：expire_due 写成 denied / decided_by=timeout，job FAILED，错误码 APPROVAL_TIMEOUT
```

---

## 9. 风险与注意事项

- **示例工位还不能当两阶段脚本跑**：`publish-shorts@1` 的机器没有 `propose` argv。运行期会 `PROPOSE_UNSUPPORTED`。这是有意的：静态检查若强制 argv，示例 Registry 会变脏。给机器补 argv 属于 M2-09 允许的 `machines/**`，补完之前不要用它做验收。
- **没写 timeout 的 foreman 审批会一直 pending**。这不是默认 2 小时。Spec 作者要停车，就必须写 `approval.timeout`。
- **InlineExecutor 会堵在 `runner.run` 里**。确定性测试用 `preset` 或 `auto_if`。并发等待由 `scheduler.md` 的 `wait` 承担，本文件不创建线程。
- **复核票可以永远等不齐**。M2 不代为投票。有 `timeout` 时到期失败；没有就停着。厂长唤醒是 M3 的事。
- **自由文本会夹带秘密**。`summary`、`plan`、`note` 入库前要 `redact`，来源证明列表不收录这些字段。
- **Windows**：`proposal.json` 用临时文件加 `os.replace`，路径用 `pathlib`。不要用 `shell=True` 跑 `propose` argv。
