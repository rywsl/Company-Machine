# 厂长操作面（foreman.md）

> 规范性文档，M3。模块：`mao.foreman`（唤醒票、只读查询、ChangeSet 提交、跨 runtime 复核、转换需求、维护产线自举）。
> 依据：架构文档 9.5、11.1、11.5、11.8–11.10、12.2。
> 跨组件约定见 `README.md`。指针与流水线见 `governance.md`。审计发现见 `reconciler.md`。
> 控制面不调用任何 LLM API（D10）。厂长是仓库外的现成 CLI，通过本文件的命令或 stdio JSON-RPC 进来。
> 唤醒信号新增 `approval`、`inbox`、`monitor`，见 `delegation.md`、`inbox.md`、`capabilities.md`（M11）。

---

## 1. 目的与范围

**做什么（M3）**

- 把需要人判断的信号写成唤醒票。外部进程轮询票，自己启动 agent CLI。控制面不启动模型，也不打开模型厂商的 URL。
- 操作面：只读查询，以及 ChangeSet 的提议、查询、撤回。不能合入，不能移动指针，不能改宪法。
- 跨 runtime 复核的**记录**。复核意见由另一个外部 CLI 提交。控制面只检查 runtime 是否不同、条数是否够，不阅读自然语言。
- ConversionRequest 的合并、状态与解决。
- 维护产线自举：`registry-sink` 只能提议，不能合入。自举命令只检查，不替人写 Spec。

**不做什么**

| 不做 | 归属 |
|---|---|
| 风险等级的计算、影子运行、金丝雀、回滚指针 | `governance.md` |
| 不变量扫描与镜像恢复 | `reconciler.md` |
| 在控制面进程里实现 agent 循环、MCP 里的模型客户端、或“帮厂长改宪法” | 禁止 |
| 取消 run、重试 job、隔离 Bean、代批审批 | 架构 11.5 有这些 L2 工具。本里程碑的操作面不暴露它们（`milestones.md` 只要求只读查询与 ChangeSet 提交） |
| 读取或返回密钥值 | 禁止（D13） |

---

## 2. 依赖

| 依赖 | 用到的接口 |
|---|---|
| `ChangePipeline`、`EpochStore` | `propose`、`withdraw`、`get`、`add_review`、`approvals`；`pointers`、`materialize` |
| `Reconciler` | `findings`、`holds` |
| `Warehouse` | `get`、`lineage`、`list` |
| `Snapshot` | `doc`、`spec`、`result.resolutions` |
| `mao.base` | `Clock`、`IdSource`、`canonical_json` |
| `mao.db` | 迁移 `023_foreman.sql` |

不新增第三方依赖（D2）。JSON-RPC 用标准库 `json`，在 stdio 上逐行收发。不引入 MCP SDK。

---

## 3. 接口

### 3.1 唤醒

```python
@dataclass(frozen=True)
class Wake:
    id: str                      # "wk-0001"
    signal: str                  # conversion | drift | invariant | review | dead-letter | other
    subject: str
    payload: dict[str, Any]      # 只有 id 与错误码，没有密钥、没有 transcript 全文
    status: str                  # open | taken | done
    at: str

class WakeQueue:
    def __init__(self, ctx: Context) -> None
    def file(self, signal: str, subject: str, payload: Mapping[str, Any]) -> Wake
        # 相同 (signal, subject) 已有 open 票时返回原票，不新开。
    def next(self, *, by: str) -> Wake | None
        # 取最早的 open 票，标 taken。没有则 None。
    def done(self, wake_id: str, *, by: str) -> Wake
    def list(self, *, status: str | None = None) -> list[Wake]
```

`file` 不创建子进程，不写 HTTP 请求。调用方（治理、调和器）只负责写票。

### 3.2 只读查询

```python
class ForemanQuery:
    def __init__(self, ctx: Context, epochs: "EpochStore") -> None
    def registry_get(self, key: str) -> dict[str, Any]
    def registry_list(self, kind: str) -> list[str]
    def registry_diff(self, base: str, commit: str, *, timeout_s: float) -> dict[str, Any]
        # {files: [...], stat: "..."}。stat 来自 git diff --stat。超时由调用方传入。
    def epoch_list(self) -> list[dict[str, Any]]
    def epoch_show(self, commit: str | None = None) -> dict[str, Any]
    def bean_get(self, bean_id: str) -> dict[str, Any]
    def bean_lineage(self, bean_id: str, direction: str) -> dict[str, Any]
    def job_get(self, job_id: str) -> dict[str, Any]
    def job_transcript(self, job_id: str) -> str
        # 返回文本前先按 5.2 节打码。
    def dlq_list(self) -> list[dict[str, Any]]          # state == DEAD 的 job
    def drift_list(self) -> list[dict[str, Any]]
    def invariant_report(self) -> list[dict[str, Any]]
    def metrics_query(self, station: str, metric: str) -> dict[str, Any]
        # 只返回 governance.md 5.6 认识的三个指标名。其他 → QUERY_DENIED。
    def approval_list(self) -> list[dict[str, Any]]     # 只读，不含决定动作
```

每个返回值在送出前过 `redact`（5.2）。打码后仍像密钥 → 抛 `SECRET_LEAK`，调用方把整段响应换成错误，不返回半截原文。

### 3.3 ChangeSet 与复核

```python
class ForemanSubmit:
    def __init__(self, ctx: Context, pipeline: "ChangePipeline") -> None
    def propose(self, document: Mapping[str, Any], commit: str, *, by: str,
                timeout_s: float) -> "ChangeSet"
        # 转给 pipeline.propose。不调用 advance，不合入。
    def status(self, changeset_id: str) -> "ChangeSet"
    def withdraw(self, changeset_id: str, *, by: str) -> "ChangeSet"
    def review(self, changeset_id: str, verdict: Mapping[str, Any], *, by: str) -> str
        # 返回复核 id "rv-0001"。见 5.3。
```

没有 `merge`、`rollback`、`advance`。外部 CLI 要推进流水线时，不能通过本操作面；`advance` 只在治理 CLI 里，由进程内的发布循环调用，不交给厂长。

### 3.4 转换需求

```python
class ConversionState(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"

@dataclass(frozen=True)
class ConversionRequest:
    id: str                      # "cr-0001"
    from_type: str
    from_guarantees: tuple[str, ...]
    to_type: str
    to_requires: tuple[str, ...]
    found_at: tuple[dict[str, Any], ...]
    samples: tuple[str, ...]
    blocked_runs: tuple[str, ...]
    status: ConversionState
    changeset_id: str | None
    at: str

class ConversionStore:
    def __init__(self, ctx: Context) -> None
    def open_or_merge(self, *, from_type: str, guarantees: Sequence[str], to_type: str,
                      requires: Sequence[str], found_at: Mapping[str, Any],
                      sample: str | None = None, run_id: str | None = None) -> ConversionRequest
        # 键是 (from_type, 排序后的 guarantees, to_type, 排序后的 requires)。已有 open 或 in_progress 则合并 found_at / samples / blocked_runs。
    def list(self, *, status: str | None = None) -> list[ConversionRequest]
    def mark(self, request_id: str, status: str, *, changeset_id: str | None = None) -> ConversionRequest
    def resolve_matching(self, changeset_id: str, snapshot: "Snapshot") -> list[str]
        # 该 ChangeSet 已 active 后：若新解析表对这条需求是 direct 或 convert，则标 resolved。返回解决了的 id。
```

`resolve_matching` 只读新 epoch 的解析表（或对这条需求调用与编译期相同的 `resolve`，结果追加到该 epoch 的表，不改已有行）。I14：追加不得改变已有键的链。

### 3.5 自举

```python
@dataclass(frozen=True)
class BootstrapReport:
    ok: bool
    missing: tuple[str, ...]     # 缺少的 Spec 键或检查名
    sink_can_merge: bool

def bootstrap_check(snapshot: "Snapshot", sink_token_ops: Sequence[str]) -> BootstrapReport
    # 纯函数。不写 Git，不写数据库。
```

要求 `active` 快照里存在 `Line:onboard-machine@*`（取该名字的最新非 retired 版本）和 `Station:registry-sink@*`。`sink_token_ops` 是这个 Sink 的凭据实际能调用的操作名列表，由调用方从配置读出后传入。列表里若有 `changeset.advance`、`epoch.rollback`、`changeset.merge` 中的任一个，`sink_can_merge` 为真，`ok` 为假。

### 3.6 CLI 与 stdio

```
factory foreman next [--json]
factory foreman done WAKE_ID
factory foreman get KEY [--json]
factory foreman list KIND [--json]
factory foreman diff BASE COMMIT [--json]
factory foreman bean get BEAN_ID [--json]
factory foreman bean lineage BEAN_ID up|down [--json]
factory foreman job get JOB_ID [--json]
factory foreman job transcript JOB_ID
factory foreman dlq ls [--json]
factory foreman drift ls [--json]
factory foreman invariant ls [--json]
factory foreman metrics STATION METRIC [--json]
factory foreman approval ls [--json]
factory foreman changeset propose --doc FILE --commit SHA [--json]
factory foreman changeset status ID [--json]
factory foreman changeset withdraw ID
factory foreman review ID --runtime RUNTIME --verdict approve|deny --note TEXT
factory foreman conversion ls [--status S] [--json]
factory maintain bootstrap [--json]
factory mcp
```

`factory mcp` 在 stdin/stdout 上跑 JSON-RPC 2.0，一行一个消息。方法名与上表命令对应：`foreman.get`、`foreman.changeset.propose`、`foreman.review` 等。未知方法 → JSON-RPC 错误，HTTP 不使用。这个服务器不是工位里的机器网关（那是 `gateway.md`），也不转发到模型 API。

`registry-sink` 在沙箱里通过 `factoryctl` 新增请求 `changeset.propose`（只增命令，不改已有 ctl 动词）。宿主 `CtlHandler` 用 Sink 的凭据调用 `ForemanSubmit.propose`。该凭据的允许操作只有这一项。

退出码：业务拒绝为 1，用法为 2，环境为 4。查询成功为 0，即使结果是空列表。

---

## 4. 数据格式

### 4.1 迁移 `src/mao/migrations/023_foreman.sql`

```sql
CREATE TABLE wakes (
  id       TEXT PRIMARY KEY,
  signal   TEXT NOT NULL,
  subject  TEXT NOT NULL,
  payload  TEXT NOT NULL,
  status   TEXT NOT NULL,
  by       TEXT,
  at       TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE conversion_requests (
  id            TEXT PRIMARY KEY,
  from_type     TEXT NOT NULL,
  guarantees    TEXT NOT NULL,           -- 排序后的 JSON 数组
  to_type       TEXT NOT NULL,
  requires      TEXT NOT NULL,
  found_at      TEXT NOT NULL,
  samples       TEXT NOT NULL,
  blocked_runs  TEXT NOT NULL,
  status        TEXT NOT NULL,
  changeset_id  TEXT,
  at            TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX conversion_key ON conversion_requests(from_type, guarantees, to_type, requires)
  WHERE status IN ('open', 'in_progress');
```

### 4.2 复核文档

`review` 的 `verdict` 只能是 `approve` 或 `deny`。`runtime` 必须是候选 epoch 里存在的 `OperatorRuntime` 键。`note` 最长 2000 个 Unicode 码点，超长 → `REVIEW_INVALID`。控制面不根据 `note` 的词改变状态。

### 4.3 转换需求文档

对外查询时组装成 `records.schema.json` 的 `conversionRequest` 形状，以便和架构 9.5 的字段对齐。存储仍以上表为准。`status` 只有 `open`、`in_progress`、`resolved`。

### 4.4 打码

送出任何字符串之前，删除：

- 环境变量值，若变量名出现在任一 Policy 的 `secrets` 或 Machine 的 `needs.secrets` 里；
- 匹配 `(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*\S+` 的片段，换成 `[REDACTED]`；
- transcript 里同样的片段。

打码是最好努力。5.2 的二次检查失败则整段拒绝。

---

## 5. 行为

### 5.1 唤醒

1. `file` 在一个事务里按 `(signal, subject)` 查找 `open` 票。有则返回。没有则插入。
2. `payload` 只允许 JSON 对象，键是字符串。值若是长度超过 500 的字符串，拒绝写入（调用方应只放 id）。这不是业务阈值，是防止把 transcript 塞进票。
3. `next` 按 `at`、`id` 升序取一条，标 `taken`。两个进程同时取时，靠事务只有一个成功。
4. 控制面代码路径上禁止出现模型厂商主机名的 HTTP 客户端。验收测试用静态扫描辅助，规范要求：`mao.foreman` 不得 `import` 网络客户端去调用模型。

### 5.2 查询与打码

1. `registry_get` 未知键 → `QUERY_DENIED`，`detail.reason = "not-found"`。不把文件系统路径返回给调用方。
2. `job_transcript` 读 `jobs/<id>/<attempt>/logs/` 下的 transcript。文件不存在 → 空串，不报错。
3. 打码之后，若结果仍包含任一已知密钥值（测试注入的假密钥，或 `SecretStore` 能列出的值——只比较，不记录）→ `SECRET_LEAK`。生产里 `SecretStore` 若不能列出值，则只靠 4.4 的模式。比较用过的值不得写入异常 `detail`。
4. `metrics_query` 的 metric 不在 `success_rate`、`gate_reject_rate`、`cost_per_run_usd` 之中 → `QUERY_DENIED`。

### 5.3 复核

复核记录在 `governance.md` 的 `reviews` 表（迁移 `021_governance.sql`），规则由 `ChangePipeline.add_review`（`governance.md` 5.8）实现。`ForemanSubmit.review(changeset_id, verdict, *, by)` 只把 `verdict["verdict"]`、`verdict["runtime"]`、`verdict.get("note", "")` 转发过去。下面的规则与那里一致，列在这里便于厂长阅读：

1. ChangeSet 必须处于 `verified`、`shadow` 或 `canary`。`draft` 或 `rejected` → `CHANGESET_STATE_INVALID`。
2. `runtime` 在候选快照里解析不到 → `REVIEW_INVALID`。
3. `runtime` 等于提出者的 runtime → `REVIEW_SAME_RUNTIME`。提出者 runtime 的取法同 `governance.md` 5.5。
4. 同一 ChangeSet、同一 runtime 已有复核 → 覆盖 verdict 与 note，不新开 id（最后一次提交有效）。
5. `deny` 不自动把 ChangeSet 打成 `rejected`。发布循环在 `governance.md` 只数 `approve`。两条不同 runtime 的 `deny` 也不会被控制面“理解”成自然语言否决以外的动作；它们只是不计入通过条数，流水线因此停在 `verified`。
6. 控制面不启动复核者的 CLI。外部监督进程看到 `review` 类唤醒票后自己启动另一个 OperatorRuntime。

### 5.4 转换需求

1. 合并键不含 `found_at`。同一对类型与属性集合只有一张未完成的单。
2. `samples` 只接受已封存 Bean 的 id。未封存 → 忽略该 sample，不失败整次合并。
3. `mark(in_progress)` 只从 `open` 来。`resolved` 只能由 `resolve_matching` 写入，外部 CLI 不能直接标 resolved（否则可以假装转换已经存在）。外部调用 `mark("resolved")` → `CONVERSION_STATE_INVALID`。
4. `resolve_matching` 在 ChangeSet 变为 `active` 之后由治理流水线调用。解析结果不是 `direct` 或 `convert` 的需求保持 `open`，并再写一张唤醒票。

### 5.5 自举

1. `factory maintain bootstrap` 调用 `bootstrap_check`。`ok` 为假时退出码 1，`missing` 列出缺的键。命令不创建提交，不调用 `propose`。
2. 缺的 Spec 由人写进初始提交，或由厂长经普通 ChangeSet 提交。自举不绕过流水线。
3. `registry-sink` 的实现是控制面里的 Sink 分支：收到 `changeset.propose` 就调用 `ForemanSubmit.propose`。它没有调用 `advance` 的代码路径。凭据配置里若出现禁止的操作名，`bootstrap_check` 失败，Sink 在运行期也拒绝，错误码 `SINK_TOKEN_FORBIDDEN`。
4. 维护产线的其他工位仍是普通工位，走 `lines.md` 的 run。控制面不因为“这是维护”而放宽检验。

### 5.6 宪法

任何查询或提交若试图读取之后再写回 `constitution/` 下的文件，拒绝，`FOREMAN_FORBIDDEN`。只读 `registry_get` 可以返回宪法文本（厂长需要知道规则），但不能提交修改宪法的 commit。那条路径已由 `governance.md` 的 `CONSTITUTION_FORBIDDEN` 挡住；这里再挡一次，避免操作面把宪法文件当作普通 upsert。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `WAKE_NOT_FOUND` | `done` 的 id 不存在 | `id` |
| `QUERY_DENIED` | 键不存在，或指标名不在允许集合 | `reason, key` |
| `SECRET_LEAK` | 打码后的响应仍含密钥值 | `channel`。不得包含值本身 |
| `CONVERSION_NOT_FOUND` | 转换需求 id 不存在 | `id` |
| `CONVERSION_STATE_INVALID` | 外部把状态直接标成 `resolved`，或从 `resolved` 退回 | `id, state, op` |
| `REVIEW_INVALID` | runtime 不存在，verdict 非法，或 note 超长 | `reason` |
| `REVIEW_SAME_RUNTIME` | 复核 runtime 与提出者相同 | `runtime` |
| `REVIEW_NOT_FOUND` | 查询的复核 id 不存在 | `id` |
| `MAINTENANCE_LINE_MISSING` | 自举检查失败 | `missing` |
| `FOREMAN_FORBIDDEN` | 操作面被要求修改宪法、合入、回滚或调用未开放的 L2 动作 | `op` |
| `SINK_TOKEN_FORBIDDEN` | registry-sink 的凭据含有合入类操作，或运行期调用了这些操作 | `op` |

`governance.md` 已有的错误码（`CHANGESET_STALE`、`CONSTITUTION_FORBIDDEN` 等）原样抛出，不在这里换名字。

---

## 7. 验收测试

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m3/test_m3_05_foreman.py` | `test_wake_dedup` | 同一 signal 与 subject 两次 `file` 得到同一个 id；`file` 期间没有子进程 |
| | `test_readonly_roundtrip` | `registry_get` / `bean_get` / `epoch_show` 返回的字段与快照一致 |
| | `test_secret_redacted` | transcript 含 `API_KEY=secret` 时，查询结果是 `[REDACTED]`，且 `detail` 不含原文 |
| | `test_propose_does_not_merge` | 用真实 commit `ForemanSubmit.propose` 之后指针不变，状态是 `draft` |
| | `test_no_merge_method` | JSON-RPC 调用 `foreman.changeset.merge` 得到错误，不是空成功 |
| | `test_constitution_write_forbidden` | 提议的 commit 含 `constitution/` 时，操作面返回 `CONSTITUTION_FORBIDDEN` 或 `FOREMAN_FORBIDDEN` |
| | `test_review_other_runtime` | draft 上复核 → `CHANGESET_STATE_INVALID`；verified 后同一 runtime → `REVIEW_SAME_RUNTIME`；另一个 runtime 的 `approve` 计入 `approvals` |
| | `test_conversion_merged` | 同一对类型两次 `open_or_merge` 只有一行，`found_at` 有两条 |
| | `test_cannot_self_resolve` | 外部 `mark(resolved)` → `CONVERSION_STATE_INVALID` |
| | `test_bootstrap_reports_missing` | 快照里没有 `registry-sink` → `ok` 为假，`missing` 含该键；数据库无新提交 |
| | `test_sink_token_cannot_promote` | 凭据列表含 `epoch.rollback` → `sink_can_merge` 为真，检查失败 |
| | `test_mcp_is_local_stdio` | `factory mcp` 的一次 `foreman.list` 不产生出站连接（测试用假网络钩子计数为 0） |

```
uv run python tools/harness.py pytest tests/acceptance/m3/test_m3_05_foreman.py
```

零失败、零跳过。

---

## 8. 错误示范与正确示范

**8.1 控制面不叫模型**

```python
# ✖ 错误：唤醒时 requests.post("https://api.anthropic.com/...") 或 subprocess 启动 claude
# ✔ 正确：只插入 wakes 行。外部监督进程轮询 factory foreman next 后自己启动 CLI
```

**8.2 复核不是第二套等级**

```python
# ✖ 错误：复核 note 里写了“请按 T1 发布”，控制面就把 risk_tier 改成 T1
# ✔ 正确：note 只存储。等级仍是 governance.md 的 risk_tier。runtime 相同则 REVIEW_SAME_RUNTIME
```

**8.3 Sink 不能合入**

```python
# ✖ 错误：registry-sink 在 propose 成功后调用 pipeline.advance 直到 active
# ✔ 正确：CtlHandler 只调用 ForemanSubmit.propose。advance 不在凭据允许的操作里
```

**8.4 查询不返回密钥**

```python
# ✖ 错误：job_get 把 attempt 的 env 字典原样放进 JSON
# ✔ 正确：env 只保留名字；值一律省略。打码失败则 SECRET_LEAK，整段不返回
```

---

## 9. 风险与注意事项

- **stdio MCP 与工位 MCP 不是同一个进程**。不要把 `factory mcp` 绑到沙箱网关的端口上，也不要为它监听 TCP。
- **唤醒风暴**。合并键已经按 signal + subject 去重。不要为同一发现的每次审计再开一张票。
- **transcript 很大**。`job_transcript` 可以只读文件。调用方若只要尾部，仍必须先打码再截断，禁止先截断再打码（密钥可能被截成两半从而漏检）。打码在完整文本上做，然后可以按调用参数截断；本文件的 CLI 返回全文的打码结果。
- **自举空库**。第一个 commit 必须由人签名写入宪法。`bootstrap_check` 在没有 `active` 时 `ok` 为假，`missing` 含 `active`。它不运行 `git init`。
---

## 10. 任务拆分

| ID | 标题 | 依赖 | 规范章节 | 允许修改的路径 | 验收 | 规模 |
|---|---|---|---|---|---|---|
| M3-05 | 唤醒、只读查询、提议、转换需求与自举检查 | M3-01 | 3–7 | `src/mao/foreman.py`、`src/mao/migrations/023_foreman.sql`、`src/mao/cli.py`、`src/mao/errors.py`、`tests/system/test_foreman_*.py` | `test_m3_05_foreman.py`（第 7 章全部测试通过） | L |

`tests/acceptance/m3/` 属于预先写好的验收材料。任务执行者不得修改它们。
