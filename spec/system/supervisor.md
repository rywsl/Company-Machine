# supervisor：厂长值守进程（M5-06）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M5-06**。
> `foreman.md` 5.3 第 6 条写着“外部监督进程看到唤醒票后自己启动 OperatorRuntime”，但没有任何规范定义这个进程。结果是：唤醒票写进了数据库，没有人读；厂长的 CLI 永远不会被启动（`plan/drift.md` D-24）。本文件定义它。
> 同时补上转换需求的生产者（D-25）和唤醒票的失败语义。没有这些，13.2 的闭环“缺少转换 → 厂长编写 → 推广”起不了头，也停不下来。

---

## 1. 目的与范围

值守进程是确定性代码。它取唤醒票，为每张票在沙箱里启动一次现成的 agent CLI（厂长或复核者），把 MCP 操作面接给它，结束后按结果关票或重开。它不读 transcript 的内容做决定，不替厂长判断，不调用模型 API。

**做什么（M5-06）**

- `mao.supervisor`：`SupervisorConfig`、`SessionRecord`、`ForemanSupervisor`。
- `mao.operator.build_runtime_invocation`：不经过工位，直接用 OperatorRuntime 模板组装命令行。
- 会话目录、Registry 克隆、MCP 配置文件、会话的 git 身份。
- 会话结果的判定，唤醒票的完成、退避重开、放弃。
- 复核者的选择：与提出者不同的 runtime，按配置顺序。
- 日预算与单会话预算。
- 转换需求的生产者：V2 的 `E-NO-CONVERTER`、MCP 工具 `registry_check`（第 3.5 节）。
- 命令行：`factory foreman supervise`、`factory foreman sessions`、`factory foreman reopen`。

**不做什么**

| 不做 | 归属 |
|---|---|
| 决定 ChangeSet 是否推进 | `governance.md`（发布循环）；值守只启动复核者，复核结论由复核者经 MCP 提交 |
| 为 Claude Code、Codex 写真实的 runtime 文档、真实模型上游 | M6-02，`runtimes.md` |
| Docker 上的端到端（真实 CLI） | M6-03，`live.md` |
| 读 transcript 判断“厂长做得好不好” | 不做。结果只看退出码、MCP 上留下的动作 |
| 让厂长直接执行 `advance`、合入、改宪法 | 禁止（`foreman.md` 第 1 章） |

---

## 2. 依赖

| 依赖 | 用途 |
|---|---|
| `Context`（README 4.1）、`Context.owner`（`durable.md`） | 时钟、ids、数据库、provider、租约 owner |
| `LeaseStore`、`LeaseKeeper`（`durable.md`） | `supervisor` 租约 |
| `WakeQueue`、`ConversionStore`、`ForemanQuery`（`foreman.md`） | 取票、标记转换需求、打码 |
| `EpochStore`、`ChangePipeline`（`governance.md`） | active epoch、快照、ChangeSet 状态与复核 |
| `McpServer`、`Session`、`McpCtlHandler`（`mcp.md`）、`CtlServer`（`factoryctl.md`） | 会话的操作面 |
| `SandboxProvider`、`SandboxSpec`、`Mount`（`sandbox.md`） | 运行 CLI |
| `build_invocation` 的模板规则、`parse_transcript`（`operator.md`） | 命令行与 transcript 摘要 |
| `register_job_cap`、`ModelProxy.spent`（`egress.md`） | 会话预算 |
| git 命令行 | 克隆、取回提交 |

---

## 3. 接口

### 3.1 `mao.supervisor`

```python
@dataclass(frozen=True)
class SupervisorConfig:
    runtime: str                       # 厂长的 OperatorRuntime 引用，如 "claude-code@4"
    model: str
    reviewers: tuple[str, ...]         # 复核者 runtime 引用，按优先级
    models: Mapping[str, str]          # runtime 引用 → 模型；reviewers 中每一项都必须有
    image: str                         # 会话容器镜像："<repo>@sha256:<64位hex>"
    network: str                       # "none" | "egress"
    session_timeout_s: float
    session_budget_usd: float
    daily_budget_usd: float
    max_turns: int
    max_attempts: int
    backoff_s: float
    env: Mapping[str, str]             # 额外传给会话的非秘密环境变量

    @classmethod
    def load(cls, root: Path) -> "SupervisorConfig"
        # 读 $FACTORY_ROOT/factory.json 的 "foreman" 段（第 4.1 节）

@dataclass(frozen=True)
class SessionRecord:
    id: str                            # "fs-0001"，ids.new("fs")
    wake_id: str
    role: str                          # "foreman" | "reviewer"
    runtime: str                       # Spec 键 "OperatorRuntime:fake-agent@1"
    actor: str                         # 名字@版本 "fake-agent@1"
    model: str
    epoch: str                         # 会话开始时的 active
    state: str                         # "running" | "ok" | "failed" | "timeout"
    exit_code: int | None
    cost_usd: float
    turns: int | None
    proposed: tuple[str, ...]          # 本会话经 MCP 成功提议的 ChangeSet id，按时间
    reviewed: tuple[str, ...]          # 本会话经 MCP 成功复核的 ChangeSet id
    error: dict[str, Any] | None       # {"code": ..., "detail": {...}}
    started_at: str
    ended_at: str | None
    def to_json(self) -> dict[str, Any]   # 键与字段同名，tuple 写成数组

class ForemanSupervisor:
    def __init__(self, ctx: Context, epochs: EpochStore, config: SupervisorConfig, *,
                 pipeline: ChangePipeline | None = None) -> None
        # pipeline 缺省：ChangePipeline(ctx, epochs)（带 M5-01 的缺省实现）
    def start(self) -> list[str]
        # 取 supervisor 租约；恢复上次崩溃留下的会话（5.8）。返回被恢复的会话 id，升序
    def run_once(self) -> SessionRecord | None
        # 取一张到期的票并跑完一次会话（5.1）。没有票、预算用尽、或票不需要会话时返回 None
    def serve(self, stop: threading.Event, *, idle_s: float = 5.0) -> None
    def shutdown(self) -> None
    def sessions(self, *, state: str | None = None) -> list[SessionRecord]   # 按 id 升序
```

`start` 之前调用 `run_once` → `SUPERVISOR_NOT_STARTED`。

### 3.2 `mao.operator` 的增补

```python
def build_runtime_invocation(snapshot: Snapshot, runtime: str, *, task: str, contract: str,
                             model: str, max_turns: int, budget_usd: float,
                             timeout_s: float) -> Invocation
```

- `runtime = snapshot.key("OperatorRuntime", runtime)`。解析不到 → `SPEC_NOT_FOUND`（`snapshot.md`）。
- `kind` 不是 `agent-cli` → `INVOCATION_INVALID`，`detail.reason = "会话需要 agent-cli runtime"`。
- 模板环境与 `operator.md` 5.3 相同：`runtime_env(Brief(task, contract, ""), {"max_turns": max_turns, "timeout_s": int(timeout_s), "budget_usd": budget_usd}, {"model": model, "runtime": 名字@版本})`。`timeout_s` 不是整秒 → `TIMEOUT_NOT_INTEGRAL`。
- argv、env、transcript、`models_allowed` 的规则与 `operator.md` 5.4 的 agent-cli 第 1–6 步逐条相同，错误码也相同（`INVOCATION_INVALID`、`TRANSCRIPT_FORMAT`、`MODEL_NOT_ALLOWED`）。
- 不追加 MCP 参数。MCP 由值守按 `invoke.mcp` 处理（5.3）。

`build_invocation` 应当改为调用同一个内部函数，避免两套模板逻辑。

### 3.3 `WakeQueue` 的增补（`foreman.md` 3.1）

```python
@dataclass(frozen=True)
class Wake:
    ...                                # 原有字段
    attempts: int                      # 失败的会话次数
    not_before: str | None             # 早于此时刻不得被 next 取走

class WakeQueue:
    def next(self, *, by: str) -> Wake | None
        # 只取 status == "open" 且 (not_before 为空或 <= now) 的票，按 at、id 升序。不改 attempts
    def get(self, wake_id: str) -> Wake
    def reopen(self, wake_id: str, *, not_before: str | None, failed: bool, by: str) -> Wake
        # taken → open。failed 为真时 attempts + 1。写 foreman.wake.reopened
    def fail(self, wake_id: str, *, code: str, by: str) -> Wake
        # taken → failed。写 foreman.wake.failed，data 含 code
    def retry(self, wake_id: str, *, by: str) -> Wake
        # failed → open，attempts 归零，not_before 清空。只给人用的命令行调用（3.6）
```

状态增加 `failed`。`file` 的去重范围是 `open`、`taken`、`failed`（`daemon.md` 3.2 已把 `taken` 加进去，本任务再加 `failed`）。放弃的票不会被信号泵自动重开，必须由人 `factory foreman reopen`。

### 3.4 其他模块的增补

```python
# mao.foreman
def redact(ctx: Context, snapshot: Snapshot, text: str) -> str
    # foreman.md 4.4 的打码，改为公开函数。ForemanQuery 内部也调用它（D-18：不跨模块用私有成员）

def file_conversions(ctx: Context, diags: Sequence[Mapping[str, Any]],
                     resolutions: Sequence[Mapping[str, Any]], *,
                     found_at: Mapping[str, Any], run_id: str | None = None) -> list[str]
    # 第 5.6 节。返回涉及的转换需求 id，升序、去重

class ConversionStore:
    def reopen(self, request_id: str) -> ConversionRequest
        # in_progress → open，changeset_id 置空，写 conversion.reopened。其他状态 → CONVERSION_STATE_INVALID

# mao.governance
class ChangePipeline:
    def reviews(self, cs_id: str) -> list[dict[str, Any]]
        # 全部复核（含 deny）：{id, runtime, verdict, note, by, at}，按 at、id 升序

# mao.mcp（mcp.md 3.1）
@dataclass(frozen=True)
class Session:
    role: str
    actor: str
    wake_id: str | None = None
    session_id: str | None = None      # 与 source_repo 同时给出或同时为空，否则 MCP_ACTOR_INVALID
    source_repo: Path | None = None    # 会话的 Registry 克隆（宿主路径）

class McpServer:
    @property
    def outcome(self) -> dict[str, list[str]]
        # {"proposed": [...], "reviewed": [...]}：本服务器上成功的 changeset_propose / review_submit
```

### 3.5 MCP 工具的增补（`mcp.md` 3.3）

| 工具 | 参数 | 调用 | 角色 |
|---|---|---|---|
| `registry_check` | `commit` | 第 5.7 节 | 仅 `foreman` |

`changeset_propose` 与 `registry_check` 在 `session.source_repo` 不为空时，先从克隆取回提交（5.4）。

### 3.6 命令行

```
factory foreman supervise [--once] [--json]
factory foreman sessions [--state S] [--json]
factory foreman reopen WAKE_ID
```

- `supervise`：`SupervisorConfig.load` → `ForemanSupervisor(ctx, epochs, config).start()` → `serve(stop)`。`--once`：`start` 后只调用一次 `run_once`，打印记录（没有则打印 `null`），然后 `shutdown`。需要治理配置（`factory.json` 的 `governance` 段），没有 → `SUPERVISOR_CONFIG_INVALID`，`detail.field = "governance"`。
- `sessions`：`sessions(state=...)`，按 `commands.md` 的 JSON 规则输出。
- `reopen`：`WakeQueue.retry`。票不是 `failed` → `WAKE_STATE_INVALID`，退出码 1。

这些命令属于 `commands.md` 的 `foreman` 组，由 M5-06 加入（`commands.md` 第 3 章已预留）。它们**不**出现在 MCP 工具里：厂长不能重开自己被放弃的票。

---

## 4. 数据格式

### 4.1 `factory.json` 的 `foreman` 段（人维护）

```json
{
  "foreman": {
    "runtime": "claude-code@4",
    "model": "claude-opus-5-5",
    "reviewers": ["codex@1"],
    "models": {"claude-code@4": "claude-opus-5-5", "codex@1": "gpt-5-codex"},
    "image": "registry.local/foreman-bench@sha256:<64位hex>",
    "network": "egress",
    "session_timeout_s": 1800,
    "session_budget_usd": 5.0,
    "daily_budget_usd": 50.0,
    "max_turns": 60,
    "max_attempts": 3,
    "backoff_s": 600,
    "env": {}
  }
}
```

- 没有 `foreman` 段 → `SUPERVISOR_CONFIG_MISSING`，`detail.path`。
- 除 `env` 外每个键都必须出现。类型不对、`network` 不在两个值里、数值不为正（`backoff_s` 可以为 0）、`max_turns` / `max_attempts` 不是 `>= 1` 的整数、`reviewers` 中某项在 `models` 里没有条目 → `SUPERVISOR_CONFIG_INVALID`，`detail.field` 为第一个出错的键（`models.<runtime>` 这种写法）。
- `env` 的键以 `FACTORY_`、`GIT_`、`HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY` 开头 → `SUPERVISOR_CONFIG_INVALID`。这些由值守设置，不允许配置覆盖。
- `env` 是给 CLI 的非秘密开关（例如关闭遥测）。秘密不放这里：模型密钥只在模型代理里（`egress.md`）。
- `runtime`、`reviewers` 是否存在、是不是 agent-cli，在每次会话开始时对当时的 active 快照检查（5.1 第 5 步），不在 `load` 时检查。active 会变。

### 4.2 迁移 `src/mao/migrations/031_supervisor.sql`

```sql
ALTER TABLE wakes ADD COLUMN not_before TEXT;
ALTER TABLE wakes ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
CREATE TABLE foreman_sessions (
  id          TEXT PRIMARY KEY,
  wake_id     TEXT NOT NULL,
  role        TEXT NOT NULL,
  runtime     TEXT NOT NULL,
  actor       TEXT NOT NULL,
  model       TEXT NOT NULL,
  epoch       TEXT NOT NULL,
  state       TEXT NOT NULL,
  exit_code   INTEGER,
  cost_usd    REAL NOT NULL DEFAULT 0,
  turns       INTEGER,
  outcome     TEXT NOT NULL DEFAULT '{"proposed":[],"reviewed":[]}',
  error       TEXT,
  started_at  TEXT NOT NULL,
  ended_at    TEXT
);
CREATE INDEX foreman_sessions_started ON foreman_sessions(started_at);
```

### 4.3 会话目录

`$FACTORY_ROOT/foreman/sessions/<session_id>/`：

| 宿主路径 | 容器路径 | 模式 | 内容 |
|---|---|---|---|
| `in/` | `/factory/in` | 只读 | `wake.json`（4.4）、`brief.md`（task 与 contract 全文） |
| `ctl/` | `/factory/ctl` | 读写 | `job.json`（4.5）、`mcp.json`（4.6）、`requests/`、`responses/` |
| `workspace/` | `/workspace` | 读写，绑定挂载 | `registry/`：active 的 Registry 克隆；`.codex/`（仅 `codex-toml`） |
| `$FACTORY_ROOT/tools/factory` | `/opt/factory` | 只读 | `factoryctl`（与工位相同） |
| （runtime 的 `inject.volume`） | `inject.mount` | 只读 | CLI 本体（与工位相同，`runner.md` 第 5 步） |
| `logs/` | 不挂载 | — | `transcript.jsonl`（打码后）、`stderr.txt`（打码后） |

工作区用绑定挂载而不是命名卷：宿主要从克隆里取回提交（5.4）。会话结束后目录保留，由 M7 的保留策略清理。

### 4.4 `in/wake.json`

```json
{"id": "wk-0003", "signal": "review", "subject": "cs-0007", "payload": {"id": "cs-0007", "need": 1},
 "attempts": 0, "epoch": "<active 40 位>", "session": "fs-0002", "role": "reviewer"}
```

### 4.5 `ctl/job.json`

与工位的 `job.json`（`factoryctl.md`）同形，使 `factoryctl mcp` 可以复用：

```json
{"job_id": "fs-0002", "attempt": 1, "station": "reviewer", "epoch": "<active>",
 "inputs": {}, "params": {}, "budget_usd": 5.0, "timeout_s": 1800}
```

`station` 是角色名，不是 Station 键。`attempt` 是 `wake.attempts + 1`。

### 4.6 MCP 配置

runtime 文档的 `invoke.mcp`（schema 里是自由对象）决定写法：

| `format` | 文件 | 命令行 | 环境变量 |
|---|---|---|---|
| `claude-json` | `ctl/mcp.json`：`{"mcpServers": {"factory": {"command": "python3", "args": ["/opt/factory/bin/factoryctl", "mcp"], "env": {"FACTORY_CTL": "/factory/ctl"}}}}` | 在 argv 末尾追加 `[invoke.mcp.flag, "/factory/ctl/mcp.json"]`；没有 `flag` → `INVOCATION_INVALID` | — |
| `codex-toml` | `workspace/.codex/config.toml`：`[mcp_servers.factory]`，`command = "python3"`，`args = ["/opt/factory/bin/factoryctl", "mcp"]`，`env = { FACTORY_CTL = "/factory/ctl" }` | 不追加；有 `flag` → `INVOCATION_INVALID` | `CODEX_HOME=/workspace/.codex` |

没有 `invoke.mcp`，或 `format` 是别的值 → `INVOCATION_INVALID`，`detail.reason` 写明。MCP 服务器名固定为 `factory`。

### 4.7 会话的 task 与 contract

task（厂长），`{...}` 为替换值，JSON 用 `canonical_json`：

```text
你是这座工厂的厂长。本次会话处理唤醒票 {wake.id}。
信号：{signal}。对象：{subject}。载荷：{payload}。
/workspace/registry 是 active epoch {epoch} 的 Registry 克隆。
用 MCP 服务器 factory 的工具查询工厂。需要改结构时：在 /workspace/registry 提交 git commit，然后调用 changeset_propose，base_epoch 填 {epoch}，commit 填你的提交。
处理完就退出。
```

task（复核者）：

```text
你是复核者。ChangeSet {subject} 等待复核。
用 changeset_status、registry_diff 查看它，然后调用 review_submit，verdict 为 approve 或 deny，note 写理由。
复核完就退出。
```

contract（两种角色相同）：

```text
工厂契约：
1. 只通过 MCP 服务器 factory 操作工厂。没有合入、推进、回滚的工具，不要尝试绕过。
2. 不要修改 constitution/ 下的文件。宪法只由人修改。
3. 不要读取或输出密钥。
4. 你的身份由会话决定，参数里不要冒充别人。
```

### 4.8 会话的环境变量

在 `Invocation.env` 之上，按顺序合并（后者覆盖前者）：`config.env`，然后：

| 变量 | 值 |
|---|---|
| `FACTORY_ACTOR` | 名字@版本 |
| `FACTORY_ROLE` | `foreman` / `reviewer` |
| `FACTORY_JOB_ID` | 会话 id |
| `FACTORY_ATTEMPT` | `wake.attempts + 1` |
| `FACTORY_WAKE` | 票 id |
| `GIT_AUTHOR_NAME`、`GIT_COMMITTER_NAME` | 名字@版本 |
| `GIT_AUTHOR_EMAIL`、`GIT_COMMITTER_EMAIL` | `<会话 id>@foreman.invalid` |
| `GIT_CONFIG_COUNT`、`GIT_CONFIG_KEY_0`、`GIT_CONFIG_VALUE_0` | `1`、`safe.directory`、`/workspace/registry`：容器用户（10001）与克隆的属主不同，git 默认拒绝在别人的仓库里提交 |
| `CODEX_HOME` | 仅 `codex-toml` |
| `HTTP_PROXY` 等 | 仅 `network == "egress"`，按 `egress.md` 5.6 |

### 4.9 事件

| kind | data |
|---|---|
| `foreman.session.started` | `{session, wake, role, actor, epoch}` |
| `foreman.session.finished` | `{session, state, exit_code, cost_usd, proposed, reviewed, code}` |
| `foreman.wake.reopened` | `{wake, attempts, not_before}` |
| `foreman.wake.failed` | `{wake, code}` |
| `foreman.budget_exhausted` | `{spent_usd, daily_budget_usd}` |
| `conversion.reopened` | `{id, changeset}` |

---

## 5. 行为

### 5.1 `run_once`

1. **日预算**：最近 24 小时（`started_at >= now - 24h`）会话的 `cost_usd` 之和 `>= daily_budget_usd` → 返回 None，不取票。从“可用”变为“用尽”的那一次写 `foreman.budget_exhausted`；持续用尽时不重复写。
2. `wake = WakeQueue.next(by="supervisor")`。None → 返回 None。
3. 角色：`signal == "review"` 为复核者，其余为厂长。
4. `epoch = epochs.current()`，`snapshot = epochs.materialize(epoch)`。
5. 选 runtime：
   - 厂长：`config.runtime`。
   - 复核者：`cs = pipeline.get(wake.subject)`。若 `cs.state` 不在 `verified`、`shadow`、`canary`，或 `len(approvals) >= reviews_required`：票 `done`，返回 None。否则按 5.2 选。没有可选的 → `WakeQueue.fail(code="REVIEWERS_EXHAUSTED")`，返回 None。
   - `build_runtime_invocation(snapshot, runtime, ...)` 与 MCP 配置（4.6）失败：不启动沙箱，记一条 `failed` 会话（`error` 为该错误），按 5.5 的失败处理票。
6. 插入 `running` 会话行，写 `foreman.session.started`，`register_job_cap(db, session_id, "foreman", config.session_budget_usd)`。
7. 建目录（4.3）。依次执行：`git clone --quiet --no-hardlinks <governance.registry> workspace/registry`；`git -C workspace/registry fetch --quiet <governance.registry> "+refs/factory/*:refs/factory/*" "+refs/changesets/*:refs/changesets/*"`；`git -C workspace/registry checkout --quiet --detach <epoch>`。推广不移动 `main`（`governance.md` 5.10），epoch 提交只在 `refs/factory/*` 与 `refs/changesets/*` 上可达，所以必须先取这两组引用再检出；复核者也靠它们看到候选提交。每条 git 命令的超时是 `governance.git_timeout_s`。POSIX 宿主上，检出之后把 `workspace/` 递归改成目录 `0o777`、文件 `0o666`（保留原有的执行位），让沙箱用户能写；Windows 宿主不改。
8. 写 `in/wake.json`、`in/brief.md`、`ctl/job.json`、MCP 配置。
9. `server = McpServer(ctx, epochs, Session(role, actor, wake_id, session_id, source_repo=workspace/registry), pipeline=pipeline)`；`ctl_server = CtlServer(ctl, McpCtlHandler(server), clock)`；`ctl_server.start(interval_s=0.05)`。
10. `SandboxSpec(name=f"mao-{session_id}-1", image=config.image, mounts=4.3, workdir="/workspace/registry", env=4.8, network=config.network)`。runtime 有 `inject.volume` 时同 `runner.md` 第 5 步准备注入卷。`provider.open(spec)`，`sandbox.exec(invocation.argv, timeout_s=config.session_timeout_s, stdout=logs/transcript.raw, stderr=logs/stderr.raw)`。
11. 无论成功与否（`finally`）：`ctl_server.stop()`，关闭沙箱。
12. 结果判定（5.5），打码归档（5.9），更新会话行，写 `foreman.session.finished`，处理票。返回记录。

一次 `run_once` 只跑一个会话。会话串行：两个厂长同时改 Registry 没有意义，而 ChangeSet 的并发上限已经由治理限制（`concurrent_rollouts`）。

### 5.2 选复核者

按 `config.reviewers` 的顺序，取第一个满足下列全部条件的：

1. 在当前 active 快照里能解析为 agent-cli runtime，且能解析为候选 epoch（`cs.commit`）里的 OperatorRuntime（`add_review` 用候选快照校验，`foreman.md` 5.3 第 2 条）；
2. 解析出的 Spec 键不等于 `cs.proposer_runtime`；
3. 不在 `pipeline.reviews(cs.id)` 已有的 runtime 里（同一 runtime 再复核只会覆盖，不增加条数）。

模型取 `config.models[引用]`。

### 5.3 命令行与 MCP

argv 是 `build_runtime_invocation` 的结果，按 4.6 追加 MCP 参数。会话不经过 shell。值守不解析 CLI 的私有参数，也不按 runtime 名字写特例：差异只来自 runtime 文档的 `invoke` 与 `invoke.mcp`。

### 5.4 取回提交

`changeset_propose` 与 `registry_check` 在 `session.source_repo` 不为空时，先执行：

```
git -c safe.directory=<source_repo> -C <governance.registry> fetch --no-tags --quiet <source_repo> \
    "+refs/heads/*:refs/foreman/<session_id>/heads/*" "+HEAD:refs/foreman/<session_id>/HEAD"
```

`safe.directory`：会话里的提交由容器用户写入，克隆里的对象可能属于另一个 uid，宿主的 git 默认拒绝从中读取。

失败 → 工具结果 `isError`，`code` 为 `COMMIT_NOT_FOUND`，`detail.stderr` 打码后给出。之后按原样调用 `pipeline.propose`：它会检查 commit 存在、以 active 为祖先、不碰 `constitution/`（`governance.md` 5.4）。值守不自己合并、不推送 `main`。

`refs/foreman/*` 只是让提交可达，不是 epoch。I11 只检查 `main` 与 `refs/factory/*`。

### 5.5 结果判定与票的处理

先判定会话状态：

| 条件 | 会话 `state` | `error.code` |
|---|---|---|
| `exec` 超时 | `timeout` | `SESSION_TIMEOUT` |
| `budget_stops` 里有该会话 id 的行 | `failed` | 该行的码（`egress.md` 5.5 第 10 条） |
| 退出码不为 0，或 transcript 摘要 `is_error` | `failed` | `SESSION_FAILED`，`detail.exit_code` |
| 厂长、`signal == "conversion"`，`server.outcome["proposed"]` 为空 | `failed` | `SESSION_NO_OUTCOME` |
| 复核者，`wake.subject` 不在 `server.outcome["reviewed"]` 里 | `failed` | `SESSION_NO_OUTCOME` |
| 其余 | `ok` | — |

`cost_usd`：模型代理有该会话的用量时取 `ModelProxy.spent(session_id)` 的口径（`model_usage` 之和），否则取 transcript 摘要的 `cost_usd`。`turns` 取摘要。

再处理票：

- **`ok`，厂长**：`signal == "conversion"` 且需求仍是 `open` → `ConversionStore.mark(subject, "in_progress", changeset_id=proposed[-1])`。票 `done`。
- **`ok`，复核者**：重新读 ChangeSet。仍在复核阶段且 `len(approvals) < reviews_required`：按 5.2 还有可选的 runtime → `reopen(not_before=None, failed=False)`，下一次 `run_once` 换下一个复核者；没有 → `fail(code="REVIEWERS_EXHAUSTED")`。否则票 `done`。
- **`failed` / `timeout`**：`attempts + 1 >= max_attempts` → `fail(code=error.code)`；否则 `reopen(not_before=now + backoff_s × 2^attempts, failed=True)`，其中 `attempts` 是重开之前的值。

`deny` 也是一次成功的复核：复核者做完了它的工作。是否推进由治理决定（`foreman.md` 5.3 第 5 条）。

### 5.6 转换需求的生产者

`file_conversions(ctx, diags, resolutions, *, found_at, run_id=None)`：

1. 对 `resolutions` 中每个 `status == "no-converter"` 的条目，若 `diags` 里有 `code == "E-NO-CONVERTER"` 且 `subject == f"{line}/steps.{step}.in.{port}"` 的诊断（只有 `auto` 绑定会生成需求，`none` 绑定是 `E-TYPE-MISMATCH`）：
2. `ConversionStore.open_or_merge(from_type=r["from"], guarantees=r["guarantees"], to_type=r["to"], requires=r["requires"], found_at={**found_at, "line": r["line"], "step": r["step"], "port": r["port"]}, run_id=run_id)`。
3. 返回涉及的需求 id。

调用点：

| 调用方 | 时机 | `found_at` |
|---|---|---|
| `ChangePipeline`（`governance.md` 5.7 第 1 步） | `epochs.build` 抛 `REGISTRY_INVALID`、ChangeSet 将被 `rejected` 之前；用该错误的 `detail.diags` 与 `detail.resolutions` | `{"changeset": cs_id}` |
| MCP `registry_check` | 5.7 | `{"commit": 40 位, "by": session.actor}` |
| `compile_plan`（`dynamic.md` 5.x，已有规定） | Plan 的 V2 | 按 `dynamic.md` |

`EpochStore.build` 抛出的 `REGISTRY_INVALID` 的 `detail` 增补 `resolutions`：同一次编译的解析结果（本任务修改）。

`resolutions` 与 `diags` 都来自同一次编译（`mrl` 的 factory check 结果，`check.py` 的 `CheckResult.resolutions` 已带 `from`、`guarantees`、`to`、`requires`）。不要从诊断消息文本里解析类型名。

### 5.7 `registry_check`

1. 取回提交（5.4），`git cat-file -e <commit>^{commit}`，失败 → `COMMIT_NOT_FOUND`。
2. 把该提交检出到临时目录（与 `EpochStore.materialize` 同样的方式，但不登记为 epoch），用与 `ChangePipeline` verifying 第 V1/V2 步相同的编译调用。
3. 有错误诊断时调用 `file_conversions(..., found_at={"commit": commit, "by": session.actor})`。
4. 结果：`{"ok": 没有 error 级诊断, "diags": [{code, severity, subject, message, file}], "conversions": [需求 id]}`。诊断按编译器的顺序。
5. 不创建 ChangeSet，不写 epoch，不改 `main`。

这是 13.2 第 3 步“检查草稿”的操作面。

### 5.8 `start` 与恢复

1. `LeaseStore.acquire("supervisor", owner=ctx.owner)`。失败 → `SUPERVISOR_ALREADY_RUNNING`，`detail` 含当前 owner 与到期时间。启动 `LeaseKeeper`。
2. 状态为 `running` 的会话：`provider.reap(session_id, 1)`；会话 `failed`，`error.code = "CONTROL_PLANE_RESTART"`；它的票若为 `taken`，按 5.5 的失败规则处理。
3. 其余 `taken` 的票（取了票、还没插会话行就崩溃）：`reopen(not_before=None, failed=False)`。
4. 返回第 2 步的会话 id。

`serve(stop, idle_s)`：`start` 已调用的前提下循环 `run_once`；返回 None 时 `stop.wait(idle_s)`。`LeaseKeeper.lost` 被置位 → 不再取票，返回。`shutdown`：停 keeper，释放租约。正在跑的会话由 `serve` 的调用方等它结束（`run_once` 不会被中途打断）。

### 5.9 打码与归档

1. `transcript.raw` 逐行 `redact(ctx, snapshot, line)` 写入 `logs/transcript.jsonl`；`stderr.raw` 同样写入 `logs/stderr.txt`。
2. 归档文件里仍能找到已知密钥值（`foreman.md` 5.2 第 3 条的比较）→ 不写归档，会话 `failed`，`SECRET_LEAK`。
3. 两个 `.raw` 文件在归档后删除。
4. `parse_transcript` 读打码前的原文件（打码不应改变 JSON 结构，但不冒这个险）。

### 5.10 egress 的增补

`ModelProxy` 识别 `X-Factory-Job` 时，除了 `egress.md` 5.4 的三种 job 状态，`foreman_sessions` 中 `state == "running"` 的会话 id 也放行。预算上限照样只读 `model_caps`（第 5.1 第 6 步登记）。会话结束后其 id 不再被放行。

### 5.11 信号泵的增补（`daemon.md` 5.5）

在原表之前加一步：`ConversionStore.list(status="in_progress")` 中，`changeset_id` 对应的 ChangeSet 状态为 `rejected`、`withdrawn`、`rolled-back` 的需求 → `ConversionStore.reopen`。之后它重新是 `open`，`updated_at` 变化，原表第一行据 `since` 为它开新票。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `SUPERVISOR_CONFIG_MISSING` | `factory.json` 没有 `foreman` 段 | `path` |
| `SUPERVISOR_CONFIG_INVALID` | 4.1 的任一规则；`supervise` 命令没有治理配置 | `field` |
| `SUPERVISOR_ALREADY_RUNNING` | `supervisor` 租约被别的 owner 持有 | `owner, expires_at` |
| `SUPERVISOR_NOT_STARTED` | `start` 之前调用 `run_once` | — |
| `SESSION_TIMEOUT` | 会话超过 `session_timeout_s` | `timeout_s` |
| `SESSION_FAILED` | CLI 退出码不为 0，或 transcript 报错 | `exit_code` |
| `SESSION_NO_OUTCOME` | 转换票的会话没有提议；复核票的会话没有复核该 ChangeSet | `signal, subject` |
| `REVIEWERS_EXHAUSTED` | 没有满足 5.2 的复核者 | `changeset, tried` |
| `WAKE_STATE_INVALID` | `reopen` / `fail` / `retry` 的前置状态不对 | `wake, status` |
| `WAKE_PAYLOAD_INVALID` | `WakeQueue.file` 的载荷不是对象，或有长度超过 500 的字符串值（`foreman.md` 5.1 第 2 条没有给码；实现借用了 `WAKE_NOT_FOUND`，见 `plan/drift.md` D-26） | `signal, subject, key` |

会话级的码写在会话行与 `foreman.session.finished` 里，不从 `run_once` 抛出。`run_once` 只对本进程的环境问题抛错（数据库、租约丢失、目录不可写）。

---

## 7. 验收测试

治理夹具 `tests/acceptance/m5/auto_world.py`：在 `gov_world` 的基础上，给 `fake-agent@1` 加 `invoke.mcp: {format: claude-json, flag: --mcp-config}`，新增 `fake-reviewer@1`（同一个 fake agent，名字不同），写 `factory.json` 的 `foreman` 段。只改测试自己的副本，不改 `tests/fixtures/registry-m2`。`LocalProvider`、`FakeClock`、`CounterIds`，治理流水线注入通过型的契约与影子替身（缺省实现由 M5-01 覆盖）。

fake agent（`tests/fakes/fake_agent.py`，`testing.md` 4.1）为本任务新增：`run` 动作（运行命令）、`mcp` 动作（经 `factoryctl mcp` 调用 MCP 工具）、按 `FACTORY_ROLE` 选择动作的 `roles`、参数里的 `@file:` / `@json:` 替换。说明在该文件的文档字符串里。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m5/test_m5_06_supervisor.py` | `test_config_required` | 没有 `foreman` 段 → `SUPERVISOR_CONFIG_MISSING`；缺 `image` → `SUPERVISOR_CONFIG_INVALID`，`field == "image"`；`env` 里有 `FACTORY_ACTOR` → `SUPERVISOR_CONFIG_INVALID` |
| | `test_single_instance` | 第二个值守 `start` → `SUPERVISOR_ALREADY_RUNNING`；第一个 `shutdown` 后可以启动；没有票时 `run_once` 返回 None，没有会话 |
| | `test_runtime_invocation` | `build_runtime_invocation` 渲染 fake-agent 的 argv 与 env；`script@1` → `INVOCATION_INVALID`；模型不在名单 → `MODEL_NOT_ALLOWED` |
| | `test_foreman_session_proposes` | 转换票：厂长会话在克隆里提交并经 MCP 提议；ChangeSet 的 `proposer` 是 `fake-agent@1`，提出者提交的作者也是；需求变为 `in_progress` 并记下 ChangeSet；票 `done`；brief 含票 id 与 epoch；transcript 已归档 |
| | `test_failure_backoff_then_give_up` | 第 1 次失败 → 票重开、`attempts == 1`、退避期内 `run_once` 返回 None；时钟越过退避后第 2 次也失败 → 票 `failed`；同一 `(signal, subject)` 再 `file` 得到原票 |
| | `test_conversion_needs_proposal` | 转换票的会话正常退出但没有提议 → 会话 `failed`、`SESSION_NO_OUTCOME`，票重开，需求仍是 `open` |
| | `test_reviewer_session` | 复核票：跳过与提出者相同的 runtime，启动 `fake-reviewer@1`；复核计入 `approvals`；票 `done` |
| | `test_reviewers_exhausted` | 复核者名单里只有提出者的 runtime → 不启动会话，票 `failed`，`REVIEWERS_EXHAUSTED` |
| | `test_daily_budget` | 一次会话花掉超过日预算后，下一张票不被取走，`foreman.budget_exhausted` 只写一次 |
| | `test_restart_recovers_session` | 残留的 `running` 会话在 `start` 时变为 `failed` / `CONTROL_PLANE_RESTART`，它的票重开且 `attempts == 1` |
| | `test_transcript_redacted` | transcript 里的 `api_key=...` 在归档中是 `[REDACTED]`，原始文件不保留 |
| | `test_no_converter_files_request` | MCP `registry_check` 对缺转换的草稿返回 `E-NO-CONVERTER` 并开出转换需求；同一草稿走 `propose` / `advance` 被拒后合并进同一张需求 |
| | `test_model_proxy_accepts_running_session` | 模型代理放行 `X-Factory-Job` 为 `running` 会话 id 的请求并计量；会话结束后同一 id 得到 403（5.10） |
| | `test_cli_sessions_and_reopen` | `factory foreman sessions --state failed` 列出失败的会话；`factory foreman reopen` 把放弃的票改回 `open`、`attempts` 归零；对 `open` 的票再 `reopen` 退出码 1 |

```
uv run python tools/harness.py pytest tests/acceptance/m5/test_m5_06_supervisor.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 14`。

---

## 8. 错误示范与正确示范

```python
# ✖ 按 runtime 名字拼命令行
if runtime.startswith("claude"): argv += ["--mcp-config", path]
# ✔ 只看 runtime 文档的 invoke.mcp
argv += [mcp["flag"], "/factory/ctl/mcp.json"] if mcp["format"] == "claude-json" else []
```

```python
# ✖ 会话退出码为 0 就算处理完，转换票关掉，需求永远没人管
wake_queue.done(wake.id, by="supervisor")
# ✔ 转换票必须留下提议；否则算失败并退避重开
if not server.outcome["proposed"]: fail_or_reopen(wake, "SESSION_NO_OUTCOME")
```

```python
# ✖ 读 transcript 里的“我已经修好了”决定关票
if "已修复" in transcript: ...
# ✔ 只看退出码和 MCP 上留下的动作
```

```python
# ✖ 复核者随便挑一个，结果和提出者是同一个 runtime
reviewer = config.reviewers[0]
# ✔ 按 5.2 跳过提出者与已复核过的 runtime
```

```python
# ✖ 失败就立刻再跑，一个坏掉的上游可以在一小时里烧掉日预算
wake_queue.reopen(wake.id, not_before=None, failed=True, by="supervisor")
# ✔ 指数退避，超过 max_attempts 放弃，等人处理
```

---

## 9. 风险与注意事项

- **成本**：每张票都会启动一次真实的 agent。日预算是最后的闸门；`since` 去重（`daemon.md` 5.5）与放弃语义防止同一问题反复唤醒。
- **持久的发现**：调和器每轮审计都可能为同一条持续存在的发现写票（`since` 为 None）。票 `done` 之后下一轮会再开。这是有意的：问题没解决就应该再看。代价由日预算限制。
- **克隆的大小**：每次会话一份完整克隆。Registry 是文本，通常很小；超过时改用 `--filter=blob:none` 需要人改本文件。
- **提示注入**：唤醒票的载荷只有 id 和错误码（`foreman.md` 5.1 第 2 条），不把外部物料的原文放进 task。厂长要看内容时经 MCP 查询，结果已打码。
- **Windows**：会话目录用 `pathlib`；git 与 CLI 都以参数列表调用，禁止 `shell=True`。`LocalProvider` 下 `/workspace` 被替换成宿主路径，git 接受混合斜杠。
- **并发**：值守与 `factory serve` 是两个租约、两个 Context。不要把 `Context` 传给别的线程（`daemon.md` 5.7）。
