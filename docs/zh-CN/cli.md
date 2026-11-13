[English](../en/cli.md) | **简体中文** | [日本語](../ja/cli.md)

# 命令行参考

`uv sync` 会安装两个入口：

| 命令 | 包 | 用途 |
|---|---|---|
| `factory` | `mao.cli:main` | 工厂控制面 |
| `mrl` | `mrl.cli:main` | MRL（MAO Relation Language）工具：求值、检查、解释诊断码 |

在沙箱内，操作工使用第三个工具 `factoryctl`，见 [运行时 § 3](runtime.md#3-工位内部)。

任何命令加 `--help` 都会显示确切的参数。规范性的命令清单见 [`spec/system/commands.md`](../../spec/system/commands.md)。

## 全局参数

| 参数 | 环境变量 | 默认值 | 含义 |
|---|---|---|---|
| `--root DIR` | `FACTORY_ROOT` | `.factory` | 工厂状态目录 |
| `--registry DIR` | `FACTORY_REGISTRY` | `registry` | registry（Spec）目录 |
| `--provider docker\|local` | | `docker` | 沙箱提供者 |
| `--local-commands FILE` | | | 本地提供者的命令名 → 宿主命令映射 |
| `--epoch COMMIT` | | 当前 epoch | 针对指定的 epoch 运行 |
| `--json` | | | 机器可读的输出 |
| `--version` | | | 打印版本 |

## 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 业务失败：job `DEAD`、run `FAILED`/`CANCELLED`、契约测试 `fail`、审计有未结发现 |
| 2 | 用法错误 |
| 3 | Registry 不合法（`REGISTRY_INVALID`） |
| 4 | 环境或设施错误，如 Docker 不可用 |
| 5 | 内部错误 |

## 结构与治理

| 命令 | 说明 |
|---|---|
| `check ROOT` | 加载并编译 registry；有错误时退出码 3 |
| `epoch init COMMIT` · `ls` · `show [COMMIT]` · `rollback [COMMIT]` · `adopt COMMIT` | 管理 epoch 与当前指针 |
| `changeset propose FILE --commit C --by ACTOR` · `ls [--state S]` · `show ID` · `withdraw ID --by ACTOR` · `advance ID` | ChangeSet 流水线 |
| `governance tick` · `governance safe-mode` | 推进一次 ChangeSet；查看或评估安全模式 |
| `contract validator …` · `golden …` · `equivalence …` · `diff …` · `sink-dry-run …` · `ls` | V4 契约测试 |
| `audit once` · `audit ls [--state S]` | 运行不变量审计 / 列出发现 |
| `drift ls` | 漂移发现 |

## 物料

| 命令 | 说明 |
|---|---|
| `bean import --type TYPE --file PATH=HOST …` | 把外部文件作为 Bean 准入，打印其 id |
| `bean lineage BEAN --direction up\|down` | 上游或下游的 job 与 Bean |
| `bean label BEAN KEY=VALUE` | 添加标签（标签可以触发产线） |
| `cache ls [--station S@N]` · `explain JOB` · `invalidate (--key\|--job\|--station\|--bean) … --reason TEXT` | job 缓存 |
| `gc [--dry-run]` | 保留期：墓碑、blob 清扫、工作区回收 |

## 执行

| 命令 | 说明 |
|---|---|
| `job run STATION --input PORT=BEAN …` | 在当前进程提交并运行一个 job（封存退出码 0，DEAD 退出码 1） |
| `job show JOB` · `job exec JOB` | 查看 job；取得已提交 job 的租约并执行它（serve 的工作子进程使用） |
| `line plan LINE@N` | 把产线编译为运行计划（节点、转换、预算） |
| `line run LINE@N --in PORT=BEAN … [--budget USD] [--variant STEP=V] [--no-cache]` | 启动一次 run 并推进到结束 |
| `line ls` · `show RUN` · `nodes RUN` · `cancel RUN` · `retry RUN` · `rebuild RUN` | 查看和控制 run |
| `trigger process` | 评估一次产线触发器 |
| `schedule tick` · `dlq ls` · `quarantine BEAN --reason TEXT` · `release BEAN` | 调度器、死信、隔离 |
| `approval ls [--job J] [--status S]` · `show ID` · `decide ID approve\|deny --by NAME [--note TEXT]` | 审批点 |

## 接入

| 命令 | 说明 |
|---|---|
| `image build …` · `image ls` | 构建和列出机器镜像（厂长编写的 Dockerfile） |
| `gateway manifest MACHINE@V` | B 形态机器的服务网关清单 |
| `egress check STATION@N` | 显示工位实际生效的出网计划 |
| `secret ls` | 密钥的名字与分类（从不输出值） |
| `backend status` · `migrate …` · `verify` | 存储后端：本地 ↔ S3 + Postgres |
| `backup create` · `verify` · `restore` | 状态与 blob 的备份 |

## 自治

| 命令 | 说明 |
|---|---|
| `serve [--once]` | 守护进程：触发、run、job、治理、审计、金丝雀、唤醒票 |
| `foreman next` · `done WAKE` · `get KEY` · `list KIND` · `diff BASE COMMIT` · `bean …` · `job …` · `dlq` · `drift` · `invariant` · `metrics` · `approval` · `changeset` · `review` · `conversion` · `supervise` · `sessions` · `wakes` · `reopen` | 厂长的操作面 |
| `mcp --role foreman\|reviewer` | 同一操作面，以 stdio 上的 MCP 服务提供 |
| `maintain bootstrap` | 检查维护产线和 `registry-sink` 是否齐全；缺失时退出码 1 并列出缺项 |

## 可观测性

| 命令 | 说明 |
|---|---|
| `status` | 整个工厂的一份 JSON 快照 |
| `metrics` | Prometheus 文本格式 |
| `events [--since SEQ] [--kind K] [--limit N]` | 事件日志 |
| `console [--port N]` | 本机只读 Web 控制台（默认 `127.0.0.1:8090`） |

## `mrl`

| 命令 | 说明 |
|---|---|
| `mrl eval EXPR` · `mrl template TEXT` | 求值表达式 / 渲染模板 |
| `mrl check MODULE` · `mrl run MODULE` | 静态检查 / 求值规则模块 |
| `mrl registry …` | registry 检查与查询（`mrl registry check --strict`） |
| `mrl resolve …` | 对一条假想的边做转换解析 |
| `mrl explain CODE` | 解释诊断码，如 `E-NO-CONVERTER` |
| `mrl conformance` · `mrl coverage` | 运行一致性用例 / 查看 factory 用例的覆盖情况 |
