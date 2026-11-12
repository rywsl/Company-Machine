# hardening：M5–M7 交付审查后的纠偏（M8-01）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M8-01**。
> M5–M7 的阶段门全部通过，但审查（`plan/review-m5-m7.md`）发现：出网代理是空壳、守护进程拥有的代理不认识任何 job、job 子进程不用守护进程的代理、厂长值守只有 `--once` 能跑、被拒绝的提议让转换需求永远卡在 `in_progress`、回收有竞态、`serve` 遇到一次错误就退出。本文件把这些缺口逐条写成可验收的规定。凡是已有规范写清楚而实现没做的，本文件只引用原文；凡是原规范没写的（设计方缺口），本文件补上规定。

---

## 1. 目的与范围

**做什么（M8-01）**

| 编号 | 内容 | 性质 |
|---|---|---|
| R-01 | 出网代理按 `egress.md` 5.4 真正转发与拒绝 | 实现缺失（M2 起） |
| R-02 | 出网计划登记表；守护进程拥有的代理按登记表放行 | 设计缺口 + 实现错误 |
| R-03 | 所有命令的 `Context` 发现守护进程的代理 | 实现缺失（`runtimes.md` 5.4 第 3 条） |
| R-04 | Docker Desktop 的出网转发容器：镜像固定、归属、清理 | 设计缺口 + 实现越界 |
| R-05 | `ForemanSupervisor.serve` 与常驻值守；`factory serve` 托管值守 | 实现缺失（`supervisor.md`、`daemon.md` 5.7） |
| R-06 | 转换需求在提议失败后重开 | 实现缺失（`supervisor.md` 5.x 信号泵增补） |
| R-07 | 回收的死 digest 在写事务内判定 | 实现错误（`retention.md` 5.2） |
| R-08 | `serve` 一轮失败不退出；SIGTERM 正常收尾 | 设计缺口 |
| R-09 | 厂长会话使用模型代理时网络取 `egress` | 设计缺口 |

**不做什么**

| 不做 | 原因 |
|---|---|
| 出网代理的 TLS 终结（CA） | `egress.md` 5.4 已写明本模块不生成 CA；有 CA 时的行为不在本任务验收 |
| 新的出网级别或白名单语法 | 沿用 `build_egress` |
| 改 Bean 身份、迁移已有表 | 只新增表 `egress_plans` |

---

## 2. 依赖

| 依赖 | 用途 |
|---|---|
| `egress.md` | `EgressPlan`、`AllowRule`、`build_egress`、出网代理行为 5.4 |
| `runtimes.md` 5.4–5.6 | 代理归属、绑定地址、`${MODEL_PROXY}` 规则 |
| `daemon.md`、`supervisor.md`、`retention.md` | 被纠正的行为原文 |
| `durable.md` | `LeaseStore`、`LeaseKeeper` |

---

## 3. 接口

```python
# mao.egress（增补与修改）
class EgressProxy:
    def __init__(self, plan_for: Callable[[str], EgressPlan | None], store: SecretStore, clock: Clock,
                 *, db: Database | None = None) -> None
        # plan_for 返回 None：该 job 不可放行 → EGRESS_JOB_INVALID（5.1）
        # db 给出时：检查 job 状态、写 egress.allowed / egress.denied 事件

def register_job_egress(db: Database, job_id: str, plan: EgressPlan) -> None
    # 覆盖写 egress_plans；只存规则与秘密名字，不存值
def plans_from_db(db: Database) -> Callable[[str], EgressPlan | None]
    # job 不在 PROVISIONING / RUNNING / AWAITING_APPROVAL → None
    # job 合法但没有登记 → EgressPlan("none", (), (), (), {})

@dataclass(frozen=True)
class ProxyEndpoints:
    host: str
    egress_port: int
    model_port: int
    alias: str | None = None      # 新字段：Docker Desktop 转发容器在 mao-egress 上的地址（5.4）

# mao.daemon（新增）
def run_serve(ctx: Context, config: ServeConfig, *, executor: Any, stop: threading.Event,
              registry: Path | None = None, epochs: Any | None = None,
              pipeline: Any | None = None) -> None
    # factory serve（非 --once）的全部装配；commands/serve.py 只解析参数、装信号处理、调用它

# mao.supervisor（补齐）
class ForemanSupervisor:
    def serve(self, stop: threading.Event, *, idle_s: float = 5.0) -> None
```

`factory foreman supervise`（不带 `--once`）：`start()` 后调用 `serve(stop)`，直到 SIGINT / SIGTERM（Windows：Ctrl+C / Ctrl+Break），然后退出码 0。

迁移 `src/mao/migrations/035_egress_plans.sql`：

```sql
CREATE TABLE egress_plans (
  job_id TEXT PRIMARY KEY,
  plan   TEXT NOT NULL,        -- 规范 JSON：network、allow（tier、host、methods 升序）、secret_names、withheld
  at     TEXT NOT NULL
);
```

`env` 不入表（它只用于组装沙箱参数，代理用不到）。

---

## 4. 数据格式

### 4.1 `serve.json` 的 `proxies` 段

```json
{"proxies": {"host": "127.0.0.1", "egress_port": 51234, "model_port": 8080, "alias": "172.30.0.2"}}
```

`alias` 只在启动了转发容器时出现。`discover_proxies` 把它读进 `ProxyEndpoints.alias`。

### 4.2 `factory.json` 的 `serve.forwarder_image`

字符串，必须是带 `@sha256:` 的镜像引用；镜像里要有 `node`。只在 Docker Desktop 上需要（5.4）。不带 digest → `SERVE_CONFIG_INVALID`，`detail.field = "forwarder_image"`。

### 4.3 事件

| kind | data |
|---|---|
| `egress.allowed` | `{job_id, method, host}` |
| `egress.denied` | `{job_id, method, host, code}` |
| `serve.tick_failed` | `{tick, code}`；`code` 是 `FactoryError.code`，`OSError` 记为 `"OS_ERROR"` |
| `conversion.reopened` | `{id, changeset_id}`（`supervisor.md` 已登记，本任务实现） |

事件里不出现路径、查询串、请求体或任何头的值。

---

## 5. 行为

### 5.1 出网代理（R-01）

按 `egress.md` 5.4 实现，补充以下细节：

1. 处理 `GET`、`HEAD`、`POST`、`PUT`、`PATCH`、`DELETE`、`OPTIONS` 与 `CONNECT`。请求行必须是绝对 URI（`http://host[:port]/...`）或 `CONNECT host:port`，否则 400。
2. 取 `X-Factory-Job`。为空，或 `plan_for(job_id)` 返回 None → 403 `{"code":"EGRESS_JOB_INVALID"}`。
3. host 比较用 URI 的主机名（小写、不含端口），与 `AllowRule.host` 完全相等；端口取 URI 中的端口（缺省 80 / 443）。不做后缀匹配。
4. 明文请求：方法在规则的 `methods` 中（或含 `*`）→ 连接 `主机名:端口`，转发请求行（改为相对路径）、去掉 `X-Factory-Job` 与 `Proxy-*` 头后的其余头和请求体，原样回传响应，不跟随重定向。否则 403 `{"code":"EGRESS_DENIED"}`。
5. `CONNECT`：规则含 `*` → 回 200 后双向转发字节；规则限制了方法且没有 CA → 403 `{"code":"EGRESS_METHOD_OPAQUE"}`；host 无规则 → 403 `EGRESS_DENIED`。
6. 拒绝响应的正文只有 `{"code": ...}`，不回显 URL。
7. `db` 给出时写 4.3 的事件。

### 5.2 出网计划登记表（R-02）

1. `Runner` 在 PROVISIONING、`build_egress` 之后，与 `register_job_cap` 同一处调用 `register_job_egress(db, job.id, plan)`（`network` 为 `none` 的 job 也登记，代理据此拒绝）。
2. `start_owned_proxies` 构造代理时用 `EgressProxy(plans_from_db(ctx.db), store, ctx.clock, db=ctx.db)`。**禁止**用常量计划。
3. 进程内回退（`ctx.proxies` 为空时 Runner 自己启动代理，`runtimes.md` 5.4 第 4 条）同样用 `plans_from_db`。

### 5.3 发现守护进程的代理（R-03）

`commands.md` 5.1 的 `open_context` 在 `Context.open` 之后设置 `ctx.proxies = discover_proxies(ctx)`，对所有命令生效（包括 `factory job exec` 与独立的 `factory foreman supervise`）。`Runner` 与值守只在 `ctx.proxies` 为 None 时才在本进程启动代理。

### 5.4 Docker Desktop 的出网转发（R-04）

Docker Desktop 上 `--internal` 网络里的容器到不了 `host-gateway`，因此需要一个同时连在 `bridge` 与 `mao-egress` 上的转发容器。

1. 只有**拥有代理的进程**（`factory serve`，或进程内回退）启动转发容器；`job exec` 子进程用 `ProxyEndpoints.alias`，不启动。
2. 镜像取 `serve.forwarder_image`（4.2）。没有配置 → `EGRESS_PROXY_UNAVAILABLE`，`detail.reason = "forwarder-image"`。**禁止**写死镜像名。
3. 容器名 `mao-fwd-<owner 的 sha256 前 12 位>`；参数 `--rm`、`--read-only`、`--cap-drop ALL`、`--security-opt no-new-privileges`。
4. 每一步 docker 调用都捕获 `OSError` 与超时，失败（含 `docker network connect`）→ `EGRESS_PROXY_UNAVAILABLE`，并删除已创建的容器。
5. 拥有者关闭代理时删除容器：`Daemon.shutdown`；进程内回退在 `Context.close`。
6. `DockerProvider` 用 `ctx.proxies.alias`（有值时）组装 `--add-host=egress:<alias>` 与 `--add-host=model-proxy:<alias>`。

### 5.5 常驻值守（R-05）

1. `ForemanSupervisor.serve(stop, idle_s)`：未 `start` 则先 `start()`；循环 `run_once()`，返回 None 时 `stop.wait(idle_s)`；`stop` 置位或租约丢失（`LeaseKeeper.lost`）时退出循环；最后总是 `shutdown()`。单个会话的异常按 `supervisor.md` 记为会话失败，不让循环退出。
2. `run_serve`：`Daemon.serve(stop)` 在当前线程；`config.supervise` 为真时另起线程 `mao-supervisor` 运行 `ForemanSupervisor(ctx, epochs, SupervisorConfig.load(root), pipeline=daemon.pipeline).serve(stop)`。两者共用 `ctx`（`Database` 自带锁，可以跨线程）——这一条替换 `daemon.md` 5.7 中“各自的 Context”的写法。`Daemon.serve` 返回后置位 `stop` 并等待值守线程结束（最多 `session_timeout_s + 60` 秒）。
3. `factory serve`（非 `--once`）与 `factory foreman supervise`（非 `--once`）在主线程安装 SIGINT、SIGTERM（Windows 另加 SIGBREAK）处理函数，处理函数只置位 `stop`。

### 5.6 提议失败后重开转换需求（R-06）

实现 `supervisor.md` 中 `ConversionStore.reopen` 与信号泵增补的那一步：每轮泵开始时，`in_progress` 的需求若其 `changeset_id` 对应的 ChangeSet 是 `rejected`、`withdrawn` 或 `rolled-back`，调用 `reopen`（回到 `open`，`changeset_id` 置空，`updated_at` 更新，写 `conversion.reopened`），本轮随即按原表第一行为它开新的 `conversion` 票。

### 5.7 回收在写事务内判定（R-07）

`retention.md` 5.2 第 1–2 步必须在**同一个** `ctx.db.tx()` 里：先在事务内查询死 digest，再删除。禁止在事务外先算好集合再进事务删除（中间可能有新 Bean 引用同一 digest）。

### 5.8 `serve` 的韧性（R-08）

1. `Daemon.serve` 里一次 `tick()` 抛出 `LEASE_LOST` 以外的 `FactoryError` 或 `OSError`：写 `serve.tick_failed`，`logging` 记一条错误，继续下一轮（仍然先 `stop.wait(tick_s)`）。其他异常类型照常抛出。
2. 连续失败不设上限；外部监控通过 `factory status` 的心跳（`observability.md`）发现。

### 5.9 厂长会话的网络（R-09）

`supervisor.md` 第 10 步的 `network` 改为：会话 runtime 的 `invoke.argv` 或 `env` 引用了 `${MODEL_PROXY}` → `egress`（与 `runtimes.md` 5.6 同一规则）；否则取 `config.network`。

---

## 6. 错误

沿用已登记的错误码：`EGRESS_JOB_INVALID`、`EGRESS_DENIED`、`EGRESS_METHOD_OPAQUE`、`EGRESS_PROXY_UNAVAILABLE`、`SERVE_CONFIG_INVALID`、`CONVERSION_STATE_INVALID`。本任务不新增错误码。

---

## 7. 验收测试

`registry-m2` 与治理夹具，`LocalProvider`。出网测试在本机起一个明文 HTTP 上游，经代理真实收发字节。值守与守护进程测试在线程里运行真实循环，用 `stop` 事件结束。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m8/test_m8_01_hardening.py` | `test_egress_forwards_allowed` | 命中规则的明文 GET 经代理到达上游并原样返回；`egress.allowed` 只含方法与主机，不含路径与查询串 |
| | `test_egress_denials` | 方法不符、主机不符 → `EGRESS_DENIED` 且上游没有收到请求；缺少头、job 已封存 → `EGRESS_JOB_INVALID`；方法受限的 `CONNECT` → `EGRESS_METHOD_OPAQUE`；拒绝正文不回显 URL |
| | `test_owned_proxy_reads_registered_plan` | `start_owned_proxies` 的代理按 `egress_plans` 放行已登记的 job，拒绝未登记的 job |
| | `test_open_context_discovers_serve_proxies` | `serve` 租约有效且 `serve.json` 有 `proxies` 时，命令的 `Context.proxies` 等于这些端点；租约释放后为 None |
| | `test_supervisor_serve_loop` | `serve(stop)` 依次处理两张票直到置位 `stop`；结束后 `supervisor` 租约已释放 |
| | `test_run_serve_hosts_supervisor` | `supervise = true` 的 `run_serve` 在守护进程运行期间由值守线程处理唤醒票；`stop` 后两个租约都已释放 |
| | `test_rejected_proposal_reopens_conversion` | `in_progress` 需求的 ChangeSet 被拒绝后，一轮 tick 把需求重开为 `open`、清空 `changeset_id`，并开出新的 `conversion` 票 |
| | `test_tick_failure_does_not_stop_serve` | 治理流水线每轮抛 `DOCKER_FAILED` 时 `serve` 继续运行并写 `serve.tick_failed`；`stop` 后正常收尾 |

```
uv run python tools/harness.py pytest tests/acceptance/m8/test_m8_01_hardening.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 8`。

M6 的实况夹具（`tests/acceptance/m6/live_world.py`）在 `serve` 段写入 `forwarder_image`，取 `tools/live_versions.json` 的 `base`（已固定 digest、带 `node`），因此 Docker Desktop 上的阶段门 M8 经由 M6 实况测试真实启动转发容器。

R-04（Docker Desktop 转发）、R-07（竞态）、R-09 以及 Runner 的登记调用（5.2 第 1 条）没有确定性的验收，由阶段门审查代码确认，并由实现方在 `tests/system/` 写单元测试。

---

## 8. 错误示范与正确示范

```python
# ✖ EgressProxy 用 BaseHTTPRequestHandler 占位，只为让“代理在线”的检查通过
# ✔ 按 5.1 处理每个方法；验收用真实上游收发字节
```

```python
# ✖ EgressProxy(lambda _job: EgressPlan("none", ...), ...)：守护进程的代理拒绝一切
# ✔ EgressProxy(plans_from_db(ctx.db), ..., db=ctx.db)
```

```python
# ✖ cmd_supervise：start(); shutdown(); return 0 —— 常驻模式什么也不做
# ✔ start(); serve(stop)；信号处理只置位 stop
```

```python
# ✖ dead = _dead_digests(); with tx(): delete(dead)
# ✔ with tx(): dead = _dead_digests(); delete(dead)
```

---

## 9. 风险与注意事项

- **阶段门通过不代表功能存在**：本任务的验收全部真实收发或真实循环，不允许用“端口已绑定”“函数可调用”代替行为。
- **转发容器**：它连接内部网络与外部网桥，是隔离边界上的一个洞；只转发两个代理端口，参数按 5.4 第 3 条收紧。
- **共享 Context**：值守线程与守护进程共用一个 `Database` 连接，靠锁串行。长事务会互相阻塞；仍然遵守“事务内不做子进程调用”（S7）。
