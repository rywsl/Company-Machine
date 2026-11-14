# mcp：厂长的 MCP 操作面与 `factoryctl mcp` 邮箱桥（M5-05）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M5-05**。
> `foreman.md` 3.6 要求 `factory mcp`，实现没有做（`plan/drift.md` D-20）。
> 原文的方法名（`foreman.get` 等）也不是 MCP 协议，Claude Code、Codex 这类客户端连不上，这是设计错误。本文件取代 `foreman.md` 3.6 中关于 `factory mcp` 的那一段，其余命令行不变。
> 厂长新增 `approval_get`、`approval_decide`、`approval_escalate`（`delegation.md` 3.3）与 `escalate`、`inbox_get`、`inbox_list`、`secret_request`、`secret_verify`（`inbox.md` 3.5），M11。
> **M12/M13 增补**：`instance_list` 见 `instances.md` 3.5（M12）；`Session.agent`、`agent` 角色与它的工具见 `agents.md` 3.4（M13）。冲突时以新规范为准。

---

## 1. 目的与范围

厂长和复核者是在沙箱里运行的现成 agent CLI。它们通过 MCP 工具查询工厂、提交 ChangeSet 和复核意见。

**做什么（M5-05）**

- `mao.mcp.McpServer`：标准 MCP 服务端（JSON-RPC 2.0，按行收发）。实现 `initialize`、`notifications/initialized`、`ping`、`tools/list`、`tools/call`。
- 按会话角色暴露工具（第 3.3 节）。
- 身份来自会话，不来自工具参数：提议的 `by`、复核的 `runtime` 都是会话的 `actor`。
- `factory mcp --role foreman|reviewer`：在 stdin/stdout 上服务，供宿主上的人或测试使用。
- `factoryctl mcp`：沙箱内的 stdio 服务端，把每条消息经控制通道（`/factory/ctl` 邮箱，D7）转给宿主。宿主侧是 `McpCtlHandler`。沙箱里不开端口。

**不做什么**

| 不做 | 归属 |
|---|---|
| 启动 agent CLI、写 MCP 配置文件 | M5-06，`supervisor.md` |
| 合入、推进、回滚、改宪法、取唤醒票 | 禁止暴露（`foreman.md` 第 1 章） |
| HTTP / SSE 传输 | 不做。只有 stdio |
| MCP 的 resources、prompts、sampling | 不做 |

---

## 2. 依赖

| 依赖 | 用到的接口 |
|---|---|
| `ForemanQuery`、`ForemanSubmit`、`ConversionStore`（`foreman.md`） | 全部只读查询；`propose`、`withdraw`、`status`、`review` |
| `EpochStore`、`ChangePipeline`（`governance.md`） | 构造上述对象 |
| `CtlServer`、`CtlHandler`（`factoryctl.md`） | 邮箱桥 |
| `jsonschema` | 校验工具参数（D2 允许） |
| `mao.base` | `canonical_json` |

---

## 3. 接口

### 3.1 `mao.mcp`

```python
PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")   # 新的在前

@dataclass(frozen=True)
class Session:
    role: str                 # "foreman" | "reviewer"
    actor: str                # OperatorRuntime 引用，例如 "claude-code@4"；在 active 快照里必须能解析
    wake_id: str | None = None

class McpServer:
    def __init__(self, ctx: Context, epochs: EpochStore, session: Session, *,
                 pipeline: ChangePipeline | None = None) -> None
        # actor 解析不到 → MCP_ACTOR_INVALID；role 不是两者之一 → MCP_ACTOR_INVALID
    def tools(self) -> list[dict[str, Any]]
        # tools/list 的 tools 数组：每项 {name, description, inputSchema}，按 name 升序
    def handle(self, message: Any) -> dict[str, Any] | None
        # 一条已解析的 JSON 值 → 一条响应；通知（没有 id）返回 None
    def serve_stdio(self, stdin: TextIO, stdout: TextIO) -> None
        # 逐行读 stdin，直到 EOF。每条响应写一行并 flush。解析失败的行按 5.2 回 -32700
```

### 3.2 `mao.ctl`

```python
class McpCtlHandler:                         # 实现 CtlHandler
    def __init__(self, server: McpServer) -> None
    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None
        # op == "mcp"：{"id", "ok": True, "result": {"message": server.handle(args["message"])}, "error": None}
        # 其他 op：ok False，error.code = "CTL_REQUEST_INVALID"
```

工位 job 的控制通道 handler（`runner.md`）收到 `op == "mcp"` 必须回 `CTL_REQUEST_INVALID`：工位里的 agent 不能经邮箱访问厂长的操作面。

### 3.3 工具

所有工具的 `inputSchema` 都是 `{"type": "object", "properties": {...}, "required": [...], "additionalProperties": false}`。

| 工具 | 参数 | 调用 | 角色 |
|---|---|---|---|
| `registry_get` | `key` | `ForemanQuery.registry_get` | 两者 |
| `registry_list` | `kind` | `registry_list` | 两者 |
| `registry_diff` | `base`、`commit` | `registry_diff`（超时 `governance.git_timeout_s`） | 两者 |
| `epoch_list` | — | `epoch_list` | 两者 |
| `epoch_show` | `commit`（可选） | `epoch_show` | 两者 |
| `bean_get` | `bean_id` | `bean_get` | 两者 |
| `bean_lineage` | `bean_id`、`direction`（`up`/`down`） | `bean_lineage` | 两者 |
| `job_get` | `job_id` | `job_get` | 两者 |
| `job_transcript` | `job_id` | `job_transcript` | 两者 |
| `dlq_list` | — | `dlq_list` | 两者 |
| `drift_list` | — | `drift_list` | 两者 |
| `invariant_report` | — | `invariant_report` | 两者 |
| `metrics_query` | `station`、`metric` | `metrics_query` | 两者 |
| `approval_list` | — | `approval_list` | 两者 |
| `conversion_list` | `status`（可选） | `ConversionStore.list` | 两者 |
| `changeset_status` | `changeset_id` | `ForemanSubmit.status` | 两者 |
| `changeset_propose` | `document`（object）、`commit` | `ForemanSubmit.propose(document, commit, by=session.actor, timeout_s=git_timeout_s)` | 仅 `foreman` |
| `changeset_withdraw` | `changeset_id` | `ForemanSubmit.withdraw(id, by=session.actor)` | 仅 `foreman` |
| `review_submit` | `changeset_id`、`verdict`（`approve`/`deny`）、`note` | `ForemanSubmit.review(id, {"verdict": v, "runtime": session.actor, "note": note}, by=session.actor)` | 仅 `reviewer` |

参数里**没有** `by`、`runtime`、`actor`。传了 → `additionalProperties: false` 校验失败 → `-32602`。角色看不到的工具，`tools/list` 不列出；`tools/call` 调用它 → `-32602`，消息说明未知工具（不透露该工具存在）。

---

## 4. 数据格式

### 4.1 帧

stdio 上每条消息是一行规范 JSON（`canonical_json`，内部没有换行），以 `\n` 结尾。stdout 只写协议消息；诊断写 stderr。

### 4.2 `initialize` 的结果

```json
{
  "protocolVersion": "2025-06-18",
  "capabilities": {"tools": {"listChanged": false}},
  "serverInfo": {"name": "mao-factory", "version": "<mao.__version__>"},
  "instructions": "工厂操作面。只读查询与 ChangeSet 提议；身份由会话决定。"
}
```

`protocolVersion`：客户端请求的版本在 `PROTOCOL_VERSIONS` 里则原样返回，否则返回 `PROTOCOL_VERSIONS[0]`。

### 4.3 `tools/call` 的结果

成功：

```json
{"content": [{"type": "text", "text": "<canonical_json(value)>"}],
 "structuredContent": {"result": value}, "isError": false}
```

组件抛出 `FactoryError`（包括 `QUERY_DENIED`、`SECRET_LEAK`、`CHANGESET_STALE` 等）：

```json
{"content": [{"type": "text", "text": "<canonical_json(exc.to_json())>"}], "isError": true}
```

这是工具执行失败，不是协议错误，所以放在 `result` 里而不是 JSON-RPC `error` 里（MCP 规范的约定）。文本先经 `redact_obj`。

### 4.4 JSON-RPC 错误

| 情况 | `code` | `id` |
|---|---|---|
| 行不是合法 JSON | `-32700` | `null` |
| 不是对象，或缺 `jsonrpc: "2.0"` / `method`；JSON 数组（批量） | `-32600` | 能取到就用请求的，否则 `null` |
| `initialize` 之前调用 `ping`、`initialize` 以外的方法 | `-32600`，`message` 为 `not initialized` | 请求的 id |
| 未知方法 | `-32601` | 请求的 id |
| 未知工具、参数不合 `inputSchema` | `-32602` | 请求的 id |
| 其他未捕获异常 | `-32603`，`message` 只有异常类型名 | 请求的 id |

### 4.5 邮箱请求

`factoryctl mcp` 写的请求（格式同 `factoryctl.md` 4.3）：

```json
{"id": "r-1a2b3c4d", "op": "mcp", "args": {"message": {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}}}
```

响应的 `result.message` 是 `McpServer.handle` 的返回值（通知时为 `null`）。

---

## 5. 行为

### 5.1 会话状态

`McpServer` 实例保存一个布尔值 `initialized`。收到 `initialize` 后置真；重复 `initialize` 返回同样的结果。`notifications/initialized` 与其他通知只被接收，不回复。

### 5.2 `serve_stdio`

1. 逐行读。空行跳过。
2. `json.loads` 失败：写 `{"jsonrpc": "2.0", "id": null, "error": {"code": -32700, "message": "parse error"}}`。
3. 否则 `handle`；返回值不为 None 时写一行。
4. EOF：返回。

### 5.3 身份

- `changeset_propose` 的 `by` 永远是 `session.actor`。`ChangePipeline` 据此确定提出者的 runtime（`governance.md` 5.5）。
- `review_submit` 的 `runtime` 永远是 `session.actor`。复核者与提出者相同时，`add_review` 抛 `REVIEW_SAME_RUNTIME`（`isError: true`）。
- 文档里的 `metadata.proposer` 等字段原样保存在 ChangeSet 文档里，但不参与身份判断。

### 5.4 `factory mcp`

```
factory mcp --role foreman|reviewer
```

1. `actor = $FACTORY_ACTOR`。没有 → 退出码 2。
2. 打开 Context、`EpochStore`（需要治理；没有 → `REGISTRY_NOT_CONFIGURED`，退出码 4）。
3. `McpServer(ctx, epochs, Session(role, actor)).serve_stdio(sys.stdin, sys.stdout)`。stdout 只写协议消息，因此本命令不打印任何其他内容；`--json` 被忽略。
4. EOF 后退出 0。

### 5.5 `factoryctl mcp`

沙箱内命令，单文件、只用标准库，兼容 Python 3.8（D7）。

1. 读 `/factory/ctl/job.json` 的 `timeout_s` 作为每条请求的等待上限。文件不存在：退出码 4。
2. 逐行读 stdin：
   - 解析失败：本地回 `-32700`，不发给宿主。
   - 否则按 `factoryctl.md` 5.4 写 `requests/<id>.json`，`op = "mcp"`，`args.message` 为原消息。
3. 每 0.05 秒查看一次 `responses/<id>.json`。等到之后：
   - `ok` 为真且 `result.message` 不为 null：把它写成一行到 stdout 并 flush；
   - `ok` 为假：若原消息有 `id`，本地回 `-32603`，`message` 为 `error.code`。
4. 超时：若原消息有 `id`，本地回 `-32603`，`message` 为 `timeout`；继续处理下一行。
5. stdin EOF：退出 0。

请求按顺序逐条处理，不并发。这样响应顺序与请求顺序一致。

### 5.6 宿主侧

值守进程（`supervisor.md`）为每个会话构造一个 `McpServer` 和一个 `CtlServer(ctl_dir, McpCtlHandler(server), clock)`，会话结束时 `stop`。`McpServer` 不跨会话复用。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `MCP_ACTOR_INVALID` | `role` 不是 `foreman`/`reviewer`，或 `actor` 在 active 快照里不是 OperatorRuntime | `role, actor` |

协议层的错误用 JSON-RPC 错误码（4.4），不登记为 `FactoryError`。

---

## 7. 验收测试

治理夹具 `tests/acceptance/m3/gov_world.py`。复核者与提出者用夹具里的两个 runtime：`fake-agent@1` 与 `script@1`。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m5/test_m5_05_mcp.py` | `test_initialize_and_tools_by_role` | `initialize` 回显支持的版本；`foreman` 的工具清单含 `changeset_propose`、不含 `review_submit`；`reviewer` 相反；清单按名字升序 |
| | `test_version_negotiation` | 不支持的版本得到 `PROTOCOL_VERSIONS[0]` |
| | `test_jsonrpc_errors` | 未初始化、未知方法、未知工具、参数多出 `by`、坏 JSON 行分别得到 4.4 的错误码；通知没有响应 |
| | `test_tool_result_and_tool_error` | `registry_get` 成功时 `structuredContent.result` 是文档；未知键时 `isError: true`，文本里是 `QUERY_DENIED` |
| | `test_propose_identity_from_session` | 经 MCP 提议的 ChangeSet 的 `proposer` 等于会话 actor，与文档里的 `metadata.proposer` 无关 |
| | `test_review_runtime_from_session` | `reviewer` 会话提交的复核计入 `approvals`，runtime 是会话 actor；与提出者相同 runtime 的会话得到 `REVIEW_SAME_RUNTIME` |
| | `test_factory_mcp_stdio` | 子进程 `factory mcp --role foreman`：`initialize`、`tools/list` 两条请求得到两行响应，id 对应 |
| | `test_factoryctl_bridge` | `factoryctl mcp` 经邮箱与宿主的 `McpCtlHandler` 往返：两条请求加一条通知，只得到两行响应，顺序一致 |
| | `test_ctl_handler_rejects_other_ops` | `McpCtlHandler` 收到 `op == "check"` 回 `CTL_REQUEST_INVALID` |

```
uv run python tools/harness.py pytest tests/acceptance/m5/test_m5_05_mcp.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 9`。

---

## 8. 错误示范与正确示范

```python
# ✖ 自创方法名，MCP 客户端连不上
if method == "foreman.get": ...
# ✔ tools/call，工具名在 params.name
if method == "tools/call" and params["name"] == "registry_get": ...
```

```python
# ✖ 身份取自工具参数，厂长可以冒充别的 runtime 给自己投票
submit.review(cs_id, {"verdict": v, "runtime": arguments["runtime"]}, by=arguments["by"])
# ✔
submit.review(cs_id, {"verdict": v, "runtime": session.actor, "note": note}, by=session.actor)
```

```python
# ✖ 工具失败时回 JSON-RPC error，agent 以为连接坏了
return {"jsonrpc": "2.0", "id": i, "error": {"code": -32000, "message": exc.code}}
# ✔ 工具执行失败放在 result 里，isError 为真
return {"jsonrpc": "2.0", "id": i, "result": {"content": [...], "isError": True}}
```

```python
# ✖ 沙箱里开一个 TCP 端口让 agent 连
# ✔ factoryctl mcp 走 /factory/ctl 邮箱（D7）
```

---

## 9. 风险与注意事项

- **协议演进**：MCP 规范仍在演进。`PROTOCOL_VERSIONS` 是唯一的版本列表，升级时只改它和对应的行为，并由人更新本文件。
- **大结果**：`job_transcript` 可能很长。打码后整段返回，不截断（截断会让 agent 误判）。邮箱文件的大小没有上限。
- **顺序**：`factoryctl mcp` 串行处理。agent 并发发送多条请求时会排队，这是有意的：控制面的写操作不需要并发。
- **打码**：工具结果一律经过 `ForemanQuery` 已有的打码，`SECRET_LEAK` 时整段换成错误（`foreman.md` 3.2）。
