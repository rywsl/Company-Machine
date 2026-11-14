# ingress：入站网关（M12）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M12-05**。设计说明见 `plan/design-resident.md` D-11 至 D-15。
> 外部推送（平台回调、私信通知、表单提交）现在进不了工厂：机器端口不发布到宿主（`gateway.md` 5.1），`egress.md` 第 1 章推迟的“入站 webhook 监听”从未写出。本文件给 `factory serve` 加一个入站端口，按 Registry 里声明的路由认证请求，把请求变成 Bean，或者送进常驻实例（`instances.md`）。M13 起也可以送进 Agent 邮箱（`agents.md` 5.2）。

---

## 1. 目的与范围

**做什么**

- `factory.json` 的 `ingress` 段开启宿主上的一个 HTTP 端口，由 `factory serve` 的线程 `mao-ingress` 运行。
- 路由写在工位的 `ingress` 段：认证方式、秘密名、投递方式。
- 投递：`bean`（封存为工位输出端口的 Bean）、`instance`（写进常驻实例的收件目录）。
- 拒绝时返回稳定的错误码；事件不记录请求体与请求头。

**不做什么**

| 不做 | 原因 |
|---|---|
| TLS、公网暴露、隧道 | 由人在工厂之外配置。缺省只监听 `127.0.0.1` |
| 把请求转发到机器端口 | 机器端口不发布。机器从收件目录读请求 |
| 同步返回机器的处理结果 | 入站只确认收到（202）。处理结果走 Bean 与产线 |
| 平台专用的签名格式 | 只做 `bearer` 与 `hmac-sha256`。其他格式由常驻实例自己校验（`deliver: instance`，路由用 `bearer` 或请求体 HMAC 之外的方式时请在机器里实现） |

---

## 2. 依赖

| 依赖 | 用途 |
|---|---|
| `EpochStore`、`Snapshot` | active 快照里的路由 |
| `SecretStore`（`egress.md` 3.1） | 认证秘密 |
| `emit`（`instances.md` 3.3） | `deliver: bean` |
| `Instances`（`instances.md` 3.2） | `deliver: instance` |
| `Clock`、`IdSource` | 速率窗口、id |

---

## 3. 接口

### 3.1 `mao.ingress`

```python
@dataclass(frozen=True)
class IngressConfig:                       # factory.json 的 ingress 段（4.2）
    port: int
    bind: str = "127.0.0.1"
    max_body_kb: int = 256
    rate_per_min: int = 60

@dataclass(frozen=True)
class Route:
    name: str
    owner: str                             # 声明它的 Spec 键（工位；M13 起也可以是 Agent）
    auth: str                              # "bearer" | "hmac-sha256"
    secret: str                            # 秘密名
    header: str                            # hmac 的请求头名；bearer 为 "Authorization"
    deliver: str                           # "bean" | "instance"（M13 增加 "agent"）
    port: str | None                       # deliver: bean 时的输出端口
    file: str | None                       # deliver: bean 时请求体在载荷里的路径
    error: str | None                      # 路由不可用时的错误码（4.1 的校验），否则 None

def routes(snapshot: Snapshot) -> dict[str, Route]     # 5.1

class IngressServer:
    def __init__(self, ctx: Context, epochs: EpochStore, config: IngressConfig, *,
                 instances: Instances | None = None,
                 runner_for: Callable[[Snapshot], Runner] | None = None) -> None
        # runner_for 缺省为 open_runner(ctx, snapshot)
    @property
    def port(self) -> int                  # 实际监听的端口（配置为 0 时由系统分配）
    def start(self) -> None                # 在线程 mao-ingress 中 serve_forever
    def stop(self) -> None
    def handle(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any]]
        # 5.2 的全部逻辑；HTTP 处理器只负责读写。测试可直接调用
```

`load_ingress_config(root) -> IngressConfig | None`：`factory.json` 没有 `ingress` 段时为 `None`。

### 3.2 `Daemon` 的装配

`Daemon.start()` 在 `load_ingress_config(root)` 不为 `None` 时构造 `IngressServer(self.ctx, self.epochs, config, instances=self.instances, runner_for=...)` 并 `start()`，保存为公开属性 `daemon.ingress`（没有入站时为 `None`）；`shutdown` 时 `stop()`。`runner_for` 取该快照在 daemon 组件缓存里的 `Runner`。

- 共用 daemon 的 `Context`：`Database` 有锁、可跨线程使用。`handle` 由服务器自己的一把锁串行执行。
- `serve.json` 心跳增加键 `ingress_port`（没有入站时为 `null`）。
- `Daemon` 没有 `epochs`（单快照模式）时不开入站。

---

## 4. 数据格式

### 4.1 工位的 `ingress` 段

```yaml
spec:
  role: source
  machine: xhs-listener@1
  recipe: listen
  ingress:
    xhs-push:                              # 路由名，^[a-z][a-z0-9-]{0,62}$
      auth: {kind: hmac-sha256, secret: XHS_PUSH_SECRET, header: X-Signature}
      deliver: bean                        # bean | instance
      port: events                         # deliver: bean 时必填
      file: event.json                     # deliver: bean 时请求体在载荷里的路径；缺省为端口类型第一个 required 的 payload 路径
```

`station.schema.json` 增加 `ingress` 属性：键是路由名，值必须有 `auth`、`deliver`；`auth.kind` ∈ `bearer`、`hmac-sha256`；`auth.secret` 匹配 `^[A-Z][A-Z0-9_]*$`；`auth.header` 只用于 `hmac-sha256` 且必填；`deliver` ∈ `bean`、`instance`（工位不能用 `agent`；Agent 的路由写在 Agent 上，见下文）。

`routes(snapshot)` 对每条路由做运行期校验，不通过时 `Route.error` 为下列码之一（路由仍在表中，请求得到 503）：

| 条件 | `error` |
|---|---|
| 同一路由名在 active 快照里出现不止一次（所有出现处都标记） | `INGRESS_ROUTE_DUPLICATE` |
| 工位 `metadata.lifecycle` 不是 `active`（缺省为 `active`） | `INGRESS_ROUTE_INVALID` |
| `hmac-sha256` 但没有 `auth.header` | `INGRESS_ROUTE_INVALID` |
| `deliver: bean`，但工位 `role` 不是 `source`，或 `port` 不在配方 `outputs` 里，或没有 `file` 且端口类型没有 required 的 payload | `INGRESS_ROUTE_INVALID` |
| `deliver: instance`，但机器不是 `resident` | `INGRESS_ROUTE_INVALID` |
| （M13）Agent 的路由：Agent 的 `metadata.lifecycle` 不是 `active`，或 `agent_config` 抛错 | `INGRESS_ROUTE_INVALID` |

M13 起，`routes` 在工位之后再扫描 active 快照里每个 Agent 的 `spec.subscribe.ingress`（`agents.md` 3.1）：这些路由的 `owner` 是 Agent 键，`deliver` 固定为 `"agent"`，`port`、`file` 为 `None`，与工位路由共用名字空间（重名照上表标记）。

### 4.2 `factory.json` 的 `ingress` 段（人维护）

```json
"ingress": {"bind": "127.0.0.1", "port": 8790, "max_body_kb": 256, "rate_per_min": 60}
```

`port` 必填，`0` 到 `65535` 的整数；其余可省略。不合法 → `SERVE_CONFIG_INVALID`（`field`）。

### 4.3 投递到实例的文件

`instances/<id>/main/inbox/<收到的序号，10 位补零>-<请求 id>.json`，先写 `.tmp` 再改名：

```json
{"id": "ig-...", "route": "xhs-push", "received_at": "2026-11-13T08:00:00Z",
 "content_type": "application/json", "body_b64": "..."}
```

只保留 `Content-Type` 一个请求头。序号是该实例收件目录现有文件的最大序号加 1。

### 4.4 响应

成功：`202`，`{"id": "ig-..."}`。请求 id 是 `ids.new("ig")`。

错误响应的 body 都是 `{"code": "<错误码>"}`，不回显路由之外的任何请求内容：

| 状态 | `code` | 何时 |
|---|---|---|
| 404 | `INGRESS_ROUTE_NOT_FOUND` | 路径不是 `/in/<路由名>`，或路由不存在 |
| 405 | `INGRESS_METHOD` | 方法不是 `POST`；响应头 `Allow: POST` |
| 400 | `INGRESS_BAD_REQUEST` | 没有或无法解析 `Content-Length` |
| 413 | `INGRESS_TOO_LARGE` | `Content-Length > max_body_kb * 1024`；不读请求体 |
| 429 | `INGRESS_RATE_LIMITED` | 该路由最近 60 秒已接受 `rate_per_min` 个请求 |
| 503 | `Route.error` | 路由不可用 |
| 503 | `INGRESS_SECRET_MISSING` | 秘密没有保存 |
| 401 | `INGRESS_AUTH_FAILED` | 认证不通过 |
| 422 | `INGRESS_PAYLOAD_INVALID` | `deliver: bean` 时 `emit` 报 `EMIT_INVALID` |
| 503 | `INSTANCE_NOT_READY` | `deliver: instance` 时实例不是 `ready` 或 `busy` |

### 4.5 事件

| kind | data |
|---|---|
| `ingress.received` | `{route, id, bytes, deliver, target}`；`target` 是 Bean id、实例 id（M13：Agent 名） |
| `ingress.rejected` | `{route, code, status}`；路径解析不出路由名时 `route` 为 `""` |

405 不写事件。

---

## 5. 行为

### 5.1 路由表

`IngressServer` 每次处理请求时取 `epochs.current()`；与上次相同则复用缓存的快照与路由表，否则重新 `materialize` 并调用 `routes`。

### 5.2 `handle`

按顺序，任何一步失败都立即返回（并按 4.5 写事件）：

1. 方法不是 `POST` → 405。
2. 路径匹配 `^/in/([a-z][a-z0-9-]{0,62})$`（忽略查询串）；不匹配或路由表里没有 → 404。
3. `Route.error` 不为 `None` → 503。
4. `Content-Length` 缺失或不是非负整数 → 400；超过上限 → 413。HTTP 处理器在这一步之后才读请求体，恰好读 `Content-Length` 字节。
5. 速率：该路由在内存窗口里最近 60 秒（`clock.now()`）已接受的请求数 `>= rate_per_min` → 429。只有返回 202 的请求计入窗口。
6. 秘密：`store.inject([route.secret])`；`SECRET_NOT_FOUND` → 503 `INGRESS_SECRET_MISSING`。
7. 认证，用 `hmac.compare_digest`：
   - `bearer`：`Authorization` 请求头等于 `"Bearer " + 秘密`。
   - `hmac-sha256`：`route.header` 请求头的值去掉可选的 `sha256=` 前缀后，等于 `hmac.new(秘密, body, "sha256").hexdigest()`（不区分大小写）。
   - 不通过 → 401。请求头名不区分大小写。
8. 投递：
   - `bean`：把请求体写到 `$FACTORY_ROOT/ingress/tmp/<请求 id>/<route.file>`，调用 `emit(runner, station=route.owner, port=route.port, source=该目录, instance_id=None, trigger={"on": "ingress", "route": 路由名, "request": 请求 id})`，`runner` 是当前 active 快照上的 `Runner`。`EMIT_INVALID` → 422。无论成败，最后删除临时目录。
   - `instance`：取工位唯一的未停止 `resident` 实例；状态不是 `ready`、`busy` → 503。按 4.3 写入。
   - `agent`（M13）：按 `agents.md` 5.2 投进 Agent 的邮箱；`post` 抛 `AGENT_MESSAGE_TOO_LARGE` → 413 `INGRESS_TOO_LARGE`。
9. 写 `ingress.received`，返回 202。

`emit` 的 `instance_id` 为 `None` 时跳过实例检查、目录位置检查与 `emit_per_min`（`instances.md` 5.7），`emits.instance` 为 `NULL`。入站的速率由本文件第 5 步负责。

### 5.3 HTTP 处理器

用标准库 `ThreadingHTTPServer`。处理器把请求头转成字典后调用 `handle`，写回状态码、`Content-Type: application/json` 与 body。不记录访问日志（`log_message` 什么都不做），因为查询串里可能有令牌。

---

## 6. 错误

新码都要登记到 `mao.errors`。HTTP 层的码见 4.4，其中只在本文件出现的有：`INGRESS_ROUTE_NOT_FOUND`、`INGRESS_METHOD`、`INGRESS_BAD_REQUEST`、`INGRESS_TOO_LARGE`、`INGRESS_RATE_LIMITED`、`INGRESS_SECRET_MISSING`、`INGRESS_AUTH_FAILED`、`INGRESS_PAYLOAD_INVALID`、`INGRESS_ROUTE_DUPLICATE`、`INGRESS_ROUTE_INVALID`。

| 码 | 何时 | detail |
|---|---|---|
| `INGRESS_ROUTE_DUPLICATE` | 4.1 | `route` |
| `INGRESS_ROUTE_INVALID` | 4.1 | `route, reason` |

其余码只作为 HTTP 响应与事件中的字符串，不以 `FactoryError` 抛出。

---

## 7. 验收测试

夹具：`tests/acceptance/m12/instance_world.py`（`instances.md` 第 7 章）。多数测试直接调用 `IngressServer.handle`；`test_serve_starts_ingress` 在线程里运行 `Daemon.serve`，经真实的 HTTP 端口。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m12/test_m12_05_ingress.py` | `test_bearer_route_seals_bean` | 正确的 Bearer：202 与请求 id；Bean 被封存，`emits` 行的 `trigger.on = ingress` |
| | `test_hmac_route` | 正确的 HMAC（带或不带 `sha256=`）：202；错误的 HMAC：401 `INGRESS_AUTH_FAILED` |
| | `test_ingress_rejections` | 404、405、400、413、429、503 `INGRESS_SECRET_MISSING`、422 各自的码；拒绝写 `ingress.rejected`，405 不写 |
| | `test_route_duplicate_and_invalid` | 两个工位声明同名路由：两处都 503 `INGRESS_ROUTE_DUPLICATE`；`role: transform` 的 `deliver: bean`：503 `INGRESS_ROUTE_INVALID` |
| | `test_deliver_to_instance` | `deliver: instance`：收件目录出现 4.3 格式的文件，只有 `Content-Type`；实例不可用时 503 `INSTANCE_NOT_READY` |
| | `test_no_secret_or_body_in_events` | 秘密值、请求体、`Authorization` 不出现在事件、响应与日志里 |
| | `test_serve_starts_ingress` | `Daemon.serve` 在有 `ingress` 段时监听端口，`serve.json` 有 `ingress_port`；经 HTTP 投递得到 202，GET 得到 405 与 `Allow: POST` |

---

## 8. 错误示范与正确示范

```python
# ✖ 错误：if request.headers["Authorization"] == "Bearer " + secret
# ✔ 正确：hmac.compare_digest(...)；秘密缺失时 503，不能当作空字符串比较

# ✖ 错误：入站请求直接 docker exec 到机器里调用它的 HTTP 端口
# ✔ 正确：写进 /factory/instance/inbox/，机器自己读；机器端口不发布、不被转发

# ✖ 错误：ingress.received 的 data 里带上请求体，方便排查
# ✔ 正确：只记路由、字节数、目标与 id；请求体可能有个人信息和令牌
```

---

## 9. 风险与注意事项

- **公网暴露是人的决定**。缺省监听 `127.0.0.1`。改成 `0.0.0.0` 或配隧道前，确认每条路由都有秘密。
- **厂长可以经 ChangeSet 加路由，但秘密只能由人设置**（`factory secret set` 或收件箱的秘密申请）。没有秘密的路由一律 503。
- **速率窗口在内存里**，`factory serve` 重启后清零。持久的限流不在本文件范围。

---

## 10. 任务拆分

| 任务 | 内容 | 依赖 | 必读 | 产出 | 验收 |
|---|---|---|---|---|---|
| M12-05 | 路由表、`IngressServer`、`emit` 的入站分支、`serve` 装配、station schema | M12-04 | 全文 | `mao.ingress`、`mao.instances`、`mao.daemon`、`spec/schemas/station.schema.json`（由设计方安装） | `test_m12_05_ingress.py` |
