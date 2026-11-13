# 治理：epoch、ChangeSet 流水线与发布（governance.md）

> 规范性文档，M3。模块：`mao.governance`。迁移：`src/mao/migrations/021_governance.sql`。
> 依据：架构文档 7.4（V3）、8.3–8.5、9.5、10.1、10.4–10.6、11.1–11.4、A.9；README D4、D10、D11。
> 风险规则：`spec/mrl/governance/risk.mrl`（L0，受保护，只有人能改）。
> 跨组件约定见 `README.md`。厂长操作面见 `foreman.md`。审计与调和见 `reconciler.md`。
> `rebuild`、`contracts` 的缺省构造改用 `open_line_runner` / `open_runner`，见 `wiring.md` 5.3（M10）。
> `verified` 阶段按签核方式（`auto`、`review`、`human`）判断、`ChangePipeline.signoff`、金丝雀路由对新产线的处理见 `delegation.md` 5.6、5.7（M11），冲突时以它为准。

---

## 1. 目的与范围

M1、M2 的 epoch 是 Registry 目录树的哈希，一个进程只服务一个快照。M3 把 epoch 换成 Registry Git 仓库里**由控制面创建**的 commit，并让结构变更只能走 ChangeSet 流水线：

```text
propose（受理） → draft → verifying → verified → shadow → canary → active（观察期）
                     任一阶段 → rejected / rolled-back；draft、verified 可由提出者 withdrawn
```

**做什么**

| 任务 | 内容 |
|---|---|
| M3-01 | `EpochStore`：Registry Git 仓库、构建 epoch（检出、`check_registry`、冻结解析表）、三个指针 `active` / `candidate` / `previous`（数据库表 + `refs/factory/*`）、回滚、人的宪法提交（`adopt`）、保留判定 |
| M3-02 | `ChangePipeline` 的受理与验证：宪法拒绝、基线过期、控制面 commit、V1/V2、声明与实际一致、V3 演进校验、V4 契约测试、风险分级（`risk.mrl`）、跨 runtime 复核的记录与计数、封版窗口、变更速率 |
| M3-03 | 发布：影子运行、金丝雀与路由、SLO 指标、推广、观察期与自动回滚、并发发布上限、安全模式、被阻塞 run 的自动重建、`factory governance tick` |

**不做什么**

| 不做 | 归属 |
|---|---|
| 厂长的唤醒票、只读查询、转换需求的存储 | `foreman.md` |
| 不变量审计、Git 引用与镜像的恢复 | `reconciler.md` |
| 在控制面调用模型评估风险、阅读复核意见的自然语言 | 禁止（D10）。风险只来自 `risk.mrl`，复核只数 `approve` |
| 修改 `constitution/`（T4） | 只有人：做一个只改 `constitution/` 的签名提交，再用 `factory epoch adopt`（5.3）。流水线拒绝任何触碰它的 ChangeSet |
| `git push`、`git reset --hard`、改写历史 | 禁止。回滚只移动指针 |
| 运行操作（重试、取消、隔离，T0） | 不是 ChangeSet。走 `lines.md`、`scheduler.md` 的命令 |

---

## 2. 依赖

| 依赖 | 用到的接口 |
|---|---|
| `mao.snapshot.Snapshot` | `load(root, *, epoch=None)`（M3-01 增加的关键字，见 3.1）、`doc`、`spec`、`key`、`result.resolutions` |
| `mrl.rules` | 加载并运行 `spec/mrl/governance/risk.mrl`（与 `mrl run` 相同的 API） |
| `git` 命令行 | `subprocess.run` 参数列表、`timeout`、UTF-8。禁止 GitPython、禁止 `shell=True` |
| `mao.contracts.ContractRunner`（`contract-tests.md`） | V4。通过 3.3 的 `contracts` 工厂注入 |
| `mao.runs.LineRunner`（`lines.md`） | 影子运行（`LineShadowRunner`）、被阻塞 run 的 `rebuild` |
| `mao.foreman.ConversionStore`、`WakeQueue`（`foreman.md`） | 推广后 `resolve_matching`；回滚、安全模式写唤醒票 |
| `mao.reconcile`（`reconciler.md`） | 只读 `audit_findings`，作为安全模式的触发条件 |
| `mao.base`、`mao.db` | `Clock`、`IdSource`、`canonical_json`、`digest_bytes`、`tx`、`query` |

不新增第三方依赖（D2）。Git 版本 ≥ 2.30。

---

## 3. 接口

### 3.1 Epoch（M3-01）

```python
@dataclass(frozen=True)
class Epoch:
    commit: str            # 40 位小写 hex
    tree: str
    built_at: str
    retain: bool           # 见 5.3

class EpochStore:
    def __init__(self, ctx: Context, *, registry: Path | None = None,
                 git: str = "git", timeout_s: float | None = None) -> None
        # registry 缺省取 factory.json 的 governance.registry（4.1）；timeout_s 缺省取 governance.git_timeout_s
        # 构造时就检查：缺配置或不是 Git 仓库 → REGISTRY_NOT_CONFIGURED
    def build(self, commit: str) -> Epoch
    def materialize(self, commit: str) -> Snapshot
    def init(self, commit: str, *, by: str) -> str
    def adopt(self, commit: str, *, by: str) -> str          # 人的宪法提交（T4）
    def current(self) -> str
    def pointers(self) -> dict[str, str | None]          # {"active": ..., "candidate": ..., "previous": ...}
    def set_pointer(self, name: str, commit: str | None, *, by: str, reason: str) -> None
    def rollback(self, commit: str | None = None, *, by: str) -> str
    def retain(self, commit: str) -> bool
    def list(self) -> list[Epoch]
```

`Snapshot.load(root, *, epoch: str | None = None)`：M3-01 给 `mao/snapshot.py` 增加的可选关键字。`None` 时与 M1 相同（目录树哈希 `e-…`）；给出时 `snapshot.epoch` 就是这个值，其余行为不变。M1 的测试不受影响。

### 3.2 ChangeSet（M3-02）

```python
CHANGESET_STATES = ("draft", "verifying", "verified", "shadow", "canary", "active",
                    "rejected", "rolled-back", "withdrawn")

@dataclass(frozen=True)
class ChangeSet:
    id: str                       # "cs-0001"，ids.new("cs")
    base_epoch: str               # 受理时的 active commit（40 位）
    source_commit: str            # 提出者给出的 commit
    commit: str                   # 控制面创建的 epoch commit（5.4）
    state: str
    risk_tier: str | None         # "T0"–"T3"；verified 之前为 None
    reviews_required: int
    proposer: str                 # propose 的 by
    proposer_runtime: str | None  # 5.5
    document: dict[str, Any]      # 去掉 spec.risk_tier、spec.status 之后的文档
    report: dict[str, Any]        # 最近一次阶段报告；{} 表示还没有
    stage_started_at: str | None
    activated_at: str | None
    created_at: str
    updated_at: str
    def to_json(self) -> dict[str, Any]

@dataclass(frozen=True)
class RiskResult:
    tier: str                     # "T0"–"T3"
    reviews_required: int
    stages: tuple[str, ...]       # risk.mrl 的 needs_stage，按字母序
    multiplier: int               # stage_multiplier
    tiers: dict[str, int]         # tier_of：变更 id → 最大等级

def diff_facts(base: Snapshot, candidate: Snapshot, *, base_resolutions: Sequence[Mapping[str, Any]]
               ) -> tuple[dict[str, list[list[Any]]], list[dict[str, Any]]]
    # 返回 (risk.mrl 的输入事实, 计算出的变更清单)。纯函数（5.6）
def score_risk(facts: Mapping[str, list[list[Any]]]) -> RiskResult
    # 运行 risk.mrl。有 unscored → RISK_UNSCORED；risk 为空 → RISK_UNSCORED
def governance_limits() -> dict[str, int]
    # risk.mrl 的 limit 关系
```

### 3.3 流水线（M3-02、M3-03）

```python
class ShadowRunner(Protocol):
    def run(self, cs: ChangeSet, *, candidate: Snapshot, active: Snapshot,
            rollout: Mapping[str, Any], golden_only: bool) -> "ShadowReport"

@dataclass(frozen=True)
class ShadowReport:
    ok: bool
    runs: int
    spent_usd: float
    regressions: list[dict[str, Any]]    # 每项 {kind, subject, detail}

class ChangePipeline:
    def __init__(self, ctx: Context, epochs: EpochStore | None = None, *,
                 contracts: Callable[[Snapshot], "ContractRunner"] | None = None,
                 shadow: ShadowRunner | None = None,
                 rebuild: Callable[[str, Snapshot], str] | None = None) -> None
    # M3-02
    def propose(self, document: Mapping[str, Any], commit: str, *, by: str, timeout_s: float) -> ChangeSet
    def get(self, cs_id: str) -> ChangeSet
    def list(self, *, state: str | None = None) -> list[ChangeSet]
    def withdraw(self, cs_id: str, *, by: str) -> ChangeSet
    def add_review(self, cs_id: str, *, runtime: str, verdict: str, note: str, by: str) -> str
    def approvals(self, cs_id: str) -> list[str]
    def advance(self, cs_id: str) -> ChangeSet
    # M3-03
    def tick(self) -> list[ChangeSet]
    def safe_mode(self) -> dict[str, Any]       # {"on": bool, "since": str|None, "until": str|None, "level": int}

class EpochRouter:                               # M3-03
    def __init__(self, ctx: Context, epochs: EpochStore) -> None
    def choose(self, key: str) -> str            # 新 run 应绑定的 commit

def run_metrics(ctx: Context, *, epoch: str, since: str) -> dict[str, Any]   # M3-03，5.11
```

- `contracts` 缺省：`lambda snap: ContractRunner(ctx, snap, Runner(ctx, snap))`。
- `shadow` 缺省：`LineShadowRunner(ctx)`（5.9）。
- `rebuild` 缺省：`lambda run_id, snap: LineRunner(ctx, snap, Runner(ctx, snap)).rebuild(run_id, by="governance").id`。
- `advance` 在 M3-02 只实现 `draft → verifying → verified/rejected`；对其他状态抛 `CHANGESET_STATE_INVALID`，直到 M3-03 补上。
- `ForemanSubmit.propose` / `review`（`foreman.md` 3.3）只转发到 `propose` / `add_review`。操作面没有 `advance`。

### 3.4 CLI（追加到 `factory`）

```text
factory epoch init COMMIT                 factory epoch ls [--json]
factory epoch show [COMMIT] [--json]      factory epoch rollback [COMMIT]
factory epoch adopt COMMIT
factory changeset propose FILE --commit COMMIT --by ACTOR
factory changeset ls [--state S] [--json] factory changeset show ID [--json]
factory changeset withdraw ID --by ACTOR  factory changeset advance ID
factory governance tick [--json]          factory governance safe-mode [--json]
```

退出码沿用 README 4.10：业务拒绝（`FactoryError` 的业务码）1，用法 2，Registry 无效 3，Git 或环境故障 4。`epoch init`、`epoch adopt`、`epoch rollback` 是人的操作；厂长的操作面不暴露它们（`foreman.md` 3.3）。

---

## 4. 数据格式

### 4.1 `$FACTORY_ROOT/factory.json` 的 `governance` 段（人维护）

```json
{
  "governance": {
    "registry": "D:/factory/registry",
    "git_timeout_s": 60,
    "freeze": [{"start": "2026-12-24T00:00:00Z", "until": "2026-12-27T00:00:00Z", "reason": "年底封版"}]
  }
}
```

| 字段 | 说明 |
|---|---|
| `registry` | Registry Git 仓库的工作目录（绝对路径）。缺失、不是 Git 仓库 → `REGISTRY_NOT_CONFIGURED` |
| `git_timeout_s` | 每次 git 调用的超时，秒。缺省 60 |
| `freeze` | UTC 时段列表，`start <= now < until` 即在封版期。缺省 `[]` |

数值上限（速率、并发、安全模式阈值）**不在**这里，只在 `risk.mrl` 的 `limit` 里（3.2 `governance_limits`）。厂长写不到这两个文件：`factory.json` 在 `FACTORY_ROOT`，不在 Registry 里。

### 4.2 迁移 `src/mao/migrations/021_governance.sql`

```sql
CREATE TABLE epochs (
  "commit"  TEXT PRIMARY KEY,
  tree      TEXT NOT NULL,
  built_at  TEXT NOT NULL
);
CREATE TABLE epoch_pointers (
  name    TEXT PRIMARY KEY,            -- active | candidate | previous
  "commit" TEXT NOT NULL
);
CREATE TABLE changesets (
  id                TEXT PRIMARY KEY,
  base_epoch        TEXT NOT NULL,
  source_commit     TEXT NOT NULL,
  "commit"          TEXT NOT NULL UNIQUE,
  state             TEXT NOT NULL,
  risk_tier         TEXT,
  reviews_required  INTEGER NOT NULL DEFAULT 0,
  proposer          TEXT NOT NULL,
  proposer_runtime  TEXT,
  document          TEXT NOT NULL,     -- 规范 JSON
  report            TEXT NOT NULL DEFAULT '{}',
  stage_started_at  TEXT,
  activated_at      TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX changesets_state ON changesets(state);
CREATE TABLE reviews (
  id             TEXT PRIMARY KEY,
  changeset_id   TEXT NOT NULL,
  runtime        TEXT NOT NULL,        -- 候选 epoch 里 OperatorRuntime 的 Spec 键
  runtime_digest TEXT NOT NULL,        -- 该文档 canonical_json 的 digest_bytes
  verdict        TEXT NOT NULL,        -- approve | deny
  note           TEXT NOT NULL DEFAULT '',
  by             TEXT NOT NULL,
  at             TEXT NOT NULL
);
CREATE UNIQUE INDEX reviews_runtime ON reviews(changeset_id, runtime);
CREATE TABLE governance_state (
  id      INTEGER PRIMARY KEY CHECK (id = 1),
  safe    INTEGER NOT NULL DEFAULT 0,
  since   TEXT,
  until   TEXT,
  level   INTEGER NOT NULL DEFAULT 0
);
```

`commit` 是 SQLite 关键字。迁移和所有查询里，这个列名都必须写成 `"commit"`（双引号）。**禁止**在 `Database.query` 里用正则改写 SQL 来补引号：那会连字符串字面量和 `COMMIT` 语句一起改掉。

`reviews` 表属于本迁移，不属于 `023_foreman.sql`：计数复核的是流水线。`foreman.md` 的 `ForemanSubmit.review` 只调用 `add_review`。

`epoch_pointers` 没有 `candidate` 行表示没有候选。`previous` 同理。

### 4.3 磁盘布局

```text
$FACTORY_ROOT/epochs/<commit>/registry/          git archive 解出的 Registry（只读使用）
$FACTORY_ROOT/epochs/<commit>/resolutions.json   构建时冻结的解析表（规范 JSON 数组，origin 均为 precomputed）
$FACTORY_ROOT/epochs/<commit>/resolution-runtime.json   dynamic.md 追加的运行期键（本文件不写）
```

`resolutions.json` 只在第一次 `build` 时写入。之后任何代码都**不得**覆盖它（`reconciler.md` I14 拿它和重算结果比较）。

Registry 仓库里的引用：

| 引用 | 含义 | 谁写 |
|---|---|---|
| `refs/factory/active`、`candidate`、`previous` | 指针的镜像 | `set_pointer`、`rollback`；`reconciler.md` 按表恢复 |
| `refs/changesets/<id>` | 每个 ChangeSet 的 epoch commit | `propose` |

控制面**从不**写 `refs/heads/*`，也不 `checkout`：分支和工作区属于人与厂长。epoch 的历史就是 `refs/factory/active` 的第一父链：每个提交要么是控制面创建的（说明以 `changeset:` 开头），要么是人经 `adopt` 采纳的宪法提交。厂长与人都应在 `refs/factory/active` 上开分支工作。

### 4.4 风险事实（`diff_facts` 的输出，`risk.mrl` 的输入）

先把两个快照的 Spec 按 `(kind, name)` 分组，再逐个 Spec 键比较（键形如 `Station:upper@2`）。只比较 `kind` 属于下表的文档；`records/` 里的 `ChangeSet`、`Plan`、`ConversionRequest` 记录不参与（它们不是结构）。

| 情况 | `change.op` |
|---|---|
| 候选有、基线没有该键；基线也没有同 `(kind, name)` 的任何版本 | `add` |
| 候选有、基线没有该键；基线有同名其他版本；kind 是 `Type` | major 相同 → `minor`；不同 → `major` |
| 同上，kind 不是 `Type` | `modify`（与基线中同名的最高版本比较字段） |
| 两边都有该键，只有 `metadata.lifecycle` 变了，`active → deprecated` | `deprecate` |
| 同上，变成 `retired` | `retire` |
| 两边都有该键，其他任何字段变了；或基线有、候选没有该键 | 不产生事实：V3 拒绝，`V3_IMMUTABLE`（5.7） |

`id` 是 `c0`、`c1`……，按 `(kind, name, 键)` 升序编号。

| 关系 | 内容 |
|---|---|
| `change(id, kind, op)` | 上表 |
| `field_changed(id, field)` | 只对 `modify`。`spec` 的每个顶层字段，若两边深度比较不相等，就写一行字段名。细分：Machine 的 `runtime` 只有 `image` 不同 → 写 `runtime.image` 而不写 `runtime`；Machine 的 `recipes` 只有各配方的 `task_template` 不同 → 写 `recipes.task_template`；Station 的 `operator` 只有 `model` 不同 → 写 `operator.model`。`metadata` 除 `name`、`version`、`lifecycle` 外有不同 → 写 `metadata`。GoldenSet、Baseline 的 `spec` 有不同 → 写 `spec` |
| `station_role(id, role)` | kind 是 Station 时，基线文档（若有）与候选文档的 `spec.role`，各一行（相同则一行） |
| `weakened(id, what)` | 放水，见下表 |
| `resolution_changed(line, step, port)` | 基线 `resolutions.json` 里的每个 `(line, step, port)`，在候选 `result.resolutions` 中缺失，或 `status`、`chain` 不同 |
| `spec_count(n)` | `change` 的行数 |

`weakened` 的 `what`（对 `modify`、`minor`、`major` 计算，与基线同名最高版本比较）：

| what | 条件 |
|---|---|
| `reject-fixture-removed` | 某 validator 的 `fixtures.reject` 路径集合不是基线的超集 |
| `validator-removed` | 基线某 validator 名在候选中不存在 |
| `guarantee-removed` | 某输出端口的 `guarantees` 不是基线的超集 |
| `accept-removed` | 某输出端口的 `accept` 不是基线的超集 |
| `approval-auto` | `approval.in_run` 或 `approval.propose` 从非 `auto` 变成 `auto`，或 `auto_if` 文本改变 |

风险等级与上述事实的对应关系**只**写在 `risk.mrl` 里。宿主不得再加一层“例外”。

### 4.5 阶段报告 `report`

每次状态变化覆盖写入，同时写一条 `changeset.state` 事件保留历史：

```json
{"stage": "verifying", "ok": false, "code": "V3_INCOMPATIBLE",
 "detail": {"type": "Type:note.text@1.1", "reason": "required payload changed"},
 "impact": {"lines": [], "runs": [], "stations": [], "sinks": []}}
```

`code` 为 `null` 表示该阶段通过。`impact` 只在 verifying 写（5.7 d）。

### 4.6 事件

| kind | data |
|---|---|
| `epoch.built` | `{commit, tree}` |
| `epoch.pointer` | `{name, from, to, by, reason}` |
| `changeset.proposed` | `{id, base_epoch, source_commit, commit, by}` |
| `changeset.state` | `{id, from, to, code}` |
| `changeset.review` | `{id, review, runtime, verdict}` |
| `governance.safe_mode` | `{on, since, until, level, reason}` |

---

## 5. 行为

### 5.1 Git 调用

- 一律 `[git, "-C", registry, ...]`，`capture_output=True`、`encoding="utf-8"`、`timeout=timeout_s`。超时或退出码非 0 → `GIT_FAILED`，`detail` 含 `args`（不含环境变量）与截断到 4000 字符的 `stderr`。
- 控制面创建的 commit 用固定身份与注入时钟，保证可重放：环境变量 `GIT_AUTHOR_NAME=GIT_COMMITTER_NAME=mao-governance`、`GIT_AUTHOR_EMAIL=GIT_COMMITTER_EMAIL=governance@factory.invalid`、`GIT_AUTHOR_DATE=GIT_COMMITTER_DATE=@<clock 的 Unix 秒> +0000`。
- 禁止的子命令：`push`、`reset`、`rebase`、`commit --amend`、`gc --prune`、`checkout`（工作区属于人）。检出 epoch 用 `git archive`。

### 5.2 `build` 与 `materialize`（M3-01）

`build(commit)`：

1. `git rev-parse --verify <commit>^{commit}`。失败 → `COMMIT_NOT_FOUND`。结果规范成 40 位。
2. 已有 `epochs` 行：直接返回（幂等），不重新解包、不重写 `resolutions.json`。
3. `git archive --format=tar <commit>`，用标准库 `tarfile` 解到 `epochs/<commit>/registry/`（先解到 `.part` 目录，成功后 `os.replace`）。拒绝绝对路径与 `..` 成员。
4. `Snapshot.load(dir, epoch=commit)`。`REGISTRY_INVALID` 原样冒泡，并删除解出的目录；不写 `epochs` 行。
5. `resolutions.json` 不存在才写：`snapshot.result.resolutions` 的每一项加上 `"origin": "precomputed"`，整体写成规范 JSON 数组（先写 `.part` 再 `os.replace`）。存在则保留原文件。
6. 一个事务里插入 `epochs` 行，写 `epoch.built`。

`materialize(commit)`：`build` 后加载（进程内按 commit 缓存）。

### 5.3 指针、回滚、保留（M3-01）

- `init(commit, *, by)`：已有 `active` 行 → `EPOCH_STATE_INVALID`。否则 `build`，写 `active`，`git update-ref refs/factory/active <commit>`，返回 commit。这是人的引导操作。
- `adopt(commit, *, by)`：人的宪法提交（T4，架构 11.1）。要求：第一父是当前 `active`；`git diff --name-only <active> <commit>` 非空且每条路径都以 `constitution/` 开头；`git verify-commit <commit>` 成功。任一不满足 → `EPOCH_ADOPT_INVALID`，`detail.reason` 为 `parent` / `paths` / `unsigned`（`paths` 时附 `detail.paths`）。满足则 `build`，`previous := active`，`active := commit`，写 `epoch.pointer`（`reason: "adopt"`）。厂长的操作面没有这个方法。
- `current()`：`active` 行；没有 → `EPOCH_UNSET`。
- `set_pointer(name, commit, *, by, reason)`：`name` 不在三个名字里 → `ValueError`（编程错误）。`commit` 非空时必须已 `build`（否则 `EPOCH_NOT_FOUND`）。事务内改表、写 `epoch.pointer`；事务**之后**更新或删除 `refs/factory/<name>`（`update-ref` / `update-ref -d`）。引用更新失败只抛 `GIT_FAILED`，不回退表——表是声明态，引用由 `reconciler.md` I9 恢复。
- `rollback(commit=None, *, by)`：目标缺省是 `previous`；没有 → `EPOCH_UNSET`。目标必须已 `build` 且不是当前 `active`，否则 `EPOCH_NOT_FOUND` / `EPOCH_STATE_INVALID`。动作：`previous := 旧 active`，`active := 目标`，`candidate` 删除。不写 `refs/heads/*`，不改 Git 历史。若有 ChangeSet 的 `commit` 等于旧 active 且状态是 `active`，把它改成 `rolled-back`（`code = "MANUAL_ROLLBACK"`）。返回目标 commit。
- `retain(commit)` 为真，当且仅当：它是任一指针；或 `runs` 表里有未终态的 run 绑定它；或 `jobs` 表里有未终态 job 绑定它；或它是某个非终态 ChangeSet 的 `base_epoch` / `commit`。`list()` 的 `retain` 字段即此值。本组件不删除任何 epoch 目录（`reconciler.md` I12）。

### 5.4 受理 `propose`（M3-02）

按顺序检查，任一失败抛错，**不**插入 ChangeSet 行：

1. 文档按 `records.schema.json` 的 `changeset` 校验；另外 `spec.rollout.shadow.budget_usd` 必须是 `>= 0` 的数（影子运行也花钱，必须声明），`rollout.slo` 的键只能是 5.11 的三个指标名，值匹配 `^(>=|<=|>|<)\s*[0-9]+(\.[0-9]+)?$`；`canary.percent` 是 1–100 的整数，`canary.min_runs` 是 `>= 1` 的整数；`observe` 能被 `parse_duration` 解析。否则 `CHANGESET_INVALID`，`detail.problems`。
2. 封版：`factory.json` 的任一 `freeze` 时段覆盖 `clock.now()` → `FREEZE_WINDOW`，`detail.until` 为该时段的 `until`。
3. 速率：`changesets` 中 `activated_at` 落在最近 24 小时内的行数 `>= limit("active_per_24h")` → `RATE_LIMITED`，`detail.count`。
4. `commit` 解析为 40 位（`COMMIT_NOT_FOUND`）。`base_epoch` 必须是当前 `active` 的前缀（7–40 位），且 `git merge-base --is-ancestor <active> <commit>` 成功；否则 `CHANGESET_STALE`，`detail` 含 `base_epoch` 与 `active`。
5. `git diff --name-only --no-renames -z <active> <commit>`：任何路径以 `constitution/` 开头 → `CONSTITUTION_FORBIDDEN`，`detail.path` 为第一条这样的路径。diff 为空 → `CHANGESET_INVALID`（`reason: "empty"`）。

全部通过后：

6. `cs_id = ids.new("cs")`。用 `git commit-tree <commit>^{tree} -p <active> -m "changeset: <cs_id>\n\n<intent>"`（5.1 的身份与时间）创建 epoch commit，`git update-ref refs/changesets/<cs_id> <新 commit>`。提出者的 commit 因此不会直接成为 epoch；`main` 上每个 epoch 都带 `changeset:` 前缀（`reconciler.md` I11）。
7. 一个事务里插入 `draft` 行（`proposer = by`，`proposer_runtime` 见 5.5；文档里的 `risk_tier`、`status` 被删除后再保存），写 `changeset.proposed`。返回 `get(cs_id)`。

`timeout_s` 是本次所有 git 调用的总时限的上限：每次调用用 `min(timeout_s, git_timeout_s)`。

### 5.5 提出者的 runtime

- `by` 以 `human:` 开头：`proposer_runtime = None`。人没有特权：复核条数照常要求，任何 runtime 的复核都计入。
- 否则 `by` 必须能在**候选** epoch 里用 `snapshot.key("OperatorRuntime", by)` 解析（例如 `claude-code@1`），`proposer_runtime` 是解析出的 Spec 键。解析在 verifying 的第 1 步之后做；失败 → 该 ChangeSet `rejected`，`code = "PROPOSER_UNKNOWN"`。

`proposer_runtime` 只用于复核规则（5.8）。它不影响风险等级。

### 5.6 `diff_facts` 与 `score_risk`（M3-02）

- `diff_facts` 是纯函数：只读两个快照的文档与基线解析表，不访问 Git、数据库、时钟。按 4.4 生成事实。同样的输入在任何平台上得到逐字节相同的规范 JSON。
- `score_risk`：用 `mrl.rules` 加载 `spec/mrl/governance/risk.mrl`（路径用 `mrl.factory.paths.spec_root()`），以事实为输入求值。
  1. `unscored` 非空 → `RISK_UNSCORED`，`detail.changes` 为 `[[id, reason], ...]`。**不得**默认为 T0 或 T1。
  2. `risk` 为空 → `RISK_UNSCORED`，`detail.changes = []`。
  3. `tier = "T" + str(risk)`；`reviews_required` 取 `reviews(risk, n)`；`stages` 取 `needs_stage(risk, _)`；`multiplier` 取 `stage_multiplier(risk, _)`。缺任何一个 → `RISK_UNSCORED`（规则文件不完整也是“没有等级”）。
- 文档里提出者写的 `risk_tier` 被忽略（受理时已删除）。

### 5.7 验证 `draft → verifying → verified`（M3-02）

`advance` 对 `draft`：先把状态写成 `verifying`（崩溃后重入时从头再验），然后依次：

1. **V1/V2**：`epochs.build(cs.commit)`。`REGISTRY_INVALID` → `rejected`，`code = "REGISTRY_INVALID"`，`detail.diags` 为诊断列表。kernel 已经覆盖消费者契约：删掉下游 `requires` 依赖的 guarantee、收窄输入类型，都会在这里变成 error。
2. 提出者 runtime（5.5）。
3. **V3 不可变与差异**（4.4）：同一 Spec 键的原地修改、删除 Spec 文件 → `rejected`，`code = "V3_IMMUTABLE"`，`detail` 含 `key` 与 `reason`（`modified` / `deleted`）。lifecycle 只能前进：`retired → active` 也是 `V3_IMMUTABLE`。
4. **声明与实际一致**：文档 `changes` 的集合 `{(kind, name, op')}` 必须等于计算出的变更集合，其中 `add`、`modify`、`minor`、`major` 记为 `upsert`。多报、漏报 → `rejected`，`code = "CHANGESET_MISMATCH"`，`detail` 含 `declared_only` 与 `actual_only`，元素是 `[kind, name, op']`，升序。
5. **V3 兼容**（架构 8.3）：对每个 `minor`，与同一 major 的最高旧 minor 比较。下列任何一条成立 → `rejected`，`code = "V3_INCOMPATIBLE"`，`detail` 含 `type` 与 `reason`：
   - `payload` 中 `required: true` 的路径集合变了，或旧的任何 payload 路径不见了；
   - `meta_schema.properties` 删除了键或改了某键的 `type`；`meta_schema.required` 变了；
   - `facts` 删除了键或改了类型；
   - `properties` 删除了名字或改了定义文本；
   - `kind`、`fields`、`item`、`target`、`extends` 有任何变化。
   新增可选 payload、可选 meta 字段、新属性、新 validator 都兼容。
6. **V3 引用**：对每个 `retire` 的键 K，若存在未终态的 run，其 `plan` 的规范 JSON 文本里出现带引号的 `"K"` → `rejected`，`code = "V3_RETIRE_REFERENCED"`，`detail` 含 `key` 与 `runs`（升序）。候选 epoch 内的引用已由 kernel 检查。
7. **风险**：`facts, changes = diff_facts(base, candidate, base_resolutions=base 的 resolutions.json)`，`score_risk(facts)`。`RISK_UNSCORED` → `rejected`，`code = "RISK_UNSCORED"`。
8. **V4**：`runner = contracts(candidate)`。对 `minor`、`major`、`add` 的 Type 的每个 validator 调 `run_validator`；对 `modify` 的 Type 调 `check_fixture_set(base, candidate, ...)`；对新增的 converter 调 `run_equivalence`；对 `modify` 的 Station 调 `run_diff`；对 `station` 与被改工位同名（任一版本）的每个 GoldenSet 调 `run_golden`。任何结果的 `verdict` 不是 `Verdict.PASS`（`"pass"`） → `rejected`，`code = "V4_FAILED"`，`detail.results` 为结果 id 列表。设施故障（抛 `FactoryError`）→ `rejected`，`code = "V4_ERROR"`，不吞掉原错误码（放进 `detail.cause`）。
9. **影响面**（只写报告，不拒绝）：`lines` = 候选中 steps 引用了被改键、或其解析结果在 `resolution_changed` 中的 Line 键；`runs` = 绑定基线 epoch 的未终态 run；`stations` = 被改工位，加上机器或 runtime 被改的工位（缓存会因此失效）；`sinks` = `lines` 中角色为 sink 的步骤工位。全部升序。
10. 通过：一个事务里写 `verified`、`risk_tier`、`reviews_required`、报告（含 `stages`、`multiplier`、`impact`），写 `changeset.state`。

第 1–9 步的任何 `rejected` 都写报告和事件，然后返回 ChangeSet，**不抛错**（`advance` 只在设施故障时抛 `GIT_FAILED` 等）。

### 5.8 复核（M3-02）

`add_review(cs_id, *, runtime, verdict, note, by)`：

1. 状态必须是 `verified`、`shadow` 或 `canary`，否则 `CHANGESET_STATE_INVALID`。
2. `verdict` 不是 `approve` / `deny`，`note` 超过 2000 码点，或 `runtime` 在候选 epoch 里解析不到 → `REVIEW_INVALID`。
3. 解析出的键等于 `proposer_runtime` → `REVIEW_SAME_RUNTIME`。
4. 同一 `(changeset_id, runtime)` 已有行 → 覆盖 `verdict`、`note`、`by`、`at`，保留原 id；否则插入，`id = ids.new("rv")`。`runtime_digest` 是候选 epoch 中该文档的摘要。写 `changeset.review`。返回 review id。

`approvals(cs_id)`：`verdict == "approve"`、`runtime != proposer_runtime`、且 `runtime_digest` 仍等于候选 epoch 中该文档摘要的行，按 runtime 键升序。`deny` 不使 ChangeSet 失败，只是不计数。控制面不阅读 `note`。

`withdraw(cs_id, *, by)`：状态必须是 `draft` 或 `verified`，否则 `CHANGESET_STATE_INVALID`。改为 `withdrawn`，写 `changeset.state`（`code: "WITHDRAWN"`）。控制面没有身份系统，不校验 `by` 是否等于提出者；`by` 只进审计。`refs/changesets/<id>` 保留。

### 5.9 发布阶段（M3-03）

`advance` 对各状态（每次只推进一步）：

| 当前 | 条件 | 下一个 |
|---|---|---|
| `verified` | 安全模式开启且等级 ≥ T2 → 不动，报告 `reason: "safe-mode"` | — |
| `verified` | `len(approvals) < reviews_required` → 不动，报告 `reason: "reviews"` | — |
| `verified` | 正在 shadow/canary 或处于观察期的 ChangeSet 数 `>= limit("concurrent_rollouts")` → 不动，`reason: "queued"` | — |
| `verified` | `base_epoch != current()` | `rejected`，`CHANGESET_STALE` |
| `verified` | 其余 | `shadow`：写 `candidate := commit`，跑影子（下文），通过则留在 `shadow` 并记报告；失败 → `rejected`，`SHADOW_FAILED`，删除 `candidate` |
| `shadow` | 封版中 → 不动，`reason: "freeze"` | — |
| `shadow` | `"canary" in stages` | `canary`：`stage_started_at := now` |
| `shadow` | 否则 | 推广（5.10） |
| `canary` | 样本不足 | 不动，`reason: "samples"` |
| `canary` | 样本足够，SLO 全部满足；封版中则不动 | 推广（5.10） |
| `canary` | 样本足够，任一 SLO 不满足 | `rolled-back`，`SLO_ROLLBACK`，删除 `candidate` |
| `active` | 观察期内 | 观察（5.10） |
| 终态、`draft` 之外的其他组合 | — | `CHANGESET_STATE_INVALID` |

**影子运行**：`shadow.run(cs, candidate=..., active=..., rollout=document.spec.rollout, golden_only=(tier == "T1" and rollout.shadow 没有 runs))`。`report.ok` 为假、`spent_usd > rollout.shadow.budget_usd`、或 `regressions` 非空 → 失败。缺省实现 `LineShadowRunner`：

- 重放基线 epoch 最近 `rollout.shadow.runs` 个 `SUCCEEDED` run 的输入（`golden_only` 时改用相关 GoldenSet 的样例），在候选快照上新建 run，`trigger = "shadow:<cs_id>"`；
- **Sink 换成空 Sink**：sink 步骤只走 `contract-tests.md` 的 `run_sink_dry_run`，绝不真实发布，也不写回执；
- 对比同一输入在两个 epoch 的结果：节点终态从 DONE 变成 FAILED、产物类型不同、检验 verdict 从 pass 变成 reject，都算一条 regression。

**金丝雀样本**：`R = run_metrics` 所用集合，即 `runs.epoch == cs.commit`、`created_at >= stage_started_at`、状态为终态的 run。样本足够指 `len(R) >= canary.min_runs × multiplier`。

**路由**：`EpochRouter.choose(key)`：有 `canary` 状态的 ChangeSet 时，`bucket = int(sha256(key.encode("utf-8")).hexdigest()[:8], 16) % 100`，`bucket < percent` 返回候选 commit，否则返回 `active`。没有金丝雀时返回 `active`。调用方（触发器、`factory line run`）用 `line + ":" + canonical_json(inputs)` 作 key，所以重放同一输入总落在同一边。

### 5.10 推广、观察、回滚（M3-03）

**推广**：

1. 一个事务里：`previous := active`，`active := cs.commit`，删除 `candidate`；ChangeSet `active`、`activated_at := now`、`stage_started_at := now`；写事件。
2. 第 1 步之前先核对 `current() == cs.base_epoch`（期间可能有人 `adopt` 了宪法提交）。不相等 → 不推广，ChangeSet `rejected`，`CHANGESET_STALE`，删除 `candidate`。第 1 步之后同步 `refs/factory/*`（5.3）。
3. `resolved = ConversionStore.resolve_matching(cs_id, 新快照)`（`foreman.md` 3.4、5.4），再对 `resolved` 中每条需求的 `blocked_runs` 里状态为 `FAILED` 的 run（按 id 升序）调 `rebuild(run_id, 新快照)`（5.12）。

**观察**：观察期长度 `parse_duration(rollout.observe) × multiplier`，从 `activated_at` 起算。`R` 同 5.9，`epoch == cs.commit`、`created_at >= activated_at`。

- 观察期内：`len(R) >= 门槛` 且任一 SLO 不满足 → 回滚。门槛是 `rollout.canary.min_runs × multiplier`；`rollout` 没有 `canary` 段时为 1。
- 观察期结束：`R` 非空且任一 SLO 不满足 → 回滚；否则报告 `{"stage": "observe", "ok": true}`，状态保持 `active`，此后 `advance` 返回原样（不再观察）。

**回滚**：先把 ChangeSet 改成 `rolled-back`（`code = "SLO_ROLLBACK"`，`detail` 含 `metric`、`value`、`bound`），再 `epochs.rollback(cs.base_epoch, by="governance")`（顺序反过来会被记成 `MANUAL_ROLLBACK`）。写 `WakeQueue.file("rollback", cs_id, {"id": cs_id})`。

### 5.11 运行指标

`run_metrics(ctx, *, epoch, since)` 只认这三个名字（`foreman.md` 的 `metrics_query`、`reconciler.md` 的漂移检查沿用同一定义）：

| 指标 | 定义 | R 为空时 |
|---|---|---|
| `success_rate` | `SUCCEEDED` 的 run 数 / R 的 run 数 | `None` |
| `gate_reject_rate` | R 的 run 经 `run_node_jobs` 关联的 job 中，`events` 里有 `kind = "job.state"` 且 `data.to == "REJECTED"` 的 job 数 / 关联 job 总数 | 没有 job 时 `None` |
| `cost_per_run_usd` | R 的 `spent_usd` 之和 / R 的 run 数，`round(x, 6)` | `None` |

返回 `{"runs": n, "jobs": m, "success_rate": ..., "gate_reject_rate": ..., "cost_per_run_usd": ...}`。SLO 比较：值为 `None` 的指标不判违反；`">= 0.9"` 表示实际值 `>= 0.9` 才满足。

### 5.12 被阻塞的 run（M3-03）

run 因为缺少转换失败时（`dynamic.md` 的运行期 `no-converter`），发现方用 `ConversionStore.open_or_merge` 登记需求，并把 run id 写进需求的 `blocked_runs`。这就是架构 9.5 的“被阻塞”：run 本身以 `FAILED` 结束，`RunState` 不增加新值。推广一个解决该需求的 ChangeSet 后，5.10 第 3 步用新 epoch 重建这些 run；已完成的节点按 `cache.md` 命中缓存，不重复执行。重建失败只写唤醒票，不影响推广结果。

### 5.13 安全模式（M3-03）

`tick()` 先评估安全模式，再对每个非终态 ChangeSet（按 id 升序）`advance` 一次，返回发生了状态变化的那些。

- **进入**：最近 `limit("safe_mode_window_s")` 秒内 `code == "SLO_ROLLBACK"` 的 `changeset.state` 事件数 `>= limit("safe_mode_rollbacks")`；或 `audit_findings` 表存在且有 `state == "wake"` 的未关闭发现。进入时 `level += 1`，`since := now`，`until := now + limit("safe_mode_cooldown_s") × 2^(level-1)` 秒，写 `governance.safe_mode` 与唤醒票。
- **生效**：等级 ≥ T2 的 ChangeSet 停在 `verified`（5.9），已经在 `shadow`、`canary` 的 T2+ 也不再推进；`active` 不会因此被移动（它已经是最近一个健康的 epoch）。T1 照常。
- **退出**：`now >= until` 且进入条件都不再成立 → `safe := 0`（`level` 保留，下次进入冷却加倍）。条件仍成立时把 `until` 顺延一个冷却期。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `REGISTRY_NOT_CONFIGURED` | `factory.json` 没有 `governance.registry`，或它不是 Git 仓库 | `path` |
| `GIT_FAILED` | git 退出码非 0 或超时 | `args`、`stderr`、`exit_code` |
| `COMMIT_NOT_FOUND` | commit 解析不到 | `commit` |
| `EPOCH_UNSET` | 没有 `active`（或回滚时没有 `previous`） | `name` |
| `EPOCH_NOT_FOUND` | 指针或回滚目标没有 `build` | `commit` |
| `EPOCH_STATE_INVALID` | 重复 `init`；回滚目标就是当前 active | `commit` |
| `EPOCH_ADOPT_INVALID` | `adopt` 的提交父不对、改了 `constitution/` 以外的路径，或没有有效签名 | `commit`、`reason`、`paths` |
| `CHANGESET_INVALID` | 文档不合 schema 或 4.x 的附加约束；diff 为空 | `problems` 或 `reason` |
| `CHANGESET_NOT_FOUND` | id 不存在 | `id` |
| `CHANGESET_STATE_INVALID` | 当前状态不允许该操作 | `id`、`state` |
| `CHANGESET_STALE` | 基线不是当前 active；推广时 `main` 不能快进 | `base_epoch`、`active` |
| `CONSTITUTION_FORBIDDEN` | diff 触碰 `constitution/` | `path` |
| `FREEZE_WINDOW` | 封版期内受理 | `until` |
| `RATE_LIMITED` | 24 小时内推广数已达上限 | `count` |
| `RISK_UNSCORED` | 风险规则给不出等级 | `changes` |

下面这些不抛出，只作为 `report.code` 出现在 `rejected` / `rolled-back` 的 ChangeSet 上：`REGISTRY_INVALID`、`PROPOSER_UNKNOWN`、`V3_IMMUTABLE`、`CHANGESET_MISMATCH`、`V3_INCOMPATIBLE`、`V3_RETIRE_REFERENCED`、`RISK_UNSCORED`、`V4_FAILED`、`V4_ERROR`、`SHADOW_FAILED`、`SLO_ROLLBACK`、`MANUAL_ROLLBACK`、`CHANGESET_STALE`、`WITHDRAWN`。凡是作为 `report.code` 出现的值也要登记在 `CODES` 里（README 3.6），便于 `mrl explain` 式的查询。

复核的错误码 `REVIEW_INVALID`、`REVIEW_SAME_RUNTIME` 由 `foreman.md` 第 6 章定义，`add_review` 抛出。

---

## 7. 验收测试

共享夹具：`tests/acceptance/m3/gov_world.py`。它把 `tests/fixtures/registry-m2` 复制成一个新的 Git 仓库（初始提交由 `human:` 身份完成），写 `factory.json`，打开 `Context`，`EpochStore.init` 初始 commit。改动 Registry 用 `commit_change(files=..., message=...)`，返回新 commit。全部用 `LocalProvider`、`FakeClock`、`CounterIds`。影子运行与契约测试用测试里的替身注入（3.3）。

### 7.1 `tests/acceptance/m3/test_m3_01_epochs.py`（M3-01）

| 测试 | 验证 |
|---|---|
| `test_init_sets_active_pointer_and_ref` | 5.3：`pointers()["active"]` 与 `refs/factory/active` 都是初始 commit；`materialize` 的 `snapshot.epoch` 等于该 commit |
| `test_init_twice_rejected` | 第二次 `init` → `EPOCH_STATE_INVALID` |
| `test_build_writes_frozen_resolutions_once` | 5.2：`resolutions.json` 等于快照的解析表；篡改文件后再 `build`，文件仍是篡改后的内容 |
| `test_build_rejects_invalid_registry` | 坏 Registry 的 commit → `REGISTRY_INVALID`，`epochs` 表没有这一行，也没有残留目录 |
| `test_rollback_moves_pointers_only` | 5.3：`active`、`previous` 交换；`refs/heads/main` 没被改动，两条 commit 都还在 |
| `test_adopt_requires_constitution_only_signed_commit` | 5.3：改了宪法以外路径 → `EPOCH_ADOPT_INVALID`（`paths`）；只改宪法但未签名 → `EPOCH_ADOPT_INVALID`（`unsigned`）；active 不变 |
| `test_rollback_target_must_be_built` | 未构建的 commit → `EPOCH_NOT_FOUND`；没有 previous → `EPOCH_UNSET` |
| `test_retain_rules` | 指针与被未终态 run 绑定的 commit 保留；其他已构建的 commit 不保留 |
| `test_registry_not_configured` | 没有 `factory.json` → `REGISTRY_NOT_CONFIGURED` |

### 7.2 `tests/acceptance/m3/test_m3_02_changesets.py`（M3-02）

| 测试 | 验证 |
|---|---|
| `test_constitution_change_forbidden` | 5.4：commit 改了 `constitution/invariants.mrl` → `CONSTITUTION_FORBIDDEN`，没有 ChangeSet 行 |
| `test_stale_base_rejected` | `base_epoch` 不是 active → `CHANGESET_STALE` |
| `test_propose_creates_control_plane_commit` | 新 commit 的树等于提出者 commit 的树、父是 active、说明以 `changeset: cs-0001` 开头；`refs/changesets/cs-0001` 指向它；状态 `draft` |
| `test_limits_change_is_t1` | `upper@2` 只改 `limits` → `verified`、`T1`、`reviews_required == 0` |
| `test_risk_ignores_proposer_claim` | 文档写 `risk_tier: T0`，实际换了机器镜像 → `T2` |
| `test_sink_change_is_t3` | 新版本 sink 工位 → `T3`、`reviews_required == 2` |
| `test_unscored_is_not_low` | 5.6：`score_risk` 遇到表外字段 → `RISK_UNSCORED`，`detail.changes` 非空 |
| `test_declared_changes_must_match` | 漏报一个 Machine 变更 → `rejected`，`CHANGESET_MISMATCH` |
| `test_in_place_edit_is_immutable` | 原地改 `upper@1` → `rejected`，`V3_IMMUTABLE` |
| `test_minor_must_be_compatible` | `note.text@1.1` 改了必需 payload → `rejected`，`V3_INCOMPATIBLE` |
| `test_v4_failure_rejects` | 注入的契约替身返回失败 → `rejected`，`V4_FAILED` |
| `test_freeze_and_rate_limit` | 封版 → `FREEZE_WINDOW`（含 `until`）；24 小时内已有上限数的推广 → `RATE_LIMITED` |
| `test_review_rules` | 同 runtime → `REVIEW_SAME_RUNTIME`；draft 上复核 → `CHANGESET_STATE_INVALID`；不同 runtime 的 approve 计入，deny 不计入 |
| `test_withdraw` | `draft` 可撤回；撤回后 `advance` → `CHANGESET_STATE_INVALID`；未知 id → `CHANGESET_NOT_FOUND` |

### 7.3 `tests/acceptance/m3/test_m3_03_rollout.py`（M3-03）

| 测试 | 验证 |
|---|---|
| `test_t1_shadow_then_active` | 5.9–5.10：T1 经影子直接推广；`active`、`previous`、`refs/factory/active` 正确，`refs/heads/main` 未被改动；影子替身收到 `golden_only=True` |
| `test_shadow_failure_rejects` | 影子有 regression → `rejected`，`SHADOW_FAILED`，没有 `candidate` |
| `test_t2_waits_for_review_then_canary` | T2 没有复核时停在 `verified`；复核后进入 `shadow`、`canary`，`candidate` 指针为该 commit |
| `test_canary_promotes_on_slo` | 插入足够的成功 run → 推广 |
| `test_canary_slo_violation_rolls_back` | 插入失败 run → `rolled-back`，`SLO_ROLLBACK`，active 不变，没有 candidate |
| `test_observe_window_rolls_back` | 推广后观察期结束时 SLO 不满足 → 回滚到 base，`previous` 是被回滚的 commit |
| `test_observe_window_passes` | 观察期结束时 SLO 满足 → 报告 `stage: observe`、`ok: true`，状态保持 `active`；之后 `advance` 不再改变它 |
| `test_router_is_deterministic` | 5.9：同一 key 总得到同一 commit；没有金丝雀时总是 active |
| `test_run_metrics_definitions` | 5.11：三个指标按定义计算；空集合为 `None` |
| `test_safe_mode_pauses_t2` | 5.13：两次 SLO 回滚后进入安全模式，T2 停在 `verified`，T1 照常；冷却期过后退出 |
| `test_freeze_blocks_promotion` | 封版期内 `shadow` 不推广 |
| `test_blocked_runs_rebuilt_after_activation` | 5.12：解决转换需求的 ChangeSet 推广后，注入的 `rebuild` 收到 `blocked_runs` 里的 run 与新快照 |

`tests/acceptance/m3/` 是预先写好的验收材料。任务执行者不得修改。

---

## 8. 错误示范与正确示范

**8.1 让提出者决定风险**

```python
# ✖ 厂长说是 T1 就按 T1
tier = document["spec"].get("risk_tier", "T1")
# ✔ 只看 diff 与 risk.mrl
facts, _ = diff_facts(base, candidate, base_resolutions=frozen)
tier = score_risk(facts).tier
```

**8.2 算不出等级就当低风险**

```python
# ✖ 未知不是安全
except FactoryError: tier = "T1"
# ✔ 没有等级就拒绝
cs = reject(cs, "RISK_UNSCORED", changes=unscored)
```

**8.3 把提出者的 commit 直接当 epoch**

```python
# ✖ epoch 历史里出现没有 changeset: 前缀的提交，I11 无法对账；提出者还能夹带别的父提交
epochs.set_pointer("active", source_commit, by=by, reason="merge")
# ✔ 受理时用同一棵树重新创建 commit，父固定为 active
new = git("commit-tree", f"{source_commit}^{{tree}}", "-p", active, "-m", f"changeset: {cs_id}\n\n{intent}")
```

**8.4 回滚用 reset**

```python
# ✖ 破坏历史，也破坏人的工作区
git("reset", "--hard", base)
# ✔ 只动指针；main 不倒退
epochs.rollback(base, by="governance")
```

**8.5 影子运行真的发布了**

```python
# ✖ 候选 epoch 上照常执行 sink
lines.start_run(line, inputs)
# ✔ sink 步骤只做 dry-run，不写回执
contracts.run_sink_dry_run(station, inputs=inputs, params=params)
```

**8.6 重写冻结的解析表**

```python
# ✖ 每次 build 都覆盖，I14 再也发现不了漂移
path.write_text(canonical_json(snapshot.result.resolutions))
# ✔ 只在第一次写
if not path.exists(): write_atomic(path, canonical_json(snapshot.result.resolutions))
```

**8.7 读复核意见的文字**

```python
# ✖ note 里写“同意，按 T1 走”就降级
if "T1" in review.note: tier = "T1"
# ✔ 只数不同 runtime 的 approve
len(pipeline.approvals(cs.id)) >= cs.reviews_required
```

---

## 9. 风险与注意事项

- **R14 规则歧义**：`risk.mrl` 没有覆盖的变更一律 `RISK_UNSCORED`。不要在宿主里补“合理的默认值”；需要新规则时写 journal，由人改 `risk.mrl`。
- **R18 破坏性 git**：本组件只用 `rev-parse`、`archive`、`diff`、`merge-base`、`commit-tree`、`update-ref`、`show-ref`、`log`、`cat-file`。PreToolUse hook 也会拦截 `reset --hard`、`clean`、`push --force`。
- **时间**：所有窗口（封版、速率、观察、冷却）都用注入的 `clock`。测试用 `FakeClock` 推进，禁止 `time.sleep`。
- **两个数据源**：指针以数据库为准，Git 引用是镜像。改表成功、改引用失败时不回滚表，由调和器恢复。
- **不碰人的分支**：控制面不写 `refs/heads/*`、不 `checkout`。否则人或厂长正在用的工作区会突然变脏。
- **Windows**：`git archive` 的输出用二进制读取；路径用 `pathlib`；解包时拒绝 `..` 与绝对路径成员；`-z` 输出按 `\0` 拆分。
- **成本**：影子运行花真钱。`budget_usd` 是硬上限；超出即失败，不“先跑完再说”。
- **不要等待人**：T3 也不要求人工审批（架构 11.4 完整自治），要求的是两个不同 runtime 的复核。人只改宪法和 `risk.mrl`。

---

## 10. 任务拆分

| ID | 标题 | 依赖 | 规范章节 | 允许修改的路径 | 验收 | 规模 |
|---|---|---|---|---|---|---|
| M3-01 | Epoch 存储、指针与回滚 | M2 阶段门 | 3.1、4.1–4.3、5.1–5.3 | `src/mao/governance/**`、`src/mao/snapshot.py`（只加 `epoch` 关键字）、`src/mao/migrations/021_governance.sql`、`src/mao/cli.py`、`src/mao/errors.py`、`tests/system/**` | `test_m3_01_epochs.py` 全部通过 | M |
| M3-02 | ChangeSet 受理、验证（V1–V4）、风险与复核 | M3-01 | 3.2–3.3、4.4–4.6、5.4–5.8 | 同 M3-01 | `test_m3_02_changesets.py` 全部通过 | L |
| M3-03 | 影子、金丝雀、推广与回滚、安全模式 | M3-02、M3-04、M3-05 | 5.9–5.13 | 同 M3-01，加 `src/mao/runs.py`（只加路由入口） | `test_m3_03_rollout.py` 全部通过 | L |
