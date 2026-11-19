# talk：人与厂长的持久对话（M15）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M15-01、M15-02、M15-03**。设计说明见 `plan/design-talk.md`。
> 对话是意图通道。卡片是命令。控制面不调用模型。
> 本文件与 `agents.md`、`mcp.md`、`supervisor.md`、`inbox.md`、`commands.md`、`console.md` 冲突时，以本文件为准。

---

## 1. 目的与范围

给人与常驻厂长一条工厂级线程。人的话进入线程和厂长邮箱。厂长用工具把回复和提议写回同一条线程。能改工厂的只有既有命令：ChangeSet、收件箱、以及人确认之后的 `start_run`。

**做什么（M15）**

- `mao.talk`：线程的五个操作，加上厂长用的 `propose_run`、控制面用的 `note_status`。
- 表 `talk_messages`。迁移 `src/mao/migrations/041_talk.sql`。
- 命令 `factory talk ls`、`factory talk show`、`factory talk send`、`factory talk answer`。
- 厂长 MCP 工具 `talk_reply`、`talk_propose_run`。
- 常驻厂长正在跑的轮次里，`changeset_propose`、`escalate`、`secret_request` 成功后追加卡片，不要求该轮已经有 human talk；这些对象后来的状态写回线程。
- 人确认运行卡片后，控制面调用 `LineRunner.start_run`。

**不做什么**

| 不做 | 原因 |
|---|---|
| 控制面调用 LLM，或让自由文本改 Registry | D10；对话不是命令 |
| 对话里改宪法、`factory.json`、调用 `advance` / `merge` / `rollback` | 这些仍走原命令 |
| 厂长或模型直接 `start_run` | 运行卡片必须由人确认 |
| 本里程碑的 Web 页面、控制台路由、公网暴露、多人聊天 | 页面以后只调用第 3.1 节的五个操作 |
| 把全厂每条 `bean.sealed` 灌进线程 | 线程只渲染对话和下面点名的卡片 |
| 复核者进入这条线程 | 复核仍是一次性会话（`agents.md` 5.8） |
| 实现前把新错误码写入 `mao.errors.CODES` | 第 6 章先写在本文件。实现这些码时再登记，否则 `FactoryError` 无法构造，`doccheck` 也会对上本文件里的反引号 |

---

## 2. 依赖

只用这些已有接口，不改它们的签名。

| 依赖 | 用到的接口 |
|---|---|
| `Mailbox.post`（`agents.md` 3.2） | 把 human talk 投进厂长邮箱。`body` 先打码，超过 64 KiB 的规则不变 |
| `AgentHost` 的一轮（`agents.md` 5.3、5.8） | 消费邮箱；本文件只在轮次结束时补未回复判定，并在提示里加一句义务 |
| `McpServer`（`mcp.md`） | 厂长工具 `talk_reply`、`talk_propose_run`；talk 轮次里三个既有工具成功后追加卡片 |
| `Inbox.open` / `Inbox.close` / `answer`（`inbox.md`） | 未回复开 `question`；秘密、审批、签核仍由人经 `Inbox.answer` 写。结案时调用 `note_status`，不把回答载荷抄进线程 |
| `ChangePipeline`（`governance.md`） | 厂长仍 `changeset_propose`。状态事件之后调用 `note_status`。不从对话调用 `advance` |
| `EpochRouter.choose`（`governance.md` 5.9） | 确认运行时选 epoch，key 与 `factory line run` 相同 |
| `LineRunner.start_run`（`lines.md` / `runs.py`） | 只由 `answer(..., confirm=True)` 调用，快照是 `choose` 返回的那一版 |
| `LeaseStore.valid`（`durable.md`） | 判断 `factory serve` 是否在跑：资源名 `serve` |
| `SupervisorConfig.load`（`supervisor.md`） | 读 `foreman.mode` |
| `AgentsConfig.load`（`agents.md`） | 确认 `factory.json` 有 `agents` 段 |
| `SecretStore.redact_obj`（`egress.md`） | 写入线程和邮箱之前打码 |

---

## 3. 接口

### 3.1 五个操作

以后的页面只调用这五个，名字固定。禁止页面调用 `propose_run`、`Mailbox`、`Inbox`、`LineRunner`、`ChangePipeline`，也禁止新增控制台路由。

```python
def list_messages(ctx: Any) -> list[TalkMessage]
def show(ctx: Any, message_id: str) -> TalkMessage
def post_human(ctx: Any, text: str, *, by: str) -> TalkMessage
def talk_reply(
    ctx: Any, text: str, *, by: str, turn_id: str, in_reply_to: str | None = None
) -> TalkMessage
def answer(ctx: Any, message_id: str, *, by: str, confirm: bool) -> TalkMessage
```

`list_messages` 按 `created_at`、`id` 升序返回全部消息。`show` 找不到 → `TALK_NOT_FOUND`。
`answer` 成功时返回新追加的状态消息，不是被确认的原卡片（5.6）。原卡片的 `card.status` 仍是 `proposed`。

读操作不检查常驻、`agents` 段和 serve。写操作按第 5.1 节检查。人的 `by` 与厂长的 `by` 不是同一条规则。

Bun Web App 通过同源 API 使用这条线程。人的消息经 `/api/v1/talk` 写入，外部 Foreman 从 `/api/v1/foreman/inbox` claim 消息并经 `/api/v1/talk/reply` 写入回复。两类写入都会产生事件，页面通过 `/api/v1/events` 的 SSE 流增量刷新，不需要控制面调用模型。

### 3.2 不是页面操作的两个函数

```python
def propose_run(
    ctx: Any,
    line: str,
    inputs: Mapping[str, str],
    *,
    by: str,
    turn_id: str,
    sink_mode: str = "execute",
) -> TalkMessage

def note_status(ctx: Any, kind: str, ref_id: str, status: str) -> TalkMessage | None
```

`propose_run` 只由厂长 MCP 工具 `talk_propose_run` 调用。它只追加运行卡片，不调用 `start_run`。

`note_status` 由控制面在既有状态变化处调用。没有对应卡片时返回 `None`，不抛错。

### 3.3 命令行

`mao.commands.talk.register` 注册到 `mao.cli` 的 `_REGISTRARS`。命令只调用第 3.1 节，不写表。

```text
factory talk ls [--json]
factory talk show ID [--json]
factory talk send --by NAME TEXT [--json]
factory talk answer ID --by NAME [--confirm] [--json]
```

`--by` 必填。`answer` 没有 `--confirm` 时调用 `answer(..., confirm=False)`，得到 `TALK_CONFIRM_REQUIRED`，不调用 `start_run`。`--json` 时 `ls` 输出数组，其余输出 `TalkMessage.to_json()`。`answer` 成功时这份 JSON 是新追加的状态消息：`card.status` 为 `PENDING`，`card.run_id` 为新 run id。错误体按 `commands.md` 4.2。

### 3.4 MCP 工具

只加在 `foreman` 角色。`reviewer` 和 `agent` 的 `tools/list` 不列出；`tools/call` 得到 `-32602`，消息为未知工具。

| 工具 | 参数 | 调用 |
|---|---|---|
| `talk_reply` | `text`（必填），`in_reply_to`（可选） | `talk_reply(ctx, text, by=session.actor, turn_id=session.wake_id 或空串, in_reply_to=...)` |
| `talk_propose_run` | `line`、`inputs`（object，值为字符串）必填；`sink_mode` 可选，缺省 `execute` | `propose_run(...)`，`by=session.actor`，`turn_id` 同上 |

`session.wake_id` 为空时，`turn_id` 传 `""`。参数多出 `by`、`actor` → 既有的 `-32602`。

常驻厂长这一轮的会话必须让 `wake_id` 和 `session_id` 都是轮次 id。现有构造（`agents.md` 5.8，实现在 `AgentHost` 启动 MCP 处）是：

```python
Session("foreman", runtime_ref, None, turn_id, source)
```

第三个参数是 `wake_id`，现在固定为 `None`。第四个是 `session_id`，放的是轮次 id，`changeset_propose` 用它做克隆 fetch。M15 改成：

```python
Session("foreman", runtime_ref, turn_id, turn_id, source)
```

改法只有一处：第三个参数从 `None` 改成轮次 id，第四个 `session_id` 保持为已经写着的轮次 id。留着 `None` 才会在轮次里得到 `TALK_NOT_IN_TURN`。把轮次 id 从 `session_id` 挪走才会拆掉 `changeset_propose`。agent 角色那一支不改：`Session("agent", runtime_ref, None, turn_id, None, name)`。复核者的一次性会话也不改，`wake_id` 仍是唤醒票 id 或空。

构造改完之后，轮次里的 `talk_reply` 拿到的 `turn_id` 是轮次 id，不是空串，因此不会得到 `TALK_NOT_IN_TURN`。

成功的 `talk_reply` 把本轮 `McpServer.outcome["talk_replied"]` 置为 `True`。缺省是 `False`。

### 3.5 记录

```python
@dataclass(frozen=True)
class TalkMessage:
    id: str
    role: str                 # human | foreman | system
    kind: str                 # text | card
    by: str
    body: str
    reply_state: str          # open | answered | unanswered | na
    in_reply_to: str | None
    card_type: str | None     # changeset | escalate | secret | run
    ref_kind: str | None      # changeset | inbox | run
    ref_id: str | None
    card: dict[str, Any]
    mailbox_id: str | None
    created_at: str

    def to_json(self) -> dict[str, Any]: ...
```

`to_json` 含上面全部字段。`card` 是对象，不是 JSON 字符串。

---

## 4. 数据格式

### 4.1 表

迁移 `041_talk.sql`。编号接在 `040_agents.sql` 之后。

```sql
CREATE TABLE talk_messages (
  id           TEXT PRIMARY KEY,
  role         TEXT NOT NULL CHECK (role IN ('human', 'foreman', 'system')),
  kind         TEXT NOT NULL CHECK (kind IN ('text', 'card')),
  by_name      TEXT NOT NULL,
  body         TEXT NOT NULL,
  reply_state  TEXT NOT NULL CHECK (reply_state IN ('open', 'answered', 'unanswered', 'na')),
  in_reply_to  TEXT,
  card_type    TEXT,
  ref_kind     TEXT,
  ref_id       TEXT,
  card         TEXT NOT NULL DEFAULT '{}',
  mailbox_id   TEXT,
  created_at   TEXT NOT NULL
);
```

列 `by_name` 对应记录字段 `by`（`by` 是 SQL 关键字）。`card` 存规范 JSON。

标识前缀是 `tm`：`ctx.ids.new("tm")`。

### 4.2 邮箱消息

`post_human` 调用：

```text
Mailbox.post("foreman", "talk", "talk:" + 线程消息 id, {"message": id, "by": by, "text": 打码后的正文})
```

`source` 以 `talk:` 加线程消息 id，便于和 `factory agent send` 的普通 `message` 区分。

### 4.3 卡片

卡片是 `role = system`、`kind = card`、`reply_state = na` 的消息。线程字段 `by` 一律是 `"control"`，包括确认之后追加的状态消息。`card.by` 只属于运行卡片：提议时是厂长 actor，确认那一次改成确认的人。changeset、escalate、secret 的键表里没有 `by`。秘密卡片的键只能是 `type`、`request_id`、`status`、`name`、`station`。确认的人另外写在 `trigger.by`，不写进线程字段 `by`。`body` 是一行给人看的中文，不含秘密值。`card` 只有下面这些键。

| `card_type` | `ref_kind` | `ref_id` | `card` |
|---|---|---|---|
| `changeset` | `changeset` | ChangeSet id | `type`、`id`、`state`、`commit` |
| `escalate` | `inbox` | 事项 id | `type`、`request_id`、`status`、`summary` |
| `secret` | `inbox` | 事项 id | `type`、`request_id`、`status`、`name`、`station` |
| `run` | `run` | 确认前为空串；确认后为 run id | `type`、`line`、`inputs`、`sink_mode`、`status`、`run_id`、`by` |

`secret` 卡片没有 `value`、`reason`、`answer`。`summary` 与 `body` 先经 `redact_obj`。

运行卡片提议时 `status` 为 `proposed`，`run_id` 为 `null`，`card.by` 为厂长 actor。确认后的新消息 `status` 为 run 的状态字符串（`PENDING` 等），`run_id` 为新 run 的 id，`card.by` 为确认的人。两条消息的线程字段 `by` 都是 `"control"`。

状态回写是**追加**一条新的 system 消息，不改已有行的 `body` 和 `card`。新消息的 `ref_kind`、`ref_id` 与所依据的卡片相同。确认那一次除外：那条新消息还要把 `card.by` 改成确认的人（5.6）。以后的 `note_status` 复制上一张的键，只改状态字段，于是 `card.by` 保持为确认的人。去重看的是这些消息里最新一条的状态，不是仍写着旧状态的第一张。确认运行时只允许改原卡片行的 `ref_id`（写成 run id）和 `card.run_id`，以便后来的 `note_status` 找得到它。

### 4.4 触发器

人确认之后，`start_run` 的 `trigger` 恰好是：

```json
{"on": "talk", "by": "<确认的人>", "message": "<运行卡片的线程消息 id>"}
```

没有其他键。

---

## 5. 行为

### 5.1 写操作的前提

`post_human`、`talk_reply`、`answer`、`propose_run` 按这个顺序检查，先失败的先抛，不写行：

1. `by` 分两种，不要混用：
   - 人的 `post_human`、`answer`：`by` 匹配 `^[A-Za-z0-9._-]+$`，否则 `TALK_ACTOR_INVALID`（`detail.by`）。
   - 厂长的 `talk_reply`、`propose_run`：`by` 是会话 actor，即 OperatorRuntime 引用（`名字@版本`，例如 `fake-agent@1`）。非空字符串即可，**允许 `@`**。空串 → `TALK_ACTOR_INVALID`（`detail.by`）。不要用上面的人名规则，否则厂长无法回复。MCP 层已经要求 actor 能在快照里解析；这里不再查一次。
2. 文本类：`text` 必须是字符串，去掉首尾空白后非空，否则 `TALK_EMPTY`（`detail.field = "text"`）。`propose_run` 不检查文本。
3. `SupervisorConfig.load(ctx.root).mode == "resident"`，否则 `TALK_FOREMAN_ABSENT`（`detail.mode` 为实际值；没有 `foreman` 段时沿用 `SUPERVISOR_CONFIG_MISSING`，本条不掩盖它）。
4. `AgentsConfig.load(ctx.root)`。没有 `agents` 段 → 既有的 `AGENTS_CONFIG_MISSING`。
5. `LeaseStore(ctx).valid("serve")` 不是 `None`，否则 `TALK_SERVE_DOWN`（`detail.resource = "serve"`）。

`list_messages` 和 `show` 不执行这五步。`talk_reply(..., by="fake-agent@1", turn_id="")` 在常驻且 serve 有效时得到 `TALK_NOT_IN_TURN`，不是 `TALK_ACTOR_INVALID`。

### 5.2 `post_human`

1. 通过 5.1。
2. `stored = SecretStore(ctx.root).redact_obj(text.strip())`。规范 JSON 的 UTF-8 超过 64 KiB → `TALK_TOO_LARGE`（`detail.bytes`）。
3. 一个事务里：插入 `role = human`、`kind = text`、`reply_state = open`、`card = {}` 的行，再 `Mailbox.post`（第 4.2 节）。任一步失败则整笔回滚。`mailbox_id` 是邮箱消息 id。
4. 不写宪法，不改 `factory.json`，不调用 `start_run`。

### 5.3 `talk_reply` 与未回复

`talk_reply`：

1. 通过 5.1。`turn_id` 必须是 `agent_turns` 里 `agent = foreman` 且 `state = running` 的轮次，否则 `TALK_NOT_IN_TURN`（`detail.tool = "talk_reply"`）。
2. `in_reply_to` 若给出，必须是已有线程消息，否则 `TALK_NOT_FOUND`。
3. 打码、长度检查同 5.2。插入 `role = foreman`、`kind = text`、`by = by`、`reply_state = na`。不写邮箱。
4. 若 `in_reply_to` 指向一条 `reply_state` 为 `open` 或 `unanswered` 的 human 消息：把它改为 `answered`；若有 `origin = "talk:" + 该消息 id` 且状态为 `open` 的收件箱事项，`Inbox.close(..., status="cancelled", answer=None, by="talk")`。不把回复正文写入该事项。
5. 调用方把 `outcome["talk_replied"]` 置真。库函数本身不碰 MCP。

未回复事项（`ok` 未回复与邮箱丢弃共用）：

```text
Inbox.open("question", subject=ctx.ids.new("hr"), origin="talk:" + 消息 id,
           summary="厂长没有回复 " + 消息 id,
           payload={"options": ["知道了"], "message": 消息 id})
```

已有 `open` 且 `origin` 相同的事项则不重复开。人经 `Inbox.answer` 关掉它，不改变线程上的 `reply_state`：关掉通知不等于厂长已回复。

轮次结束（只对内置厂长，接在 `agents.md` 5.3 第 9–10 步之后）：

- 本轮不是 `ok`，邮箱消息回到 `pending`（仍会重试）：不改 `reply_state`，不开未回复事项。
- 本轮不是 `ok`，且 `kind = talk` 的邮箱消息被标成 `dropped`（`agents.md` 5.3：`attempts >= 3`）：用邮箱 `body.message` 找到线程消息，改为 `unanswered`，并按上面的规则开事项。线程不再保持 `open`。
- 本轮 `ok`，且 `outcome["talk_replied"]` 为真：本轮消费的、`kind = talk` 的 human 消息，凡 `reply_state` 仍为 `open` 的，改为 `answered`。已经是 `answered` 的不要改回 `unanswered`。
- 本轮 `ok`，批次里有 `kind = talk`，且 `talk_replied` 不为真：这些 human 消息里仍为 `open` 的改为 `unanswered`，并按上面的规则开事项。

一条 `talk_reply` 覆盖本轮全部 human talk。不要求每条消息各回复一次。

### 5.4 任务书

常驻厂长任务书（第一轮 charter）增加这句，原文不变：

```text
本轮有 human talk 消息时必须 talk_reply。
```

工作轮提示在本批消息含 `kind = talk` 时，再附同一句。没有 talk 消息的轮次不附。不要把历史事件或 Bean 列表拼进提示。

### 5.5 卡片回写

常驻厂长正在跑的 MCP 会话里，下面三个工具**成功返回之后**追加首张卡片。条件是：`role = foreman`，`foreman.mode = resident`，`wake_id` 是 `state = running` 的厂长轮次。这个 `wake_id` 就是 3.4 写明的轮次 id，与 `session_id` 相同；按旧构造它是 `None`，卡片不会出现。**不要求**该轮已经消费了 human talk。人上一轮说的话、厂长下一轮才 `changeset_propose`，卡片仍进同一条线程。

不追加：工具失败；不是厂长角色；没有正在跑的厂长轮次（复核者的一次性会话、没有 `wake_id` 的 `factory mcp`）；`mode` 不是 `resident`。不要因为「本轮没有 talk 消息」就跳过。

| 工具 | 卡片 |
|---|---|
| `changeset_propose` | `changeset`，`state` 为返回的 ChangeSet 状态，`commit` 为返回的 commit |
| `escalate` | `escalate`，`status = open`，`summary` 为打码后的 summary |
| `secret_request` | `secret`，`status = open`，只有 `name` 和 `station`，没有值 |

这些路径不调用 `advance`、`merge`、`rollback`、`start_run`。

后来的状态：凡是插入 `changeset.state` 事件的地方（`mao.governance.changesets` 与 `mao.governance.rollout`），同一事务末尾调用 `note_status(ctx, "changeset", id, 新状态)`。`Inbox.close` 在更新状态之后调用 `note_status(ctx, "inbox", request_id, status)`，不传入 `answer`。`LineRunner._set_run` 以及 `mao.dynamic` 里直接 `UPDATE runs SET state` 的语句，同一事务里调用 `note_status(ctx, "run", run_id, 新状态)`。

`note_status`：

1. 找出 `ref_kind = kind` 且 `ref_id = ref_id` 的消息，按 `created_at`、`id` 升序。没有则返回 `None`。`kind` 不是 `changeset`、`inbox`、`run` 时也返回 `None`。`ref_id` 为空串的提议卡片不参与 run 的匹配，直到确认把它写成 run id。
2. 最新一条的状态字段已经是 `status` 时返回 `None`。changeset 看 `card.state`，其余看 `card.status`。不要拿第一张仍是 `proposed` 或 `open` 的卡片来判断。
3. 否则追加一条 system 卡片。`ref_kind`、`ref_id`、`card_type` 与所依据的那条相同。`card` 复制最新一条的键，只改状态字段，不改 `card.by`。确认那一次不走这里，由 5.6 把 `card.by` 写成确认的人；之后的 `note_status` 才只改状态，`card.by` 保持为确认的人。不复制收件箱的 `answer`，不加入 `value`。同一状态不写第二次。

不订阅 `bean.sealed`。密封 Bean 不产生线程消息。

### 5.6 运行确认

`propose_run`：

1. 通过 5.1。厂长的 `by` 用第 1 步的第二种，允许 `@`。`turn_id` 必须是正在跑的厂长轮次，否则 `TALK_NOT_IN_TURN`（`detail.tool = "talk_propose_run"`）。不要求该轮已经有 talk 消息。
2. `sink_mode` 不是 `execute` 或 `dry-run` → `TALK_RUN_INVALID`（`detail.field = "sink_mode"`）。`inputs` 不是字符串值的对象 → `TALK_RUN_INVALID`（`detail.field = "inputs"`）。不在这里检查产线是否存在。
3. 追加 `run` 卡片，`status = proposed`，`run_id = null`，`ref_id = ""`。线程字段 `by` 是 `"control"`。`card.by` 为厂长 actor。`runs` 表不插入行。同一事务里开一条收件箱事项，让人不用自己轮询线程：

```text
Inbox.open("question", subject=ctx.ids.new("hr"), origin="talk:" + 卡片 id,
           summary="厂长提议运行 " + line,
           payload={"options": ["知道了"], "message": 卡片 id, "card_type": "run"})
```

已有 `open` 且 `origin` 相同的事项则不重复开。人用 `Inbox.answer` 关掉通知，卡片仍是 `proposed`，不启动 run。确认成功时把这条 `open` 事项 `Inbox.close(..., status="cancelled", answer=None, by=确认的人)`。

`answer`：

1. 通过 5.1。`confirm` 不是 `True` → `TALK_CONFIRM_REQUIRED`（`detail.id`），到此为止。
2. 消息不存在 → `TALK_NOT_FOUND`。`card_type` 不是 `run`，或 `kind` 不是 `card` → `TALK_NOT_CONFIRMABLE`（`detail.id`、`detail.kind`）。
3. 该卡片 `card.status` 已不是 `proposed`，或 `ref_id` 已非空 → `TALK_ALREADY_ANSWERED`（`detail.id`）。
4. 选 epoch 与 `factory line run` 相同，不固定用 active：

```text
key = card.line + ":" + canonical_json(card.inputs)
commit = EpochRouter(ctx, EpochStore(ctx)).choose(key, line=card.line)
snapshot = EpochStore(ctx).materialize(commit)
open_line_runner(ctx, snapshot).start_run(
    card.line, card.inputs,
    trigger={"on": "talk", "by": by, "message": message_id},
    sink_mode=card.sink_mode)
```

`key` 与 `governance.md` 5.9 的调用方相同。没有金丝雀时 `choose` 返回 active。有金丝雀时按百分比和「产线是否只在候选上」决定，对话确认的 run 落在选出的那一版，`runs.epoch` 等于该 commit。不传 `budget_usd`、`variants`、`rebuilt_from`，不改 `use_cache` 的缺省。不调用 `run_to_completion`，也不调用 `advance`、`merge`、`rollback`。`start_run` 抛错则事务回滚：卡片仍是 `proposed`，没有 run 行，通知事项仍为 `open`，异常原样抛出。

5. 成功：把该卡片行的 `ref_id` 和 `card.run_id` 设为新 run id。原卡片的 `card.status` 仍是 `proposed`。再追加一条状态消息，`card.status` 为 run 的状态（初始是 `PENDING`），`card.run_id` 为新 run id，`card.by` 为确认的人。线程字段 `by` 仍是 `"control"`（以 4.3 为准）。`answer` 和 `factory talk answer` 返回这条新消息，不返回被确认的原卡片。按「返回原卡片」实现时，`--json` 的 `card.status` 仍是 `proposed`，验收失败。

对话路径没有拒绝动词。不确认的卡片一直是 `proposed`，不会启动 run。

---

## 6. 错误

本组件新增的码。实现时登记进 `mao.errors.CODES`，一句中文说明。规划阶段不登记。

| 错误码 | 触发条件 | detail |
|---|---|---|
| `TALK_FOREMAN_ABSENT` | `foreman.mode` 不是 `resident` | `mode` |
| `TALK_SERVE_DOWN` | `serve` 租约无效或不存在 | `resource` |
| `TALK_NOT_FOUND` | `show`、`answer`、`in_reply_to` 的 id 不存在 | `id` |
| `TALK_ACTOR_INVALID` | 人的 `by` 不匹配 `^[A-Za-z0-9._-]+$`，或厂长的 `by` 为空。运行时引用里的 `@` 不算不合格 | `by` |
| `TALK_EMPTY` | 文本为空 | `field` |
| `TALK_TOO_LARGE` | 打码后超过 64 KiB | `bytes` |
| `TALK_NOT_IN_TURN` | `talk_reply` 或 `propose_run` 不在正在跑的厂长轮次里 | `tool` |
| `TALK_NOT_CONFIRMABLE` | 确认的不是运行卡片 | `id`、`kind` |
| `TALK_ALREADY_ANSWERED` | 运行卡片已经确认 | `id` |
| `TALK_RUN_INVALID` | `sink_mode` 或 `inputs` 不合法 | `field` |
| `TALK_CONFIRM_REQUIRED` | `answer` 的 `confirm` 不是 `True` | `id` |

既有码照旧抛出，不包装：`AGENTS_CONFIG_MISSING`、`SUPERVISOR_CONFIG_MISSING`、`SUPERVISOR_CONFIG_INVALID`，以及 `start_run` 的 `RUN_INPUT_INVALID` 等。

---

## 7. 验收测试

夹具 `tests/acceptance/m15/talk_world.py`。三个文件都以 `pytest.importorskip("mao.talk")` 开头。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m15/test_m15_01_thread.py` | `test_post_human_writes_thread_and_mailbox` | `send` 写 human 行并 `Mailbox.post` 给 `foreman`，`kind = talk`；正文里的秘密值变成 `***` |
| | `test_send_checks_resident_agents_and_serve` | 非常驻 → `TALK_FOREMAN_ABSENT`；无 `agents` 段 → `AGENTS_CONFIG_MISSING`；无 serve 租约 → `TALK_SERVE_DOWN`；不合格的 `by`、空文本分别是 `TALK_ACTOR_INVALID`、`TALK_EMPTY`；失败时不写行 |
| | `test_ls_show_and_talk_reply` | `ls` / `show` 按时间返回；未知 id → `TALK_NOT_FOUND`；没有轮次的 `talk_reply` → `TALK_NOT_IN_TURN` |
| | `test_other_roles_cannot_talk_reply` | reviewer 与 agent 的工具清单没有 `talk_reply`，调用得到未知工具 |
| | `test_ok_without_reply_opens_inbox` | 轮次 `ok` 且没有 `talk_reply`：消息 `unanswered`，收件箱有 `origin` 以 `talk:` 开头的 `question`；提示含「本轮有 human talk 消息时必须 talk_reply。」 |
| | `test_reply_covers_the_batch` | 一轮里一次 `talk_reply` 使本批 human talk 都成为 `answered`，且不因未回复开事项 |
| | `test_failed_turn_does_not_open_inbox` | 一轮失败：`reply_state` 仍为 `open`，不开未回复事项，邮箱消息回到 `pending` |
| | `test_dropped_talk_marks_unanswered` | 连续失败直到邮箱消息 `dropped`：线程改为 `unanswered`，并开 `origin` 为 `talk:` 加消息 id 的事项 |
| | `test_later_reply_clears_unanswered` | 后一轮 `talk_reply` 带 `in_reply_to`：该消息改为 `answered`，对应事项 `cancelled` |
| | `test_no_console_talk_route` | `src/mao/console.py` 没有 `/talk` |
| `tests/acceptance/m15/test_m15_02_cards.py` | `test_escalate_card_on_foreman_turn` | 常驻厂长轮次里没有 human talk 也追加 `escalate` 卡片；卡片带事项 id |
| | `test_changeset_card_and_status` | talk 轮次里 `changeset_propose` 追加卡片且状态仍为 `draft`；随后 `withdraw` 追加 `withdrawn`，不调用 `advance` |
| | `test_secret_card_omits_value` | `secret_request` 的卡片没有值；取消事项后的状态消息也不含秘密值 |
| | `test_bean_sealed_stays_out` | 密封 Bean 不增加线程消息 |
| | `test_talk_does_not_advance_or_write_constitution` | 上述操作不改宪法和 `factory.json`，ChangeSet 不会自己离开 `draft` |
| `tests/acceptance/m15/test_m15_03_confirm.py` | `test_propose_run_records_card_only` | `talk_propose_run` 只写 `proposed` 卡片并开收件箱通知，`runs` 无新行；厂长调用 `run_start` 仍是未知工具 |
| | `test_confirm_starts_run` | `answer ID --confirm` 调用 `start_run`，返回的是新状态消息：`card.status` 为 `PENDING` 且 `card.run_id` 非空；`trigger` 为 `{"on": "talk", "by": <人>, "message": <卡片 id>}`；通知事项改为 `cancelled` |
| | `test_confirm_follows_epoch_router` | 有金丝雀时，run 的 `epoch` 等于 `EpochRouter.choose` 的结果，不是固定的 active |
| | `test_answer_without_confirm_does_not_start` | 没有 `--confirm` → `TALK_CONFIRM_REQUIRED`，无 run |
| | `test_confirm_rejects_non_run_and_repeat` | 确认普通消息 → `TALK_NOT_CONFIRMABLE`；再次确认 → `TALK_ALREADY_ANSWERED`；仍然只有一条 run |
| | `test_run_state_writes_back` | run 状态随后变化时，线程追加一条带同一 `run_id` 的状态消息 |

```text
uv run python tools/harness.py pytest tests/acceptance/m15/test_m15_01_thread.py
uv run python tools/harness.py pytest tests/acceptance/m15/test_m15_02_cards.py
uv run python tools/harness.py pytest tests/acceptance/m15/test_m15_03_confirm.py
```

断言分别是 `passed >= 10`、`passed >= 5`、`passed >= 6`，且 `failed == 0`、`errors == 0`、`skipped == 0`。阶段门 `acceptance-m15` 为 `passed >= 21`。

---

## 8. 错误示范与正确示范

```python
# ✖ 错误：厂长在回复正文里写“已启动产线”，控制面就此 start_run
# ✔ 正确：talk_propose_run 只追加 proposed 卡片；人 answer(..., confirm=True) 才 start_run（5.6）

# ✖ 错误：本轮 ok 但没调用 talk_reply，仍把 human 消息标成已回复
# ✔ 正确：标 unanswered，并按消息开 question（5.3）

# ✖ 错误：把 bean.sealed、收件箱 answer.value 写进线程正文
# ✔ 正确：只回写第 5.5 节点名的对象；秘密值经 redact，回答载荷不进卡片（4.3、5.5）

# ✖ 错误：为对话加 /talk 控制台路由，或让页面直接调用 LineRunner
# ✔ 正确：页面只调用 3.1 的五个操作；M15 不加路由

# ✖ 错误：用人名规则拒绝 fake-agent@1，厂长无法 talk_reply
# ✔ 正确：人名规则只用于人的 --by；厂长 by 是运行时引用，允许 @（5.1）

# ✖ 错误：厂长下一轮才 changeset_propose，因为本轮没有 human talk 就不写卡片
# ✔ 正确：常驻厂长任一轮成功的提议都追加卡片（5.5）

# ✖ 错误：确认运行永远用 active 快照，金丝雀期间对话启动的 run 不进候选
# ✔ 正确：EpochRouter.choose，key 与 factory line run 相同（5.6）

# ✖ 错误：把轮次 id 只放进 wake_id，session_id 改成空；或 wake_id 仍是 None
# ✔ 正确：wake_id 从 None 改成轮次 id，session_id 保持为轮次 id（3.4）

# ✖ 错误：answer 返回被确认的原卡片，status 仍是 proposed
# ✔ 正确：返回新追加的状态消息，card.status 是 PENDING（5.6）
```

---

## 9. 风险与注意事项

- **一条线程。** 配置讨论和运行确认混在一起。靠 `kind` 和 `card_type` 区分，不靠正文关键词。
- **轮次与工具的时序。** 卡片在工具成功时追加，不看本轮有没有 human talk。未回复判定在轮次结束时做。仍会重试的失败不产生未回复事项；邮箱消息被丢弃时产生。
- **租约。** `serve` 租约过期视为没在跑。测试用 `LeaseStore.acquire("serve", ...)` 持有租约；不要用真的 `factory serve` 进程。
- **打码不是审查。** `redact_obj` 只能替换已存进秘密库的值。卡片结构本身不得有 `value` 字段。
- **确认不是发布。** 确认运行不推进 ChangeSet。epoch 由 `EpochRouter.choose` 决定，与 `factory line run` 同一条 key。
- **Windows。** 路径用 `pathlib`。命令行参数是列表，禁止 `shell=True`。
