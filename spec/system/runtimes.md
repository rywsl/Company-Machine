# runtimes：真实 agent CLI 的接入与模型代理的生产路径（M6-01）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M6-01**。
> M1–M5 的 agent 全是 `tests/fakes/fake_agent.py`。它不连模型代理，所以下面几处缺口从没暴露：真实 CLI 带不上 `X-Factory-Job`（`plan/drift.md` D-28）；模型代理按 M2 的规则数不对 Anthropic 与 OpenAI 的流式用量；代理只能活在发起 job 的那个进程里（D-27）；容器怎样连到宿主上的代理没有人规定（D-30）。本文件补齐这些，并给出 Claude Code 与 Codex 的参考 runtime 文档。
> 本任务不需要 Docker，也不需要真实 CLI：用 `tests/fakes/fake_llm.py` 当模型上游。真实 CLI 在 Docker 里的验收在 M6-03（`live.md`）。

---

## 1. 目的与范围

**做什么（M6-01）**

- 控制面变量 `${...}`：runtime 模板渲染之后、启动之前由控制面替换（5.1）。
- 参考 runtime 文档 `spec/runtimes/claude-code.yaml`、`spec/runtimes/codex.yaml`（4.1）。
- transcript 格式 `codex-jsonl`（5.2）。
- 模型代理：上游 URL 的拼接、可计量路径、流式用量的取法、客户端鉴权头的剥离（5.3）。
- 代理归 `factory serve` 所有，`job exec` 子进程与值守经 `serve.json` 发现（5.4）。
- 代理的绑定地址与 `mao-egress` 网络（5.5）。
- 引用模型代理的 agent 工位总能到达它，即使策略只有 `none`（5.6，D-31）。

**不做什么**

| 不做 | 归属 |
|---|---|
| 构建 CLI 的注入镜像、机器镜像 | M6-02，`images.md` |
| 真实 CLI 在 Docker 里的端到端 | M6-03，`live.md` |
| 按 runtime 名字写特例（“如果是 claude 就加某个头”） | 禁止。差异只来自 runtime 文档 |
| 内置任何模型的价格 | 禁止（`egress.md` 第 3 章） |

---

## 2. 依赖

| 依赖 | 用途 |
|---|---|
| `build_invocation`、`build_runtime_invocation`、`parse_transcript`（`operator.md`、`supervisor.md` 3.2） | 渲染与 transcript |
| `ModelProxy`、`EgressProxy`、`egress_run_args`（`egress.md`） | 代理 |
| `Runner`（`runner.md`）、`ForemanSupervisor`（`supervisor.md`） | 替换控制面变量的两个调用方 |
| `Daemon`、`ServeConfig`、`serve.json`（`daemon.md`） | 代理的归属 |
| `LeaseStore`（`durable.md`） | 判断 `serve` 是否在线 |

---

## 3. 接口

### 3.1 `mao.operator`

```python
CONTROL_VARS: tuple[str, ...] = ("FACTORY_JOB_ID", "FACTORY_ATTEMPT", "MODEL_PROXY", "EGRESS_PROXY")

def expand_control_vars(inv: Invocation, values: Mapping[str, str]) -> Invocation
    # 把 argv 与 env 值里的 "${NAME}" 换成 values[NAME]。见 5.1
```

`build_invocation` 与 `build_runtime_invocation` 的第 4 步之后增加一步：渲染结果里出现 `${NAME}` 而 `NAME` 不在 `CONTROL_VARS` → `INVOCATION_INVALID`，`detail.var = NAME`。这两个函数**不**替换变量，返回值里保留 `${NAME}` 原文。

`parse_transcript(path, fmt)` 的 `fmt` 增加 `"codex-jsonl"`；`build_invocation` 第 5 步的 `transcript` 相应接受 `format == "codex-jsonl"`。

### 3.2 `mao.egress`

```python
@dataclass(frozen=True)
class ProxyEndpoints:
    host: str            # 代理绑定的地址（宿主侧）
    egress_port: int
    model_port: int

def discover_proxies(ctx: Context) -> ProxyEndpoints | None
    # serve 租约有效，且 serve.json 含 proxies 段 → 返回；否则 None。见 5.4
def proxy_online(host: str, port: int, *, timeout_s: float = 1.0) -> bool
    # TCP 连接成功即 True。不发 HTTP 请求
def proxy_bind_host(docker_os: str, gateway: str | None) -> str
    # 纯函数。见 5.5
def ensure_egress_network(docker: str, *, timeout_s: float) -> str
    # 没有 mao-egress 网络就创建；返回其网关地址。见 5.5
```

### 3.3 其他模块的增补

```python
# mao.context
class Context:
    proxies: ProxyEndpoints | None      # Context.open(..., proxies=None)。Runner 与值守据此取端口

# mao.daemon
@dataclass(frozen=True)
class ServeConfig:
    ...                                  # daemon.md 原有字段
    model_port: int = 8080               # factory.json serve.model_port，可省略
```

`ServeConfig` 的新字段有缺省值，M5-04 的构造方式不变。

---

## 4. 数据格式

### 4.1 参考 runtime 文档

`spec/runtimes/` 下的两份 YAML 是部署时复制进 Registry 的起点。它们不属于示例 Registry（`spec/examples/registry/` 不改），由本任务的验收测试复制进测试 Registry 编译。

`spec/runtimes/claude-code.yaml`：

```yaml
apiVersion: mao/v1
kind: OperatorRuntime
metadata: {name: claude-code, version: 1}
spec:
  kind: agent-cli
  inject:
    volume: mao-live/claude-code@sha256:0000000000000000000000000000000000000000000000000000000000000000
    mount: /opt/operator
  invoke:
    argv: [/opt/operator/bin/claude, -p, "{{ brief.task }}",
           --append-system-prompt, "{{ brief.factory_contract }}",
           --output-format, stream-json, --verbose,
           --max-turns, "{{ limits.max_turns }}",
           --model, "{{ operator.model }}",
           --permission-mode, bypassPermissions,
           --strict-mcp-config]
    mcp: {format: claude-json, flag: --mcp-config}
  env:
    ANTHROPIC_BASE_URL: "${MODEL_PROXY}"
    ANTHROPIC_API_KEY: factory-proxy
    ANTHROPIC_CUSTOM_HEADERS: "X-Factory-Job: ${FACTORY_JOB_ID}"
    HOME: /workspace/.home
    DISABLE_AUTOUPDATER: "1"
    DISABLE_TELEMETRY: "1"
    DISABLE_ERROR_REPORTING: "1"
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1"
  transcript: {format: stream-json, from: stdout}
  models_allowed: [claude-opus-5-5, claude-sonnet-5-5]
```

`spec/runtimes/codex.yaml`：

```yaml
apiVersion: mao/v1
kind: OperatorRuntime
metadata: {name: codex, version: 1}
spec:
  kind: agent-cli
  inject:
    volume: mao-live/codex@sha256:0000000000000000000000000000000000000000000000000000000000000000
    mount: /opt/operator
  invoke:
    argv: [/opt/operator/bin/codex, exec, --json, --skip-git-repo-check,
           --dangerously-bypass-approvals-and-sandbox,
           --model, "{{ operator.model }}",
           -c, "model_provider=\"factory\"",
           -c, "model_providers.factory.name=\"factory\"",
           -c, "model_providers.factory.base_url=\"${MODEL_PROXY}/v1\"",
           -c, "model_providers.factory.wire_api=\"responses\"",
           -c, "model_providers.factory.env_key=\"FACTORY_MODEL_KEY\"",
           -c, "model_providers.factory.http_headers={\"X-Factory-Job\"=\"${FACTORY_JOB_ID}\"}",
           "{{ brief.factory_contract }}\n\n{{ brief.task }}"]
    mcp: {format: codex-toml}
  env:
    FACTORY_MODEL_KEY: factory-proxy
    HOME: /workspace/.home
  transcript: {format: codex-jsonl, from: stdout}
  models_allowed: [gpt-5-codex]
```

要点：

- `ANTHROPIC_API_KEY`、`FACTORY_MODEL_KEY` 的值 `factory-proxy` 不是秘密，只是让 CLI 不去走登录流程。模型代理会剥掉它（5.3 第 3 条），换成真实密钥。
- 注入卷的 digest 是占位值。部署时用 `images.md` 构建出的引用替换，再经 ChangeSet 提交。
- CLI 的参数以 `tools/live_versions.json` 固定的版本为准。M6-03 的验收在 Docker 里用这些版本真实运行这两份文档。实现方发现某个参数在固定版本里不存在时，按 `plan/README.md` 第 5 节写日志并阻塞，不要自己改参考文档或换版本。

### 4.2 `serve.json` 的增补（`daemon.md` 4.2）

```json
{"...": "daemon.md 原有字段", "proxies": {"host": "127.0.0.1", "egress_port": 53121, "model_port": 8080}}
```

### 4.3 codex-jsonl 的形状

每行一个 JSON 对象，`type` 字段区分事件。本规范只依赖这些：

| `type` | 用到的字段 |
|---|---|
| `turn.completed` | `usage`（不使用，计费以模型代理为准） |
| `turn.failed` | `error` |
| `error` | `message` |
| `item.completed` | `item.type`、`item.text`（`agent_message`） |

其他类型计入 `events`，不解释。

---

## 5. 行为

### 5.1 控制面变量

1. 识别：正则 `\$\{([A-Z0-9_]+)\}`。只有 `CONTROL_VARS` 里的名字合法。
2. `build_*` 时检查名字（3.1），不替换。
3. 启动前由调用方替换：

| 变量 | `Runner`（工位 job） | 值守（`supervisor.md`） |
|---|---|---|
| `FACTORY_JOB_ID` | job id | 会话 id |
| `FACTORY_ATTEMPT` | attempt | `wake.attempts + 1` |
| `MODEL_PROXY` | 见下表 | 见下表 |
| `EGRESS_PROXY` | 见下表 | 见下表 |

| provider | `MODEL_PROXY` | `EGRESS_PROXY` |
|---|---|---|
| Docker，`network == "egress"` | `http://model-proxy:<model_port>` | `http://egress:<egress_port>` |
| Local | `http://127.0.0.1:<model_port>` | `http://127.0.0.1:<egress_port>` |

端口取自 `ctx.proxies`（5.4）；`ctx.proxies` 为空时取本进程里启动的代理（M2 的行为）。

4. 引用了某个变量而它没有值（例如 `network == "none"` 却引用 `MODEL_PROXY`，或没有任何模型代理）→ 不启动沙箱：job `FAILED`，`error.code = "EGRESS_PROXY_UNAVAILABLE"`，`detail.var`；会话 `failed`，同一个码。
5. 替换只发生在 argv 与 env 的**值**里。env 的键不替换。替换结果不再递归展开。

为什么不把 job id 放进 MRL 模板环境：模板环境是 factory-profile 的类型环境，属于 M0 的内核；控制面变量是运行期才知道的值（端口、job id），不应进入 Spec 的编译期语义。

### 5.2 `codex-jsonl`

`parse_transcript(path, "codex-jsonl")`：

- `events`：能解析为 JSON 对象的行数；`malformed`：其余非空行数；
- `turns`：`type == "turn.completed"` 的行数；
- `is_error`：出现 `turn.failed` 或 `error` 事件，或者 `turns == 0`；
- `result`：最后一个 `item.completed` 且 `item.type == "agent_message"` 的 `item.text`；没有则 None；
- `cost_usd`：`0.0`。Codex 不报告金额，花费以模型代理的计量为准（`egress.md` 5.5 第 10 条：有模型用量时不再加 transcript 的成本）。

### 5.3 模型代理（`egress.md` 5.5 的增补）

1. **上游地址**：`u = urlsplit(upstreams[model].url)`。请求路径（含查询串）为 `p`。若 `u.path` 非空、不是 `/`，且 `p` 的路径部分以 `u.path` 开头 → 目标是 `u` 的 scheme 与 netloc 加上 `p`；否则目标是 `url.rstrip("/") + p`。例：`https://api.anthropic.com` + `/v1/messages?beta=true`；`https://api.openai.com/v1` + `/v1/responses` → `https://api.openai.com/v1/responses`；M2 测试里的 `http://127.0.0.1:N/v1/messages` + `/v1/messages` 不变。
2. **可计量路径**：`POST` 且路径以 `/messages`、`/responses` 或 `/chat/completions` 结尾。其余请求（例如 `/v1/messages/count_tokens`、`GET /v1/models`）照样要求 `X-Factory-Job` 与 `model_caps` 行（第 1、2 步），转发但不计量，也不因为没有用量而 502。模型名缺省时用 `"*"` 的上游。
3. **剥离客户端的鉴权头**：转发前删除 `authorization`、`x-api-key`、`proxy-authorization`、`x-factory-job`（大小写不敏感），再加上游配置的 `header`。客户端给的值一律不转发、不记录。
4. **用量的取法**（替换原第 6 条中“流式响应：把含上述字段的事件加总”）：
   - 非流式：响应体是一个对象；流式（`Content-Type: text/event-stream`）：每个 `data:` 行是一个对象，`[DONE]` 忽略。
   - 对每个对象，候选用量对象依次是 `obj["usage"]`、`obj["message"]["usage"]`、`obj["response"]["usage"]`（存在且是对象的才算）。
   - 输入 token：`input_tokens` 加上存在的 `cache_creation_input_tokens`、`cache_read_input_tokens`；没有 `input_tokens` 时用 `prompt_tokens`。输出 token：`output_tokens`，没有则 `completion_tokens`。
   - **同一响应内，输入、输出各取所有候选里的最大值**。两家 API 的流式用量都是累计值（Anthropic 的 `message_start` 报输入、`message_delta` 报累计输出；OpenAI 的 `response.completed` 报总数）。加总会重复计数。
   - `cost_usd` 仍然优先：任一对象顶层或用量对象里有 `cost_usd` 时，取其中最大值，不再按单价计算。
   - 一个候选都没有：按原第 6 条 `MODEL_USAGE_UNPARSEABLE`。
5. **单价**：`prices.json` 先按模型名查，再查 `"*"`；都没有 → `BUDGET_PRICE_MISSING`（原第 7 条不变）。真实 CLI 会用次要模型发内部请求，部署时要么为它们写单价，要么写 `"*"`。
6. **转发方式**：代理读完上游响应再转发给沙箱（M2 的行为不变）。流式响应因此整段到达。CLI 能处理这种情况；M6-03 在 Docker 里验证。

### 5.4 代理的归属

1. `Daemon.start`（`factory serve`）在取得 `serve` 租约之后启动两个代理：`EgressProxy.start(host, 0)`、`ModelProxy.start(host, config.model_port)`；`host` 按 5.5。端口写进 `serve.json` 的 `proxies`。守护进程自己的 `Context.proxies` 设为同样的值。`shutdown` 时关闭代理。
2. `discover_proxies(ctx)`：`LeaseStore(ctx).valid("serve")` 不为 None，且 `serve.json` 有 `proxies` 段，且 `proxy_online(host, model_port)` → 返回 `ProxyEndpoints`；任一条件不满足 → None。
3. `factory job exec`（`durable.md`）与独立运行的 `factory foreman supervise`：`Context.open(..., proxies=discover_proxies(...))`。值守由 `serve` 托管时直接用守护进程的端点。
4. `Runner` 与值守：`ctx.proxies` 不为空时**不**在本进程启动代理，用它的端口组装 `HTTP_PROXY`（`egress.md` 5.6）与控制面变量；为空时保持 M2 的行为（本进程启动）。
5. `DockerProvider.open` 判断“有活着的出网代理”的方式改为：`spec.env` 里 `HTTP_PROXY` 的端口在 `ctx.proxies.host`（没有则 `127.0.0.1`）上 `proxy_online`。不再要求代理与 provider 在同一个进程。不在线 → `EGRESS_UNSUPPORTED`（码不变）。
6. 模型代理只有一个监听端口。多个 `job exec` 子进程共用 `serve` 的代理，不各自绑定 8080。

### 5.5 绑定地址与 `mao-egress` 网络

容器经 `host-gateway` 访问宿主上的代理（`egress.md` 5.6）。`host-gateway` 在不同平台指向不同的地址：

| 平台（`docker info --format '{{.OperatingSystem}}'`） | 代理绑定 | 原因 |
|---|---|---|
| 含 `Docker Desktop` | `127.0.0.1` | Docker Desktop 把 `host-gateway` 转发到宿主回环 |
| 其他（Linux 原生引擎） | `mao-egress` 网络的网关地址 | 原生引擎上 `host-gateway` 是网桥地址，访问不到只绑定回环的服务 |

- `proxy_bind_host(docker_os, gateway)`：第一行返回 `"127.0.0.1"`；否则返回 `gateway`；`gateway` 为 None → `EGRESS_PROXY_UNAVAILABLE`，`detail.reason = "no-gateway"`。**禁止**返回 `0.0.0.0`。
- `ensure_egress_network(docker, timeout_s)`：`docker network inspect mao-egress` 失败时执行 `docker network create --internal mao-egress`；然后用 `docker network inspect mao-egress --format '{{(index .IPAM.Config 0).Gateway}}'` 取网关。命令失败 → `EGRESS_PROXY_UNAVAILABLE`，`detail.reason` 为打码后的 stderr。
- `--internal`：容器不能绕过代理直接出网。代理在网关地址上监听，容器仍能连到它。
- `LocalProvider`：一律 `127.0.0.1`，不调用 docker。
- 本进程自己启动代理时（`ctx.proxies` 为空，5.4 第 4 条），只要 provider 是 Docker，同样按本表选择绑定地址。
- `Daemon.start` 在 Docker provider 下调用 `ensure_egress_network` 与 `proxy_bind_host`，再启动代理（5.4 第 1 条）。

### 5.6 agent 工位与模型代理

`egress.md` 5.3 第 2 步规定：策略只有 `none` 时 `network` 为 `none`。这对脚本工位正确，对使用模型代理的 agent 工位是错的：容器在 `--network none` 下连不到 `model-proxy`，真实 CLI 一个请求也发不出去（D-31）。架构 6.4 说模型流量不占用出网级别。

修改 `build_egress`：工位的 OperatorRuntime 是 `agent-cli`，**且**其 `invoke.argv` 或 `env` 的任一值引用了 `${MODEL_PROXY}` 时，

- `network` 一律为 `egress`；
- `allow` 仍按策略计算。策略只有 `none` 时 `allow` 为空：出网代理拒绝一切主机，容器只能到达 `model-proxy`；
- `secret_names` 不变：仍然不注入模型类秘密，`none` 策略也不因此获得任何秘密。

不引用 `${MODEL_PROXY}` 的 runtime（例如测试用的 fake agent）与脚本工位不受影响，M1、M2 的行为不变。判断只看 runtime 文档里有没有这个变量，不看 runtime 的名字。

---

## 6. 错误

本任务不新增错误码。用到的码：

| 错误码 | 本文件中的触发条件 | detail |
|---|---|---|
| `INVOCATION_INVALID` | runtime 模板里出现不在 `CONTROL_VARS` 中的 `${NAME}` | `var` |
| `EGRESS_PROXY_UNAVAILABLE` | 引用的控制面变量没有值；取不到 `mao-egress` 的网关；建网络失败 | `var` 或 `reason` |
| `EGRESS_UNSUPPORTED` | `network == "egress"` 而 `HTTP_PROXY` 的端口不在线 | `port` |
| `TRANSCRIPT_FORMAT` | `transcript.format` 不是 `stream-json` / `codex-jsonl` | `format` |

---

## 7. 验收测试

不需要 Docker。模型上游是 `tests/fakes/fake_llm.py`（脚本化的 Anthropic Messages 与 OpenAI Responses，见其文档字符串）。transcript 夹具在 `tests/fixtures/transcripts/`。参考 runtime 文档由测试复制进 `registry-m2` 的副本编译。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m6/test_m6_01_runtimes.py` | `test_control_vars_kept_until_expanded` | `build_invocation` 的结果保留 `${FACTORY_JOB_ID}`；`expand_control_vars` 替换 argv 与 env 值；缺值 → `EGRESS_PROXY_UNAVAILABLE`；`${HOME}` 这类不在名单的名字 → `INVOCATION_INVALID` |
| | `test_reference_runtimes_compile` | 两份参考文档复制进 Registry 后编译通过；`build_runtime_invocation` 的 argv 头部、`transcript`、env 中的 `${FACTORY_JOB_ID}` 与 `${MODEL_PROXY}` 符合 4.1 |
| | `test_parse_claude_stream` | 真实形状的 stream-json 夹具：`turns`、`cost_usd`、`result`、`is_error` |
| | `test_parse_codex_jsonl` | codex 夹具：`turns == 2`、`result` 为最后一条 agent_message、`cost_usd == 0.0`；失败夹具 `is_error` 为真 |
| | `test_meter_anthropic_stream` | 经模型代理请求 fake_llm 的 Anthropic 流：花费按“各取最大值”计算（不是加总）；上游收到的 `x-api-key` 是密钥值，不是 `factory-proxy`；上游没有收到 `x-factory-job` |
| | `test_meter_openai_stream` | OpenAI Responses 流：`response.completed` 的用量被计量；URL 拼接得到 `/v1/responses` |
| | `test_count_tokens_not_metered` | `/v1/messages/count_tokens` 被转发、返回 200，不写 `model_usage`，也不写 `budget_stops` |
| | `test_discover_proxies` | `serve` 租约有效且 `serve.json` 有 `proxies`、端口在线 → 返回端点；租约过期或端口不在线 → None |
| | `test_agent_station_reaches_model_proxy_only` | fake-agent 的 env 引用 `${MODEL_PROXY}` 后，`writer@1`（策略只有 `none`）：`network == "egress"`、`allow` 为空、没有注入秘密；不引用时仍是 `none`；脚本工位 `upper@1` 是 `none` |
| | `test_proxy_bind_host` | Docker Desktop → `127.0.0.1`；Linux 引擎 → 网关地址；没有网关 → `EGRESS_PROXY_UNAVAILABLE`；从不返回 `0.0.0.0` |

```
uv run python tools/harness.py pytest tests/acceptance/m6/test_m6_01_runtimes.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 10`。

---

## 8. 错误示范与正确示范

```python
# ✖ 在 Runner 里认识 Claude Code
if runtime_name.startswith("claude"): env["ANTHROPIC_CUSTOM_HEADERS"] = f"X-Factory-Job: {job_id}"
# ✔ runtime 文档写 "X-Factory-Job: ${FACTORY_JOB_ID}"，控制面只做通用替换
inv = expand_control_vars(inv, {"FACTORY_JOB_ID": job_id, ...})
```

```python
# ✖ 流式用量逐事件加总：message_start 的 output_tokens=1 加上 message_delta 的累计值
out_tokens = sum(e["usage"]["output_tokens"] for e in events)
# ✔ 同一响应内取最大值
out_tokens = max(candidates_output)
```

```python
# ✖ 把客户端的 x-api-key 一起转发上游
# ✔ 先删 authorization / x-api-key / proxy-authorization / x-factory-job，再加配置的头
```

```python
# ✖ 为了让容器连得上，把代理绑到 0.0.0.0
proxy.start(host="0.0.0.0")
# ✔ Docker Desktop 绑回环；Linux 绑 mao-egress 的网关
proxy.start(host=proxy_bind_host(docker_os, gateway))
```

```python
# ✖ 每个 job exec 子进程各开一个模型代理
# ✔ 代理归 serve；子进程 discover_proxies
```

---

## 9. 风险与注意事项

- **CLI 版本漂移**：真实 CLI 的参数与事件格式会变。参考文档与 `versions.json` 一起固定，升级 CLI 是一次 ChangeSet（改注入卷与 runtime 文档），走 T2。M6-03 的验收就是升级时的回归。
- **整段转发**：代理读完响应才转发，长回复的首字延迟变大。换成边读边转发需要先解决“流到一半发现超预算”时怎么办，由人改 `egress.md`。
- **缓存 token 的价格**：本规范把缓存读写 token 计为普通输入，偏保守（多算）。需要精确计价时，由人给 `prices.json` 增加字段并修改本文件。
- **`--internal` 网络**：容器里依赖直连的工具会失败，这是有意的：出网只走代理（D12）。
- **Windows**：Docker Desktop 是唯一受支持的 Windows 引擎；`proxy_bind_host` 据 `docker info` 判断，不靠 `os.name`。
