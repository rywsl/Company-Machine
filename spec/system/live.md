# live：真实 CLI 在 Docker 里的端到端（M6-03）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M6-03**。
> M5 用 fake agent 证明了闭环的控制逻辑；M6-01、M6-02 补齐了真实 CLI 需要的生产路径。本任务把它们放在一起：固定版本的 Claude Code 与 Codex，在 Docker 里，经 `serve` 持有的代理与 `mao-egress` 网络，对着脚本化的模型上游跑完工位、厂长会话、复核会话和 13.2 闭环。
> 不需要真实的模型密钥：上游是 `tests/fakes/fake_llm.py`。CLI 是真的，网络路径是真的，只有模型的回答是脚本。

---

## 1. 目的与范围

**做什么（M6-03）**

- 现场镜像：`mao-test/live-bench:dev`（工作台）、`mao-test/claude-code:dev`、`mao-test/codex:dev`（注入卷来源），由 `tools/build_live_images.py` 按 `tools/live_versions.json` 构建（4.1）。
- 固定 CLI 版本：实现方在 `tools/live_versions.json` 写入精确版本与基础镜像 digest，并让 `tools/live_images/` 下的 Dockerfile 适配该版本的包布局。
- 端到端验收（第 7 章）。验收失败时修的是 M5、M6-01、M6-02 的实现，或者（CLI 参数不符时）按 `plan/README.md` 第 5 节阻塞，由设计方改参考 runtime 文档。
- 写 M6 交接记录。

**不做什么**

| 不做 | 归属 |
|---|---|
| 用真实模型密钥跑测试 | 不做。验收不依赖外网与密钥 |
| 新的组件接口 | 不做 |
| 接入真实产品（OpenMontage 等） | 厂长的日常工作，经 ChangeSet 完成，不是控制面的里程碑 |

---

## 2. 依赖

| 依赖 | 用途 |
|---|---|
| `DockerProvider`（`sandbox.md`） | 真实容器 |
| `Daemon`、`ServeConfig`（`daemon.md`，含 `runtimes.md` 3.3 的 `model_port`） | 持有代理、推进治理、派发 job |
| `ForemanSupervisor`（`supervisor.md`） | 厂长与复核会话 |
| 参考 runtime 文档（`runtimes.md` 4.1） | 两个 CLI 的命令行 |
| `ModelProxy`、`EgressProxy`、`ensure_egress_network`、`proxy_bind_host`（`egress.md`、`runtimes.md`） | 网络路径 |
| `tests/fakes/fake_llm.py` | 模型上游 |

---

## 3. 接口

本任务不新增 Python 接口。新增两个由实现方维护的文件（不受保护）：

| 文件 | 内容 |
|---|---|
| `tools/live_versions.json` | `{"base": "<镜像>@sha256:<64 hex>", "claude_code": "x.y.z", "codex": "x.y.z"}` |
| `tools/live_images/<名字>/Dockerfile` | 三个现场镜像的构建上下文 |

`tools/build_live_images.py`（设计方提供，也由实现方维护）读版本文件，构建三个镜像，并给每个镜像打上 `mao.live.<键>=<值>` 标签。

---

## 4. 数据格式

### 4.1 现场镜像

| 镜像 | 要求 |
|---|---|
| `mao-test/live-bench:dev` | glibc 系统；有 `git`、`python3`；`/workspace` 属主 10001；`/workspace/drafts` 存在 |
| `mao-test/claude-code:dev` | `/opt/operator/bin/claude` 可执行，所需的运行时（node）也在 `/opt/operator` 下；在 live-bench 里能运行 |
| `mao-test/codex:dev` | `/opt/operator/bin/codex` 可执行，不依赖机器镜像里的任何运行时 |

注入卷按 `sandbox.md` 5.5 从镜像的 `/opt/operator` 初始化，只读挂进容器。

### 4.2 现场 Registry

`tests/acceptance/m6/live_world.py` 在 `registry-m2` 的副本上：

- 把三个测试镜像（tool、studio、operator）的占位 digest 换成本机镜像 ID（与 M1 的 Docker 测试相同）；
- 复制两份参考 runtime 文档，注入卷换成现场镜像，`models_allowed` 换成 `fake-opus` / `fake-codex`；
- 新增机器 `bench@1`（镜像 live-bench，配方 `write-note`）、策略 `agent-live@1`（`egress: {none: true}`，内存 2048Mi）、工位 `writer-live@1`（`claude-code@1` + `fake-opus`）；
- 写 `factory.json` 的 `governance`、`serve`（`model_port: 0`）、`foreman`（厂长 `claude-code@1`，复核者 `codex@1`，镜像 live-bench，`network: egress`）；
- 写 `egress/upstreams.json`、`egress/prices.json`（含 `"*"`）、`secrets/`（一个模型类秘密），上游指向 fake_llm。

---

## 5. 行为

### 5.1 一次真实的工位 job 经过哪些组件

1. 守护进程 `start`：建 `mao-egress`（`--internal`），按 `proxy_bind_host` 绑定两个代理，端口写进 `serve.json`，`ctx.proxies` 就位。
2. Runner：`build_invocation` 得到带 `${MODEL_PROXY}`、`${FACTORY_JOB_ID}` 的命令行；`build_egress` 因为 runtime 引用了 `${MODEL_PROXY}`，`network = egress`、`allow` 为空；`register_job_cap`；`expand_control_vars`。
3. 容器：`--network mao-egress`，`--add-host=model-proxy:host-gateway`。CLI 用 `ANTHROPIC_BASE_URL=http://model-proxy:<port>`、`ANTHROPIC_CUSTOM_HEADERS=X-Factory-Job: <job>` 发请求。
4. 模型代理：认出 job，剥掉客户端的 `x-api-key`，加上真实密钥，转发给 fake_llm，计量。
5. CLI 按脚本调用 `Write` 写 `drafts/`；Runner 收货、检验、封存；花费取模型代理的计量。

### 5.2 厂长会话

与 `supervisor.md` 5.1 相同，只是 CLI 是真的：Claude Code 读 `--mcp-config /factory/ctl/mcp.json`，启动 `python3 /opt/factory/bin/factoryctl mcp`，经邮箱与宿主的 `McpServer` 对话。工具名由 CLI 决定（例如 `mcp__factory__changeset_propose`），fake_llm 按后缀匹配。

### 5.3 复核会话

Codex 读 `$CODEX_HOME/config.toml` 里的 `[mcp_servers.factory]`，用 Responses API 经模型代理访问 fake_llm，调用 `review_submit`。

---

## 6. 错误

没有新的错误码。

---

## 7. 验收测试

需要 Docker、M1 的测试镜像（`tools/build_test_images.py`）和现场镜像（`tools/build_live_images.py`）。镜像缺失时测试失败并提示构建命令，不跳过。验收命令设置 `MAO_REQUIRE_DOCKER=1`。fake_llm 在宿主上监听，模型代理从宿主拨出。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m6/test_m6_03_live.py` | `test_live_images_pinned` | `tools/live_versions.json` 是精确版本与带 digest 的基础镜像；三个镜像存在，标签与版本文件一致 |
| | `test_claude_station_job` | `writer-live@1` 在 Docker 里由真实 Claude Code 完成：job `SEALED`，正文是脚本写的两行；上游收到的 `x-api-key` 是密钥值；模型代理为该 job 记了用量，job 的花费等于计量值 |
| | `test_sandbox_isolation` | 同一工位里 CLI 执行 `env` 与直连外网：环境里有 `ANTHROPIC_API_KEY=factory-proxy` 与 `X-Factory-Job: <job id>`，没有密钥值；直连失败 |
| | `test_budget_stop_real_cli` | 上游报告超大用量：第一次请求之后预算越线，后续请求得到 402；job `FAILED`、`BUDGET_EXCEEDED`，不封存 |
| | `test_claude_foreman_session` | 真实 Claude Code 作为厂长：在克隆里提交，经 MCP 提议；ChangeSet 的提出者与提交作者都是 `claude-code@1`；会话 `ok` |
| | `test_codex_reviewer_session` | 真实 Codex 作为复核者：经 MCP `review_submit`，复核计入 `approvals`，runtime 是 `codex@1` |
| | `test_autonomy_loop_in_docker` | `autonomy.md` 的闭环在 Docker 里用两个真实 CLI 走完：厂长补转换、Codex 复核、金丝雀 run 在 tool 镜像里执行、推广、需求 `resolved` |

```
uv run python tools/harness.py pytest tests/acceptance/m6/test_m6_03_live.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 7`。

---

## 8. 错误示范与正确示范

```python
# ✖ CLI 在固定版本里不认某个参数，就在 Runner 里删掉它
argv = [a for a in argv if a != "--strict-mcp-config"]
# ✔ 写日志、阻塞，由设计方改参考 runtime 文档
```

```python
# ✖ 测试里给容器 --network host，"先跑通再说"
# ✔ 只走 mao-egress 与 host-gateway；跑不通就是 runtimes.md 5.5 的实现有问题
```

```python
# ✖ 为了让 fake_llm 匹配上，在 McpServer 里给工具改名
# ✔ 工具名由 CLI 决定，fake_llm 按后缀匹配
```

---

## 9. 风险与注意事项

- **CLI 内部请求**：真实 CLI 会发脚本之外的请求（标题生成、计数、次要模型）。fake_llm 对非主模型回固定文本，按 `assistant` 条数选轮次；价格表要有 `"*"`（`runtimes.md` 5.3），否则次要模型的请求会触发 `BUDGET_PRICE_MISSING`。
- **构建现场镜像需要出网**：由人执行一次 `tools/build_live_images.py`。阶段门不负责构建镜像。
- **时间**：真实 CLI 启动慢。阶段门给这组测试 3600 秒。
- **glibc**：Claude Code 的注入卷带 node，只能在 glibc 系统的机器镜像里运行。Alpine 机器上的 agent 工位要换用静态链接的 CLI，或者在机器镜像里提供运行时。这是部署选择，记在厂长的接入说明里。
- **Read-before-Write**：Claude Code 拒绝覆盖没读过的文件。测试脚本用 `Bash` 写文件，不依赖这一行为。
