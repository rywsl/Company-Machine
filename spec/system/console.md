# console：本机只读 Web 控制台（M7-02）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M7-02**。
> `factory status` 适合脚本和监控。人排查问题时还要能点进一次 run、一个 job、一张唤醒票、一次厂长会话，看前因后果。本文件定义一个只绑本机、只读、要令牌的 Web 控制台。它不提供任何写操作：改东西仍然走命令行和 ChangeSet。
> 收件箱页面、`POST /inbox/<id>`（控制台因此不再完全只读）、截图与 CSP 的 `img-src 'self'` 见 `inbox.md` 5.4、5.12（M11），冲突时以 `inbox.md` 为准。

---

## 1. 目的与范围

**做什么（M7-02）**

- `mao.console.ConsoleServer`：标准库 `http.server` 上的只读 HTTP 服务。
- 页面（HTML）与 `/api/*`（JSON），JSON 与对应命令的 `--json` 输出相同（4.2）。
- 令牌鉴权、安全响应头、Host 校验、HTML 转义。
- 命令行：`factory console [--port N]`；补一个只读命令 `factory foreman wakes [--status S]`。

**不做什么**

| 不做 | 原因 |
|---|---|
| 任何写操作（审批、重开、回滚、提议） | 写操作要有身份和审计，命令行已经有。控制台被盗用时最多泄露只读数据 |
| 绑定 `127.0.0.1` 以外的地址 | 远程访问由人用 SSH 端口转发等手段自己解决 |
| JavaScript、外部资源、模板引擎依赖 | 不新增依赖；页面是服务端拼出的静态 HTML |
| 展示 Bean 载荷、秘密值 | 载荷是不可信内容；控制台只展示元数据和血缘 |

---

## 2. 依赖

| 依赖 | 用途 |
|---|---|
| `mao.status`（`observability.md`） | `factory_status`、`render_metrics`、`tail_events` |
| `LineRunner`（`lines.md`） | run 列表与详情 |
| `ForemanQuery`（`foreman.md` 3.1） | job、Bean、血缘 |
| `ChangePipeline`（`governance.md`） | ChangeSet 列表与详情 |
| `WakeQueue`（`foreman.md`、`supervisor.md`） | 唤醒票 |
| `ForemanSupervisor.sessions` 的存储（`supervisor.md`） | 会话记录与归档的 transcript |
| `Approvals`（`approvals.md`） | 审批列表 |

---

## 3. 接口

```python
# mao.console
TOKEN_FILE = "console.token"          # 位于 $FACTORY_ROOT
COOKIE = "mao_console"
DEFAULT_PORT = 8090

def load_token(root: Path) -> str
    # 文件存在：读取并去掉首尾空白；少于 32 个字符 → CONSOLE_TOKEN_INVALID。
    # 文件不存在：secrets.token_urlsafe(32) 写入（原子写；POSIX 上权限 0600），返回它。

class ConsoleServer:
    def __init__(self, ctx: Context, *, token: str, epochs: EpochStore | None = None,
                 pipeline: ChangePipeline | None = None,
                 snapshot: Snapshot | None = None) -> None
        # 需要快照的读取（run 详情、job、Bean）按 commands.md 5.2：有 epochs 时用对象记录的 epoch
        # materialize；没有治理配置时用这里给的 snapshot（factory console 传 open_snapshot 的结果）。
    def start(self, port: int = DEFAULT_PORT) -> int     # 绑定 127.0.0.1；port=0 由系统分配；返回实际端口
    def serve_forever(self) -> None                      # 阻塞直到 close()
    def close(self) -> None
```

```
factory console [--port N]
factory foreman wakes [--status open|taken|done|failed] [--json]
```

- `console`：端口取 `--port`，否则 `factory.json` 的 `console.port`，否则 8090。启动后打印 `http://127.0.0.1:<port>/` 与令牌文件路径（**不打印令牌**），阻塞到 SIGINT/SIGTERM，退出码 0。绑定失败 → `CONSOLE_BIND_FAILED`，退出码 4。
- `foreman wakes`：`WakeQueue.list(status=...)`，每张票输出 `id`、`signal`、`subject`、`status`、`attempts`、`not_before`、`at`、`updated_at`，按 `at`、`id` 升序。

两个命令由本任务加入 `commands.md` 的命令表。

---

## 4. 数据格式

### 4.1 页面

所有页面 `Content-Type: text/html; charset=utf-8`，无脚本。每页顶部是同一个导航条。

| 路径 | 内容 |
|---|---|
| `/` | `factory_status` 的告警、在线状态、计数、当天花费 |
| `/runs` | run 列表（`?state=`），顺序与 `line ls` 相同（`commands.md` 5.4，按 `id` 升序），页面只显示最后 `?limit=` 条（缺省 100、最大 1000） |
| `/runs/<id>` | run 详情与节点表；节点的 job 链接到 `/jobs/<id>` |
| `/jobs/<id>` | job 详情（`ForemanQuery.job_get`）与打码后的 transcript 末尾 200 行 |
| `/beans/<id>` | Bean 元数据、标签、上下游血缘（各一层），**不含载荷** |
| `/changesets`、`/changesets/<id>` | ChangeSet 列表与详情（阶段、报告、复核） |
| `/wakes` | 唤醒票列表（`?status=`） |
| `/sessions`、`/sessions/<id>` | 厂长会话列表与详情；详情含 `logs/transcript.jsonl` 末尾 200 行 |
| `/approvals` | 审批列表（`?status=`，缺省 `pending`） |
| `/events` | `tail_events`（`?since=`、`?kind=`、`?limit=`） |
| `/metrics` | `render_metrics` 的文本，`Content-Type: text/plain; version=0.0.4; charset=utf-8` |
| `/login` | 令牌表单（GET）与提交（POST），见 5.2 |
| `/healthz` | `ok`，`text/plain`，不需要令牌，不含任何状态 |

### 4.2 JSON 接口

`/api/...` 与页面同参数。响应 `Content-Type: application/json; charset=utf-8`，正文等于下列命令 `--json` 的输出（同一个转换函数 `to_jsonable`，`commands.md` 3.1）：

| 路径 | 等于 |
|---|---|
| `/api/status` | `factory status --json` |
| `/api/runs` | `factory line ls --json`（`?state=` 对应 `--state`） |
| `/api/runs/<id>` | `factory line show RUN --json` |
| `/api/runs/<id>/nodes` | `factory line nodes RUN --json` |
| `/api/jobs/<id>` | `factory foreman job get JOB --json` |
| `/api/beans/<id>` | `factory foreman bean get BEAN --json` |
| `/api/beans/<id>/lineage?direction=up\|down` | `factory foreman bean lineage BEAN up\|down --json` |
| `/api/changesets`、`/api/changesets/<id>` | `factory changeset ls --json`、`factory changeset show ID --json` |
| `/api/wakes` | `factory foreman wakes --json` |
| `/api/sessions` | `factory foreman sessions --json`（`?state=`） |
| `/api/sessions/<id>` | `SessionRecord.to_json()` 加键 `transcript`（末尾 200 行，字符串） |
| `/api/approvals` | `factory approval ls --json`（`?status=`） |
| `/api/events` | `factory events --json` |

`factory status --json` 的 `at` 是调用时刻，比较时除外。

### 4.3 错误响应

| 情况 | 状态码 | 正文 |
|---|---|---|
| 未鉴权的 `/api/*`、`/metrics` | 401 | JSON `{"error": "unauthorized"}`；带 `WWW-Authenticate: Bearer` |
| 未鉴权的页面 | 303 | `Location: /login` |
| 不允许的方法 | 405 | 带 `Allow: GET`（`/login` 为 `Allow: GET, POST`） |
| Host 不合法（5.4） | 421 | 纯文本 |
| 对象不存在（`*_NOT_FOUND`） | 404 | 页面或 JSON `{"error": "not_found"}` |
| 参数不合法 | 400 | JSON `{"error": "bad_request", "field": ...}` |
| 其他异常 | 500 | 只有 `{"error": "internal"}`，不含异常文本；异常写日志 |

---

## 5. 行为

### 5.1 只读

1. 除 `POST /login` 外，只接受 `GET` 与 `HEAD`。其他方法、以及对其他路径的 `POST` → 405。
2. 请求处理不写数据库、不写事件、不改文件（令牌文件只在 `load_token` 里、服务启动前写）。
3. 服务器单线程（`http.server.HTTPServer`），请求串行处理，共用构造时传入的 `Context`。套接字超时 10 秒，慢客户端不会挂住服务。

### 5.2 鉴权

1. 凭据二选一：请求头 `Authorization: Bearer <token>`，或 Cookie `mao_console=<token>`。比较用 `hmac.compare_digest`。
2. `GET /login` 返回令牌表单（`<form method="post" action="/login">`，字段 `token`）。
3. `POST /login`：正文是 `application/x-www-form-urlencoded`，最长 4096 字节。令牌正确 → 303 到 `/`，`Set-Cookie: mao_console=<token>; HttpOnly; SameSite=Strict; Path=/`；错误 → 401，重新显示表单，不回显输入。
4. 令牌只能来自令牌文件。不接受 URL 查询参数里的令牌（会进浏览器历史和日志）。
5. 不需要令牌的只有 `/login`、`/healthz`。

### 5.3 响应头

每个响应（包括错误）都带：

```
Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
Cache-Control: no-store
```

### 5.4 Host 校验

`Host` 必须是 `127.0.0.1:<port>` 或 `localhost:<port>`（`<port>` 为实际端口）。其他值（包括缺失）→ 421。这是对 DNS rebinding 的防御：浏览器把恶意域名解析到 127.0.0.1 时，`Host` 仍是恶意域名。

### 5.5 转义

所有动态值（id、状态、错误信息、标签、transcript 行、事件数据）在写进 HTML 前经 `html.escape(value, quote=True)`。链接里的 id 先按 `urllib.parse.quote(id, safe="")` 编码再转义。不允许把任何字段当作 HTML 片段原样输出。

### 5.6 transcript

job 页面用 `ForemanQuery.job_transcript`（已打码）。会话页面读 `$FACTORY_ROOT/foreman/sessions/<id>/logs/transcript.jsonl`（`supervisor.md`，已打码）；文件不存在时显示“无 transcript”。两者都只取末尾 200 行，每行最多 4000 个字符，超出截断并加 `…`。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `CONSOLE_TOKEN_INVALID` | 令牌文件存在但内容少于 32 个字符 | `path` |
| `CONSOLE_BIND_FAILED` | 端口被占用或无权绑定 | `port` |

---

## 7. 验收测试

`registry-m2` 与治理夹具（`tests/acceptance/m3/gov_world.py` 的 `open_world`），`LocalProvider`、`FakeClock`、`CounterIds`。服务器 `start(port=0)`，用 `http.client` 发请求（可以自定义 `Host` 头）。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m7/test_m7_02_console.py` | `test_token_file` | `load_token` 首次生成并写文件、再次读取相同；POSIX 上权限 0600；过短的令牌 → `CONSOLE_TOKEN_INVALID` |
| | `test_requires_token` | 无凭据：`/api/status` 401 带 `WWW-Authenticate`，`/` 303 到 `/login`；错误令牌 401；正确 Bearer 200；`/healthz` 无需令牌；查询参数里的令牌无效 |
| | `test_login_cookie` | `POST /login` 正确令牌 → 303 与 `HttpOnly`、`SameSite=Strict` 的 Cookie，带 Cookie 访问 `/` 得 200；错误令牌 401 且正文不含输入 |
| | `test_read_only` | `PUT`、`DELETE`、`PATCH`，以及对 `/api/status` 的 `POST` → 405 带 `Allow`；前后 `events` 行数与数据库文件内容不变 |
| | `test_security_headers_and_host` | 200、401、404 响应都带 5.3 的五个头；`Host: evil.example:<port>` → 421 |
| | `test_html_escaped` | subject 含 `<script>` 的唤醒票在 `/wakes` 中只以转义形式出现 |
| | `test_api_matches_cli` | `/api/status`（除 `at`）、`/api/wakes`、`/api/sessions` 与对应命令的 `--json` 输出相等 |
| | `test_pages_render` | 4.1 的每个页面对存在的对象返回 200 与正确的 `Content-Type`；不存在的 job、会话 → 404 |

```
uv run python tools/harness.py pytest tests/acceptance/m7/test_m7_02_console.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 8`。

---

## 8. 错误示范与正确示范

```python
# ✖ 为了方便加一个“重开唤醒票”按钮
# ✔ 控制台只读；重开用 factory foreman reopen
```

```python
# ✖ HTTPServer(("0.0.0.0", port), ...) 方便同事访问
# ✔ 只绑 127.0.0.1；远程访问用 SSH 端口转发
```

```python
# ✖ f"<td>{wake.subject}</td>"
# ✔ f"<td>{html.escape(wake.subject, quote=True)}</td>"
```

```python
# ✖ /login?token=... 方便做书签
# ✔ 令牌只走请求头或 POST 表单，不进 URL
```

---

## 9. 风险与注意事项

- **本机其他用户**：绑定 127.0.0.1 不防同机的其他账户，所以要令牌；令牌文件权限 0600。
- **CSRF**：控制台没有写操作；`SameSite=Strict` 与 Host 校验再挡一层。
- **单线程**：一次慢查询会让其他请求排队。这是有意的：控制台给一个人用，不需要并发。
- **数据量**：列表都有 `limit`。事件和 transcript 都只取末尾。
