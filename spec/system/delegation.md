# delegation：授权模式、厂长审批与 ChangeSet 签核（M11）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M11-02、M11-03**；端到端演练 **M11-09** 的验收也列在本文件第 7 章。设计说明见 `plan/design-delegation.md`。
> 人通过 `factory.json` 决定交给厂长多少权力。`standard` 模式保留现有审批。`bypass` 模式把审批全部交给厂长，但 V1–V4、影子运行、金丝雀与自动回滚、安全模式、沙箱隔离和秘密规则都不变，宪法仍只由人修改。
> 本文件同时补上下面几处缺口：
> - `foreman` 路由的审批没有唤醒票，厂长也没有决定动作，job 会永远停在 `AWAITING_APPROVAL`。
> - 操作工没有办法向厂长申请。
> - ChangeSet 没有人工签核；只有一个 agent CLI 时，T2/T3 永远凑不齐复核。
> - 新产线在金丝雀期间会被路由到没有它的 active epoch。
>
> 本文件与 `approvals.md`、`governance.md`、`foreman.md`、`mcp.md`、`supervisor.md`、`factoryctl.md`、`daemon.md` 冲突时，以本文件为准。

---

## 1. 目的与范围

**做什么**

- `factory.json` 新增 `autonomy` 段，对应模块 `mao.delegation`。
  - 授权模式：`standard` 或 `bypass`。
  - 每个风险等级的 ChangeSet 签核方式：`auto`、`review` 或 `human`。
- 审批路由新增 `human`，审批进入人工收件箱（`inbox.md`）。`bypass` 下，`human` 与 `review` 都改由厂长决定。
- `foreman` 路由的审批：
  - 写唤醒票 `approval`；
  - 新增 MCP 工具 `approval_get`、`approval_decide`、`approval_escalate`；
  - 值守为这张票开审批会话。
- 操作工向厂长申请：`factoryctl ask`（ctl op `ask`）。申请总是交给厂长，不读工位的审批规则。
- 定向审批 `Approvals.open_directed`：路由固定为 `foreman`。`ask` 与监控（`capabilities.md`）都用它。
- ChangeSet 人工签核：
  - `ChangePipeline.signoff` 与命令 `factory changeset signoff`；
  - 收件箱的 `signoff` 项；
  - 信号泵只在签核方式为 `review` 时才开复核票。
- 金丝雀路由：产线只存在于候选 epoch 时，run 走候选。
- 命令 `factory autonomy show`。

**不做什么**

| 不做 | 原因或归属 |
|---|---|
| 让厂长修改宪法、`risk.mrl` 或 `factory.json` | L0 只由人改，`bypass` 也不例外。`factory.json` 在 `FACTORY_ROOT`，厂长写不到 |
| `bypass` 下跳过 V1–V4、影子、金丝雀、观察期、安全模式 | 人确认过：`bypass` 只免审批 |
| 厂长代填秘密、代人解验证码 | 需要人，见 `inbox.md` |
| 厂长发起 run、重试 job 等 L2 动作 | 不在本里程碑 |
| `standard` 下降低 `review` 路由的法定人数 | 仍按 `approvals.md` 5.5 |

---

## 2. 依赖

| 依赖 | 用途 |
|---|---|
| `approvals.md` | `Approvals`、路由、`apply_decision`、ctl 响应 |
| `governance.md` | `ChangePipeline.advance`（5.9）、风险等级、`EpochRouter`（5.9） |
| `foreman.md`、`supervisor.md` | `WakeQueue`、会话的 task 与结果判定 |
| `mcp.md` | `McpServer`、`Session`、工具结果格式 |
| `factoryctl.md` | 请求 / 响应文件协议（4.3、5.4） |
| `inbox.md` | `Inbox.open`、`Inbox.close`、`answer`（M11-01） |
| `daemon.md` 5.5 | 信号泵 |

---

## 3. 接口

### 3.1 `mao.delegation`

```python
Mode = Literal["standard", "bypass"]
SignoffMode = Literal["auto", "review", "human"]
TIERS = ("T0", "T1", "T2", "T3")

@dataclass(frozen=True)
class AutonomyConfig:
    mode: Mode
    changesets: Mapping[str, SignoffMode]      # 总有 T0–T3 四个键；缺的键补 "review"

    @classmethod
    def load(cls, root: Path) -> "AutonomyConfig"     # 读 $FACTORY_ROOT/factory.json，见 4.1
    def route(self, route: str) -> str                 # 5.1
    def signoff(self, tier: str) -> SignoffMode        # 5.5；未知等级 → AUTONOMY_CONFIG_INVALID
```

### 3.2 `mao.approvals` 的增补

- 工位 schema 的审批人（`station.schema.json` 的 `$defs/approver`）增加 `human`。`auto_if` 的 `else` 也可以是 `human`。
- `Approval.route` 的取值增加 `human`。表结构不变。

```python
class Approvals:
    def open_directed(self, *, job_id: str, attempt: int, station_key: str,
                      payload: Mapping[str, Any], detail: str = "",
                      timeout: str | None) -> Approval
        # 5.3。phase 固定 in_run，route 固定 foreman
    def escalate(self, approval_id: str, *, by: str, note: str = "") -> Approval
        # 5.4。foreman → human
```

### 3.3 MCP 工具（只给 `foreman` 角色，追加到 `mcp.md` 3.3）

| 工具 | 参数 | 调用 | 返回 |
|---|---|---|---|
| `approval_get` | `approval_id` | `Approvals.get` | 4.3 的审批对象 |
| `approval_decide` | `approval_id`、`decision`（`approve` / `deny`）、`note` | 5.2 | 结案后的审批对象 |
| `approval_escalate` | `approval_id`、`note` | `Approvals.escalate(id, by="foreman:" + session.actor, note=note)` | 改道后的审批对象 |

`reviewer` 角色看不到这三个工具，调用时得到 `-32602`（`mcp.md` 3.3 的规则）。

### 3.4 `factoryctl ask`

```
factoryctl ask --summary TEXT [--kind KIND] [--detail-file PATH]
```

- `--kind` 缺省为 `help`，必须匹配 `^[a-z][a-z0-9-]{0,31}$`。
- `--detail-file` 按 UTF-8 读取，读出的文本最多 8000 个码点。
- 下列情况退出码 2，不写请求：缺少 `--summary`；`--summary` 为空；`--kind` 不合格；详情文件不存在或超长。
- 成功时写 ctl 请求，op 为 `ask`，args 为 `{"kind": K, "summary": S, "detail": D}`（没有详情时 `D` 为 `""`）。
- 之后的等待、上限、输出与退出码都与 `factoryctl approval request` 相同（`approvals.md` 3.3、`factoryctl.md` 5.4）。stdout 是 `approvals.md` 4.3 的响应 JSON。

### 3.5 ChangeSet 签核

```python
class ChangePipeline:
    def signoff(self, cs_id: str, decision: Literal["approve", "deny"], *, by: str,
                note: str = "") -> ChangeSet          # 5.6
    def signoff_of(self, cs_id: str) -> dict[str, Any] | None
        # {"decision", "by", "note", "at"}；没有签核则 None
```

```
factory changeset signoff CS_ID approve|deny --by human:NAME [--note TEXT] [--json]
factory autonomy show [--json]
```

- `signoff` 成功时退出码 0，输出 ChangeSet 的 JSON。`SIGNOFF_INVALID`、`CHANGESET_STATE_INVALID`、`CHANGESET_NOT_FOUND` 退出码 1。缺少 `--by` 退出码 2。
- `autonomy show` 输出 4.2 的对象，退出码 0。配置不合格时报 `AUTONOMY_CONFIG_INVALID`，退出码 1。

### 3.6 金丝雀路由

```python
class EpochRouter:
    def choose(self, key: str, *, line: str | None = None) -> str     # 5.7
```

`factory line run`（`commands/common.py` 选快照处）和守护进程按触发器建 run 时（`daemon.py`），把产线引用（`NAME@N`）作为 `line` 传入。

---

## 4. 数据格式

### 4.1 `factory.json` 的 `autonomy` 段（人维护，可省略）

```json
{"autonomy": {"mode": "standard", "changesets": {"T0": "auto", "T1": "auto", "T2": "review", "T3": "human"}}}
```

| 字段 | 说明 |
|---|---|
| `mode` | `standard` 或 `bypass`。缺省 `standard` |
| `changesets` | 对象，键只能是 `T0`–`T3`，值只能是 `auto`、`review`、`human`。缺的键补 `review` |

- 整段缺省：`mode = "standard"`，四个等级都是 `review`。这与 M10 之前的行为相同。
- 段里出现别的键、`mode` 取值不对、`changesets` 不是对象、键或值不合格，都报 `AUTONOMY_CONFIG_INVALID`，`detail.field` 指向第一个出错的位置，例如 `mode`、`changesets`、`changesets.T9`、`changesets.T2`。
- **每次判断都重新读文件**：
  - `Approvals.open_*` 在开审批时读；
  - `ChangePipeline.advance` 每推进一步读；
  - 信号泵每个 tick 读。

  人改了文件就生效，不需要重启 `serve`。

### 4.2 `factory autonomy show` 的输出

```json
{"mode": "bypass", "changesets": {"T0": "auto", "T1": "auto", "T2": "auto", "T3": "auto"},
 "configured": {"T0": "auto", "T1": "auto", "T2": "review", "T3": "human"}}
```

- `changesets`：生效值，即 `signoff(tier)` 的结果。
- `configured`：文件里的值，缺的键已补成 `review`。

### 4.3 MCP 返回的审批对象

`Approval` 的全部字段（`approvals.md` 3.1），另加三个字段：

- `job`：`{"id", "station", "state"}`；
- `detail`：`ask` 的详情文本，其他审批为 `""`；
- `kind`：in_run 为载荷的 `kind`，propose 为 `"proposal"`。

整个对象经 `redact_obj` 处理后才返回。

### 4.4 载荷与存储

- `open_directed` 的 `payload` 只有 `approvals.md` 4.2 的 in_run 三字段：`kind`、`summary`、`cost_usd`。`detail` 存进 `approvals.payload` 列的 `detail` 键，求值前剥掉（定向审批也不求值）。
- `ask` 的 `kind` 写成 `"ask:" + KIND`。监控的写法见 `capabilities.md`。

### 4.5 迁移 `src/mao/migrations/037_signoffs.sql`

```sql
CREATE TABLE changeset_signoffs (
  changeset_id TEXT PRIMARY KEY,
  decision     TEXT NOT NULL CHECK (decision IN ('approve', 'deny')),
  by           TEXT NOT NULL,
  note         TEXT NOT NULL DEFAULT '',
  at           TEXT NOT NULL
);
```

### 4.6 事件

| kind | data |
|---|---|
| `approval.delegated` | `{id, from, to}`：`bypass` 把路由从 `from` 改成了 `to` |
| `approval.escalated` | `{id, by}` |
| `changeset.signoff` | `{id, decision, by}` |

`approvals.md` 4.4 的四种事件，data 不变。

---

## 5. 行为

### 5.1 路由改写

`approvals.md` 5.2 第 6 步得出最终路由之后、第 7 步写事件之前，先调用 `AutonomyConfig.load(ctx.root).route(最终路由)`：

| 最终路由 | `standard` | `bypass` |
|---|---|---|
| `auto` | `auto` | `auto` |
| `foreman` | `foreman` | `foreman` |
| `review` | `review` | `foreman` |
| `human` | `human` | `foreman` |

- 改写后的路由才是 `Approval.route`。路由被改写时，在 `approval.requested` 之前写 `approval.delegated`。
- `preset` 仍然最先生效，不经过改写（只给测试用）。
- `review` 在 `standard` 下仍按 `approvals.md` 5.5 处理，法定人数未设置时仍报 `REVIEW_QUORUM_UNSET`。在 `bypass` 下它已经变成 `foreman`，不再读法定人数。

按改写后的路由，`approvals.md` 5.2 第 8 步的处理增加下面两种：

| 路由 | 处理 |
|---|---|
| `foreman` | 保持 `pending`，job 进入 `AWAITING_APPROVAL`。然后 `WakeQueue.file("approval", approval_id, {"id": approval_id, "job": job_id, "kind": kind})`。这一步不在审批的事务里：票写失败时只记日志，审批照常停在 `pending` |
| `human` | 保持 `pending`，job 进入 `AWAITING_APPROVAL`。然后 `Inbox.open("approval", subject=approval_id, origin="job:" + job_id, summary=载荷的 summary, payload={"approval": id, "job": job_id, "station": station_key, "phase": phase, "kind": kind, "cost_usd": 金额, "deadline": deadline})` |

- 表中的 `kind` 与 `approval_get` 返回的 `kind` 相同（4.3）。
- 表中的“金额”：in_run 取 `cost_usd`，propose 取 `cost_estimate_usd`。

**结案时关闭收件箱项**。审批离开 `pending` 时，无论经过 `apply_decision`、`expire_due` 还是复核计票，只要有 `kind == "approval"` 且 `subject == approval_id` 的 `open` 收件箱项，就把它关掉：

- 批准或拒绝：`Inbox.close(id, status="answered", answer={"decision": d, "note": note}, by=by)`；
- 超时：`status="expired"`、`answer=None`、`by="timeout"`。

### 5.2 厂长决定

`approval_decide(approval_id, decision, note)`：

1. `Approvals.get`。审批不存在时报 `APPROVAL_NOT_FOUND`。
2. `route != "foreman"` 时报 `FOREMAN_FORBIDDEN`，`detail.op = "approval_decide"`。这样厂长不能替人决定 `human` 路由的审批，也不能投复核票。
3. `apply_decision(approval_id, decision, by="foreman:" + session.actor, note=note)`。状态不对时照样报 `APPROVAL_STATE_INVALID`。

`approval_get` 对任何路由的审批都可以调用，因为厂长需要看清自己转交出去的事项。

### 5.3 定向审批 `open_directed`

1. 用 `approvals.md` 4.2 的 in_run 规则校验 `payload`，不合格报 `APPROVAL_PAYLOAD_INVALID`。`detail` 必须是最多 8000 个码点的字符串，否则同样报这个码。
2. 对字符串先 `redact`，再插入 `pending` 行：`phase = in_run`，`route = "foreman"`。
3. `deadline`：`timeout` 不为 `None` 时为 `created_at + parse_duration(timeout)`，否则为 `None`。
4. 写 `approval.requested`，job 进入 `AWAITING_APPROVAL`，按 5.1 表中 `foreman` 一行写唤醒票。
5. 定向审批不读工位规则，不求值，不消费 `preset`，也不经过 5.1 的改写，所以在 `bypass` 下仍交给厂长。
6. 定向审批的结案、超时与 ctl 响应都和普通 in_run 审批相同（`approvals.md` 5.3、4.3）。

### 5.4 改道给人 `escalate`

1. 审批必须是 `pending` 且 `route == "foreman"`，否则报 `APPROVAL_STATE_INVALID`。
2. 一个事务里完成：`route := "human"`，写 `approval.escalated`。
3. 按 5.1 表中 `human` 一行开收件箱项，并在 `payload` 里加上 `note`。
4. 已经交出去的唤醒票不需要撤回。值守在会话结束后按 5.8 判定这张票成功。

`bypass` 下厂长也可以改道。人在 `factory.json` 里放权以后，厂长仍然可以主动把事项交回给人。

### 5.5 `ask`：操作工向厂长申请

工位 job 的 ctl handler（`runner.md` 的 `_CtlHandler`）增加 op `ask`：

1. 校验 args：
   - `kind` 匹配 3.4 的模式；
   - `summary` 是非空字符串，最多 2000 个码点；
   - `detail` 是字符串，最多 8000 个码点。

   不合格时回 `ok: false`，`error.code = "CTL_REQUEST_INVALID"`。
2. 第一次见到这个请求 id 时调用 `open_directed`：
   - `payload = {"kind": "ask:" + kind, "summary": summary, "cost_usd": 0.0}`；
   - `detail = detail`；
   - `timeout` 取工位的 `approval.timeout`，没有则为 `None`。

   把请求 id 映射到审批 id，映射方式与 op `approval` 相同。同一个请求 id 再次轮询时不重新开审批。
3. 审批未结案时返回 `None`。结案后返回 `approvals.md` 4.3 的响应。
4. 工位没有任何审批规则时 `ask` 也照样可用：申请不是审批规则授予的权力，只是一条交给厂长的消息。

### 5.6 ChangeSet 签核

`AutonomyConfig.signoff(tier)`：

- `bypass` 下总是 `auto`；
- `standard` 下返回 `changesets[tier]`。

`governance.md` 5.9 表中 `verified` 的第二行（复核数不够就不动）改成按签核方式判断。第一行（安全模式）仍然在它前面，不变。

| 签核方式 | 条件 | 动作 |
|---|---|---|
| `auto` | — | 不看复核，继续往下判断（`queued`、`stale`、进入 `shadow`） |
| `review` | `len(approvals) < reviews_required` | 不动，报告 `reason: "reviews"`。这是原来的行为 |
| `human` | 没有签核 | 不动，报告 `reason: "signoff"`。没有 `open` 的 `signoff` 收件箱项时开一个：`Inbox.open("signoff", subject=cs.id, origin="governance", summary=f"{tier} {cs.id}", payload={"changeset": cs.id, "tier": tier, "changes": 文档的 changes, "impact": 报告里的 impact})` |
| `human` | 签核为 `deny` | `rejected`，`code = "SIGNOFF_DENIED"`，`detail = {"by": by}` |
| `human` | 签核为 `approve` | 继续往下判断 |

- 因签核或复核不够而不动时，阶段报告（`governance.md` 4.5）除了 `reason`，还增加 `signoff` 键，值为本次判断用的签核方式。
- 签核方式只在 `verified` 这一步判断。已经进入 `shadow` 或 `canary` 的 ChangeSet，不会因为人中途改了配置而退回。

`ChangePipeline.signoff(cs_id, decision, *, by, note)`：

1. ChangeSet 不存在时报 `CHANGESET_NOT_FOUND`。
2. 状态不是 `verified` 时报 `CHANGESET_STATE_INVALID`。
3. `by` 不匹配 `^human:\S+$`、`decision` 不是 `approve` 或 `deny`、`note` 超过 2000 个码点时，报 `SIGNOFF_INVALID`，`detail.reason` 分别为 `by`、`decision`、`note`。
4. 已有签核时报 `CHANGESET_STATE_INVALID`。签核不能覆盖。
5. 插入 `changeset_signoffs`，写 `changeset.signoff`，再按 5.1 的方式关掉 `kind == "signoff"`、`subject == cs_id` 的 `open` 收件箱项（`answer = {"decision", "note"}`）。
6. 签核本身不推进状态，由下一次 `advance` 处理。签核也不受当前签核方式限制：人在 `review` 方式下也可以先签，等配置改成 `human` 时立即生效。

收件箱项用 `factory inbox answer ID --approve|--deny` 回答时，调用的就是 `signoff`（`inbox.md` 5.3）。

### 5.7 信号泵与路由

**信号泵**（`daemon.md` 5.5）：对 `verified` 的 ChangeSet，只有 `signoff(risk_tier) == "review"` 时才按原规则开 `review` 唤醒票。`auto` 和 `human` 方式下不开。

**金丝雀路由** `choose(key, *, line)`：

1. 没有 `canary` 状态的 ChangeSet：返回 active（不变）。
2. 给了 `line`，它在 active 快照里解析不到、在候选快照里解析得到：返回候选 commit。解析的方式是 `snapshot.doc(snapshot.key("Line", line))` 抛不抛 `SPEC_NOT_FOUND`。`Snapshot.key` 只拼键，从不抛错，不能单独用来判断存在。快照用 `EpochStore.materialize`。
3. 其他情况：按原来的哈希分桶。

新产线只在候选 epoch 里存在。如果按分桶把 run 送回 active，run 会失败，金丝雀也永远凑不够样本。

### 5.8 值守

`supervisor.md` 5.1 的修改：

**第 3 步（角色）**：`approval` 与 `inbox` 信号的会话角色是 `foreman`。

**第 5 步（选 runtime）**：对 `approval` 票，先 `Approvals.get(wake.subject)`。审批不存在、不是 `pending` 或 `route != "foreman"` 时，票直接 `done`，返回 `None`，不开会话。

**task**（`supervisor.md` 4.7）：`approval` 票的厂长 task 用下面这段，`{...}` 按 4.7 的规则替换：

```text
你是这座工厂的厂长。本次会话处理审批 {subject}（唤醒票 {wake.id}）。载荷：{payload}。
用 MCP 服务器 factory 的工具 approval_get 查看请求，再调用 approval_decide，decision 为 approve 或 deny，note 写理由。
你无法判断时调用 approval_escalate，把它交给人。
处理完就退出。
```

`inbox` 票用厂长原来的 task。

**结果判定**（5.5）：`approval` 票的会话退出码为 0，并且审批已经不是“`pending` 且 `route == "foreman"`”（已经结案或已改道）时才算成功。否则按失败处理，错误码 `APPROVAL_UNDECIDED`，退避后重开。规则与转换票“必须留下提议”相同。

### 5.9 审批的接线（取代 `runner.md` 5.6 的 approval 部分）

现有的 `Runner` 仍按 M1 自己读写 `approvals` 表，没有经过 `Approvals`。这样路由改写、唤醒票、收件箱都接不上。M11 改成：

1. **ctl op `approval`**：
   - 按 `approvals.request_id` 查这个请求 id。没有时调用 `Approvals.open_request(..., request_id=请求 id)`。同一个请求 id 只开一次审批。
   - `open_request` 抛 `APPROVAL_RULE_MISSING` 或 `REVIEW_QUORUM_UNSET` 时，返回 `decision: "deny"`，`code` 就是这个码。
   - 每次轮询先调用 `expire_due()`，再读审批。还是 `pending` 时返回 `None`。
   - 结案后按 `approvals.md` 4.3 返回 `{"ok": true, "result": {"decision", "note", "code"}}`。拒绝和超时也是 `ok: true`，`factoryctl approval request` 照样退出 0。
   - handler 内部不再有 600 秒的缺省期限。工位没写 `approval.timeout` 时，审批没有期限。
2. **job 状态**：路由是 `foreman`、`human` 或 `review` 时，审批开出后 job 进入 `AWAITING_APPROVAL`（5.1）。handler 看到审批结案时，把 job 改回 `RUNNING`。
3. **`Runner.decide`**：存在性与状态检查不变，然后委托给 `Approvals.apply_decision`。`decided_by` 与收件箱项都由它处理。
4. **载荷**：`approvals.payload` 列保存经 `redact_obj` 的完整载荷，包括 `summary`。收件箱与 `approval_get` 都要显示它。`for_provenance` 仍不放 `summary`（`approvals.md` 4.4）。
5. **到期**：`kind` 以 `ask:` 或 `monitor:` 开头、或等于 `egress` 的审批到期时，照常结案并写 `approval.expired`，但 job 不因此失败。操作工收到 `timeout` 响应后自己决定怎么办。其他审批到期仍按 `approvals.md` 让 job `FAILED` / `APPROVAL_TIMEOUT`。`Runner` 在 `exec` 之后判断“本次 attempt 有没有超时的审批”时，同样跳过这三种。

---

## 6. 错误

新码都要登记到 `mao.errors`。

| 码 | 何时 | detail |
|---|---|---|
| `AUTONOMY_CONFIG_INVALID` | 4.1 | `field` |
| `SIGNOFF_INVALID` | 5.6 第 3 步 | `reason` |
| `SIGNOFF_DENIED` | ChangeSet 因签核拒绝而 `rejected` 的 `code`（不抛出） | `by` |
| `APPROVAL_UNDECIDED` | 审批会话结束时审批仍由厂长待决（会话行的 `error`，不抛出） | `approval` |

沿用的码：`FOREMAN_FORBIDDEN`、`APPROVAL_NOT_FOUND`、`APPROVAL_STATE_INVALID`、`APPROVAL_PAYLOAD_INVALID`、`CHANGESET_NOT_FOUND`、`CHANGESET_STATE_INVALID`、`CTL_REQUEST_INVALID`。

---

## 7. 验收测试

夹具：`tests/acceptance/m11/deleg_world.py`。

- 它把 `registry-m2` 复制成 Git 仓库，增加机器 `deskbox@1` 与若干工位、策略。操作工是测试运行时写出的标准库脚本，经 `LocalProvider` 的命令映射执行。
- 它写 `factory.json` 的 `governance`、`foreman`（fake agent）、`autonomy`、`inbox` 段，初始化 epoch，`Runner` 由 `open_runner` 构造。
- 提供的入口：
  - `World.start` 在后台线程里跑 `Runner.run`；
  - `World.mcp` 经 `McpServer.handle` 调用工具；
  - `World.cli` 经 `mao.cli.main` 操作工厂目录；
  - `World.supervisor` 构造真实的 `ForemanSupervisor`；
  - `World.pipeline` 构造 `ChangePipeline`，契约测试与影子运行用替身。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m11/test_m11_02_approvals.py` | `test_autonomy_config` | 缺省为 `standard` 加四个 `review`；`bypass` 时 `factory autonomy show` 的 `changesets` 全是 `auto`、`configured` 保留文件值；未知键、坏值报 `AUTONOMY_CONFIG_INVALID`，`field` 正确 |
| | `test_foreman_route_wakes_and_decides` | `else: foreman` 的 in_run 审批：job `AWAITING_APPROVAL`，开出 `approval` 唤醒票；`approval_get` 返回 `kind`、`job`；`approval_decide` 批准后 job `SEALED`，`decided_by` 是 `foreman:fake-agent@1` |
| | `test_supervisor_decides_approval` | 值守为 `approval` 票开厂长会话，fake agent 经 MCP 批准：审批 `approved`、票 `done`。另一张票的会话没有决定：会话失败，错误码 `APPROVAL_UNDECIDED`。票所指审批已经结案时不开会话 |
| | `test_human_route_uses_inbox` | `in_run: human`：收件箱出现 `approval` 项，`approval_decide` 报 `FOREMAN_FORBIDDEN`；`factory inbox answer --deny` 使审批 `denied`、收件箱项 `answered`，操作工收到 `deny` |
| | `test_bypass_delegates_to_foreman` | `bypass`：`human` 路由改成 `foreman` 并写 `approval.delegated`；`review` 路由在法定人数未设置时也交给厂长，不报 `REVIEW_QUORUM_UNSET` |
| | `test_escalate_moves_to_inbox` | 厂长 `approval_escalate`：路由变成 `human`，写 `approval.escalated`，收件箱项带 `note`；人批准后 job `SEALED` |
| | `test_factoryctl_ask` | 工位规则是 `auto` 时，`factoryctl ask --kind access --detail-file` 仍交给厂长：`kind` 为 `ask:access`，`approval_get` 带详情；厂长拒绝时操作工的 stdout 是 `deny` 加 note；用法错误退出码 2 且不写请求 |
| `tests/acceptance/m11/test_m11_03_signoff.py` | `test_auto_signoff_skips_reviews` | T2 设为 `auto`：`verified` 的 ChangeSet 没有任何复核也直接进入 `shadow`，不开收件箱项 |
| | `test_human_signoff_flow` | T2 设为 `human`：停在 `reason: "signoff"` 并开 `signoff` 收件箱项（重复推进不重复开）；`factory inbox answer --approve` 记录签核、关掉收件箱项，下一次推进进入 `shadow`；另一个 ChangeSet 被 `factory changeset signoff … deny` 后 `rejected`，`SIGNOFF_DENIED` |
| | `test_bypass_keeps_shadow` | `bypass` 下 T3 的 Sink 变更不等复核，但影子运行失败时仍然 `rejected`，`SHADOW_FAILED` |
| | `test_signoff_errors` | `by` 不是 `human:` → `SIGNOFF_INVALID`；状态不是 `verified` → `CHANGESET_STATE_INVALID`；第二次签核 → `CHANGESET_STATE_INVALID`；命令行缺 `--by` 退出码 2 |
| | `test_pump_files_reviews_only_in_review_mode` | 守护进程的信号泵在 `review` 方式下为 T2 开 `review` 票，改成 `human` 后另一个 ChangeSet 不开票 |
| | `test_new_line_routes_to_candidate` | 金丝雀阶段的 ChangeSet 新增一条产线：`factory line run` 这条产线时，run 的 epoch 是候选 commit |
| `tests/acceptance/m11/test_m11_09_onboarding.py` | `test_bypass_onboarding_walkthrough` | `bypass` 下的完整演练：厂长会话经 MCP 提议机器、策略、Sink 工位与产线（T3），不等复核经影子进入金丝雀；厂长会话申请秘密，人填值通过核验；产线两次运行都请求验证码协助，人回答后发布，回执核验带秘密经代理访问上游，run 落在候选 epoch；样本够了之后推广为 active |

验收命令：`uv run python tools/harness.py pytest <文件>`，断言 `out.failed == 0 && out.errors == 0 && out.skipped == 0`，`passed` 不少于上表的行数。

---

## 8. 错误示范与正确示范

```python
# ✖ 错误：bypass 下把审批全改成 auto，省得叫醒厂长
# ✔ 正确：bypass 把 human / review 改成 foreman。auto_if 不成立的事项仍由厂长判断，并留下 decided_by
```

```python
# ✖ 错误：bypass 时 advance 直接推广到 active，跳过影子与金丝雀
# ✔ 正确：bypass 只让签核方式变成 auto。安全模式、shadow、canary、观察期与回滚照旧
```

```python
# ✖ 错误：approval_decide 不看路由，厂长也能批准 human 路由的审批
# ✔ 正确：route != "foreman" → FOREMAN_FORBIDDEN。要交给人，用 approval_escalate
```

```python
# ✖ 错误：ask 按工位的 approval.in_run 规则求值，auto_if 成立就自动批准
# ✔ 正确：ask 是 open_directed，总是交给厂长。工位规则只管 approval request
```

```python
# ✖ 错误：把 autonomy 读进进程常量，人改了 factory.json 要重启 serve
# ✔ 正确：每次开审批、每次 advance、每个信号泵 tick 都调用 AutonomyConfig.load
```

---

## 9. 风险与注意事项

- **bypass 的边界**：厂长可以用 ChangeSet 加 Sink、改策略、加出网域名，这些变更都会自动生效（仍然要过验证、影子、金丝雀）。人打开 `bypass` 之前要明白这一点。`factory autonomy show` 给出的是当前生效值。
- **单一 CLI**：只装了一个 agent CLI 时，`review` 方式的 T2/T3 永远凑不齐复核。这时人应该选 `human` 或 `auto`。信号泵在 `review` 方式下才开复核票，值守仍会在复核者用尽时报 `REVIEWERS_EXHAUSTED`。
- **没有值守**：没有配置 `foreman` 段、或 `serve` 没有运行时，`foreman` 路由与 `ask` 会一直停在 `pending`，直到超时。工位应该写 `approval.timeout`。
- **审批会话的成本**：每个交给厂长的审批都会开一个会话。`auto_if` 写得越准，厂长被叫醒的次数越少。
