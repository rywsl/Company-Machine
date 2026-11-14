# agents：常驻 Agent（M13）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M13-01 至 M13-05**。设计说明见 `plan/design-resident.md` D-16 至 D-24。
> 现在能长期运转的只有“重复触发”：每张唤醒票起一次新的厂长会话，处理完退出，下一张票不继承对话（`supervisor.md` 5.1）。长期收集信息、接收不定期消息这类工作被打散成许多次会话，状态散落在系统里，每次再拼成新会话的上下文。本文件让 agent 常驻：容器长期存活，对话由 CLI 自己保存并在每批消息到来时续接，上下文在 agent 身上。
> 依赖 M12：常驻沙箱沿用 `instances.md` 的做法，入站路由沿用 `ingress.md`。

---

## 1. 目的与范围

**做什么**

- Registry 新 kind `Agent`：职责、runtime、策略、订阅、可启动的产线、预算、上下文规则。
- 邮箱：定时、入站、新 Bean、收件箱回答、人或别的 Agent 发来的消息、自己预约的提醒、（厂长）唤醒票。
- 轮次：常驻沙箱、续接对话（`invoke.resume`）、预算、超时、失败重试与丢弃、恢复。
- 上下文轮换：对话太长时先交接再开新对话。
- Agent 的 MCP 身份与工具。
- 厂长常驻模式（`foreman.mode = "resident"`）。
- 命令行 `factory agent`、控制台页面。

**不做什么**

| 不做 | 原因 |
|---|---|
| 把 CLI 进程本身常驻、经 stdin 喂消息 | 用户选了“常驻容器 + 续接会话”（设计文档第 0 章）。进程每轮退出，对话在 CLI 的会话存储里 |
| 主循环调用模型 | `factory serve` 仍然确定。模型只在 Agent 沙箱的 CLI 里运行，经模型代理计费 |
| 复核者常驻 | 复核要求独立的一次性判断，仍按 `supervisor.md` |
| Agent 之间共享对话 | 每个 Agent 一段对话；要协作就发消息 |
| 多机调度 Agent | 单机 |

---

## 2. 依赖

| 依赖 | 用途 |
|---|---|
| `Snapshot`、`mrl.factory`（`snapshot.md`） | `Agent` kind 的加载、引用检查 |
| `build_runtime_invocation`（`supervisor.md` 3.2） | 首轮的 argv；本文件增补续接 |
| `parse_transcript`（`runtimes.md`） | 会话 id、成本、轮数；本文件增补 `input_tokens` |
| `SandboxProvider`（`sandbox.md`、`instances.md` 3.1） | 常驻沙箱 |
| `McpServer`、`Session`（`mcp.md`）、`CtlServer` | Agent 的操作面 |
| `register_job_cap`、`ModelProxy.spent`（`egress.md`） | 每轮预算 |
| `Inbox`（`inbox.md`） | 向人提问、丢弃通知 |
| `IngressServer`、`routes`（`ingress.md`） | `deliver: agent` |
| `WakeQueue`（`foreman.md`、`supervisor.md`） | 厂长常驻模式 |
| `LineRunner.start_run`（`lines.md`） | `run_start` |

---

## 3. 接口

### 3.1 `Agent` kind

`agents/<名>.yaml`，schema `spec/schemas/agent.schema.json`：

```yaml
apiVersion: mao/v1
kind: Agent
metadata: {name: xhs-ops, version: 1}
spec:
  runtime: claude-code@1            # agent-cli 的 OperatorRuntime，必须有 invoke.resume
  model: claude-sonnet-5-5          # 可选；runtime 有 models_allowed 时必须在其中
  charter: |                        # 职责说明，非空，最多 8000 字符
    你负责小红书账号的日常运营……
  policy: agent-web@1               # 沙箱的出网与秘密
  lines: [note-pipeline@1]          # 可以经 run_start 启动的产线，可为空
  subscribe:
    schedule:
      - {every: 1h, note: 整理今天收集到的笔记}
    beans:
      - {type: note.text@1, label: "source=xhs"}     # label 可选，写成 K=V
    ingress:
      xhs-dm: {auth: {kind: hmac-sha256, secret: XHS_DM_SECRET, header: X-Signature}}
  budget: {daily_usd: 5.0, turn_usd: 0.5, turn_timeout: 15m}
  context: {batch: 20, rotate_turns: 200, rotate_tokens: 150000, max_turns: 50}
```

- 必填：`runtime`、`charter`、`policy`、`budget`。schema 只检查结构，数值与引用的语义由 `agent_config` 检查。
- 缺省：`subscribe` 各项为空；`lines` 为空；`context.batch = 20`、`rotate_turns = 200`、`rotate_tokens = 150000`、`max_turns = 50`。
- `schedule[].every` 匹配 `^[0-9]+(s|m|h|d)$`，至少 60 秒。
- Agent 的 `subscribe.ingress` 路由与工位路由共用一个命名空间（`ingress.md` 4.1 的重名规则同样适用），投递方式固定为 `agent`。

`mrl.factory` 的增补：

- `registry.py`：`agents/*.yaml` → kind `Agent`；
- `schema.py`：`KIND_SCHEMA["Agent"] = "agent.schema.json"`，`REGISTRY_KINDS` 增加 `"Agent"`；
- `extract.py`：`spec.runtime` → `ref(..., "OperatorRuntime", ...)`；`spec.policy` → `Policy`；`spec.lines[i]` → `Line`；`subscribe.beans[i].type` → `Type`。悬空引用因此由现有的引用规则报出。

运行期再校验（`agent_config`，4.1），不改 MRL 规则。

### 3.2 `mao.agents`

```python
@dataclass(frozen=True)
class AgentConfig:
    name: str
    key: str                               # Spec 键
    runtime: str                           # OperatorRuntime 键
    model: str
    charter: str
    policy: str
    lines: tuple[str, ...]                 # Line 键
    schedule: tuple[tuple[float, str], ...]   # (every 秒, note)
    beans: tuple[tuple[str, str | None], ...] # (Type 键, "K=V" 或 None)
    daily_usd: float
    turn_usd: float
    turn_timeout_s: float
    batch: int
    rotate_turns: int
    rotate_tokens: int
    max_turns: int
    digest: str                            # sha256(canonical_json(spec)) 的十六进制

def agent_config(snapshot: Snapshot, name: str, *, spec: Mapping[str, Any] | None = None) -> AgentConfig
    # 4.1；违反 → AGENT_INVALID。spec 不为 None 时代替文档里的 spec 做校验（测试与 registry_check 用）
def foreman_agent(snapshot: Snapshot, foreman: SupervisorConfig) -> AgentConfig   # 5.8；key 与 policy 为 ""
def resident_foreman(root: Path) -> SupervisorConfig | None
    # factory.json 的 foreman 段存在且 mode == "resident" → SupervisorConfig.load(root)；否则 None
def agent_names(snapshot: Snapshot, root: Path) -> list[str]   # 5.1，升序；常驻模式含 "foreman"

def agent_status(ctx: Context, name: str, *, detail: bool = False) -> dict[str, Any]
    # {name, state, session, pending, conversation}；detail 时再加 turns_in_conv、rotate_pending、
    # budget_exhausted（布尔）与 turns（最近 20 轮的 to_json）。没有 agents 行时按插入时的缺省值给出
def set_agent_state(ctx: Context, name: str, state: str) -> None    # "active" | "paused"；没有行就插入
def request_rotation(ctx: Context, name: str) -> None               # rotate_pending = 1；没有行就插入
def list_turns(ctx: Context, name: str) -> list[TurnRecord]         # started_at、id 升序
def agent_tool(ctx: Context, epochs: EpochStore, agent: str, name: str,
               arguments: Mapping[str, Any]) -> Any                 # 3.4 的四个工具；McpServer 转给它

@dataclass(frozen=True)
class Message:
    id: str                                # "am-..."
    agent: str                             # Agent 名
    kind: str                              # schedule|ingress|bean|inbox|message|remind|wake
    source: str                            # 例如 "schedule:0"、"ingress:xhs-dm"、"human:alice"、"agent:other"
    body: dict[str, Any]                   # 写入时已打码
    state: str                             # pending|delivered|done|dropped
    turn: str | None
    attempts: int
    not_before: str | None
    created_at: str
    def to_json(self) -> dict[str, Any]    # 以上字段

class Mailbox:
    def __init__(self, ctx: Context) -> None
    def post(self, agent: str, kind: str, source: str, body: Mapping[str, Any], *,
             not_before: str | None = None) -> Message          # 5.2
    def list(self, agent: str, *, state: str | None = None) -> list[Message]   # id 升序

@dataclass(frozen=True)
class TurnRecord:
    id: str                                # "at-..."
    agent: str
    conversation: int                      # 从 1 开始
    number: int                            # 对话内已成功的工作轮数 + 1；失败的轮次重试时沿用同一编号
    kind: str                              # "work" | "handoff"
    session: str | None                    # CLI 会话 id（本轮结束后的）
    state: str                             # running|ok|failed|timeout|interrupted
    messages: tuple[str, ...]
    cost_usd: float
    input_tokens: int
    exit_code: int | None
    error: dict[str, Any] | None
    started_at: str
    finished_at: str | None
    def to_json(self) -> dict[str, Any]    # 以上字段

@dataclass(frozen=True)
class AgentsConfig:                        # factory.json 的 agents 段（4.4）
    image: str
    network: str = "none"
    poll_s: float = 5.0
    env: Mapping[str, str] = field(default_factory=dict)   # 额外传给每轮 CLI 的非秘密环境变量

    @classmethod
    def load(cls, root: Path) -> "AgentsConfig"   # 段缺失 → AGENTS_CONFIG_MISSING；不合法 → AGENTS_CONFIG_INVALID

class AgentHost:
    def __init__(self, ctx: Context, epochs: EpochStore, config: AgentsConfig, *,
                 pipeline: ChangePipeline | None = None, foreman: SupervisorConfig | None = None) -> None
        # foreman 为 None 时用 resident_foreman(ctx.root)；给出但 mode 不是 "resident" 时当作没有常驻厂长。
        # pipeline 为 None 时 ChangePipeline(ctx, epochs)
    def collect(self) -> list[str]             # 5.2：处理订阅与唤醒票，返回新消息 id，升序
    def run_once(self, name: str) -> TurnRecord | None   # 5.3：为一个 Agent 跑至多一轮
    def tick(self) -> list[str]                # collect，然后对每个有待处理消息的 Agent（名升序）run_once；返回轮次 id
    def recover(self) -> list[str]             # 5.5
    def serve(self, stop: threading.Event) -> None
        # 取租约 agents（取不到 → AGENTS_ALREADY_RUNNING，由 LeaseKeeper 续约）；recover；
        # 循环 tick 与 stop.wait(poll_s)，tick 抛出的异常记日志后继续；退出时释放租约并 close()
    def pause(self, name: str) -> None
    def resume(self, name: str) -> None
    def rotate(self, name: str) -> None        # 下一轮之前先轮换（5.6）
    def turns(self, name: str) -> list[TurnRecord]
    def max_turn_timeout_s(self) -> float      # 当前快照里有效 Agent 的 turn_timeout_s 最大值，没有为 0
    def close(self) -> None                    # 放下本对象持有的沙箱引用，不关容器（5.4）
```

### 3.3 运行时增补

OperatorRuntime 的 `invoke` 增加可选的 `resume`：参数模板，变量与 `argv` 相同，另有 `{{ session.id }}`。`build_runtime_invocation` 增加关键字参数 `session: str | None = None`：为 `None` 时用 `argv`；否则用 `resume`，`resume` 不存在时报 `INVOCATION_INVALID`（`reason = "resume"`）。

`parse_transcript` 的结果增加 `session_id: str | None`（最后一条带 `session_id` 的事件）与 `input_tokens: int`（`result` 事件 `usage.input_tokens`，没有为 0）。

`spec/runtimes/claude-code.yaml`：`resume: [/opt/operator/bin/claude, --resume, "{{ session.id }}", -p, "{{ brief.task }}", ...]`，其余参数与 `argv` 相同。`codex.yaml`：`[..., exec, resume, "{{ session.id }}", "{{ brief.task }}", ...]`。

### 3.4 MCP

`Session` 增加最后一个字段 `agent: str | None = None`。角色增加 `agent`：`actor` 仍是 OperatorRuntime 引用，`agent` 是 Agent 名；`role == "agent"` 与 `agent` 非空必须同时成立，否则 `MCP_ACTOR_INVALID`。`agent` 角色可以只给 `session_id`（轮次 id）不给 `source_repo`：它没有 Registry 克隆，`mcp.md` 的“会话克隆不完整”检查只对另两个角色做。

`agent` 角色的工具是 `mcp.md` 的共享只读工具加上下表四个；`McpServer` 把它们原样转给 `agent_tool(ctx, epochs, session.agent, 名, arguments)`。输入 schema 不符仍是 JSON-RPC 的 `invalid params`；语义错误是工具错误（`isError`），码见下表。

| 工具 | 参数（必填加粗） | 行为 |
|---|---|---|
| `run_start` | **`line`**、**`inputs`**（端口 → Bean id 或 Bean id 列表） | `line` 解析不了，或解析后不在 `AgentConfig.lines` → `AGENT_LINE_FORBIDDEN`。`inputs` 的值不是字符串或字符串列表 → `AGENT_TOOL_INVALID`（`reason = "inputs"`）。否则 `open_line_runner(ctx, snapshot).start_run(line, inputs, trigger={"on": "agent", "agent": 名})`，返回 `{id, state}`（`state` 为字符串） |
| `ask_human` | **`summary`**；`detail`、`options`（至多 10 个字符串） | `options` 不合法 → `AGENT_TOOL_INVALID`（`reason = "options"`）。`Inbox.open("question", subject=ctx.ids.new("aq"), origin="agent:" + 名, summary=…, payload={"detail", "options"})`，返回事项的 `to_json()`。回答经 5.2 回到邮箱 |
| `remind` | **`after`**（时长）、**`note`** | `after` 不在 60 秒到 30 天之间 → `AGENT_TOOL_INVALID`（`reason = "after"`）；写法不合法 → `DURATION_INVALID`。`post(名, "remind", "agent:" + 名, {"note": note}, not_before=now + after)`，返回消息的 `to_json()` |
| `agent_send` | **`to`**、**`text`** | `to` 不在 `agent_names(snapshot, root)` 里 → `AGENT_NOT_FOUND`。`post(to, "message", "agent:" + 名, {"text": text})`，返回消息的 `to_json()` |

其他工具（`changeset_propose`、`review_submit`、审批、秘密）对 `agent` 角色不存在，调用得到 `unknown tool`。厂长常驻模式下内置 Agent `foreman` 的角色仍是 `foreman`（5.8）。

### 3.5 命令行

```text
factory agent list [--json]                       # [{name, state, session, pending, conversation}]，名升序
factory agent show <名> [--json]                  # list 的字段加 turns_in_conv、rotate_pending、budget_exhausted、turns（最近 20 轮的 to_json）
factory agent send <名> --text T                  # post(名, "message", "human:<FACTORY_ACTOR 或 cli>", {"text": T})，输出 Message.to_json()
factory agent pause <名> | resume <名> | rotate <名>
factory agent turns <名> [--json]
factory agent run-once <名>                       # 一次 collect 加一次 run_once，供人工排查；输出轮次 to_json 或 null
```

`<名>` 不在 `agent_names(snapshot, root)` 里 → `AGENT_NOT_FOUND`，退出 1。`pause`、`resume` 输出 `agent_status(名)`，`rotate` 输出 `agent_status(名, detail=True)`。除 `run-once` 外这些命令不构造 `AgentHost`，没有 `agents` 段也能用。

---

## 4. 数据格式

### 4.1 `agent_config` 的规则

违反任一条 → `AGENT_INVALID`，`detail.reason` 如下：

| 条件 | `reason` |
|---|---|
| active 快照里同名 Agent 不止一个版本 | `versions` |
| `runtime` 解析不了、不是 `agent-cli`，或没有 `invoke.resume` | `runtime` |
| `model` 不在 runtime 的 `models_allowed` 里（runtime 写了它时）；省略 `model` 时取 `models_allowed` 第一项，runtime 也没写 | `model` |
| `policy` 的 `secrets` 是 `from-machine`（Agent 没有机器） | `policy` |
| `charter` 为空或超过 8000 字符 | `charter` |
| `schedule[].every` 不合法或少于 60 秒 | `schedule` |
| `beans[].label` 不是 `K=V` | `label` |
| `budget` 缺 `daily_usd`、`turn_usd`、`turn_timeout`，或不是正数 | `budget` |
| `context` 的数不是正整数 | `context` |

名字不存在 → `AGENT_NOT_FOUND`。

### 4.2 迁移 `src/mao/migrations/040_agents.sql`

```sql
CREATE TABLE agents (
  name            TEXT PRIMARY KEY,
  state           TEXT NOT NULL CHECK (state IN ('active', 'paused')),
  session         TEXT,
  conversation    INTEGER NOT NULL DEFAULT 1,
  turns_in_conv   INTEGER NOT NULL DEFAULT 0,
  last_tokens     INTEGER NOT NULL DEFAULT 0,
  rotate_pending  INTEGER NOT NULL DEFAULT 0,
  budget_exhausted INTEGER NOT NULL DEFAULT 0,
  sandbox         TEXT,                 -- 最近一次 open 的沙箱摘要（5.4）
  updated_at      TEXT NOT NULL
);
CREATE TABLE agent_messages (
  id          TEXT PRIMARY KEY,
  agent       TEXT NOT NULL,
  kind        TEXT NOT NULL,
  source      TEXT NOT NULL,
  body        TEXT NOT NULL,
  state       TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'done', 'dropped')),
  turn        TEXT,
  attempts    INTEGER NOT NULL DEFAULT 0,
  not_before  TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX agent_messages_pending ON agent_messages(agent, state, id);
CREATE TABLE agent_turns (
  id           TEXT PRIMARY KEY,
  agent        TEXT NOT NULL,
  conversation INTEGER NOT NULL,
  number       INTEGER NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('work', 'handoff')),
  session      TEXT,
  state        TEXT NOT NULL CHECK (state IN ('running', 'ok', 'failed', 'timeout', 'interrupted')),
  messages     TEXT NOT NULL,
  cost_usd     REAL NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  exit_code    INTEGER,
  error        TEXT,
  started_at   TEXT NOT NULL,
  finished_at  TEXT
);
CREATE INDEX agent_turns_agent ON agent_turns(agent, started_at);
CREATE TABLE agent_schedule (
  agent    TEXT NOT NULL,
  idx      INTEGER NOT NULL,
  last_at  TEXT NOT NULL,
  PRIMARY KEY (agent, idx)
);
```

`agents` 行在第一次 `post` 或 `run_once` 时以 `state = 'active'` 插入。

### 4.3 目录

`$FACTORY_ROOT/agents/<名>/`：

| 路径 | 挂到 | 说明 |
|---|---|---|
| `workspace/` | `/workspace` | 持久。`notebook/`（打开沙箱时建好）是 Agent 自己的笔记；runtime 的 `HOME` 应在 `/workspace` 之下（`claude-code` 的 runtime 文档已是 `/workspace/.home`），CLI 的会话存储因此跨容器重启保留 |
| `in/` | `/factory/in`（只读） | 每轮开始前清空内容（目录本身不删，它挂在活着的容器里），写 `messages.json`（本轮消息 `to_json()` 的数组） |
| `ctl/` | `/factory/ctl` | MCP 控制通道。每轮开始前清空内容，建 `requests/`、`responses/`，写 `job.json`（同 `supervisor.md` 5.1，`job_id` 为轮次 id、`attempt` 为 1、`station` 为角色）与 runtime 的 MCP 配置（同 `supervisor.md` 5.1：`claude-json` 写 `mcp.json` 并在 argv 末尾加 `flag /factory/ctl/mcp.json`；`codex-toml` 写 `/workspace/.codex/config.toml` 并设 `CODEX_HOME`） |
| `turns/<轮次 id>/` | 不挂 | `transcript.jsonl`、`stderr.txt`（打码后；原始输出 `transcript.raw`、`stderr.raw` 读完即删，同 `supervisor.md` 5.9） |

`SandboxSpec`：名 `mao-agent-<名>`；镜像与网络取 `AgentsConfig`（内置厂长取 `foreman.image`、`foreman.network`），runtime 的 argv、resume 或 env 引用 `${MODEL_PROXY}` 时网络为 `egress`；挂载 `in/`、`ctl/`、`workspace/` 与 `tools/factory` → `/opt/factory`（只读），runtime 有 `inject.volume` 且是 Docker 时同 `supervisor.md` 5.1 准备注入卷；`workdir` 为 `/workspace`（内置厂长 `/workspace/registry`）；`env` 为 `{"FACTORY_AGENT": 名}`；`command` 为空：CLI 每轮经 `exec` 运行。

出网：Agent 的策略经 `build_egress(snapshot, cfg.key, store, station={"policy": 策略引用, "role": "transform", "operator": {"runtime": runtime 引用}})` 得到计划（`build_egress` 增加关键字参数 `station: Mapping | None = None`，给出时代替按键查到的工位 spec）。计划要求出网时，`open` 之前并上计划的 `env`、注入的秘密、代理地址，用 `register_check_egress(db, "agent-<名>", plan, token=…)` 登记凭据并经 `credentialed_env` 写入 `env`；关闭沙箱时 `close_egress(db, "agent-<名>")`。

### 4.4 `factory.json` 的 `agents` 段（人维护）

```json
"agents": {"image": "mao/agent@sha256:...", "network": "none", "poll_s": 5}
```

`image` 是非空字符串；`network` 为 `none` 或 `egress`；`poll_s` 是正数；`env` 可省略，值必须是字符串，键不能以 `FACTORY_`、`GIT_`、`HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY` 开头。违反 → `AGENTS_CONFIG_INVALID`（`field`）。没有这一段时 `factory serve` 不运行 `AgentHost`，Registry 里的 Agent 只收消息不跑轮次。

### 4.5 提示

每轮 `-p` 的文本（`brief.task`）：

```text
你是常驻 agent {名}。以下是自上一轮以来的 {n} 条新消息，按时间排列；完整 JSON 也在 /factory/in/messages.json。
处理完就结束本轮，不要等待新消息。需要以后再看的事，用 remind 预约，或写进 /workspace/notebook/。

[1] {kind} {source} {created_at} {id}
{body 的 canonical JSON}
...
```

对话的第一轮（`session` 为空的工作轮）在上面的文本前加：`{charter}`，以及（`workspace/notebook/HANDOFF.md` 存在时）“上一段对话的交接在 /workspace/notebook/HANDOFF.md，先读它。”，各段之间空一行。`brief.factory_contract` 是固定的规则文本：身份由会话决定、不要冒充别人、只能经 MCP 操作工厂、秘密不进笔记与输出、一轮处理完就结束。

交接轮（5.6）的文本：`请把仍在进行的事项、关键事实和下一步写进 /workspace/notebook/HANDOFF.md（覆盖旧文件），然后结束本轮。`

### 4.6 事件

| kind | data |
|---|---|
| `agent.message` | `{agent, id, kind, source}` |
| `agent.turn.started` | `{agent, turn, kind, conversation, number, messages}` |
| `agent.turn.finished` | `{agent, turn, state, cost_usd, exit_code, code}` |
| `agent.rotated` | `{agent, conversation, ok}` |
| `agent.budget_exhausted` | `{agent, spent_usd, daily_usd}` |
| `agent.dropped` | `{agent, messages, request}`（`request` 是收件箱事项 id） |
| `agent.invalid` | `{agent, code, reason}` |

### 4.7 `tests/fakes/fake_agent.py` 的增补

- 参数 `--resume S`：会话 id 取 `S`；否则仍是 `fake-<FACTORY_JOB_ID>-<FACTORY_ATTEMPT>`。
- 对话历史：每次运行把 `{"task": -p 的文本}` 追加到 `/workspace/.home/.fake-agent/<会话 id>.jsonl`（经 `FACTORY_WORKSPACE` 映射；会话存储在 `/workspace` 下，同 4.3 的 HOME 约定）。动作 `{"do": "history", "path": P}` 把本次运行**之前**的历史（task 列表）写成 JSON 数组到 `P`。
- 选脚本：`roles` 之后、`attempts` 之前，若脚本有 `by_turn` 且环境变量 `FACTORY_TURN` 在其中，用 `by_turn[FACTORY_TURN]`。
- 动作 `{"do": "usage", "input_tokens": N}`：结果事件增加 `"usage": {"input_tokens": N}`。

---

## 5. 行为

### 5.1 哪些 Agent

`agent_names(snapshot, root)` = active 快照中 `metadata.lifecycle` 为 `active`（缺省）的 Agent 名，升序；厂长常驻模式（`resident_foreman(root)` 不为 `None`）另加 `foreman`。`agent_config` 抛错的 Agent：写 `agent.invalid`（同名、同码、同 `reason`、同 epoch 只写一次），不收订阅消息、不跑轮次；已经在邮箱里的消息保留。

### 5.2 邮箱与订阅

`post`：

1. `body` 先经 `SecretStore.redact_obj`，canonical JSON 的 UTF-8 字节数超过 64 KiB → `AGENT_MESSAGE_TOO_LARGE`。
2. 同一事务里：没有 `agents` 行就插入（`state = 'active'`），插入 `pending` 行（`id = ctx.ids.new("am")`），写 `agent.message`。`agent` 不必在快照里（Agent 暂时被删除时消息保留，恢复后照常处理）。

`collect()` 依次：

1. **定时**：对每个有效 Agent 的每条 `schedule[i]`，`agent_schedule` 没有行或 `now >= last_at + every` → `post(名, "schedule", f"schedule:{i}", {"note": note, "every": 原文})`，写或更新 `last_at = now`。第一次没有行就算到期。
2. **新 Bean**：游标 `cursors.name = "agents.beans"`（没有行时为 0），按 `seq` 读之后的 `bean.sealed` 事件。对每个有效 Agent 的每条 `beans[j]`：Bean 的类型（取主版本）等于该 Type 或是其子类型（`snapshot.is_subtype`），且（写了 `label` 时）Bean 当前有这个标签 → `post(名, "bean", f"bean:{j}", {"bean", "type", "labels", "job"})`。读到事件时游标推进到读过的最大 `seq`。
3. **唤醒票**（仅常驻模式，5.8）。

返回本次新消息的 id，升序。

其他来源在发生时直接 `post`：

- 入站：`ingress.md` 的 `deliver: agent`（见该文件 4.1、5.2）：`post(Agent 名, "ingress", f"ingress:{路由}", {"id", "route", "content_type", "text"})`；请求体不能按 UTF-8 解码时不放 `text`，改放 `body_b64`。`post` 抛 `AGENT_MESSAGE_TOO_LARGE` → 413 `INGRESS_TOO_LARGE`。`ingress.received` 的 `target` 是 Agent 名。
- 收件箱：`origin` 以 `agent:` 开头的事项被 `Inbox.close` 关成 `answered`、`cancelled` 或 `expired` 时（`inbox.md` 5.1 的唤醒规则之外再加这一条，不开唤醒票），`post(名, "inbox", "inbox:" + 事项 id, {"id", "kind", "status", "reply"})`。`reply` 是事项存下的回答里的 `choice`、`text`、`note` 三个键（有哪个放哪个；`question` 的回答只存 `choice` 或 `text`），没有回答时为 `{}`。秘密类事项不会由 Agent 开出。
- 人：`factory agent send`、控制台。
- 别的 Agent：`agent_send`；自己：`remind`。

### 5.3 一轮：`run_once(name)`

1. `cfg = agent_config(...)`（内置厂长用 `foreman_agent`）；抛错 → 返回 `None`。没有 `agents` 行就插入。`state == 'paused'` → `None`。
2. **日预算**：`started_at >= now - 24h` 的该 Agent 各轮 `cost_usd` 之和 `>= daily_usd` → 置 `budget_exhausted = 1`，从 0 变 1 时写 `agent.budget_exhausted`；返回 `None`，消息留在邮箱。未用尽时把 `budget_exhausted` 置 0。
3. **取消息**：`pending` 且 `not_before` 为空或 `<= now`，按 `id` 升序取前 `batch` 条。没有 → `None`。
4. `rotate_pending` 为真 → 先做交接轮（5.6），再继续。
5. **沙箱**：5.4。
6. 插入 `running` 轮次（`id = ctx.ids.new("at")`，`number = turns_in_conv + 1`，`session` 为当前会话），消息置 `delivered`、`turn` 为本轮 id，写 `agent.turn.started`，`register_job_cap(db, 轮次 id, "agent", turn_usd)`。按 4.3 准备 `in/` 与 `ctl/`。内置厂长另按 5.8 第 3 步准备克隆。
7. **调用**：`build_runtime_invocation(snapshot, runtime 引用, task=4.5 的文本, contract=4.5 的规则, model=cfg.model, max_turns=cfg.max_turns, budget_usd=cfg.turn_usd, timeout_s=cfg.turn_timeout_s, session=当前会话)`。`exec` 的 `env` 依次合并：`AgentsConfig.env`（内置厂长再并上 `foreman.env`）、调用的 `env`、`FACTORY_JOB_ID=<轮次 id>`（模型代理按它计费）、`FACTORY_ATTEMPT=1`、`FACTORY_TURN=<number>`、`FACTORY_ROLE`（`agent`；内置厂长为 `foreman`）、`FACTORY_AGENT=<名>`、`FACTORY_ACTOR=<runtime 引用>`；内置厂长再加 `supervisor.md` 5.1 的 `GIT_*` 变量（邮箱用轮次 id）。沙箱网络为 `egress` 时并上 `proxy_env`，并用 `expand_control_vars` 代入 `FACTORY_JOB_ID`、`FACTORY_ATTEMPT`、`MODEL_PROXY`、`EGRESS_PROXY`。超时 `turn_timeout_s`，stdout、stderr 写到 `turns/<id>/`。执行期间运行 `CtlServer(agents/<名>/ctl, McpCtlHandler(McpServer(ctx, epochs, Session("agent", runtime 引用, None, 轮次 id, None, agent=名), pipeline=…)))`，`exec` 返回后停止它。
8. **判定**，与 `supervisor.md` 5.5 相同的顺序：超时 → `timeout`（`SESSION_TIMEOUT`）；`budget_stops` 有本轮 → `failed`（该码）；退出码非 0 或摘要 `is_error` → `failed`（`SESSION_FAILED`，`detail.exit_code`）；其余 `ok`。`cost_usd` 取摘要的成本，没有摘要为 0。
9. **`ok`**：本轮消息置 `done`。工作轮另外：`agents.session` = 摘要的 `session_id`（没有时保留旧值），`turns_in_conv + 1`，`last_tokens` = 摘要的 `input_tokens`；`number >= rotate_turns` 或 `input_tokens >= rotate_tokens` → `rotate_pending = 1`。
10. **不是 `ok`**：本轮消息 `attempts + 1`，回到 `pending`、`turn` 置空；`attempts >= 3` 的改为 `dropped`（`turn` 保留），之后 `Inbox.open("question", subject=ctx.ids.new("aq"), origin="agent:" + 名, summary=f"agent {名} 连续 3 轮失败，{k} 条消息已丢弃", payload={"options": ["知道了"], "messages": [...id]})`，写 `agent.dropped`。`agents` 行不变：下一轮仍续接同一段对话，`number` 不变。
11. 打码归档 transcript（4.3），更新轮次行（`session` 为本轮结束后的会话 id：摘要有就取摘要，否则为本轮开始时的会话），写 `agent.turn.finished`，返回记录。

一次 `run_once` 至多跑一个工作轮（之前可能有一个交接轮）。同一 Agent 的轮次串行。`tick` 对不同 Agent 依次调用，`AgentHost` 不并发跑多个 CLI。

### 5.4 沙箱的生命周期

沙箱摘要 = `sha256(canonical_json({"config": cfg.digest, "image": 镜像, "network": 网络}))`。每轮开始前：

1. 本对象持有该 Agent 的沙箱、摘要相同且 `alive()` → 复用。
2. 持有但摘要不同或不活 → `close()` 它并 `close_egress(db, "agent-<名>")`。
3. 本对象没持有过（例如 `factory serve` 刚重启）且 `agents.sandbox` 等于当前摘要 → `provider.attach(spec)`；抛 `FactoryError` 或 `alive()` 为假时当作没有。
4. 仍没有 → 按 4.3 做出网准备后 `provider.open(spec)`，`agents.sandbox` = 当前摘要。

`close()` 只放下本对象持有的引用，不关容器：重启后由第 3 步接上。Agent 从快照里删除、或配置摘要变化时，按上面的规则关旧开新，对话保留（`/workspace` 不动）。`pause`、`rotate` 不关沙箱。

### 5.5 恢复：`recover`

`state = 'running'` 的轮次改为 `interrupted`（`error = {"code": "AGENT_INTERRUPTED", "detail": {"turn": id}}`，`finished_at = now`），其 `delivered` 消息回到 `pending`、`turn` 置空（不加 `attempts`），写 `agent.turn.finished`（`state = "interrupted"`，`code = "AGENT_INTERRUPTED"`）。返回这些轮次 id，升序。`serve` 开始时调用一次。

### 5.6 上下文轮换

交接轮：`kind = "handoff"`，没有消息，`number = turns_in_conv + 1`；提示为 4.5 的交接文本；用当前会话续接（`session` 为空时跳过交接，直接轮换）。它照 5.3 第 6–11 步运行，只是 `ok` 时不改 `agents` 行。之后无论成败：`session = NULL`，`conversation + 1`，`turns_in_conv = 0`，`last_tokens = 0`，`rotate_pending = 0`，写 `agent.rotated`（`conversation` 为新值，`ok` 为交接轮是否 `ok`，跳过交接时为 `true`）。交接轮的成本计入日预算。

新对话的第一轮（`session` 为空的工作轮）用 `invoke.argv`，提示开头带 `charter` 与 HANDOFF 的引用（4.5）。

### 5.7 `factory serve` 的装配

`factory.json` 有 `agents` 段时，`run_serve` 构造 `AgentHost(ctx, daemon.epochs, AgentsConfig.load(root), pipeline=daemon.pipeline)`，另起守护线程 `mao-agents` 运行 `host.serve(stop)`，与 `supervisor.md` 的托管值守相同：共享 `ctx` 与 `stop` 事件，各用各的租约。`daemon.serve` 返回后 `stop.set()`，再 `join` 该线程，至多等 `host.max_turn_timeout_s() + 60` 秒（在启动线程前算好）。`AgentsConfig.load` 抛 `AGENTS_CONFIG_MISSING` 时不起线程；抛其他错误时 `run_serve` 失败。`IngressServer` 投递 `deliver: agent` 时只写邮箱，不需要 `AgentHost`。

### 5.8 厂长常驻模式

`factory.json` 的 `foreman.mode`：`"session"`（缺省，M5 行为不变）或 `"resident"`。`SupervisorConfig` 增加最后一个字段 `mode: str = "session"`，其他值 → `SUPERVISOR_CONFIG_INVALID`（`field = "mode"`）。

常驻模式下：

1. 内置 Agent `foreman`：`foreman_agent(snapshot, foreman)` 从 `foreman` 段构造：`runtime`（解析为键）、`model`、`max_turns` 照搬；`turn_timeout_s = session_timeout_s`，`turn_usd = session_budget_usd`，`daily_usd = daily_budget_usd`；`batch`、`rotate_*` 取缺省；`charter` 是固定的厂长任务书（`supervisor.md` 4.7 的厂长任务书去掉“处理唤醒票 {wake.id}”与具体 epoch）；`digest` 是 `foreman` 段的摘要；runtime 没有 `invoke.resume` → `AGENT_INVALID`（`reason = "runtime"`）。沙箱镜像用 `foreman.image`、网络用 `foreman.network`；不在 Registry 里，不受 ChangeSet 管理。
2. `WakeQueue.next` 增加关键字参数 `signals: tuple[str, ...] | None = None`（只取这些信号；空元组 → `None`）与 `exclude: tuple[str, ...] = ()`（不取这些信号）。`ForemanSupervisor.run_once` 在常驻模式下用 `signals=("review",)`；`AgentHost.collect` 第 3 步循环 `next(by="agents", exclude=("review",))`，把每张票作为 `post("foreman", "wake", "wake:" + id, {"wake": 票的全部字段})`，票保持 `taken`。`ForemanSupervisor` 的恢复与会话异常处理只重开 `by` 不是 `"agents"` 的 `taken` 票：`agents` 取走的票由邮箱负责。
3. 厂长 Agent 的 MCP 角色是 `foreman`（工具与一次性会话相同），`Session("foreman", runtime 引用, None, 轮次 id, agents/foreman/workspace/registry)`。克隆：`.git` 不存在时按 `supervisor.md` 5.1 第 7 步克隆、拉取 `refs/factory/*` 与 `refs/changesets/*` 并检出 active；已存在时每轮开始前只做这次 `fetch`，不检出，保留厂长未提交的工作。
4. 一轮结束后，对本轮里每条 `wake` 消息（票仍为 `taken` 时）：
   - 本轮 `ok`：`signal == "conversion"` 且 `server.outcome["proposed"]` 为空 → 失败处理，码 `SESSION_NO_OUTCOME`；`signal == "approval"` 且这条审批仍待厂长决定 → 失败处理，码 `APPROVAL_UNDECIDED`；其余 → 票 `done`（`by = "agents"`；转换票先照 `supervisor.md` 5.5 把转换需求标为 `in_progress`）。失败处理同 `supervisor.md` 5.5：`attempts + 1 >= max_attempts` → `fail`，否则 `reopen`（`failed=True`，`not_before = now + backoff_s × 2^attempts`）。
   - 本轮不是 `ok`：票保持 `taken`，消息照 5.3 重试；消息被丢弃时票 `fail`，码为本轮的错误码。
5. 复核票仍由 `ForemanSupervisor` 起一次性会话。

---

## 6. 错误

新码都要登记到 `mao.errors`。

| 码 | 何时 | detail |
|---|---|---|
| `AGENT_INVALID` | 4.1；内置厂长的 runtime 没有 `invoke.resume` | `agent, reason` |
| `AGENT_NOT_FOUND` | 名字不在 `agent_names` 里（`agent_config`、命令行、`agent_send`） | `agent` |
| `AGENT_MESSAGE_TOO_LARGE` | 5.2 `post` 第 1 步 | `agent, bytes` |
| `AGENT_LINE_FORBIDDEN` | `run_start` 的产线解析不了或不在 `lines` 里 | `agent, line` |
| `AGENT_TOOL_INVALID` | 3.4 工具的语义参数错误 | `tool, reason` |
| `AGENT_INTERRUPTED` | 5.5 | `turn` |
| `AGENTS_CONFIG_MISSING` | `factory.json` 不存在、无法解析或没有 `agents` 段，而调用方要 `AgentsConfig.load` | `path` |
| `AGENTS_CONFIG_INVALID` | 4.4 | `field` |
| `AGENTS_ALREADY_RUNNING` | `serve` 取不到租约 `agents` | — |

---

## 7. 验收测试

夹具：`tests/acceptance/m13/agent_world.py`。在 M5 的 `auto_world` 之上：`fake-agent@1` 增加 `invoke.resume`；Registry 增加 Agent `scout@1`（订阅定时、`note.text@1` Bean、入站路由 `scout-in`，可启动 `note-trigger@1`）与 `helper@1`；`factory.json` 带 `agents` 段。CLI 是 `tests/fakes/fake_agent.py`，脚本按 `FACTORY_TURN` 分轮。时钟是 `FakeClock`。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m13/test_m13_01_kind.py` | `test_agent_kind_compiles` | 带 Agent 的 Registry 能编译，`snapshot.keys("Agent")` 有它；引用不存在的产线时 `factory check` 报引用错误 |
| | `test_agent_config_rules` | 4.1 每条规则的 `AGENT_INVALID` 与 `reason`；缺省值 |
| | `test_resume_invocation` | `build_runtime_invocation(session=...)` 用 `invoke.resume` 并代入会话 id；没有 `resume` 时 `INVOCATION_INVALID` |
| | `test_transcript_session_and_tokens` | `parse_transcript` 给出 `session_id` 与 `input_tokens` |
| `tests/acceptance/m13/test_m13_02_mailbox.py` | `test_schedule_messages` | 第一次 `collect` 投递定时消息；不推进时钟不再投；推进一个周期后再投 |
| | `test_bean_subscription` | 封存匹配类型与标签的 Bean 后 `collect` 投递一条 `bean` 消息；不匹配标签的不投；游标不重复投 |
| | `test_ingress_to_agent` | `deliver: agent` 的路由：202，邮箱有 `ingress` 消息（`text`），`ingress.received` 的 `target` 是 Agent 名 |
| | `test_send_remind_and_redaction` | `factory agent send` 投递 `message`；带 `not_before` 的 `remind` 入箱为 `pending`；消息体里的秘密值被打码；超过 64 KiB 报 `AGENT_MESSAGE_TOO_LARGE` |
| | `test_inbox_answer_returns` | `ask_human` 开出的事项被回答后，邮箱有 `inbox` 消息，带 `reply` |
| `tests/acceptance/m13/test_m13_03_turns.py` | `test_turns_resume_one_conversation` | 两轮：第一轮用 `argv`，第二轮用 `resume` 和第一轮的会话 id；第二轮的 CLI 看得到第一轮的提示（对话历史）；沙箱只打开一次 |
| | `test_batch_and_messages_file` | 一轮最多取 `batch` 条，按 id 升序；`/factory/in/messages.json` 与提示一致；处理完的消息为 `done` |
| | `test_daily_budget` | 日预算用尽：不跑轮次，消息留在 `pending`，`agent.budget_exhausted` 只写一次；24 小时后恢复 |
| | `test_failures_retry_then_drop` | 失败的轮次让消息回到 `pending`；连续 3 次后 `dropped`，开出收件箱事项，写 `agent.dropped`；会话 id 不变 |
| | `test_recover_interrupted` | `running` 的轮次在 `recover` 后为 `interrupted`，消息回到 `pending` 且 `attempts` 不变 |
| | `test_agent_cli` | `factory agent list/show/send/pause/resume/turns` 的输出；暂停时 `run_once` 返回 `None` |
| | `test_run_serve_hosts_agents` | `run_serve` 有 `agents` 段时起线程 `mao-agents`、持有租约 `agents` 并处理邮箱；停止后线程退出、租约释放 |
| `tests/acceptance/m13/test_m13_04_context.py` | `test_rotation_by_turns` | 达到 `rotate_turns` 后：交接轮续接旧会话并写出 `HANDOFF.md`；下一轮用 `argv` 开新对话，提示里有 charter 与 HANDOFF 的引用；`agent.rotated` |
| | `test_rotation_by_tokens` | 上一轮 `input_tokens >= rotate_tokens` 时同样轮换；`factory agent rotate` 也能触发 |
| | `test_agent_mcp_tools` | `run_start` 能启动列出的产线、不能启动其他产线（`AGENT_LINE_FORBIDDEN`）；`agent_send` 投到别的 Agent；`remind` 生效；`changeset_propose` 是 `unknown tool` |
| `tests/acceptance/m13/test_m13_05_foreman.py` | `test_foreman_resident_takes_wakes` | 常驻模式：非复核票进 `foreman` 邮箱，`ForemanSupervisor.run_once` 不起厂长会话；一轮 `ok` 后票 `done`；转换票没有提议时按 5.5 重开 |
| | `test_reviewer_still_session` | 常驻模式下复核票仍由一次性会话处理 |
| | `test_session_mode_unchanged` | 缺省 `session` 模式：`AgentHost.collect` 不取唤醒票，值守照旧起会话 |

---

## 8. 错误示范与正确示范

```python
# ✖ 错误：每轮把数据库里的历史事件、Bean 列表拼成一大段提示，重新起会话
# ✔ 正确：续接同一段对话（invoke.resume），提示只放新消息；长期记忆在对话和 /workspace/notebook 里

# ✖ 错误：AgentHost 在 tick 里等 CLI 跑完才处理下一件事，阻塞 daemon 主循环
# ✔ 正确：AgentHost 在自己的线程里跑（5.7）；daemon 主循环不碰 CLI

# ✖ 错误：轮次失败就丢掉消息
# ✔ 正确：回到 pending，连续 3 次才丢弃，并告诉人

# ✖ 错误：对话太长就静默删掉会话目录
# ✔ 正确：先交接轮写 HANDOFF.md，再开新对话并引用它（5.6）
```

---

## 9. 风险与注意事项

- **CLI 的会话格式不受我们控制**。续接只依赖 runtime 文档的 `invoke.resume` 与 transcript 里的 `session_id`；CLI 升级改了格式时，失败的轮次会重试，最终丢弃并告诉人，不会静默丢上下文。
- **上下文压缩由 CLI 负责**。轮换只是兜底：轮数或输入 token 超过上限才做。
- **预算**：每轮有上限，每日有上限。常驻不等于无限花钱。
- **秘密**：消息体写入时打码；Agent 的笔记在工厂目录里，规则文本要求它不写秘密，但无法强制，备份与保留策略同 `retention.md`。

---

## 10. 任务拆分

| 任务 | 内容 | 依赖 | 必读 | 产出 | 验收 |
|---|---|---|---|---|---|
| M13-01 | `Agent` kind、`agent_config`、`invoke.resume`、transcript 增补 | M12 | 3.1、3.3、4.1 | `mrl.factory`、`mao.agents`、`mao.operator` | `test_m13_01_kind.py` |
| M13-02 | 邮箱与订阅（`AgentsConfig`、`AgentHost` 的构造与 `collect`、迁移 `040`）、入站 `deliver: agent`、收件箱回流、命令行 `factory agent send` | M13-01 | 3.2、3.5、4.2、4.4、5.1、5.2 | `mao.agents`、`mao.ingress`、`mao.inbox`、`mao.commands.agent` | `test_m13_02_mailbox.py` |
| M13-03 | 轮次、沙箱、预算、失败与恢复、命令行、`serve` 装配；`McpServer` 接受 `agent` 角色（3.4 第一段，只有共享只读工具）；`fake_agent` 增补（由设计方安装） | M13-02 | 3.4 第一段、4.2–4.5、5.3–5.5、5.7 | `mao.agents`、`mao.mcp`、`mao.commands.agent`、`mao.daemon` | `test_m13_03_turns.py` |
| M13-04 | 上下文轮换、`agent` 角色的四个 MCP 工具 | M13-03 | 3.4、5.6 | `mao.agents`、`mao.mcp` | `test_m13_04_context.py` |
| M13-05 | 厂长常驻模式 | M13-04 | 5.8 | `mao.agents`、`mao.supervisor`、`mao.foreman` | `test_m13_05_foreman.py` |
