# inbox：人工收件箱——上报、秘密申请与人工协助（M11）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M11-01、M11-05、M11-06**。设计说明见 `plan/design-delegation.md`。
> 凡是需要人亲自处理的事项，都进同一个收件箱：
> - 厂长向人提的问题（上报）；
> - 交给人的审批与 ChangeSet 签核（`delegation.md`）；
> - 秘密申请：人填值，系统先核验再保存；
> - 操作工请求的人工协助，例如验证码。
>
> 收件箱是一个独立功能：有自己的存储、命令、控制台页面和通知钩子。其他模块只调用 `Inbox.open` 和 `Inbox.close`。
> 本文件与 `console.md`、`egress.md`、`factoryctl.md`、`mcp.md`、`commands.md` 冲突时，以本文件为准。

---

## 1. 目的与范围

**做什么**

- `mao.inbox`：
  - 存储人工事项：`open`、`get`、`list`、`close`、`cancel`；
  - `answer` 按事项种类把人的回答交给负责的模块；
  - 通知钩子。
- 命令：`factory inbox ls/show/answer/cancel`。
- 控制台：
  - 收件箱的列表页与详情页；
  - 只对 `POST /inbox/<id>` 开放写入，带 CSRF 校验；
  - 人工协助的截图页面，可以点图回答。
- 厂长的上报：MCP 工具 `escalate`、`inbox_get`、`inbox_list`。人回答后写唤醒票 `inbox`。
- 秘密申请（M11-05）：
  - 机器的 `needs.secret_checks` 声明核验命令；
  - MCP 工具 `secret_request`、`secret_verify`；
  - 人填值后在核验沙箱里核验，通过才保存；
  - 命令 `factory secret set`、`factory secret verify`。
- 人工协助（M11-06）：
  - 策略的 `handoff` 段规定允许哪些种类、等多久；
  - 命令 `factoryctl handoff`，可以附截图；
  - 回答方式：文字、点、多个点、滑块偏移、已完成。

**不做什么**

| 不做 | 原因或归属 |
|---|---|
| 实时远程浏览器（CDP 画面转发） | 以后的里程碑。本文件在 4.4 里给 `view.kind` 留了 `live` 这个值 |
| 让厂长读秘密值，或让秘密值进入事件、日志、收件箱、MCP | D13 |
| 控制台的其他写操作（回滚、重开票、提议） | 只开放回答收件箱这一个入口 |
| 推送服务的实现（Windows 通知、聊天机器人） | 由人写通知钩子脚本，本文件只负责调用 |

---

## 2. 依赖

| 依赖 | 用途 |
|---|---|
| `Context`（README 4.1） | 时钟、ids、数据库 |
| `egress.md` | `SecretStore`、`build_egress`、代理环境；秘密文件布局（4.1） |
| `capabilities.md` | 出网主体与令牌（核验沙箱要用） |
| `approvals.md`、`delegation.md` | `Runner.decide`、`ChangePipeline.signoff` |
| `foreman.md`、`mcp.md` | `WakeQueue`、`McpServer`、`Session` |
| `console.md` | 鉴权、Host 校验、响应头、转义 |
| `factoryctl.md` | 请求 / 响应、虚拟路径映射 |

---

## 3. 接口

### 3.1 `mao.inbox`

```python
class RequestKind(StrEnum):
    QUESTION = "question"; APPROVAL = "approval"; SIGNOFF = "signoff"
    SECRET = "secret"; HANDOFF = "handoff"

class RequestStatus(StrEnum):
    OPEN = "open"; ANSWERED = "answered"; CANCELLED = "cancelled"; EXPIRED = "expired"

@dataclass(frozen=True)
class HumanRequest:
    id: str                        # "hr-0001"
    kind: str
    subject: str                   # 4.2
    origin: str                    # "foreman:<runtime>" | "job:<job id>" | "governance"
    summary: str
    payload: dict[str, Any]        # 已脱敏
    status: str
    answer: dict[str, Any] | None  # 已脱敏；秘密值永远不在这里
    answered_by: str | None
    deadline: str | None
    created_at: str
    answered_at: str | None

    def to_json(self) -> dict[str, Any]          # 4.3

class Inbox:
    def __init__(self, ctx: Context) -> None
    def open(self, kind: str, *, subject: str, origin: str, summary: str,
             payload: Mapping[str, Any], deadline: str | None = None) -> HumanRequest
    def get(self, request_id: str) -> HumanRequest
    def list(self, *, status: str | None = None, kind: str | None = None) -> list[HumanRequest]
    def find(self, kind: str, subject: str) -> HumanRequest | None      # status == open 的那一条
    def close(self, request_id: str, *, status: Literal["answered", "cancelled", "expired"],
              answer: Mapping[str, Any] | None, by: str) -> HumanRequest
    def cancel(self, request_id: str, *, by: str, note: str = "") -> HumanRequest

def answer(ctx: Context, request_id: str, reply: Mapping[str, Any], *, by: str) -> HumanRequest
    # 5.3。命令行和控制台都只调用这个函数
```

### 3.2 `mao.secrets`（M11-05）

```python
@dataclass(frozen=True)
class CheckResult:
    ok: bool
    code: str                     # "" | "SECRET_INVALID" | "SECRET_CHECK_ERROR"
    exit_code: int | None
    log: str                      # 相对 FACTORY_ROOT 的日志路径

def request_secret(ctx: Context, snapshot: Snapshot, *, name: str, secret_class: str,
                   station: str, reason: str, origin: str) -> HumanRequest      # 5.5
def verify_secret(ctx: Context, snapshot: Snapshot, *, station: str, name: str, value: str,
                  label: str) -> CheckResult                                  # 5.6
def store_secret(root: Path, name: str, secret_class: str, value: str) -> None   # 5.7
def normalize_value(raw: bytes) -> str                                          # 5.5 第 1 步
```

### 3.3 `mao.handoff`（M11-06）

```python
def open_handoff(ctx: Context, snapshot: Snapshot, *, job_id: str, attempt: int, station_key: str,
                 request_id: str, args: Mapping[str, Any], screenshot: Path | None) -> dict[str, Any] | HumanRequest
    # 5.9。不允许或参数不对时返回一个 ctl 响应 dict；否则返回开出的收件箱项
def poll_handoff(ctx: Context, request: HumanRequest) -> dict[str, Any] | None
    # 5.10。还在等时返回 None，否则返回 ctl 响应的 result
def check_answer(request: HumanRequest, reply: Mapping[str, Any]) -> dict[str, Any]
    # 5.11。返回规范化后的回答；不合格 → INBOX_ANSWER_INVALID
```

### 3.4 命令行

```
factory inbox ls [--status open|answered|cancelled|expired] [--kind KIND] [--json]
factory inbox show ID [--json]
factory inbox answer ID (--choice C | --text T | --approve | --deny | --secret-file P | --secret-stdin
                         | --point X,Y | --points "X,Y;X,Y" | --offset N | --done) [--note TEXT] [--json]
factory inbox cancel ID [--note TEXT] [--json]
factory secret set NAME --class publish|ingress|other (--file P | --stdin) [--check STATION@N] [--json]
factory secret verify NAME --station STATION@N [--json]
```

- `by` 取环境变量 `$FACTORY_ACTOR`，没有则为 `human:cli`。规则与 `images.md` 相同。
- `ls` 不带 `--status` 时只列 `open` 的事项。排序：`created_at`、`id` 升序。
- `answer` 必须恰好给一个回答旗标，否则退出码 2。`--point`、`--points`、`--offset` 写法不对时退出码 2。
- 旗标与事项种类不符时报 `INBOX_ANSWER_INVALID`，退出码 1。
- `answer`、`cancel` 成功时退出码 0，输出事项的 JSON（4.3）。秘密核验不通过时报 `SECRET_INVALID`，核验设施故障时报 `SECRET_CHECK_ERROR`，退出码都是 1，事项保持 `open`。
- `secret set`：
  - 带 `--check` 时先核验（5.6），通过才保存；
  - 不带时直接保存，这是人对“不核验”的明确选择；
  - 成功输出 `{"name", "class", "verified": bool}`。
- `secret verify`：用已保存的值核验，取值方式同 MCP `secret_verify`。通过时退出码 0，输出 `{"name", "ok": true, "code": "", "exit_code": 0}`。不通过时报 `SECRET_INVALID`，设施故障时报 `SECRET_CHECK_ERROR`，退出码都是 1。
- `secret set` 的值同样先过 `normalize_value`，不合格报 `SECRET_VALUE_INVALID`。

### 3.5 MCP 工具（追加到 `mcp.md` 3.3）

| 工具 | 参数 | 调用 | 角色 |
|---|---|---|---|
| `escalate` | `summary`；可选 `detail`、`options`（字符串数组，1–10 项） | `Inbox.open("question", subject=<新 id>, origin="foreman:" + actor, summary, payload={"detail": d, "options": o})`。`subject` 就是事项自己的 id | 仅 `foreman` |
| `inbox_get` | `request_id` | `Inbox.get(...).to_json()` | 仅 `foreman` |
| `inbox_list` | 可选 `status` | `Inbox.list(status=...)`，每项 `to_json()` | 仅 `foreman` |
| `secret_request` | `name`、`secret_class`、`station`、`reason` | `request_secret(..., origin="foreman:" + actor)`，快照按 5.5 的“工位所在快照” | 仅 `foreman` |
| `secret_verify` | `name`、`station` | `verify_secret`，值用公开的 `SecretStore.inject([name])[name]` 取（不要访问私有成员），没有保存过时报 `SECRET_NOT_FOUND`；返回 `{"name", "ok", "code", "exit_code"}`，不通过时不抛错，`ok` 为假 | 仅 `foreman` |

### 3.6 `factoryctl handoff`（M11-06）

```
factoryctl handoff --kind KIND --summary TEXT --answer text|point|points|offset|done
                   [--screenshot PATH] [--hint TEXT]
```

- 缺少必填参数、`--answer` 取值不在五个之中、`--kind` 不匹配 `^[a-z][a-z0-9-]{0,31}$` 时，退出码 2，不写请求。
- 成功时写 ctl 请求，op 为 `handoff`，args 为 `{"kind", "summary", "answer", "screenshot", "hint"}`。`screenshot` 是调用方写的虚拟路径，没给就是 `""`；`hint` 没给也是 `""`。
- 之后的等待与退出码与 `approval request` 相同。等待上限取 `contract.json` 的 `handoff.timeout_s` 加 5 秒；没有这个键时，与 `approval request` 相同。stdout 是 4.5 的响应 JSON。
- `Runner` 写 `contract.json` 时，工位策略有 `handoff` 段就加一个键 `"handoff": {"timeout_s": parse_duration(handoff.timeout)}`。

---

## 4. 数据格式

### 4.1 迁移 `src/mao/migrations/036_inbox.sql`

```sql
CREATE TABLE human_requests (
  id           TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,
  subject      TEXT NOT NULL,
  origin       TEXT NOT NULL,
  summary      TEXT NOT NULL,
  payload      TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('open', 'answered', 'cancelled', 'expired')),
  answer       TEXT,
  answered_by  TEXT,
  deadline     TEXT,
  created_at   TEXT NOT NULL,
  answered_at  TEXT
);
CREATE INDEX human_requests_open ON human_requests (kind, subject, status);
```

### 4.2 种类、`subject` 与回答

| kind | 谁开 | `subject` | `reply`（`answer` 的参数） | 关闭时存的 `answer` |
|---|---|---|---|---|
| `question` | 厂长 `escalate` | 事项自己的 id | `{"choice": C}` 或 `{"text": T}` | 同 `reply` |
| `approval` | `delegation.md` 5.1、5.4 | 审批 id | `{"decision": "approve"\|"deny", "note": N}` | 同 `reply` |
| `signoff` | `delegation.md` 5.6 | ChangeSet id | 同上 | 同上 |
| `secret` | `request_secret` | 秘密名 | `{"secret": V}` | `{"verified": true, "check": n}`，不含值 |
| `handoff` | `open_handoff` | `<job id>:<ctl 请求 id>` | 5.11 | 规范化后的回答 |

- 除 `question` 外，同一 `(kind, subject)` 已经有 `open` 事项时，`open` 直接返回原事项：不新插入行，不写事件，也不再通知。
- `question` 总是新开。

### 4.3 `to_json()`

```json
{"id": "hr-0001", "kind": "secret", "subject": "BILI_COOKIE", "origin": "foreman:claude-code@4",
 "summary": "B 站投稿需要登录 Cookie", "payload": {"name": "BILI_COOKIE", "class": "publish", "station": "bili-upload@1",
 "check": {"timeout_s": 60}, "exists": false, "rejected": 0},
 "status": "open", "answer": null, "answered_by": null, "deadline": null,
 "created_at": "2026-11-13T08:00:00.000Z", "answered_at": null}
```

`factory inbox ls --json` 输出这种对象的数组；`show` 和 `answer` 输出单个对象。

### 4.4 各种类的 `payload`

| kind | payload |
|---|---|
| `question` | `{"detail": str, "options": [str, ...]}`。没给就是 `""` 和 `[]` |
| `approval` | `delegation.md` 5.1 |
| `signoff` | `delegation.md` 5.6 |
| `secret` | `{"name", "class", "station", "check": {"timeout_s"}, "exists": bool, "rejected": int}` |
| `handoff` | `{"job", "station", "kind", "answer_type", "hint", "view"}`。`view` 是 `{"kind": "screenshot", "width": w, "height": h}`，没有截图时为 `null`。`kind` 只实现 `screenshot`，`live` 留给以后的实时画面 |

### 4.5 `handoff` 的 ctl 响应

```json
{"decision": "answered", "answer": {"point": [120, 45]}, "note": "", "code": ""}
```

| decision | 何时 | `code` |
|---|---|---|
| `answered` | 人回答了 | `""` |
| `cancelled` | 人取消 | `HANDOFF_CANCELLED` |
| `timeout` | 到期 | `HANDOFF_TIMEOUT` |
| `deny` | 策略不允许这种协助 | `HANDOFF_NOT_ALLOWED` |

不是 `answered` 时 `answer` 为 `null`。`note` 经过 `redact`。

### 4.6 `factory.json` 的增补（人维护）

```json
{"inbox": {"notify": {"argv": ["python", "D:/factory/hooks/toast.py"], "timeout_s": 10}},
 "console": {"port": 8090, "user": "alice"}}
```

- `inbox.notify` 可以省略。写了就必须有 `argv`（非空字符串数组）和 `timeout_s`（正数），不能有别的键。不合格报 `INBOX_CONFIG_INVALID`，`detail.field` 是 `inbox.notify.argv` 这种写法。
- `console.user` 可以省略，缺省为 `"console"`，必须匹配 `^[A-Za-z0-9._-]+$`，否则报 `CONSOLE_CONFIG_INVALID`，`detail.field = "console.user"`。

### 4.7 机器的 `needs.secret_checks`（`machine.schema.json`，M11-05）

```yaml
needs:
  secrets: [BILI_COOKIE]
  secret_checks:
    BILI_COOKIE: {run: [check-bili-login], timeout_s: 60}
```

- 键是秘密名，匹配 `^[A-Z][A-Z0-9_]*$`。
- 值只能有两个键：`run`（非空字符串数组）和 `timeout_s`（正数）。

### 4.8 策略的 `handoff` 段（`policy.schema.json`，M11-06）

```yaml
handoff: {kinds: [captcha, sms], timeout: 15m}
```

- `kinds`：非空、不重复的字符串数组，每项匹配 `^[a-z][a-z0-9-]{0,31}$`。
- `timeout`：时长（`common.schema.json#/$defs/duration`）。
- 两个键都必填，不能有别的键。

### 4.9 磁盘

```
$FACTORY_ROOT/inbox/<id>/screenshot.png        人工协助的截图副本
$FACTORY_ROOT/inbox/<id>/check-<n>/            第 n 次秘密核验的工作目录（workspace、logs）
```

### 4.10 事件

| kind | data |
|---|---|
| `inbox.opened` | `{id, kind, subject, origin}` |
| `inbox.closed` | `{id, status, by}` |
| `inbox.notified` | `{id}` |
| `inbox.notify_failed` | `{id, code}`。`code` 为 `NOTIFY_EXIT`、`NOTIFY_TIMEOUT` 或 `NOTIFY_ERROR` |
| `secret.stored` | `{name, class, by, request}`。`request` 是事项 id，`secret set` 时为 `null` |
| `secret.rejected` | `{name, request, code, exit_code}` |
| `handoff.requested` | `{id, job, kind}` |

---

## 5. 行为

### 5.1 `open`、`close` 与 `cancel`

**`open`**：

1. 先读 `factory.json` 的 `inbox` 段（4.6），每次 `open` 都重新读。不合格报 `INBOX_CONFIG_INVALID`，不插入行。
2. `kind` 必须是 `RequestKind` 之一。`summary` 必须是非空字符串，最多 2000 个码点。`payload` 必须是 JSON 对象。否则报 `INBOX_REQUEST_INVALID`，`detail.reason`。
3. 一个事务里完成：按 4.2 去重，插入 `open` 行（`id = ids.new("hr")`，`payload` 先 `redact_obj`），写 `inbox.opened`。
4. 事务提交之后才通知（5.2），用的是第 1 步读到的配置。去重返回的旧事项不通知。

**`close`**：事项不是 `open` 时报 `INBOX_STATE_INVALID`。写入状态、`answer`（先 `redact_obj`）、`answered_by`、`answered_at`，写 `inbox.closed`。

**`cancel`**：

- 只用于 `question`、`secret`、`handoff`。`approval` 和 `signoff` 要用 `--deny` 拒绝，`cancel` 报 `INBOX_STATE_INVALID`。
- 取消就是 `close(status="cancelled", answer={"note": note})`。

**唤醒厂长**：`origin` 以 `foreman:` 开头的事项被 `answered` 或 `cancelled` 时，写 `WakeQueue.file("inbox", id, {"id": id, "kind": kind, "status": status})`。

### 5.2 通知钩子

1. 没有配置 `inbox.notify`：什么都不做。
2. 否则执行 `subprocess.run(argv, input=canonical_json(body).encode("utf-8"), capture_output=True, timeout=timeout_s, check=False)`。不经过 shell，环境变量继承当前进程。
3. `body` 是 `{"id", "kind", "subject", "origin", "summary", "deadline", "url"}`，其中 `url = f"http://127.0.0.1:{port}/inbox/{id}"`。`port` 取 `console.port`，没有则为 8090。`body` 里没有 `payload`，所以秘密名以外的申请细节不会离开工厂。
4. 退出码 0：写 `inbox.notified`。退出码非 0、超时、`OSError` 时写 `inbox.notify_failed`，**不抛错**，事项照常 `open`。

### 5.3 `answer`：按种类分派

`answer(ctx, request_id, reply, *, by)`：

1. 事项不存在时报 `INBOX_NOT_FOUND`；不是 `open` 时报 `INBOX_STATE_INVALID`。
2. `reply` 的键与种类不符（看 4.2 的表）时，报 `INBOX_ANSWER_INVALID`，`detail.reason = "kind"`。
3. 按种类处理：

| kind | 处理 |
|---|---|
| `question` | 有 `options` 时，`choice` 必须是其中之一；`text` 最多 4000 个码点。不合格报 `INBOX_ANSWER_INVALID`。合格就 `close(answered, reply)` |
| `approval` | 用与 `factory approval decide` 相同的方式取得 `Runner`，调用 `Runner.decide(subject, decision, by=by, note=note)`。收件箱项由 `delegation.md` 5.1 关闭 |
| `signoff` | `ChangePipeline(ctx, EpochStore(ctx)).signoff(subject, decision, by=by, note=note)`，收件箱项由它关闭 |
| `secret` | 5.5 第 2 步之后的流程 |
| `handoff` | `check_answer` 规范化回答，再 `close(answered, 规范化后的回答)`。操作工那边由 5.10 取走 |

4. 返回 `Inbox.get(request_id)`，也就是处理之后的最新状态。

### 5.4 控制台

在 `console.md` 的基础上：

- **页面**：
  - `GET /inbox`：列出 `open` 事项；
  - `GET /inbox/<id>`：显示一个事项和与它种类相符的表单；
  - `GET /api/inbox`：等于 `factory inbox ls --json`；
  - `GET /api/inbox/<id>`：等于 `show --json`。
- **写入**：`console.md` 5.1 的“只读”改为：除 `POST /login` 外，只接受 `POST /inbox/<id>`。处理顺序：
  1. 鉴权与 Host 校验照旧。
  2. 正文是 `application/x-www-form-urlencoded`，最长 70000 字节，否则返回 413。
  3. 字段 `csrf` 必须等于 `hmac.new(token.encode(), ("inbox:" + id).encode(), "sha256").hexdigest()`，用 `hmac.compare_digest` 比较。不相等返回 403，页面显示 `CONSOLE_CSRF_INVALID`。表单里的 `csrf` 由服务端渲染时填入。
  4. `action=cancel` 调用 `Inbox.cancel`；否则把字段换成 `reply`，调用 `answer`：
     - `choice`、`text`、`secret`、`note` 原样传；
     - `decision=approve|deny`；
     - `point.x` 与 `point.y`（截图上 `<input type="image" name="point">` 的点击坐标）合成 `point`；
     - `points` 文本按 `--points` 的写法解析；
     - `offset` 转成整数；
     - `done=1` 变成 `{"done": true}`。
  5. 成功返回 303，跳到 `/inbox/<id>`。`FactoryError` 返回 400，页面显示错误码（转义），事项保持原样。
  6. `by = "human:" + console.user`。
- **页面内容**：
  - `secret` 的表单字段是 `<input type="password" name="secret" autocomplete="off">`；
  - 页面和响应里永远不出现秘密值；
  - 截图见 5.12。

### 5.5 秘密申请 `request_secret`

**工位所在快照**：先在 active 快照里解析 `station`（解析成功指 `snapshot.doc(snapshot.key("Station", station))` 不抛 `SPEC_NOT_FOUND`）；解析不到时，按 id 升序查看状态为 `verified`、`shadow`、`canary` 的 ChangeSet（已经通过 V1–V4），用第一个能解析出这个工位的候选快照。厂长接入新工位时，ChangeSet 还在金丝雀阶段就需要秘密，这时工位只在候选快照里。`request_secret`、人填值后的核验、`secret_verify`、`secret set --check`、`secret verify` 都用这个规则找快照。

开事项之前，按下面的顺序检查。都对工位所在快照进行。

1. `name` 不匹配 `^[A-Z][A-Z0-9_]*$` 时，报 `SECRET_REQUEST_INVALID`，`reason = "name"`。
2. `secret_class` 不是 `publish`、`ingress`、`other` 之一时，`reason = "class"`。`model` 类秘密只由人放进模型代理的配置。
3. 按上面的规则仍然解析不到 `station` 时，`reason = "station"`。
4. 工位机器的 `needs.secret_checks` 里没有 `name` 时，报 `SECRET_CHECK_MISSING`，`detail = {"name", "machine"}`。
5. `name` 不在工位要注入的名单里（`egress.md` 5.1 第 6 步：策略的 `secrets` 列表，或 `from-machine` 时取机器的 `needs.secrets`）时，`reason = "not-bound"`。
6. 分类与工位角色要相容：`publish` 只给 `sink`，`ingress` 只给 `source`，`other` 不限。否则 `reason = "role"`。规则与 `connectors.md` 5.6 相同。
7. 同名秘密已经存在、但分类不同时，`reason = "class-mismatch"`。

然后 `Inbox.open("secret", subject=name, origin=origin, summary=reason, payload=4.4)`。`exists` 取同名秘密当时是否存在。

**人填值之后**（`answer` 收到 `{"secret": V}`）：

1. `V` 先过 `normalize_value`：
   - 按 UTF-8 解码，失败报 `SECRET_VALUE_INVALID`，`reason = "encoding"`；
   - 去掉末尾一个 `\n` 或 `\r\n`；
   - 如果整体能按 JSON 解析，就改写成 `json.dumps(obj, ensure_ascii=False, separators=(",", ":"))`；
   - 结果为空、超过 65536 个码点、含 `\x00`、仍含换行时，报 `SECRET_VALUE_INVALID`，`reason` 分别为 `empty`、`too-long`、`nul`、`newline`。
2. `n` = 这个事项已有的核验次数 + 1。调用 `verify_secret(ctx, 工位所在快照, station=payload.station, name=subject, value=V, label=f"{id}-check-{n}")`。
3. 核验通过：
   - `store_secret`；
   - 写 `secret.stored`；
   - `close(answered, {"verified": true, "check": n}, by)`，按 5.1 唤醒厂长。
4. 核验不通过或设施故障：
   - 不保存，同名旧值保持不变；
   - `payload.rejected += 1`；
   - 写 `secret.rejected`；
   - 抛 `SECRET_INVALID` 或 `SECRET_CHECK_ERROR`，`detail = {"request", "exit_code", "log"}`。
   - 事项保持 `open`，人可以再填。

### 5.6 核验 `verify_secret`

形状与回执核验（`wiring.md` 5.1）相同。

1. `check = 工位机器.needs.secret_checks[name]`，没有时报 `SECRET_CHECK_MISSING`。
2. `plan = build_egress(snapshot, station, store)`。`name` 不在 `plan.secret_names` 里时，报 `SECRET_REQUEST_INVALID`，`reason = "not-bound"`。
3. env 由这几部分组成：
   - `store.inject(其余名字中已经保存的)`。还没保存的名字不注入，核验命令自己决定缺了它算不算失败。人可以按任意顺序逐个填秘密；
   - `{name: value}`；
   - `FACTORY_SECRET_CHECK=<name>`。

   `plan.network == "egress"` 时，按 `capabilities.md` 5.1 登记一个 `check` 类出网主体（主体名就是 `label`），并把带凭据的代理环境并进 env。
4. 工作目录是 `$FACTORY_ROOT/inbox/<事项 id>/check-<n>/`（`secret set --check`、`secret verify` 与 MCP `secret_verify` 用 `$FACTORY_ROOT/secrets/checks/<label>/`），其下建 `workspace/` 与 `logs/`。这三处的 `label` 是 `f"secret-{name.lower()}-{n}"`，`n` 是 `secrets/checks/` 下同前缀目录的最大编号加 1。
5. 沙箱：
   - `SandboxSpec(name="mao-" + label, image=机器镜像, mounts=(workspace → /workspace 可写，tools/factory → /opt/factory 只读), workdir="/workspace", env, network=plan.network, 资源同机器)`；
   - 资源包括 `capabilities.md` 5.6 的四项。
6. `exec(check.run, timeout_s=check.timeout_s, stdout=logs/check.stdout, stderr=logs/check.stderr)`。
7. 无论成功与否（`finally`）：关闭沙箱，关闭出网主体。两个日志文件先把 `value` 替换成 `***`，再整体 `store.redact`。
8. 结果：
   - 退出码 0：`ok`；
   - 退出码 1：`SECRET_INVALID`；
   - 超时、其他退出码、或沙箱抛 `FactoryError`：`SECRET_CHECK_ERROR`。

### 5.7 保存 `store_secret`

1. 先写 `secrets/values/<NAME>.part`，再 `os.replace` 成 `values/<NAME>`。POSIX 上权限为 `0o600`。
2. 读 `manifest.json`（没有就当 `{}`），设置 `manifest[NAME] = class`，同样先写临时文件再替换。
3. 只有成功的核验、`secret set`，或者人自己编辑文件，才能写这两个文件。厂长没有写入路径。

### 5.8 厂长上报与查询

- `escalate` 返回事项的 `to_json()`。厂长随后可以退出会话；人回答后，按 5.1 唤醒厂长。
- `inbox_get` 返回的 `secret` 事项只有状态与 `answer = {"verified", "check"}`，永远没有值。

### 5.9 开人工协助 `open_handoff`

工位 job 的 ctl handler 增加 op `handoff`。第一次见到某个请求 id 时：

1. 工位策略没有 `handoff` 段、或 `kind` 不在 `kinds` 里时，返回 `{"decision": "deny", "answer": null, "note": "", "code": "HANDOFF_NOT_ALLOWED"}`，不开事项。
2. `summary` 为空或超过 2000 个码点、`answer` 不在五个之中、`hint` 超过 500 个码点时，返回 `ok: false`，`error.code = "CTL_REQUEST_INVALID"`。
3. 截图：
   - 给了 `screenshot` 时，路径必须落在本 attempt 某个**绑定挂载**的目标之下，然后换成宿主路径。推荐写 `/factory/ctl/handoff/<名字>.png`：`/factory/ctl` 在两种 provider 下都是宿主目录。Docker 下 `/workspace` 是卷，宿主读不到。落不进任何挂载（`_map_virtual` 原样返回）、或落在 `volume:` 挂载里，都算映射不到。
   - 下列任一情况都返回 `ok: false`，`error.code = "HANDOFF_SCREENSHOT_INVALID"`，`error.message` 里写明原因词 `path`、`format`、`too-large`：映射不到或不是文件；前 8 字节不是 PNG 签名、或读不出 IHDR 的宽高；超过 5 MiB（5242880 字节）。ctl 错误对象只有 `code` 与 `message`（`factoryctl.md` 4.3）。
   - `answer` 是 `point` 或 `points` 却没有截图时，同样返回这个码，原因词是 `required`。
4. `Inbox.open("handoff", subject=f"{job_id}:{request_id}", origin="job:" + job_id, summary, payload=4.4, deadline=now + parse_duration(handoff.timeout))`。
5. 有截图时，复制到 `inbox/<id>/screenshot.png`。
6. job 进入 `AWAITING_APPROVAL`，写 `handoff.requested`。

`bypass` 不改变人工协助：它总是交给人。

### 5.10 等回答 `poll_handoff`

handler 每次轮询同一个请求 id 时，调用 `poll_handoff(ctx, Inbox.find("handoff", subject) or 已关闭的那条)`：

| 事项状态 | 返回 |
|---|---|
| `open`，未到期 | `None` |
| `open`，`clock.now() >= deadline` | 先 `close(expired, None, by="timeout")`，再返回 `timeout` 响应 |
| `answered` | `answered` 响应，`answer` 是存下的规范化回答 |
| `cancelled` | `cancelled` 响应，`note` 取存下的 `note` |

返回非 `None` 时 job 回到 `RUNNING`。等待的时间计入 `limits.timeout`：工位的超时要比 `handoff.timeout` 长。

### 5.11 回答的规范化 `check_answer`

按 `payload.answer_type`：

| answer_type | 接受的 `reply` | 规范化结果 |
|---|---|---|
| `text` | `{"text": T}`，T 非空，最多 1000 个码点 | `{"text": T}` |
| `point` | `{"point": [x, y]}`，整数，`0 <= x < width`，`0 <= y < height` | `{"point": [x, y]}` |
| `points` | `{"points": [[x, y], ...]}`，1–20 个点，每个点同上 | 同 `reply` |
| `offset` | `{"offset": n}`，整数，`abs(n) <= 100000` | `{"offset": n}` |
| `done` | `{"done": true}` | `{"done": true}` |

`reply` 里还可以有 `note`（最多 2000 个码点），它会存进回答。其他情况都报 `INBOX_ANSWER_INVALID`，`detail.reason = "answer"`。命令行写法：`--point 120,45`；`--points "1,2;30,40"`；`--offset -37`；`--done`。

### 5.12 控制台的截图

- `GET /inbox/<id>/screenshot`：鉴权照旧，返回 `inbox/<id>/screenshot.png`，`Content-Type: image/png`。没有截图时返回 404。
- `console.md` 5.3 的 CSP 改为 `default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`。
- `point` 类事项的详情页，用 `<input type="image" name="point" src="/inbox/<id>/screenshot" alt="截图">` 作为提交按钮，点哪里就提交哪里的坐标，不需要 JavaScript。`points` 类显示截图，再给一个文本框。

---

## 6. 错误

新码都要登记到 `mao.errors`。

| 码 | 何时 | detail |
|---|---|---|
| `INBOX_NOT_FOUND` | 事项 id 不存在 | `id` |
| `INBOX_STATE_INVALID` | 事项不是 `open`，或对 `approval`/`signoff` 调用 `cancel` | `id, status` |
| `INBOX_REQUEST_INVALID` | 5.1 第 2 步 | `reason` |
| `INBOX_ANSWER_INVALID` | 回答与种类或格式不符 | `reason` |
| `INBOX_CONFIG_INVALID` | 4.6 | `field` |
| `CONSOLE_CONFIG_INVALID` | `console.user` 不合格 | `field` |
| `CONSOLE_CSRF_INVALID` | 5.4 第 3 步（只出现在 HTTP 403 页面上） | — |
| `SECRET_REQUEST_INVALID` | 5.5 | `reason` |
| `SECRET_CHECK_MISSING` | 机器没有声明这个秘密的核验命令 | `name, machine` |
| `SECRET_INVALID` | 核验命令退出码 1 | `request, exit_code, log` |
| `SECRET_CHECK_ERROR` | 核验超时、其他退出码、沙箱或依赖故障 | `request, exit_code, log` |
| `HANDOFF_NOT_ALLOWED` | 4.5（ctl 响应的 `code`） | — |
| `HANDOFF_SCREENSHOT_INVALID` | 5.9 第 3 步（ctl 错误的 `code`；原因词在 `message` 里） | — |
| `SECRET_VALUE_INVALID` | `normalize_value` 拒绝了值 | `reason` |
| `HANDOFF_CANCELLED`、`HANDOFF_TIMEOUT` | 4.5（ctl 响应的 `code`） | — |

---

## 7. 验收测试

夹具：`tests/acceptance/m11/deleg_world.py`，见 `delegation.md` 第 7 章。通知钩子是测试运行时写出的脚本，它把收到的 stdin 追加到文件。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m11/test_m11_01_inbox.py` | `test_escalate_and_answer` | 厂长 `escalate`（带 `options`）开出 `question` 事项；`factory inbox ls` 列出它；`answer --choice` 不在选项里报 `INBOX_ANSWER_INVALID`，在选项里则 `answered`，并写 `inbox` 唤醒票；`inbox_get` 能看到回答 |
| | `test_notify_hook` | 新事项触发钩子，stdin 是 4.10 的 `body`，`url` 指向控制台；`approval`/`secret` 去重时不再触发；钩子退出码非 0 时写 `inbox.notify_failed`，事项照常 `open` |
| | `test_inbox_cli_errors` | 不存在的 id 报 `INBOX_NOT_FOUND`（退出码 1）；没给回答旗标或给了两个，退出码 2；`cancel` 后再 `answer` 报 `INBOX_STATE_INVALID`；`inbox.notify` 配置不合格报 `INBOX_CONFIG_INVALID` |
| | `test_console_answers_with_csrf` | 控制台 `GET /inbox/<id>` 渲染带 `csrf` 的表单；`POST` 不带或带错 `csrf` 返回 403，事项不变；带对的 `csrf` 返回 303，事项 `answered`，`answered_by = human:<console.user>`；其他路径的 `POST` 仍是 405 |
| `tests/acceptance/m11/test_m11_05_secrets.py` | `test_request_checks` | `secret_request` 在下列情况报错：机器没有核验命令 → `SECRET_CHECK_MISSING`；名字没有绑定到工位 → `not-bound`；`publish` 给非 Sink 工位 → `role`；分类非法 → `class` |
| | `test_secret_fulfil_verifies_before_store` | 人填错值：`SECRET_INVALID`，没有值文件，`rejected == 1`，事项仍 `open`；填对值（末尾带换行）：保存、manifest 分类正确、事项 `answered`、写 `inbox` 唤醒票；日志、事件、事项里都没有这两个值 |
| | `test_check_reaches_upstream_with_candidate` | 核验命令经出网代理带候选值访问测试上游，上游只认正确的值；`check` 类主体在核验结束后被关闭，再用同一凭据访问得到 403 |
| | `test_secret_set_and_verify_commands` | `factory secret set --check` 的值核验不通过时不保存，通过才保存；`factory secret verify` 对保存的值返回 `ok`；值改坏后返回 `SECRET_INVALID`（退出码 1）；MCP `secret_verify` 给出同样结论且不含值 |
| | `test_json_secret_is_compacted` | 多行 JSON（例如浏览器的 storage state）规范成一行再核验并保存；`secret set` 收到含裸换行的非 JSON 值报 `SECRET_VALUE_INVALID`，`reason = "newline"` |
| `tests/acceptance/m11/test_m11_06_handoff.py` | `test_handoff_point_answer` | 操作工 `factoryctl handoff --answer point --screenshot` 附 PNG：job `AWAITING_APPROVAL`，开出 `handoff` 事项，截图已复制，`view` 带宽高；`answer --point` 超界报 `INBOX_ANSWER_INVALID`；界内的点回到操作工的 stdout，job `SEALED` |
| | `test_handoff_not_allowed` | 策略没有 `handoff` 段，或 `kind` 不在 `kinds` 里：操作工收到 `deny`、`HANDOFF_NOT_ALLOWED`，不开事项 |
| | `test_handoff_timeout_and_cancel` | 到期后操作工收到 `timeout`，事项 `expired`；另一次 `factory inbox cancel` 后操作工收到 `cancelled` |
| | `test_handoff_screenshot_rules` | 截图不是 PNG：ctl 错误 `HANDOFF_SCREENSHOT_INVALID`，消息含 `format`；`answer` 是 `point` 却没有截图：消息含 `required`；都不开事项；`factoryctl handoff` 缺 `--answer` 退出码 2 |
| | `test_console_screenshot_click` | 控制台详情页含 `<input type="image" name="point"`，`/inbox/<id>/screenshot` 返回 PNG，CSP 含 `img-src 'self'`；`POST point.x=…&point.y=…` 把点交给操作工 |

验收命令：`uv run python tools/harness.py pytest <文件>`，断言 `out.failed == 0 && out.errors == 0 && out.skipped == 0`，`passed` 不少于上表的行数。

---

## 8. 错误示范与正确示范

```python
# ✖ 错误：人填了 Cookie 就直接写进 secrets/values，等 Sink 失败才发现过期了
# ✔ 正确：先在核验沙箱里跑机器声明的核验命令，退出码 0 才保存；不通过时旧值不动，事项保持 open
```

```python
# ✖ 错误：把秘密值放进 human_requests.answer、事件、日志或 MCP 返回，方便排错
# ✔ 正确：answer 只记 {"verified": true, "check": n}；核验日志先把候选值换成 ***
```

```python
# ✖ 错误：通知钩子用 shell=True 拼命令，或把 payload 整个发出去
# ✔ 正确：argv 列表，stdin 只放 id、种类、摘要和控制台链接；失败只写事件
```

```python
# ✖ 错误：控制台为了方便，开放任意 POST，或者用 JavaScript 取点击坐标
# ✔ 正确：只开放 POST /inbox/<id>，校验 csrf；点击坐标用 <input type="image">
```

```python
# ✖ 错误：bypass 下把验证码交给厂长
# ✔ 正确：人工协助总是进收件箱；策略的 handoff.kinds 决定可以请求哪些种类
```

---

## 9. 风险与注意事项

- **秘密的去向**：核验沙箱里的命令能拿到候选值，也能访问工位策略允许的主机。核验命令由机器声明，跟着 ChangeSet 一起审查。人填值之前，可以先在 `factory inbox show` 里看工位与机器。
- **控制台写入**：令牌被盗后，攻击者可以回答收件箱，包括批准审批、填秘密。控制台只绑定 `127.0.0.1`。令牌文件的权限要保护好。
- **人工协助的耗时**：等人的时间算在 job 的 `limits.timeout` 里。工位的超时要留出余量。
- **截图内容**：截图可能含有账号信息。它只存在工厂目录里，钩子不发送截图。
- **实时画面**：以后的实时远程浏览器复用 `handoff` 事项，`view.kind` 用 `live`，回答格式不变。
