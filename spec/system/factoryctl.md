# factoryctl：沙箱内客户端与宿主 CtlServer（mao.ctl）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M1-05**。
> 沙箱内脚本：`src/mao/ctl/factoryctl.py`。宿主侧：`mao.ctl`。
> 依据：README D7、第 4.6 节、第 5–6 节。架构文档 6.2 的命令名已经定了，**禁止改名**。
> 新命令 `factoryctl ask`（`delegation.md` 3.4）与 `factoryctl handoff`（`inbox.md` 3.6），M11。

---

## 1. 目的与范围

沙箱里的进程不能开网络，也不能 import `mao`。它们用一个单文件脚本 `factoryctl` 跟宿主说话。通道是 `/factory/ctl` 下的文件邮箱：写临时文件，再 `os.replace` 到最终名字（原子改名，D7）。

- 沙箱内：`factoryctl` 读契约、交产物、收货、写日志、声明失败；需要宿主回答的操作写成请求文件并等待响应。
- 宿主侧：`CtlServer` 轮询请求目录，调用 `CtlHandler`，把响应原子写入。

不做的事：

| 不做 | 归属 |
|---|---|
| 审批决定、出厂检验本身 | `runner.md`、`gate.md`。本组件只传请求和响应 |
| `factoryctl receipt`、`submit --label` | M2（`connectors.md`、`lines.md`）在本文件上**追加**子命令和参数。M1 遇到未知子命令或未知参数，退出码 2 |
| MCP 版 factoryctl、Station Gateway | M4 |
| 为控制通道开端口 | README D7，禁止 |

`factoryctl` 必须能在 Python ≥ 3.8 上运行，只用标准库，**禁止** `import mao` 或 `mao.*`。宿主侧 `CtlServer` 用 Python ≥ 3.12，可以 import `mao.base`。

---

## 2. 依赖

沙箱内脚本：`argparse`、`json`、`os`、`sys`、`shutil`、`glob`、`pathlib`、`time`、`uuid`、`datetime`。禁止第三方包。D9 不约束这个文件（`foundation.md` 5.12）：它可以用 `uuid`、`time`、`datetime`。

宿主侧：

- `mao.base.Clock`、`utc_iso`（给响应盖时间时用；后台线程的等待**不用** `clock.sleep`，见 5.4）。
- 标准库 `threading`、`json`、`os`。
- 不 import `mao.runner`。Runner 把 handler 传进来，避免循环依赖。

安装位置由 `foundation.md` 5.11 的 `install_factoryctl` 完成：源文件复制到 `$FACTORY_ROOT/tools/factory/bin/factoryctl`，CRLF 规范成 LF，`chmod 0o755`。沙箱把它只读挂到 `/opt/factory/bin/factoryctl`。

---

## 3. 接口

### 3.1 宿主侧（README 4.6，签名不改）

```python
class CtlServer:
    def __init__(self, ctl_dir: Path, handler: "CtlHandler", clock: Clock) -> None
    def poll_once(self) -> int                         # 处理当前所有请求，返回处理的条数
    def start(self, interval_s: float = 0.2) -> None   # 后台线程
    def stop(self) -> None

class CtlHandler(Protocol):
    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None
        # 返回响应；返回 None 表示暂不答复，下次轮询再问
```

`mao.ctl` 导出 `CtlServer`。实现可以放在 `mao/ctl/server.py`，由 `mao/ctl/__init__.py` 再导出。`factoryctl.py` 不被 `mao.ctl` 在导入时执行。

### 3.2 沙箱内命令（名字锁定）

这些名字来自架构文档 6.2 和 README 第 6 节。禁止改成 `submit-output`、`ask` 之类。

```text
factoryctl inputs [--json]
factoryctl contract [--json]
factoryctl check <port> <path>
factoryctl submit <port> <path> [--meta KEY=VALUE ...]
factoryctl approval request --kind KIND --summary TEXT [--cost-usd N]
factoryctl budget [--json]
factoryctl log <message>
factoryctl fail <reason>
factoryctl harvest
```

`tests/fakes/fake_agent.py` 的 `ctl` 动作会执行 `sys.executable $FACTORYCTL <args>`，并把退出码对 `allow_exit`（默认 `[0]`）检查。所以：

- 成功的命令退出码必须是 0；
- 脚本必须能用当前解释器直接运行（不依赖 shebang 在 Windows 上生效）；
- 容器里同时保留 shebang `#!/usr/bin/env python3`，因为 `docker exec /opt/factory/bin/factoryctl` 靠它。

未知子命令、缺少参数、`--meta` 不是 `KEY=VALUE`：退出码 2，stderr 一行中文说明。M2 才会加上的 `receipt` 和 `submit --label` 在 M1 就是未知参数，退出码 2。不要预先实现它们。

---

## 4. 数据格式

所有 JSON 都是 UTF-8、无 BOM。宿主写入响应时用 `json.dumps(..., ensure_ascii=False, sort_keys=True)`。沙箱内脚本写出的文件同样 `sort_keys=True`，便于测试比较。临时文件后缀是 `.part`，`os.replace` 成最终名字。`CtlServer` 只认 `requests/*.json`，忽略 `.part`。

路径一律先按环境变量换成真实路径（5.1），再读写。

### 4.1 `job.json`（Runner 写，factoryctl 读）

```json
{
  "job_id": "j-0001",
  "attempt": 1,
  "station": "Station:writer@1",
  "epoch": "e-0123456789abcdef",
  "inputs": {"task": {"id": "sha256:…", "type": "agent.task@1.0", "facts": {}, "meta": {}, "payload": []}},
  "params": {"topic": "工厂"},
  "budget_usd": 1.0,
  "timeout_s": 120.0
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `job_id` | string | job ID |
| `attempt` | int | 从 1 开始 |
| `station` | string | Spec 键 |
| `epoch` | string | 快照 epoch |
| `inputs` | object | 端口 → `bean_value` 的对象；`list<T>` 端口的值是这种对象的数组 |
| `params` | object | `params_value` 之后的参数 |
| `budget_usd` | number | 工位预算；没有时为 0 |
| `timeout_s` | number | 本次 attempt 的超时，秒 |

`inputs` 的值就是 README 4.3 的 `bean_value`。未封存候选不会出现在这里（R9）。

### 4.2 `contract.json`（Runner 写，factoryctl 读）

```json
{
  "station": "Station:writer@1",
  "outputs": {
    "note": {
      "type": "note.text@1",
      "optional": false,
      "guarantees": ["non_empty"],
      "accept": ["out.facts.lines >= 2"],
      "payload": ["note.txt"]
    }
  },
  "approval": {"in_run": {"auto_if": "request.cost_usd <= 0.5", "else": "foreman"}, "timeout_s": 600.0},
  "limits": {"timeout_s": 120.0, "budget_usd": 1.0, "retries": {"rejected": 1, "failed": 1, "mode": "fresh"}}
}
```

| 字段 | 说明 |
|---|---|
| `outputs.<port>.type` | 工位上书写的类型表达式，不解析 minor |
| `outputs.<port>.payload` | 该类型 required 且不含通配符的 payload 路径，升序。给 agent 看交付文件名 |
| `approval` | 没有审批规则时为 `{}`。`timeout_s` 来自 `approval.timeout`，缺省 600 |
| `approval.in_run` | 规则是字符串时写成 `{"approver": "foreman"}`；是 `{auto_if, else}` 时原样放这两个键 |
| `limits` | 缺省字段不写。`retries` 没有时不写 |

`contract` 命令把这个文件原样打到 stdout（`--json` 与否相同，它本来就是 JSON）。文件不存在：退出码 4。

### 4.3 请求与响应

`requests/<id>.json`：

```json
{"id": "r-1a2b3c4d", "op": "approval", "args": {"kind": "cost", "summary": "买素材", "cost_usd": 1.0}}
```

| 字段 | 说明 |
|---|---|
| `id` | `r-` + 8 位小写 hex（`uuid.uuid4().hex[:8]`）。文件名的主干必须等于 `id` |
| `op` | `check` 或 `approval` |
| `args` | 见下表 |

| op | args |
|---|---|
| `check` | `{"port": "<port>", "path": "<调用方写的路径>"}` |
| `approval` | `{"kind": "<kind>", "summary": "<text>", "cost_usd": <number>}`。省略 `--cost-usd` 时 `cost_usd` 为 0 |

`responses/<id>.json`（handler 返回的对象，原样写入）：

```json
{"id": "r-1a2b3c4d", "ok": true, "result": {"decision": "approve", "by": "auto", "note": ""}, "error": null}
```

失败时 `ok` 为 false，`result` 为 null，`error` 是 `{"code": "...", "message": "..."}`。`factoryctl` 只认文件名，不要求响应里再重复 id；但 Runner 的 handler 必须写上 `id`，与请求相同。

### 4.4 `submissions/<port>.json`

`submit` 成功后覆盖写入（最后一次为准）：

```json
{"port": "note", "source": "/workspace/drafts/a.txt", "files": ["a.txt"], "meta": {"title": "天"}, "at": "2026-11-10T00:00:00Z"}
```

`files` 是复制到 `/factory/out/<port>/` 下的相对路径。`meta` 没有 `--meta` 时为 `{}`。`at` 是 UTC，秒精度，`Z` 结尾。factoryctl 不追求毫秒。

同时，若有 `--meta`，把 `/factory/out/<port>/meta.json` 写成这些键值（值都是字符串，`sort_keys=True`）。重复的键，后写的覆盖先写的。

### 4.5 `harvest.json` / `harvest-result.json`

Runner 在调用 `factoryctl harvest` 之前写好 `harvest.json`：

```json
{
  "outputs": {
    "note": {"harvest": "drafts/*.txt", "place_as": "note.txt", "optional": false, "list": false}
  }
}
```

| 字段 | 说明 |
|---|---|
| `harvest` | 配方里的收货模式。以 `/` 开头是虚拟绝对路径，否则相对 `/workspace` |
| `place_as` | 复制到出口时用的文件名。非 list 且恰好一个匹配时，目标是 `/factory/out/<port>/<place_as>` |
| `optional` | 0 个匹配是否算成功 |
| `list` | true 时每个匹配放到 `/factory/out/<port>/<i>/<place_as>`，`i` 从 0 开始，按匹配路径升序 |

没有 `harvest` 字段的端口不要写进这个文件。`submissions/<port>.json` 已存在的端口，harvest **跳过**（显式提交优先，架构文档 5.1）。

`harvest-result.json`：

```json
{
  "ok": true,
  "ports": {
    "note": {"ok": true, "files": ["note.txt"], "matches": ["drafts/a.txt"]}
  }
}
```

某个端口失败时，该端口是 `{"ok": false, "code": "HARVEST_EMPTY", "files": [], "matches": []}`，顶层 `ok` 为 false。`code` 只可能是 `HARVEST_EMPTY`、`HARVEST_AMBIGUOUS`、`HARVEST_IO`。这三个是结果里的字符串，不是 `FactoryError` 错误码，不要登记进 `CODES`。Runner 看到顶层 `ok: false` 时抛自己的 `HARVEST_FAILED`。

### 4.6 `log.jsonl`、`fail.json`、`brief.md`

- `log.jsonl`：每条一行 `{"at": "...", "message": "..."}`，追加，不改已有行。
- `fail.json`：`{"reason": "<reason>"}`。`fail` 命令覆盖写入后退出码 1。
- `brief.md`：Runner 写的 Station Brief 原文。factoryctl 不解析它。

---

## 5. 行为

### 5.1 虚拟路径

与 `tests/fakes/fake_agent.py` 同一套前缀，这样 LocalProvider 把环境变量换成宿主路径之后，脚本仍然能找到文件：

| 前缀 | 环境变量 |
|---|---|
| `/factory/in` | `FACTORY_IN` |
| `/factory/out` | `FACTORY_OUT` |
| `/factory/ctl` | `FACTORY_CTL` |
| `/workspace` | `FACTORY_WORKSPACE` |

算法与 fake agent 的 `real_path` 相同：路径等于前缀，或以前缀加 `/` 开头，且环境变量非空，则换成 `环境变量 + 剩余部分`。邮箱目录是 `FACTORY_CTL` 的值本身，不要求它在字面上等于 `/factory/ctl`。

`FACTORY_CTL` 未设置或不是目录：任何命令都退出码 4，stderr 说明要设置 `FACTORY_CTL`。

### 5.2 只读命令

- `inputs --json`：stdout 是 `{"job_id", "attempt", "inputs"}`，取自 `job.json`。没有 `--json` 时，每个端口一行：`<port>\t<id>\t<type>`。`list` 端口每个元素一行，id 前面加 `[i]`。
- `contract`：stdout 是 `contract.json` 的原文。
- `budget --json`：`{"budget_usd": <job.json 的值>, "spent_usd": null, "note": "M1 在 job 结束后从 transcript 结算，运行中没有余额"}`。M1 没有模型代理，禁止编造已花费金额。

`job.json` 缺字段：退出码 4。

### 5.3 `submit`、`log`、`fail`

`submit <port> <path>`：

1. `path` 经 5.1 映射后必须是已存在的文件（不是目录）。不存在：退出码 1。
2. 复制到 `/factory/out/<port>/<basename>`（映射后的真实目录）。父目录创建。用 `shutil.copyfile`，不整文件读入内存。
3. 写 `meta.json`（若有 `--meta`）和 `submissions/<port>.json`（4.4）。
4. 退出码 0。stdout 在 `--json` 时是 submissions 文件的内容，否则一行 `submitted <port> <basename>`。

同一端口再提交：覆盖出口文件和 `submissions/<port>.json`。harvest 之后看到这个文件就会跳过该端口。

`log <message>`：追加一行到 `log.jsonl`，退出码 0。message 原样保存，不解释为格式串。

`fail <reason>`：写 `fail.json`，退出码 1。不删除已经 submit 的文件；Runner 看到 `fail.json` 就不再封存。

### 5.4 请求 / 响应与 `CtlServer`

`factoryctl` 发请求：

1. 生成 id，写 `requests/<id>.json.part`，再 `os.replace` 成 `requests/<id>.json`。
2. 每 0.2 秒看一次 `responses/<id>.json`（`time.sleep`，这是沙箱内脚本，不用注入时钟）。
3. 等到文件出现：解析 JSON。`ok` 为 true 则把 `result` 以 JSON 打到 stdout 并退出 0；否则把 `error.message` 打到 stderr 并退出 1。
4. 等待上限：`contract.json` 的 `approval.timeout_s`（check 用同一个数）再加 5 秒。超时：退出码 1，stderr `等待宿主响应超时`。不写响应文件。

`CtlServer.poll_once`：

1. 列出 `ctl_dir/requests/*.json`（不递归，忽略 `.part`），按文件名升序。
2. 对每个文件：若 `responses/<主干>.json` 已存在，跳过。
3. 解析 JSON。不是对象、没有字符串 `id`、没有字符串 `op`、或 `id` 与文件名主干不一致：写一条 `ok: false` 的响应，`error.code` 为 `CTL_REQUEST_INVALID`，计数加一，继续下一个。
4. 调用 `handler.handle(request)`。
   - 返回 `None`：不写文件，不计数，下次再问。
   - 返回 dict：原子写入 `responses/<id>.json`，计数加一。
   - 抛出异常：不吞掉。`poll_once` 把异常冒泡（测试能看见）。后台线程的兜底见下一条。
5. 返回本轮新写的响应数。

`start(interval_s=0.2)`：启动名为 `mao-ctl` 的守护线程。循环是“`poll_once`，然后 `time.sleep(interval_s)`”，**不**调用 `clock.sleep`（README 3.4：CtlServer 的后台线程是例外）。`poll_once` 抛异常时：线程用 `logging.getLogger("mao.ctl")` 记下 `exception`，写一条 `error.code` 为 `CTL_HANDLER_ERROR` 的响应（让沙箱不要永远等），然后继续循环。禁止 `except Exception: pass`。

`stop()`：置事件，`join(5)`（真实 5 秒）。线程还活着：`RuntimeError`。重复 `start`：`RuntimeError`。没 `start` 就 `stop`：直接返回。

`clock` 不参与轮询间隔。Runner 的 handler 自己用 `clock` 判断审批超时。

### 5.5 `check` 与 `approval request`

`check <port> <path>`：写 `op=check` 的请求，等待响应。stdout/退出码按 5.4。本组件不跑检验。响应里的 `result` 由 Runner 的 handler 填，格式是 `{"verdict": "pass"|"reject"|"error", "feedback": "<str>"}`。`verdict != "pass"` 时 handler 仍返回 `ok: true`（请求被处理了），factoryctl 看到 `ok: true` 就退出 0，并把 result 打到 stdout。agent 自己读 verdict。这样“预检没过”和“邮箱坏了”不是同一个退出码。fake agent 的 `ctl` 动作只在退出码非 0 时失败；预检失败不应该让动作失败，除非脚本自己检查 stdout。

`approval request`：写 `op=approval`。`--cost-usd` 必须是非负数字，否则退出码 2。等待期间 job 处于 `AWAITING_APPROVAL` 是 Runner 的事，本组件只阻塞到响应出现。响应 `result.decision` 为 `approve` 时，handler 令 `ok: true`；`deny` 或超时令 `ok: false`，`error.code` 为 `APPROVAL_DENIED` 或 `APPROVAL_TIMEOUT`。factoryctl 因此退出 0 或 1。fake agent 默认 `allow_exit: [0]`，被拒绝会让动作失败并让 agent 退出 1。这是预期行为。

### 5.6 `harvest`

1. 读 `harvest.json`。缺失或不是对象：退出码 2。
2. 对 `outputs` 的每个端口（键名升序）：
   - `submissions/<port>.json` 已存在：跳过，不写入该端口的结果（Runner 用提交记录）。
   - 展开 `harvest`。相对路径相对 `FACTORY_WORKSPACE`（缺省 `/workspace`）。模式里的 `*` 只匹配一个路径段，不跨越 `/`。不用 `**`。实现用 `pathlib.Path.glob`，模式是相对 workspace 的 POSIX 相对路径；绝对虚拟路径先经 5.1 映射。
   - 匹配数 0 且 `optional`：该端口 `ok: true`，`files: []`。
   - 匹配数 0 且非 optional：`ok: false`，`code: HARVEST_EMPTY`。
   - 非 list 且匹配数大于 1：`ok: false`，`code: HARVEST_AMBIGUOUS`。不复制。
   - 非 list 且匹配数等于 1：复制到 `/factory/out/<port>/<place_as>`。
   - list：按匹配路径升序，第 i 个复制到 `/factory/out/<port>/<i>/<place_as>`。
   - 复制用 `shutil.copyfile`。IO 错误：该端口 `HARVEST_IO`，继续其他端口。
3. 原子写入 `harvest-result.json`。只要文件写出来了，退出码就是 0，即使顶层 `ok` 为 false。退出码 1 只留给写结果文件本身失败。

夹具 `studio` 的配方是 `harvest: drafts/*.txt`，类型要求 `note.txt`。Runner 会把 `place_as` 写成 `note.txt`。一个匹配时，`drafts/a.txt` 变成 `/factory/out/note/note.txt`。

---

## 6. 错误

宿主侧 `FactoryError`：

| 错误码 | 触发条件 | detail |
|---|---|---|
| `CTL_REQUEST_INVALID` | 请求文件不是合法对象，或 id 与文件名不一致。`poll_once` 把它写成响应，不抛出；Runner 若在 handler 里发现 args 缺字段，可以抛这个码 | `id`、`reason` |
| `CTL_HANDLER_ERROR` | 后台线程里 handler 抛异常。写进响应的 `error.code`，同时记日志。`poll_once` 本身不吞异常，所以这个码只出现在线程兜底的响应里 | `error` |

沙箱脚本不抛 `FactoryError`。它的失败是退出码：

| 退出码 | 含义 |
|---|---|
| 0 | 成功（harvest 写完结果文件也算，即使某个端口为空） |
| 1 | 业务失败：文件不存在、`fail`、审批拒绝、等待超时、响应 `ok: false` |
| 2 | 用法错误，或 `harvest.json` 缺失 |
| 4 | `FACTORY_CTL` 不可用，或 `job.json` / `contract.json` 缺失 |

`HARVEST_EMPTY`、`HARVEST_AMBIGUOUS`、`HARVEST_IO` 只出现在 `harvest-result.json` 的 `ports.<port>.code`，不是 `CODES` 里的错误码。

---

## 7. 验收测试

文件：`tests/acceptance/m1/test_m1_05_factoryctl.py`。开头 `pytest.importorskip("mao.ctl")`。脚本路径是仓库里的 `src/mao/ctl/factoryctl.py`，用 `sys.executable` 运行。

| 测试 | 验证 |
|---|---|
| `test_poll_once_writes_response_and_skips_done` | 5.4：处理一条请求，返回 1；再调用返回 0 |
| `test_poll_once_rejects_invalid_request` | 5.4 第 3 步：缺 `op`、id 与文件名不一致 → 写 `ok: false`、`CTL_REQUEST_INVALID` 的响应并计数；handler 不被调用 |
| `test_poll_once_defers_when_handler_returns_none` | 返回 None 时不写响应，返回 0；下次返回 dict 才写 |
| `test_factoryctl_harvest_places_single_match` | 5.6：`drafts/a.txt` 复制为 `note.txt`，退出码 0，`harvest-result.json` 的 `ok` 为 true |
| `test_factoryctl_harvest_ambiguous_empty_and_submitted` | 5.6：多个匹配 → `HARVEST_AMBIGUOUS` 且不复制；0 个匹配 → `HARVEST_EMPTY`，optional 时 `ok: true`；list 端口放到 `<i>/`；已有 submissions 的端口不出现在结果里；退出码仍是 0 |
| `test_factoryctl_submit_and_inputs` | 3.2、5.2、5.3：submit 写出 submissions；`inputs --json` 读到 `job.json` 的 id |
| `test_factoryctl_fail_and_log` | `fail` 退出码 1 且写出 `fail.json`；`log` 追加一行 |
| `test_check_roundtrip_with_server` | 5.5：后台 `CtlServer` 回答 check，脚本退出码 0，handler 看到 `op == "check"` |

---

## 8. 错误示范与正确示范

**8.1 控制通道开端口**

```python
# ✖ 沙箱没有网络时这条通道直接死掉，也违反 D7
sock = socket.socket(); sock.bind(("127.0.0.1", 8765))
# ✔ 只写文件，写完再改名
tmp = ctl / "requests" / (rid + ".json.part")
tmp.write_text(text, encoding="utf-8", newline="\n")
os.replace(tmp, ctl / "requests" / (rid + ".json"))
```

**8.2 在 factoryctl.py 里 import mao**

```python
# ✖ 沙箱镜像没有控制面源码，而且 D7 禁止
from mao.base import canonical_json
# ✔ 脚本自包含。宿主侧的 CtlServer 才 import mao
```

**8.3 改掉 fake agent 已经使用的命令名**

```text
✖ factoryctl put note /workspace/drafts/a.txt
✔ factoryctl submit note /workspace/drafts/a.txt
```

fake agent 把 `args` 原样传进来。夹具脚本和架构文档 6.2 用的就是 `submit`、`approval request`、`fail`、`log`。

**8.4 轮询用 clock.sleep，或把异常吞掉**

```python
# ✖ 后台线程若调用 FakeClock.sleep，生产环境会睡在虚拟时间上；测试又依赖真实间隔
self.clock.sleep(interval_s)
# ✖ 禁止
except Exception:
    pass
# ✔ 线程用 time.sleep；异常写成 CTL_HANDLER_ERROR 响应并记日志
```

**8.5 harvest 匹配到 0 个文件就自己退出 1**

```python
# ✖ optional 端口没有文件是正常的；退出 1 会让 Runner 分不清“规则算完了”和“脚本崩了”
if not matches:
    return 1
# ✔ 写 harvest-result.json，optional 则 ok true，必填则 code HARVEST_EMPTY，退出码仍是 0
```

---

## 9. 风险与注意事项

- **原子改名必须在同一文件系统。** 临时文件写在 `requests/` 目录里，不要写到 `/tmp` 再跨盘 replace。Windows 上 `os.replace` 可以覆盖已存在的文件；响应不应当已存在（`poll_once` 会跳过）。
- **并发。** 一个 attempt 一个 `CtlServer`。不要两个线程同时 `poll_once` 同一个目录。`start` 之后测试应当只通过线程间接触发，或者只用 `poll_once` 而不同时 `start`。
- **真实等待。** 后台线程的 0.2 秒是真实时间。审批测试要等这一拍，不能假设 `FakeClock` 会推进它。确定性测试用 `poll_once`。
- **换行。** 安装时已经把 CRLF 换成 LF（`foundation.md` 5.11）。源文件本身也应是 LF，否则容器里 shebang 变成 `python3\r`。
- **M2 追加命令。** `receipt` 和 `--label` 是后续任务加的。M1 的 argparse 不要用 `parse_known_args` 把未知参数悄悄丢掉，否则加错标志也不会被发现。
- **大文件。** submit 和 harvest 用 `shutil.copyfile`，禁止 `read_bytes()` 把视频读进内存（R20）。
