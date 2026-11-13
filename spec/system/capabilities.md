# capabilities：出网凭据、通配域名、按需放行、监控规则与沙箱资源（M11）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M11-04、M11-07、M11-08**。设计说明见 `plan/design-delegation.md`。
> 现有限制让真实的浏览器类操作工无法工作：
> - 出网代理只认 `X-Factory-Job` 请求头，浏览器、curl、Playwright 经 `HTTPS_PROXY` 出网时都不会发这个头。而且任何沙箱都能冒用别的 job id。
> - 域名只能逐个写死，`upos-*.bilivideo.com` 这类动态主机无法列出。
> - 名单外的主机一律拒绝，没有办法临时申请。
> - 回执核验沙箱拿不到秘密，也过不了代理。
> - `/dev/shm`、`/tmp`、进程数写死，浏览器不够用。
> - 镜像构建缺省不出网，装不了依赖。
>
> 本文件放宽这些限制，同时不降低隔离；并增加厂长写的**监控规则**：操作工出网命中规则时，暂停这次出网，等厂长审批。
> 本文件与 `egress.md`、`sandbox.md`、`images.md`、`wiring.md`、`factoryctl.md` 冲突时，以本文件为准。

---

## 1. 目的与范围

**做什么**

- **出网主体与令牌（M11-04）**：
  - 每个 attempt 发一个随机令牌，代理按 `Proxy-Authorization: Basic` 识别主体；
  - 没有凭据时回 407 质询，Chrome、Playwright 据此发送凭据；
  - 回执核验沙箱、秘密核验沙箱登记短期的 `check` 类主体；
  - 转发明文 HTTP 之前删掉 `Proxy-Authorization`。
- **通配域名（M11-04）**：策略里可以写 `*.example.com`。
- **按需放行（M11-04）**：策略 `egress.on_unlisted: ask`。名单外主机的出网被挂起，按工位的 `approval.in_run` 规则审批，批准后放行。
- **回执核验（M11-04）**：核验沙箱与 attempt 同样注入秘密并带代理凭据（补 `wiring.md` 5.1）。
- **监控规则（M11-07）**：工位的 `monitors` 段，CEL 条件，动作为 `hold`、`deny`、`wake`；`hold` 交给厂长审批。
- **沙箱资源（M11-08）**：机器 `runtime.resources` 的 `shm_mb`、`tmp_mb`、`pids`、`writable`。
- **镜像构建（M11-08）**：`images.network` 缺省改为 `default`。

**不做什么**

| 不做 | 原因 |
|---|---|
| 终结 TLS、检查 HTTPS 隧道里的方法与路径 | 需要 CA，仍按 `egress.md` 5.4。监控规则对 HTTPS 只看得见 `CONNECT` 与主机 |
| `--privileged`、`--cap-add`、`--network host`、挂载 docker.sock | D11 不变 |
| 让厂长执行任意监控程序 | 监控条件是 CEL：确定、无副作用、可以静态检查 |
| `*` 出现在第一段以外的位置，或 `*.com` 这种只剩一段后缀的写法 | 防止一条规则放开整个顶级域 |

---

## 2. 依赖

| 依赖 | 用途 |
|---|---|
| `egress.md` | `build_egress`、`EgressPlan`、`EgressProxy`、`register_job_egress`、`proxy_env` |
| `approvals.md`、`delegation.md` | `open_request`（按需放行）、`open_directed`（监控 `hold`） |
| `foreman.md` | `WakeQueue`（监控 `wake`） |
| `factoryctl.md` | ctl 请求 / 响应文件（代理借它向 job 的 handler 提问） |
| `snapshot.md`、`factory-profile.md` | 表达式位置与类型环境 |
| `sandbox.md` | `SandboxSpec`、`build_run_argv` |
| `wiring.md` | 回执核验沙箱 |

---

## 3. 接口

### 3.1 `mao.egress` 的增补

```python
@dataclass(frozen=True)
class EgressPlan:
    network: Literal["none", "egress"]
    allow: tuple[AllowRule, ...]
    secret_names: tuple[str, ...]
    withheld: tuple[str, ...]
    env: dict[str, str]
    on_unlisted: Literal["deny", "ask"] = "deny"       # 新增，有缺省值
    monitored: bool = False                            # 新增：工位有 on: egress 的监控规则

def register_job_egress(db: Any, job_id: str, plan: EgressPlan, *, token: str | None = None) -> None
def register_check_egress(db: Any, principal: str, plan: EgressPlan, *, token: str) -> None
def close_egress(db: Any, principal: str) -> None
def host_matches(pattern: str, host: str) -> bool                       # 5.2
def credentialed_env(env: Mapping[str, str], principal: str, token: str) -> dict[str, str]   # 5.1 第 3 步

class EgressProxy:
    def __init__(self, plan_for, store, clock, *, db=None, root: Path | None = None,
                 ids: IdSource | None = None) -> None
        # root 是 FACTORY_ROOT。为 None 时不能向 job 提问：按需放行与监控都按拒绝处理
        # ids 用来生成事件 id（5.1 第 8 步）
```

`start_owned_proxies` 构造 `EgressProxy` 时传入 `root=ctx.root`、`ids=ctx.ids`。

### 3.2 工位的 `monitors` 段（`station.schema.json`，M11-07）

```yaml
monitors:
  publish-call:
    on: egress
    when: 'request.summary == "CONNECT member.bilibili.com"'
    action: hold
    timeout: 10m
```

- 对象，键匹配 `common.schema.json#/$defs/name`（小写字母、数字与连字符）。
- 每项有：
  - `on`：只能是 `egress`；
  - `when`：表达式（`$defs/expr`）；
  - `action`：`hold`、`deny` 或 `wake`；
  - 可选的 `timeout`（时长）。
- 不能有别的键。

### 3.3 `SandboxSpec` 的增补（M11-08）

```python
@dataclass(frozen=True)
class SandboxSpec:
    ...                                   # 原字段不变
    shm_mb: int | None = None
    tmp_mb: int | None = None
    pids: int | None = None
    writable: tuple[str, ...] = ()

def sandbox_resources(machine: Mapping[str, Any]) -> dict[str, Any]
    # mao.sandbox。读 runtime.resources，返回 {"cpus", "memory_mb", "shm_mb", "tmp_mb", "pids", "writable"}
    # 不合格 → RESOURCES_INVALID
```

---

## 4. 数据格式

### 4.1 迁移 `src/mao/migrations/038_egress_tokens.sql`

```sql
ALTER TABLE egress_plans ADD COLUMN token_sha256 TEXT;
ALTER TABLE egress_plans ADD COLUMN kind TEXT NOT NULL DEFAULT 'job';
ALTER TABLE egress_plans ADD COLUMN closed_at TEXT;
```

- `job_id` 列存主体名：job 主体是 job id，`check` 主体是登记时给的名字。
- 计划 JSON 增加 `on_unlisted` 与 `monitored` 两个键。

### 4.2 策略

```yaml
egress:
  publish: {hosts: [member.bilibili.com, "*.bilivideo.com"], methods: ["*"]}
  web_read: [www.bilibili.com]
  on_unlisted: ask
```

- `on_unlisted` 只能是 `deny` 或 `ask`，缺省 `deny`。`policy.schema.json` 的 `egress` 对象增加这个键。
- 只有 `none: true` 时写 `on_unlisted` 报 `EGRESS_CONFIG_INVALID`。

### 4.3 代理向 job 提问的 ctl 请求

代理写进 `$FACTORY_ROOT/jobs/<job>/<attempt>/ctl/requests/<id>.json`：

```json
{"id": "r-1a2b3c4d", "op": "egress", "args": {"method": "CONNECT", "host": "upos-sz.bilivideo.com", "listed": false}}
```

- `id` 的格式同 `factoryctl.md` 4.3，`<attempt>` 取 `jobs` 表中这个 job 当前的 attempt。
- 响应的 `result` 是 `{"decision": "approve"|"deny"|"timeout", "note": "", "code": ""}`。

### 4.4 监控的事件记录

条件的求值环境，与 `approval.in_run` 的环境相同（`factory-profile.md` 4.2 第 2 行）：

```text
request = {kind: "egress", summary: "<METHOD> <host>", cost_usd: 0.0}
inputs  = 本 job 的输入；params = 本 job 的参数
```

- `METHOD` 大写，HTTPS 隧道为 `CONNECT`。
- `host` 小写，不带端口。

### 4.5 事件

| kind | data |
|---|---|
| `egress.asked` | `{job_id, method, host, approval}` |
| `monitor.hit` | `{job_id, monitor, action, summary}` |

`egress.denied` 的 `code` 增加三个取值：`EGRESS_AUTH_REQUIRED`、`EGRESS_AUTH_INVALID`、`MONITOR_DENIED`。

### 4.6 机器的 `runtime.resources`

```yaml
runtime:
  resources: {cpus: 2, memory: 2Gi, shm_mb: 1024, tmp_mb: 512, pids: 1024, writable: [/home/pwuser]}
```

| 键 | 规则 | 缺省 |
|---|---|---|
| `shm_mb` | 整数，`>= 1` | 不传 `--shm-size` |
| `tmp_mb` | 整数，`>= 1` | 256 |
| `pids` | 整数，`>= 1` | 256 |
| `writable` | 字符串数组，不重复。每项以 `/` 开头，不含 `..`、`:`、`,`，不是 `/`，也不在 `/factory`、`/workspace`、`/opt/factory`、`/tmp` 之下 | `[]` |

`cpus`、`memory` 不变。有任何不合格项时报 `RESOURCES_INVALID`，`detail.field`。

### 4.7 `factory.json` 的 `images.network`

缺省值由 `"none"` 改为 `"default"`。人可以显式写 `"none"`，关闭构建网络。

---

## 5. 行为

### 5.0 解除 M1 的静态拒绝

`runner.md` 5.3 静态拒绝表里的 `EGRESS_UNSUPPORTED`、`SECRETS_UNSUPPORTED` 两行从 M11 起作废。现有代码一直保留着这两行，所以策略要出网的工位从来到不了沙箱。

策略要出网或要秘密时，`Runner` 按 `egress.md` 3.2：

- `PROVISIONING` 调用 `build_egress`；
- `network == "egress"` 时确保代理在跑，把 `plan.env`、`store.inject(plan.secret_names)` 与 `proxy_env` 合并进 `SandboxSpec.env`，再按 5.1 第 3 步加上凭据；
- `network == "none"` 时不注入秘密。

`inject` 遇到没保存的秘密抛 `SECRET_NOT_FOUND`，job 在 `PROVISIONING` 以 `FAILED` 结束，按 M1 的失败规则处理。

### 5.1 主体与令牌

1. **job**：
   - `Runner` 每个 attempt 在 `PROVISIONING` 生成 `token = secrets.token_hex(16)`；
   - 调用 `register_job_egress(db, job.id, plan, token=token)`。存进数据库的只有 `sha256(token)` 的十六进制。
2. **check**：
   - 回执核验沙箱的主体名是 `f"{job}-verify-{n}"`，秘密核验沙箱的主体名是它的 `label`（`inbox.md` 5.6）；
   - 用各自的新令牌调用 `register_check_egress`；
   - 沙箱关闭后（`finally`）调用 `close_egress`，写入 `closed_at`。
3. **环境**：`network == "egress"` 时，沙箱的 env 用 `credentialed_env(proxy_env(...) 并上 plan.env, principal, token)`。它做两件事：
   - 把 `HTTP_PROXY`、`HTTPS_PROXY`、`EGRESS_PROXY` 改写成 `http://<principal>:<token>@<host>:<port>`；
   - 增加 `FACTORY_EGRESS_USER=<principal>` 与 `FACTORY_EGRESS_TOKEN=<token>`。Chrome 不接受 URL 里的凭据，Playwright 要用 `proxy: {server, username, password}`。

   令牌不写入 job 行、事件、日志或 `build_run_argv` 的持久化副本。
4. **识别顺序**（取代 `egress.md` 5.4 的“识别 job”）：
   1. 有 `Proxy-Authorization`：
      - 不是 `Basic`，或 base64 解不开时，回 407（见第 3 小步）。
      - 用户名就是主体名。主体不存在、或 `sha256(密码)` 与 `token_sha256` 不一致时，回 403 `EGRESS_AUTH_INVALID`。比较用 `hmac.compare_digest`。
   2. 没有 `Proxy-Authorization` 但有 `X-Factory-Job: J`：
      - J 的计划没有令牌（旧式登记）时，按原规则识别；
      - 有令牌时回 407 `EGRESS_AUTH_REQUIRED`。冒用 job id 不再有效。
   3. 都没有：回 407，响应头 `Proxy-Authenticate: Basic realm="mao-egress"`，body 为 `{"code":"EGRESS_AUTH_REQUIRED"}`。
5. **主体是否开着**：
   - `job` 主体：job 状态在 `PROVISIONING`、`RUNNING`、`AWAITING_APPROVAL` 之中；
   - `check` 主体：`closed_at IS NULL`。

   否则回 403 `EGRESS_JOB_INVALID`。
6. **转发明文 HTTP 之前**，删掉请求里的 `Proxy-Authorization` 与 `X-Factory-Job`。上游永远看不到令牌。
7. 所有拒绝都写 `egress.denied`（`job_id` 是声称的主体名，可能为 `""`），body 不回显 URL。规则与 `egress.md` 5.4 第 4 条相同。
8. 代理写的事件 id 在多个代理实例之间也必须唯一。同一个数据库会先后有多个代理：守护进程重启，或者命令行为秘密核验自己开了一个。现在的 `ev-px%06d` 每个实例都从 1 数起，第二个实例写第一条事件就违反主键。改成用注入的 `IdSource`：`ids.new("ev")`。只有直接构造、没有传 `ids` 时才沿用 `ev-px` 序号。

### 5.2 通配域名

1. `build_egress` 规范主机名时，第一段可以是 `*`，也就是写成 `*.` 加后缀。后缀至少有两段，每段都是合法的主机名标签，而且不是 IP。否则报 `EGRESS_CONFIG_INVALID`。例如 `*.com`、`a.*.com`、`*` 都不合格。
2. `AllowRule.host` 存原样的模式，比如 `*.bilivideo.com`。
3. `host_matches(pattern, host)`：
   - 模式不以 `*.` 开头时，就是整串相等；
   - 以 `*.` 开头时，`host` 必须以 `"." + 后缀` 结尾，而且不等于后缀本身。`*.bilivideo.com` 匹配 `upos-sz.bilivideo.com`，也匹配 `a.b.bilivideo.com`，但不匹配 `bilivideo.com`。
4. 代理挑规则时一律用 `host_matches`。各个等级（包括 `publish`）和 `from-machine` 名单都可以写通配。

### 5.3 按需放行

代理拿到主体与计划之后：

| 主机在名单里 | `monitored` | `on_unlisted` | 动作 |
|---|---|---|---|
| 是 | 否 | — | 按方法规则放行或拒绝（原规则） |
| 是 | 是 | — | 先过方法规则，再向 job 提问（`listed: true`） |
| 否 | — | `deny` | 403 `EGRESS_DENIED`（原规则） |
| 否 | — | `ask` | 向 job 提问（`listed: false`） |

- `check` 主体与旧式登记的主体不提问：名单外一律拒绝，也不跑监控。
- 方法不符时总是拒绝，不提问。HTTPS 方法受限的规则仍然报 `EGRESS_METHOD_OPAQUE`（`egress.md` 5.4）。

**向 job 提问**：

1. 代理按 4.3 原子地写请求文件。请求 id 是 `"r-" + secrets.token_hex(4)`。
2. 每 0.05 秒（真实时间）看一次响应文件，每 1 秒检查一次主体是否还开着。主体关闭了：回 403 `EGRESS_JOB_INVALID`。
   - 等待用 `threading.Event().wait(0.05)`。`time.sleep`、`time.monotonic`、`uuid` 被代码规则禁止（`tests/test_codebase_policy.py`，D9）；注入的 `clock.sleep` 在 `FakeClock` 下会推进假时间，让审批提前到期，也不能用。
   - “每 1 秒”按每 20 次轮询计。
3. 读到响应后：
   - `approve`：放行，写 `egress.allowed`；
   - 其他：回 403，`code` 取响应里的 `code`，为空时用 `EGRESS_DENIED`。
4. 代理在等待时不占全局锁，别的连接照常处理。

**job 的 handler 收到 op `egress`**（`runner.md` 的 `_CtlHandler`）：

1. 先跑监控（5.4）。监控拒绝或挂起时，返回对应的结果。
2. `listed` 为真：返回 `approve`。
3. `listed` 为假：
   - 本 attempt 对这个 `host` 已经有结论时，直接返回它；
   - 否则调用 `Approvals.open_request(..., payload={"kind": "egress", "summary": f"{METHOD} {host}", "cost_usd": 0.0})`，按工位的 `approval.in_run` 规则路由（含 `delegation.md` 5.1 的改写）。没有规则时，`approvals.md` 5.2 第 3 步给出 `deny` / `APPROVAL_RULE_MISSING`；
   - 写 `egress.asked`；
   - 审批未结案时返回 `None`，结案后把决定（`timeout` 也算）记成这个 `host` 在本 attempt 的结论。
4. 结论只在本 attempt 内有效，新的 attempt 会重新提问。

### 5.4 监控规则

**编译**：kernel 把每条监控的 `when` 当作表达式位置抽出来：

- 主体是 `Station:<key>/monitors.<name>`，位置是 `spec.monitors.<name>.when`；
- 类型环境与 `approval.in_run` 相同；
- 类型错误按原规则是 `E-EXPR`。

`factory-profile.md` 4.2 的表增加这一行。`build_egress` 在工位有 `on: egress` 的规则时，把 `monitored` 置为真。

**求值**（handler 收到 op `egress` 时）：

1. 按 4.4 构造 `request`，按规则名升序逐条处理。每条规则按 `(规则名, summary)` 缓存结论，本 attempt 内同样的出网只判断一次。
2. 求值：`snapshot.compiled(f"{station}/monitors.{name}").evaluate(approval_env("in_run", request, inputs, params))`。
   - 求值抛错时**按命中处理**（失败即收紧），事件 data 加 `"error": "MONITOR_EVAL_ERROR"`。
   - 结果不是 `True`：不命中，看下一条。
3. 命中时写 `monitor.hit`，再按动作处理：

| 动作 | 处理 |
|---|---|
| `deny` | 返回 `{"decision": "deny", "code": "MONITOR_DENIED"}` |
| `wake` | `WakeQueue.file("monitor", f"{job}:{name}", {"job": job, "monitor": name, "summary": summary})`，然后继续下一条，不挡这次出网 |
| `hold` | `Approvals.open_directed(payload={"kind": "monitor:" + name, "summary": summary, "cost_usd": 0.0}, timeout=规则的 timeout 或工位的 approval.timeout 或 None)`。审批未结案时返回 `None`；批准后继续下一条；拒绝或超时时返回 `{"decision": "deny", "code": "MONITOR_DENIED"}` |

`hold` 总是交给厂长（`open_directed`）。`bypass` 不改变这一点，厂长也可以用 `approval_escalate` 把它交给人。

### 5.5 回执核验沙箱（补 `wiring.md` 5.1）

- 核验沙箱的 env 与 attempt 相同：`store.inject(plan.secret_names)`，再并上带凭据的代理环境（5.1 第 3 步，主体名 `f"{job}-verify-{n}"`）。
- 核验结束后，按 5.1 第 2 步关闭主体。

### 5.6 沙箱资源

1. 凡是由工位机器派生的 `SandboxSpec`，都调用 `sandbox_resources(machine)` 填 `cpus`、`memory_mb`、`shm_mb`、`tmp_mb`、`pids`、`writable` 这六个字段。这包括 job attempt、回执核验、秘密核验。厂长会话的沙箱不变。
2. 不合格时报 `RESOURCES_INVALID`。job 在 `PROVISIONING` 以 `FAILED` 结束，错误码就是这个，然后按 M1 的失败规则处理。
3. `DockerProvider.build_run_argv`：
   - `--pids-limit` 的值为 `str(pids or 256)`；
   - 原来的 `/tmp` tmpfs 写成 `/tmp:rw,nosuid,size={tmp_mb or 256}m`；
   - 紧接着，对 `writable` 的每一项按顺序追加 `--tmpfs <path>:rw,nosuid,size={tmp_mb or 256}m,mode=1777`；
   - `shm_mb` 不为 `None` 时，再追加 `--shm-size {shm_mb}m`；
   - 然后才是 `--cpus`、`--memory`。

   四个新字段都是缺省值时，argv 与原来逐字相同。D11 的禁止项不变。
4. `LocalProvider` 忽略这四个字段。

### 5.7 镜像构建网络

`ImageConfig` 的 `network` 缺省为 `"default"`（`images.md` 4.1）。合法值仍然只有 `none` 和 `default`。

---

## 6. 错误

新码都要登记到 `mao.errors`。

| 码 | 何时 | detail / 位置 |
|---|---|---|
| `EGRESS_AUTH_REQUIRED` | 没有凭据，或计划要求令牌却只给了 `X-Factory-Job`（HTTP 407 的 body） | — |
| `EGRESS_AUTH_INVALID` | 主体不存在或令牌不对（HTTP 403 的 body） | — |
| `MONITOR_DENIED` | 监控 `deny` 命中，或 `hold` 被拒绝、超时（HTTP 403 的 body 与 ctl 响应的 `code`） | — |
| `RESOURCES_INVALID` | 4.6 | `field` |

沿用的码：`EGRESS_CONFIG_INVALID`、`EGRESS_DENIED`、`EGRESS_JOB_INVALID`、`EGRESS_METHOD_OPAQUE`、`APPROVAL_RULE_MISSING`。

---

## 7. 验收测试

夹具：`tests/acceptance/m11/deleg_world.py`，见 `delegation.md` 第 7 章。

- 出网测试用进程内的上游 HTTP 服务（`127.0.0.1`，明文），记录收到的请求头。
- 操作工是标准库脚本，用 `urllib` 按环境变量里的代理访问上游。`urllib` 会把代理 URL 里的凭据变成 `Proxy-Authorization`。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m11/test_m11_04_egress.py` | `test_job_reaches_upstream_with_token` | 经 `Runner.run`，操作工按环境变量出网，上游收到请求，但请求头里没有 `Proxy-Authorization`、`X-Factory-Job`；`egress_plans` 只存令牌的 sha256；`FACTORY_EGRESS_USER` 是 job id |
| | `test_proxy_challenges_and_rejects` | 没有凭据 → 407，带 `Proxy-Authenticate: Basic`；令牌错误 → 403 `EGRESS_AUTH_INVALID`；有令牌的 job 只给 `X-Factory-Job` → 407；job 封存后用原凭据 → 403 `EGRESS_JOB_INVALID` |
| | `test_wildcard_hosts` | `*.example.invalid` 放行 `a.example.invalid` 与 `a.b.example.invalid`（`egress.allowed`，上游不可达时 502），拒绝 `example.invalid`；`*.invalid`、`a.*.example.invalid` 报 `EGRESS_CONFIG_INVALID` |
| | `test_unlisted_host_asks` | `on_unlisted: ask`：`auto_if` 成立的主机立即放行；不成立时 job `AWAITING_APPROVAL`、写 `egress.asked`，厂长拒绝后操作工收到 403，`code` 是审批响应里的 `APPROVAL_DENIED`；同一主机再请求不再开审批 |
| | `test_unlisted_without_rule_is_denied` | `on_unlisted: ask` 但工位没有 `approval.in_run`：403，审批表里没有行；`on_unlisted: deny`：不提问直接 403；只有 `none: true` 却写 `on_unlisted` → `EGRESS_CONFIG_INVALID` |
| | `test_receipt_verify_gets_secrets_and_proxy` | Sink 的核验配方读到注入的秘密，并带 `check` 主体的凭据经代理访问上游；核验后这个主体被关闭 |
| `tests/acceptance/m11/test_m11_07_monitors.py` | `test_monitor_hold_waits_for_foreman` | `hold` 命中：job `AWAITING_APPROVAL`、`monitor.hit`、审批 `kind` 为 `monitor:<name>` 且路由 `foreman`；厂长批准后出网放行；同一 `summary` 再请求不再挂起 |
| | `test_monitor_deny_and_wake` | `deny` → 403 `MONITOR_DENIED`，上游没收到；`wake` → 出网照常，开出 `monitor` 唤醒票（同一 job 同一规则只开一张） |
| | `test_monitor_hold_denied_or_timeout` | 厂长拒绝或到期 → 403 `MONITOR_DENIED`；`bypass` 下 `hold` 仍交给厂长 |
| | `test_monitor_compiles_in_kernel` | `when` 的类型错误使 kernel 检查器（`mrl.factory.check.check_registry`）报 `E-EXPR`，位置是 `spec.monitors.<name>.when`；`on` 不是 `egress` 时 `factory check` 以 schema 错误退出 3 |
| `tests/acceptance/m11/test_m11_08_sandbox.py` | `test_resources_reach_docker_argv` | 经 `Runner.run` 记录下的 `SandboxSpec` 带机器声明的四项资源，`DockerProvider.build_run_argv` 生成对应的 `--pids-limit`、`/tmp` 大小、`writable` tmpfs、`--shm-size`；没有声明时 argv 与 M1 相同 |
| | `test_invalid_resources_fail_job` | `writable` 含 `/workspace/x`、`pids: 0`：job `FAILED`，`RESOURCES_INVALID`，`field` 正确 |
| | `test_image_build_network_default` | 没有 `images` 段时，`ImageConfig.load(...).network == "default"`，构建 argv 里是 `--network default`；显式写 `none` 仍是 `none` |

验收命令：`uv run python tools/harness.py pytest <文件>`，断言 `out.failed == 0 && out.errors == 0 && out.skipped == 0`，`passed` 不少于上表的行数。

---

## 8. 错误示范与正确示范

```python
# ✖ 错误：识别 job 还是只看 X-Factory-Job，给 Playwright 写个插件补这个头
# ✔ 正确：每个 attempt 一个令牌，代理按 Proxy-Authorization 识别，没有就回 407 质询
```

```python
# ✖ 错误：转发时把客户端的请求头原样带给上游
# ✔ 正确：删掉 Proxy-Authorization 与 X-Factory-Job；上游看不到令牌
```

```python
# ✖ 错误：on_unlisted: ask 时代理自己弹审批、自己决定
# ✔ 正确：代理只写一个 op=egress 的 ctl 请求；job 的 handler 按工位规则开审批并给出结论
```

```python
# ✖ 错误：监控条件求值出错就当作没命中，免得卡住操作工
# ✔ 正确：求值出错按命中处理（失败即收紧），事件里记 MONITOR_EVAL_ERROR
```

```python
# ✖ 错误：为了让浏览器跑起来，给沙箱加 --privileged 或去掉 --read-only
# ✔ 正确：用 shm_mb、tmp_mb、pids、writable 给够资源；D11 的禁止项不变
```

---

## 9. 风险与注意事项

- **HTTPS 的粒度**：没有 CA 时，按需放行与监控对 HTTPS 只看得见主机。批准一次 `CONNECT host` 后，本 attempt 里对这个主机的后续隧道都会放行。
- **令牌的生命周期**：令牌在 attempt 结束、job 离开三个开着的状态后作废。操作工在 transcript 里打印出令牌，影响的也只是它自己那个 job。
- **按需放行的审批人**：工位的 `approval.in_run` 同时管 `approval request` 与按需放行。`auto_if` 可以用 `request.kind == "egress"` 把两者分开。
- **构建网络**：`images.network` 缺省出网以后，镜像构建步骤可以下载任意内容。需要更强保证的人，可以显式写 `"none"`。
- **长连接**：被挂起的出网会占住代理的一个线程，直到有结论或主体关闭。
