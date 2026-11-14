# 测试分层与夹具（testing.md）

> 规范性文档，M1。任务：**M1-10**。
> 本文件描述**已经存在**的替身、夹具 Registry 和测试镜像。不要改它们的文件名或契约来迁就新测试。
> **M12/M13 增补**：`tests/fakes/fake_agent.py` 的续接、对话历史、`by_turn`、`usage` 增补见 `agents.md` 4.7（M13）。冲突时以新规范为准。

---

## 1. 目的与范围

**做什么**

- 固定 M1 验收测试怎么分层、用哪些替身、夹具 Registry 里有什么、测试镜像怎么建。
- 端到端测试在 Local 与 Docker 下跑同一个工位，结果的 Bean id 与 facts 一致。

**不做什么**

- 不把测试镜像的构建放进 `harness gate`。镜像由人按第 5 节预先构建。
- 不新增第三方测试库。

---

## 2. 依赖

`tests/fakes/`、`tests/fixtures/registry-m1/`、`tests/fixtures/images/`、`tools/build_test_images.py`。这些路径受保护。

---

## 3. 接口

```python
def render_registry(dest: Path, *, images: Mapping[str, str] | None = None) -> Path
def local_commands() -> dict[str, list[str]]
def image_id(name: str) -> str
```

这些函数在 `tests/fakes/fakelib.py`，不是 `mao` 的公共 API。验收测试可以 import 它们。

---

## 4. 数据格式

### 4.1 `fake_agent.py`

命令行模仿 Claude Code 无头模式：`fake-agent -p TASK [--append-system-prompt TEXT] [--output-format stream-json] ...`。其余参数忽略。

脚本查找：`FAKE_AGENT_SCRIPT`，否则 `$FACTORY_IN/task/task.json`。字段：`cost_usd`、`turns`、`default`、`attempts`。动作有 `write`、`copy`、`delete`、`require`、`ctl`、`sleep`、`cost`、`emit`、`garbage`、`echo_task`、`exit`。路径前缀 `/factory/in`、`/factory/out`、`/factory/ctl`、`/workspace` 按对应环境变量替换。stdout 是 stream-json，最后一条是 `type=result`。

M5 起增加的动作与规则（`supervisor.md` 第 7 章）：`run`（执行 argv，可存输出）、`mcp`（经 `factory mcp` 调用工具）；脚本有 `roles` 且 `FACTORY_ROLE` 在其中时按角色取动作；动作参数里的 `${NAME}` 按环境变量替换。说明以 `tests/fakes/fake_agent.py` 的文件头为准。

`fake_llm.py`（M6）：脚本化的模型上游，实现 Anthropic Messages 与 OpenAI Responses 的最小子集，供真实 CLI 在测试里使用（`runtimes.md`、`live.md` 第 7 章）。它记录每个请求的路径、模型、请求头、轮次与工具结果，测试用这些记录断言秘密没有进入沙箱。

### 4.2 夹具工具

| 脚本 | 契约 |
|---|---|
| `upper_text.py` | `upper-text IN OUT`。行 `#fail` 退出码 3；`#slow` 先睡 30 秒；`#keep-lower` 不转大写。只用标准库，兼容 3.8 |
| `count_text.py` | validator。接受时 stdout 是 `chars`、`lines`、`has_lower`。拒绝退出码 1。用法错误退出码 2 |
| `check_task.py` | validator。接受时 stdout 是 `{"steps": N}`。拒绝退出码 1。用法错误退出码 2 |

### 4.3 夹具 Registry

`tests/fixtures/registry-m1/`：类型 `note`、`note.text`、`task`；机器 `toolbox`、`studio`；工位 `upper`、`writer`；runtime `script`、`fake-agent`。宪法副本必须与 `spec/mrl/constitution/invariants.mrl` 逐字节相同。镜像 digest 是占位值，Docker 测试用 `render_registry(..., images=...)` 替换。

---

## 5. 行为

### 5.1 构建镜像

```bash
uv run python tools/build_test_images.py
```

构建 `mao-test/tool`、`mao-test/studio`、`mao-test/operator`。上下文来自 `tests/fixtures/images/<名字>/`，并复制 `tests/fakes/`。复制时把文本文件的 CRLF 换成 LF。本命令不在阶段门里运行。

### 5.2 端到端

`test_m1_10_e2e.py`：

1. Local：`render_registry` 到临时目录，`LocalProvider` 加 `local_commands()`，跑 `upper` 工位。输入是已封存的 note。输出 Bean 已封存，正文是大写。
2. Docker：同一夹具，镜像 digest 换成 `image_id`。标记 `docker`。`MAO_REQUIRE_DOCKER=1` 时不得跳过。
3. 两次得到的 facts 集合相同。Bean id 允许不同（路径字节可能因换行规范化不同），facts 与正文必须相同。

---

## 6. 错误

本文件不登记新的运行时错误码。夹具脚本的退出码就是契约：0 接受，1 拒绝，2 用法或设施，3 是 `upper_text` 的内容失败。

| 错误码 | 触发条件 | detail |
|---|---|---|
| `FIXTURE_IMAGE_MISSING` | 仅测试代码在 `MAO_REQUIRE_DOCKER=1` 且镜像不存在时抛出 | `name` |

这个码可以只出现在测试里。若登记进 `mao.errors.CODES`，就必须在本文件用反引号写出。默认不登记，测试直接失败并打印镜像名。

---

## 7. 验收测试

文件：`tests/acceptance/m1/test_m1_10_e2e.py`。

| 测试 | 验证 |
|---|---|
| `test_local_upper_seals` | Local 下 upper 工位封存大写产物 |
| `test_local_lineage` | 输出血缘指回输入 |
| `test_docker_upper_matches_local` | 标记 `docker`。facts 与 Local 相同 |
| `test_docker_agent_workspace_harvest` | 标记 `docker`。writer 工位：fake agent 从只读注入卷运行，在镜像初始化的工作区卷里写 `drafts/a.txt`，收货并封存 |
| `test_registry_m1_matches_constitution` | 夹具宪法与参考宪法逐字节相同 |

---

## 8. 错误示范与正确示范

**8.1 在测试里 mock 掉 Runner**

```python
# ✖ 验收测的就是真的 Runner
runner.run = lambda job: sealed
# ✔ 用 LocalProvider 和假 CLI，控制面代码是真的
Runner(ctx, snapshot).run(job.id)
```

**8.2 把占位 digest 当成已构建**

```python
# ✖ 占位值在本机并不存在
image = "registry.local/machines/toolbox@sha256:dd22..."
# ✔ Docker 测试用 image_id 替换后再 load
render_registry(dest, images={"toolbox": image_id("tool")})
```

**8.3 修改夹具来让测试变绿**

```text
✖ 改 upper.yaml 去掉验收条件
✔ 修实现。夹具与替身受保护
```

---

## 9. 风险与注意事项

- **R13**：`docker` 标记在阶段门的 `MAO_REQUIRE_DOCKER=1` 下是失败不是跳过。本地日常运行可以跳过。
- **R5**：构建上下文必须把 CRLF 换成 LF，否则 Linux 镜像里的 shebang 无法执行。
- **S4**：digest 以本机 inspect 为准，写进报告，不写死在受保护的 yaml 里。
