# B 形态网关（gateway.md）

> 规范性文档，M4。模块：`mao.gateway`（服务型机器：OpenAPI → 沙箱内 MCP，长任务 submit → poll → fetch）。
> 依据：架构文档 5.1 示例 B、附录 A.3；`spec/examples/registry/machines/moneyprinter/machine.yaml`。
> `connectors.md` 规定：M2 运行 `form: service` 时报 `FORM_UNSUPPORTED`。本文件是该形态成为合法执行路径的地方。M2 函数在未安装网关时仍抛 `FORM_UNSUPPORTED`。
> 控制面内不新增 agent 运行时，不调用 LLM（D10）。

---

## 1. 目的与范围

**做什么（M4）**

- 机器 `spec.form == "service"` 时，由本模块在**沙箱内**驱动长任务：提交、轮询、取回，再把文件放进 `/factory/out`，交给既有的出厂检验。
- 从服务的 OpenAPI 生成 MCP 工具清单，供沙箱里的 operator CLI 使用。生成是纯函数。MCP 进程跑在沙箱里，控制面进程不监听它的端口。
- 脚本工位不经过 agent：宿主只通过已有的文件邮箱让 `factoryctl` 发 HTTP，HTTP 的目标是沙箱网络里的服务，不是宿主网卡。

**不做什么**

| 不做 | 归属 |
|---|---|
| 在控制面进程里跑对话循环、选择工具、或调用模型 API | 禁止 |
| 把服务端口发布到宿主（`-p`、`--network host`） | 禁止（D11） |
| 用本模块替换 `factory mcp`（那是厂长操作面，`foreman.md`） | 两者不要接在一起 |
| 猜测示例 YAML 里没写完的 `done` / `fetch` 散文 | 缺字段就 `LONG_TASK_INVALID` |
| 新的第三方 OpenAPI 或 MCP 库 | 禁止（D2）。只解析下面写明的子集，用标准库 `json` 与已允许的 `pyyaml` |

---

## 2. 依赖

| 依赖 | 用到的接口 |
|---|---|
| `SandboxProvider` | `open`。网络仍是 `none` 或 M2 已实现的 `egress`。服务只在容器网络内可访问 |
| `Snapshot` | `spec`、`compiled`（配方 `call` 里的模板） |
| `mao.values` | `recipe_env` |
| `CtlServer` / `factoryctl` | 新增动词 `service`（只增，不改已有动词） |
| `Runner` | M4 在分派到“执行配方”之前：若 `form == service` 且本模块已安装，走网关；否则保持 `FORM_UNSUPPORTED` |
| `Clock` | 轮询间隔用 `clock.sleep`，测试可跳过真实等待 |

---

## 3. 接口

```python
@dataclass(frozen=True)
class LongTask:
    submit: str                  # "POST /api/v1/videos"
    poll: str                    # "GET /api/v1/tasks/{task_id}"
    task_id: str                 # JSON 指针，如 "/id"
    done: str                    # MRL 表达式，环境名 response，结果必须是 bool
    fetch: str                   # MRL 表达式，环境名 response，结果必须是 list<string>
    poll_every: str              # 时长，parse_duration

@dataclass(frozen=True)
class ToolSpec:
    name: str                    # operationId
    method: str                  # GET | POST | PUT | DELETE
    path: str
    parameters: tuple[dict[str, Any], ...]

def parse_long_task(machine_spec: Mapping[str, Any]) -> LongTask
    # 缺字段或形状不对 → FactoryError("LONG_TASK_INVALID")

def openapi_to_tools(document: Mapping[str, Any], *, allow: Sequence[str] | None) -> tuple[ToolSpec, ...]
    # 纯函数。见 5.2。无法映射的 operation 跳过并记入返回前的校验；被 allow 点名却映射失败 → OPENAPI_UNSUPPORTED

def mcp_manifest(tools: Sequence[ToolSpec], *, fmt: str) -> dict[str, Any]
    # fmt 只接受 OperatorRuntime 已经声明的 "claude-json"。其他 → OPENAPI_UNSUPPORTED

class ServiceGateway:
    def __init__(self, ctx: Context, snapshot: Snapshot) -> None
    def run_recipe(self, *, station_key: str, job_id: str, attempt: int,
                   sandbox: "Sandbox", inputs: Mapping[str, Any],
                   params: Mapping[str, Any], timeout_s: float) -> dict[str, Any]
        # 阻塞到取回或失败。返回 {task_id, fetches: [...]}。产物文件已在沙箱 /factory/out。
```

`timeout_s` 取工位 `limits.timeout`。工位没写超时 → `LONG_TASK_INVALID`，不使用一份全局默认秒数。

### 3.1 factoryctl 增补

沙箱内：

```
factoryctl service submit --method M --path P --body FILE
factoryctl service poll --method M --path P
factoryctl service fetch --url URL --dest PATH
factoryctl service openapi --url URL --out FILE
```

请求与响应仍走 `/factory/ctl` 邮箱（D7）。`submit` / `poll` 的 HTTP 由沙箱内的 `factoryctl` 发往机器 `runtime.service` 声明的地址（示例：`http://127.0.0.1:8080`，仅容器内）。宿主看不到这个端口。

宿主 `CtlHandler` 不代发 HTTP。它只记录邮箱里的状态，供审计。真正的 HTTP 在沙箱内完成。

### 3.2 CLI

```
factory gateway manifest MACHINE@V [--json]
```

在宿主上对已保存的 OpenAPI 文件跑 `openapi_to_tools`，不启动服务。用于验收纯函数。退出码：映射失败为 1。

---

## 4. 数据格式

### 4.1 机器字段

`spec.operate.long_task` 必须含 3. 节 `LongTask` 的全部键。`moneyprinter` 示例只写了 `submit` 与 `poll`，那份示例不能直接跑；缺 `done`、`fetch`、`task_id`、`poll_every` 时 `parse_long_task` 失败。不要把架构文档里的占位句子当成表达式。

`done` 与 `fetch` 用 `mrl.expr.compile_expr` 编译（D3）。`response` 的类型是 `map<dyn>`。`done` 的期望类型是 `bool`，`fetch` 的期望类型是 `list<string>`。静态错误 → `LONG_TASK_INVALID`。

`runtime.service` 至少有 `name` 与 `port`（整数）。`runtime.instance` 为 `per_job` 或 `pooled`。`pooled` 必须有 `reset`（字符串或字符串列表）。

### 4.2 邮箱文件

`/factory/ctl/service.json`（规范 JSON）：

```json
{
  "phase": "submit",
  "task_id": "",
  "polls": 0,
  "last_status": null,
  "fetches": []
}
```

`phase` 只允许 `submit`、`poll`、`fetch`、`done`、`failed`。宿主若看到沙箱把 `phase` 写成 `done` 但 `/factory/out` 仍空，且配方声明了输出：不当作成功，错误码 `SERVICE_FAILED`。

### 4.3 MCP 清单

`fmt == "claude-json"` 时，写出 operator 已有约定的 MCP 配置文件（路径用该 OperatorRuntime 的 `invoke.mcp` 声明）。工具条目只含 `name`、`description`（可为空串）、`input_schema`（由 OpenAPI parameter 的 `schema` 原样嵌上；没有 schema 则为 `{"type": "object"}`）。不嵌入服务的密钥。

### 4.4 迁移

不需要新表。一次调用的 `task_id` 写在 job 的事件里：

| kind | data |
|---|---|
| `service.submit` | `{job, task_id, method, path}` |
| `service.poll` | `{job, polls}` |
| `service.fetch` | `{job, count}` |
| `service.failed` | `{job, code}` |

`data` 不含响应体全文（可能含上游令牌）。响应体留在沙箱日志里，按 `foreman.md` 4.4 的规则打码之后才允许被查询。

---

## 5. 行为

### 5.1 何时接管

`Runner` 在 M2 见到 `form: service` 会抛 `FORM_UNSUPPORTED`。M4 的分派改为：

1. 本模块未安装（调用方没有构造 `ServiceGateway`）→ 仍抛 `FORM_UNSUPPORTED`，与 M2 相同。
2. 已安装 → `parse_long_task`。失败则 job `DEAD`，错误码 `LONG_TASK_INVALID`，不启动容器。
3. `instance == per_job`：本次 attempt 一个容器，结束即销毁。
4. `instance == pooled`：按机器键复用容器。两次 job 之间在沙箱内执行 `reset`（参数列表，禁止 `shell=True`；`reset` 若是字符串，则按空格拆分——只接受不含引号的简单拆分，含引号 → `LONG_TASK_INVALID`，要求写成字符串列表）。`reset` 非 0 → `SERVICE_FAILED`，销毁容器，下次重建。

容器参数仍由 `DockerProvider.build_run_argv` 产生。禁止额外添加 `-p`、`--network host`、`--privileged`。服务端口不映射到宿主。

### 5.2 OpenAPI → 工具

`openapi_to_tools`：

1. 文档必须是 OpenAPI 3.x 的 JSON 对象（`openapi` 字段以 `3.` 开头）。YAML 文本先用 `pyyaml` 载入，再当对象处理。
2. 只读 `paths` 下的 `get`、`post`、`put`、`delete`。其他方法忽略。
3. 每个操作必须有 `operationId`，匹配 `^[A-Za-z][A-Za-z0-9_]*$`。没有或重复 → 该操作不可用。
4. `allow is None`：不导出整个 API。只导出长任务四条路径对应的操作（若 OpenAPI 里有）。这是失败即关闭：没有白名单就不把服务的全部端点交给 agent。
5. `allow` 非空：只导出 `operationId` 在列表里的操作。列表里有、文档里没有或映射失败 → `OPENAPI_UNSUPPORTED`，`detail.missing` 列出 id。
6. 工位 `operator.tools` 若存在，它就是 `allow`。agent 调用不在清单里的工具时，沙箱内 MCP 进程返回错误，不转发 HTTP。错误码 `MCP_TOOL_DENIED` 记在沙箱日志里；宿主把 job 判为 `FAILED` 仅当 operator 因此非 0 退出（与普通工位相同）。

MCP 进程是随 `factoryctl` 一起装进镜像的标准库脚本，监听容器内 `127.0.0.1` 的临时端口。控制面不为它 `listen` 宿主地址。

### 5.3 submit → poll → fetch

1. 用 `snapshot.compiled` 渲染配方 `call` 里每个字符串值，环境是 `recipe_env`。非字符串值原样放进 JSON 体。禁止 `str.format`。
2. `factoryctl service submit`。响应必须是 JSON 对象。用 `task_id` 指针取值，缺指针或类型不是字符串 → `SERVICE_FAILED`。
3. 写 `service.json` 的 `phase = poll`。之后每次 `clock.sleep(parse_duration(poll_every))`，再 poll 一次。
4. 把 poll 的 JSON 作为 `response` 求值 `done`。真则进入 fetch。假则继续。求值错误 → `SERVICE_FAILED`，不要当成“未完成”。
5. 从进入 poll 到 `done` 为真的墙钟时间（`clock.monotonic`）超过 `timeout_s` → `SERVICE_TIMEOUT`。job `DEAD`，不重试（与超时的普通工位相同，除非工位自己的 `retries` 允许；那部分仍由 `runner.md` 决定）。
6. `fetch` 表达式得到 URL 或沙箱内绝对路径的列表。URL 必须是 `http://127.0.0.1:<port>/...` 或机器 service 名的容器内地址。其他主机 → `SERVICE_FAILED`（防止配方把取回指到公网，绕过出网策略）。路径必须在 `/workspace` 或服务自己的数据目录下，由 `factoryctl` 复制到 `/factory/out/<port>/`。
7. 复制完成后 `phase = done`。出厂检验仍由 `Gate.inspect` 做。网关不封存 Bean。

脚本工位走完 1–7 即结束，不启动 operator CLI。agent 工位在容器起来之后还按 `operator.md` 启动 CLI，MCP 清单指向沙箱内适配器；长任务的 submit/poll/fetch 仍由网关状态机执行，不交给模型决定何时算完成。

### 5.4 健康

容器启动后，`factoryctl` 对 `http://127.0.0.1:<port>/openapi.json`（或 `operate.openapi` 的路径部分）做一次 GET。连接失败 → `SERVICE_UNHEALTHY`，销毁本次容器。`pooled` 实例在 `reset` 之后也做这一次。不做无限重试。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `LONG_TASK_INVALID` | 缺字段、表达式编译失败、没有工位超时、`reset` 字符串含引号 | `machine, missing` 或 `mrl` |
| `OPENAPI_INVALID` | 文档不是对象，或 `openapi` 不是 3.x | `reason` |
| `OPENAPI_UNSUPPORTED` | allow 点名的 operation 无法映射，或 MCP fmt 未知 | `missing` 或 `fmt` |
| `SERVICE_UNHEALTHY` | 容器内 OpenAPI 地址连接失败 | `url`。url 不得含用户信息 |
| `SERVICE_TIMEOUT` | poll 超过工位超时仍未 done | `job, polls, timeout_s` |
| `SERVICE_FAILED` | 响应形状不对、done 求值失败、fetch 指向容器外主机、reset 非 0 | `job, reason` |
| `MCP_TOOL_DENIED` | 沙箱内调用了清单之外的工具（写入日志与事件；不一定单独抛出） | `tool` |

M2 的 `FORM_UNSUPPORTED` 保留：网关未安装时仍抛它，`detail` 与 `connectors.md` 相同。

---

## 7. 验收测试

服务用测试里的本地 HTTP 处理器（标准库）代替真实产品，由 `LocalProvider` 的命令映射把 `factoryctl service` 指到这个处理器。不要求 Docker，除非个别测试显式标记；验收文件整体必须能在无 Docker 时跑完且零跳过，因此本文件的验收**不**依赖 Docker。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m4/test_m4_03_gateway.py` | `test_m2_still_unsupported_without_gateway` | 不构造 `ServiceGateway` 时，`form: service` 仍是 `FORM_UNSUPPORTED` |
| | `test_incomplete_long_task` | 只有 submit/poll、没有 done → `LONG_TASK_INVALID`，不发 HTTP |
| | `test_submit_poll_fetch_order` | 假服务记录的调用顺序是 submit，然后若干 poll，然后 fetch；中间没有宿主侧的 listen |
| | `test_done_expression` | `done` 为 `response.status == "ok"` 时，假服务先返回 `run` 再返回 `ok`，轮询次数与 `clock.sleep` 次数一致 |
| | `test_timeout_uses_station_limit` | 假服务永不 done，`FakeClock` 推进超过工位超时 → `SERVICE_TIMEOUT` |
| | `test_fetch_rejects_public_host` | fetch 表达式返回 `https://example.com/a` → `SERVICE_FAILED`，没有出站请求 |
| | `test_openapi_allowlist` | `allow` 为空导出的工具集是长任务路径，不是文档里的全部 operationId |
| | `test_named_tool_missing` | `allow` 含不存在的 id → `OPENAPI_UNSUPPORTED` |
| | `test_mcp_manifest_pure` | 同一文档两次 `mcp_manifest` 逐字节相同 |
| | `test_no_host_port_flag` | 若测试调用 `DockerProvider.build_run_argv`，结果中没有 `-p` 与 `--network` 的 `host` |
| | `test_pooled_reset_between_jobs` | 两个 job 之间执行了 `reset` 的参数列表；第一个 job 的输出目录不被第二个读取 |

```
uv run python tools/harness.py pytest tests/acceptance/m4/test_m4_03_gateway.py
```

零失败、零跳过。

---

## 8. 错误示范与正确示范

**8.1 不要在控制面里当 agent**

```python
# ✖ 错误：网关进程连接模型 API，让模型决定 poll 还是 fetch
# ✔ 正确：done 是机器 Spec 里的 MRL 表达式，由控制面求值。模型若存在，只在沙箱内的 operator CLI 里
```

**8.2 不要把端口映到宿主**

```python
# ✖ 错误：docker run -p 8080:8080，然后宿主 requests.get("http://127.0.0.1:8080")
# ✔ 正确：不加 -p。HTTP 由沙箱内 factoryctl 发往容器内地址，宿主只看邮箱与 /factory/out
```

**8.3 缺字段不要脑补**

```python
# ✖ 错误：示例没写 done，就当成响应里 status == "completed"
# ✔ 正确：parse_long_task 抛 LONG_TASK_INVALID
```

**8.4 白名单默认关闭**

```python
# ✖ 错误：operator.tools 没写，就把 OpenAPI 里每个 operation 都变成 MCP 工具
# ✔ 正确：allow is None 时只保留长任务路径；点名了却没有的 id → OPENAPI_UNSUPPORTED
```

---

## 9. 风险与注意事项

- **pooled 容器的残留**。`reset` 失败必须销毁容器。不要在失败后继续复用，否则下一个 job 会看见上一个 job 的文件。
- **轮询与 FakeClock**。生产用 `clock.sleep`。测试推进虚拟时间。不要在网关里直接调用 `time.sleep`（D9）。
- **响应里的令牌**。事件只记 `task_id` 与次数。查询日志走 `foreman.md` 的打码。网关自己的异常 `detail` 不放响应体。
- **compose**。机器用 `runtime.compose` 时，仍由 `DockerProvider` 启动，本文件不引入 compose 库。compose 文件里若写了端口发布，`build_run_argv` 的安全检查（既有单元测试）应拒绝；网关不放松这条。
- **与 FORM_UNSUPPORTED 的并存**。M2 验收不构造 `ServiceGateway`。不要为了 M4 去改 `connectors.md` 的错误码语义。
---

## 10. 任务拆分

| ID | 标题 | 依赖 | 规范章节 | 允许修改的路径 | 验收 | 规模 |
|---|---|---|---|---|---|---|
| M4-03 | 长任务网关、OpenAPI 工具与 MCP 清单 | M2-11 | 3–7 | `src/mao/gateway.py`、`src/mao/ctl/**`（只增 `factoryctl service`）、`src/mao/cli.py`、`src/mao/errors.py`、`tests/system/test_gateway_*.py` | `test_m4_03_gateway.py`（第 7 章全部测试通过） | L |

不构造 `ServiceGateway` 时，`form: service` 仍是 `FORM_UNSUPPORTED`（`connectors.md`）。`tests/acceptance/m4/` 属于预先写好的验收材料。任务执行者不得修改它们。
