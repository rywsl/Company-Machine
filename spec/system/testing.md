# 测试分层与夹具（testing.md）

> 规范性文档，M1。任务：**M1-10**。
> 本文件描述**已经存在**的替身、夹具 Registry 和测试镜像。不要改它们的文件名或契约来迁就新测试。
> **M12/M13 增补**：`tests/fakes/fake_agent.ts` 的续接、对话历史、`by_turn`、`usage` 增补见 `agents.md` 4.7（M13）。冲突时以新规范为准。

---

## 1. 目的与范围

**做什么**

- 固定 M1 验收测试怎么分层、用哪些替身、夹具 Registry 里有什么、测试镜像怎么建。
- 端到端测试在 Bun 注入替身和 Podman/Kiri 容器下跑同一个业务单元，结果的记录摘要与 lineage 一致。

**不做什么**

- 不把测试镜像的构建放进 `harness gate`。镜像由人按第 5 节预先构建。
- 不新增第三方测试库。

---

## 2. 依赖

`tests/fakes/`、`tests/fixtures/registry-m1/`、`tests/fixtures/images/`、`tools/build_test_images.ts`。这些路径受保护。

---

## 3. 接口

```ts
function renderRegistry(destination: string, options?: { images?: Record<string, string> }): Promise<string>
function localCommands(): Record<string, string[]>
function imageIds(executable?: string): Promise<Record<string, string>>
```

这些函数在 `tests/fakes/fakelib.ts`，不是业务控制面的公共 API。Bun 验收测试可以 import 它们。

---

## 4. 数据格式

### 4.1 `fake_agent.ts`

命令行模仿 Claude Code 无头模式：`fake-agent -p TASK [--append-system-prompt TEXT] [--output-format stream-json] ...`。其余参数忽略。

脚本查找：`FAKE_AGENT_SCRIPT`，否则 `$FACTORY_IN/task/task.json`。字段：`cost_usd`、`turns`、`default`、`attempts`。动作有 `write`、`copy`、`delete`、`require`、`ctl`、`sleep`、`cost`、`emit`、`garbage`、`echo_task`、`exit`。路径前缀 `/factory/in`、`/factory/out`、`/factory/ctl`、`/workspace` 按对应环境变量替换。stdout 是 stream-json，最后一条是 `type=result`。

M5 起增加的动作与规则（`supervisor.md` 第 7 章）：`run`（执行 argv，可存输出）、`mcp`（经 Bun `factory mcp` 调用工具）；脚本有 `roles` 且 `FACTORY_ROLE` 在其中时按角色取动作。说明以 `tests/fakes/fake_agent.ts` 的文件头为准。

`fake_llm.ts`（M6）：脚本化的模型上游，实现 Anthropic Messages 与 OpenAI Responses 的最小子集，供真实 CLI 在测试里使用（`runtimes.md`、`live.md` 第 7 章）。它记录每个请求的路径、模型、请求头、轮次与工具结果，测试用这些记录断言秘密没有进入沙箱。

### 4.2 夹具工具

| 脚本 | 契约 |
|---|---|
| `upper_text.ts` | `upper-text IN OUT`。行 `#fail` 退出码 3；`#slow` 先睡 30 秒；`#keep-lower` 不转大写。由 Bun 执行 |
| `count_text.ts` | validator。接受时 stdout 是 `chars`、`lines`、`has_lower`。拒绝退出码 1。用法错误退出码 2 |
| `check_task.ts` | validator。接受时 stdout 是 `{"steps": N}`。拒绝退出码 1。用法错误退出码 2 |

### 4.3 夹具 Registry

`tests/fixtures/registry-m1/`：类型 `note`、`note.text`、`task`；机器 `toolbox`、`studio`；工位 `upper`、`writer`；runtime `script`、`fake-agent`。宪法副本必须与 `spec/mrl/constitution/invariants.mrl` 逐字节相同。镜像 digest 是占位值，Docker 测试用 `render_registry(..., images=...)` 替换。

---

## 5. 行为

### 5.1 构建镜像

```bash
bun run build:test-images
```

构建 `mao-test/tool`、`mao-test/studio`、`mao-test/operator`。上下文来自 `tests/fixtures/images/<名字>/`，并复制 `tests/fakes/`。复制时把文本文件的 CRLF 换成 LF。Linux Kiri 的构建和镜像检查使用与控制面相同的 Unix 套接字。构建通过 Kiri 提供的经典 Docker build API 提交。

`--save-to DIR` 为选中的每个夹具导出一个 Docker 格式的镜像归档。`--load-from DIR` 将归档装入所选引擎后检查夹具标签和摘要，供同架构的构建主机和无桌面 Kiri 主机之间传输。`--build-timeout SECONDS` 可覆盖经典 Kiri 构建的 300 秒默认时限。

在实际容器引擎上运行工位验收：

```bash
MAO_CONTAINER_RUNTIME=kiri bun run build:test-images
MAO_CONTAINER_RUNTIME=kiri bun run tools/verify-runtime.ts
```

设为 `podman` 可以复用同一套验收。夹具在 UID 10001 下执行，核对只读输入和已安装的 `factoryctl`，并经文件邮箱完成预检和自动审批。宿主收货后核对封存内容、Gate facts 和来料血缘。失败保留临时工厂目录和日志以供复查。

### 5.2 端到端

活动 Bun 测试在 `backend/tests/legacy-runner.test.ts` 中用 `LocalProvider` 执行 `upper` 工位，检查已封存的输入、脚本执行和输出封存。业务 Harness 在容器启动前的就绪检查由 `backend/tests/business-runtime.test.ts` 覆盖。验证实际容器时，`MAO_CONTAINER_RUNTIME` 选择引擎，`imageIds` 提供已构建夹具的镜像 ID。用同一工位比较正文和 facts，Bean ID 可随规范化后的文件字节变化。

---

## 6. 错误

本文件不登记新的运行时错误码。夹具脚本的退出码就是契约：0 接受，1 拒绝，2 用法或设施，3 是 `upper_text` 的内容失败。

| 错误码 | 触发条件 | detail |
|---|---|---|
| `FIXTURE_IMAGE_MISSING` | 仅测试代码在 `MAO_REQUIRE_DOCKER=1` 且镜像不存在时抛出 | `name` |

这个码可以只出现在测试里。若登记进 `mao.errors.CODES`，就必须在本文件用反引号写出。默认不登记，测试直接失败并打印镜像名。

---

## 7. 验收测试

### 7.1 活动 Bun 验收

`bun run verify:bun` 校验系统规范章节、MRL 诊断码与标准库说明、工厂内核声明以及本表与活动测试目录之间的双向映射。Bun 的 JUnit 报告须逐文件记录实际执行的用例，任何失败、跳过、待办或遗漏文件都会使验收失败。该命令还运行后端与 Web 的 TypeScript 检查，以及 `spec/mrl/conformance/` 中的 719 项一致性用例。通过的用例须覆盖全部内核诊断码，并实际触发全部 85 条内核诊断规则。

| 文件 | 验证 |
|---|---|
| `backend/tests/api.test.ts` | 业务系统和项目接收、密钥保险箱、SSE 回放 |
| `backend/tests/backup.test.ts` | SQLite、CAS 与 Registry Git bundle 备份，检查原 ID、仓库与上传文件字节、Git 引用、凭据边界和损坏情况 |
| `backend/tests/business-records.test.ts` | 物料查询、业务 DAG 迁移与长链环路校验 |
| `backend/tests/business-runtime.test.ts` | 业务 DAG、同业务物料说明与文件引用传递、启动前的运行时就绪 |
| `backend/tests/cli.test.ts` | Bun 工厂命令、Registry 检查、从冻结快照恢复审批和调度运行 |
| `backend/tests/changesets.test.ts` | ChangeSet 受理、Git 候选、MRL 风险、合同验证和影子运行 |
| `backend/tests/foreman.test.ts` | 厂长队列和官方 Harness 回复事件 |
| `backend/tests/server-recovery.test.ts` | 重开 SQLite 后恢复可派发的队列，保留外部结果未知的执行状态，检查运行时就绪等待期间停止服务 |
| `backend/tests/harness.test.ts` | 官方 CLI、ACP 会话和事件流 |
| `backend/tests/harness-verification.test.ts` | Harness 配置通过官方 CLI 和 ACP 验证，检查结果投影、运行时就绪、超时和配置更新 |
| `backend/tests/legacy-approvals.test.ts` | 工位审批的费用规则、尝试隔离、到期与拒绝 |
| `backend/tests/legacy-contract.test.ts` | validator 正反样例、GoldenSet、Baseline、转换等价、差分及 Sink 方案试运行恢复 |
| `backend/tests/legacy-cache.test.ts` | 缓存键、候选可用性、失效范围与审计 |
| `backend/tests/legacy-core.test.ts` | 规范 JSON、SQLite 仓库、并发 CAS 提交、快照和值投影 |
| `backend/tests/legacy-snapshot.test.ts` | 目录树 epoch、类型索引、编译表达式查找、冻结快照重启读取、旧记录补齐和跨 epoch 执行保护 |
| `backend/tests/registry-epochs.test.ts` | Git 提交冻结、解析表保留、active 指针与引用、回滚、保留判定及签名边界 |
| `backend/tests/legacy-gate.test.ts` | 来料检查和产物验收 |
| `backend/tests/legacy-harvest.test.ts` | 工位产物采集、路径边界、匹配顺序和异常结果 |
| `backend/tests/legacy-connectors.test.ts` | Source 摄入与检验、Sink 幂等发布与核验、回执血缘和沙箱命令 |
| `backend/tests/legacy-egress.test.ts` | Registry 出网策略编译、授权等级、域名与方法匹配、CONNECT 和非法配置 |
| `backend/tests/legacy-egress-forwarder.test.ts` | 本地代理认证、域名和方法拒绝、地址固定、本地 HTTP 和 CONNECT 转发、Unix 套接字权限与清理 |
| `backend/tests/legacy-mailbox.test.ts` | 异步文件邮箱、并发请求、审批响应与异常路径 |
| `backend/tests/legacy-operator.test.ts` | 工位任务渲染、调用参数和官方 CLI 转录 |
| `backend/tests/legacy-runplan.test.ts` | 产线展开、绑定、执行顺序与计划持久化 |
| `backend/tests/dynamic-switch.test.ts` | switch 计划、条件顺序、默认分支、重启恢复和失败原子性 |
| `backend/tests/dynamic-until.test.ts` | until 有界执行、结果聚合、重启恢复和预算预留 |
| `backend/tests/dynamic-subline.test.ts` | Subline 冻结计划、父子运行关联、产物和费用结算 |
| `backend/tests/dynamic-map.test.ts` | Map 列表展开、保序聚合、溢出与单轮提交数量限制 |
| `backend/tests/dynamic-map-literal.test.ts` | 字面 Map 编译、实例转换、预算预留、持久化执行、分支跳过与存量计划恢复 |
| `backend/tests/legacy-runner.test.ts` | 工位执行、单元素列表物化和封存 |
| `backend/tests/legacy-two-phase.test.ts` | 方案审批、Job 持久化恢复及 Sink 回执时序 |
| `backend/tests/legacy-runs.test.ts` | 运行恢复、事务回滚、预算和节点重试 |
| `backend/tests/legacy-sandbox.test.ts` | 本地夹具执行、超时和文件邮箱 |
| `backend/tests/legacy-scheduler.test.ts` | 任务准入、资源与隔舱配额、熔断探针、自动隔离、死信和审批到期 |
| `backend/tests/legacy-triggers.test.ts` | 事件去重、游标恢复、并发准入和定时触发 |
| `backend/tests/runtime.test.ts` | 运行时状态缓存、就绪去重与失败诊断 |
| `backend/runtime/kiri.test.ts` | Linux 套接字、服务就绪与受限容器参数 |
| `backend/runtime/podman.test.ts` | 沙箱参数、受管出网配置、rootless 探测和命令日志 |
| `backend/mrl/checker.test.ts` | MRL 表达式静态类型和模板 |
| `backend/mrl/cli.test.ts` | MRL 命令行输出和用法 |
| `backend/mrl/factory.test.ts` | Registry YAML、布局和 schema |
| `backend/mrl/mrl.test.ts` | MRL 求值、宏和关系查询 |
| `backend/mrl/rules.test.ts` | MRL 规则解析、连接、守卫和否定 |
| `tests/fakes/fakes.test.ts` | Bun validator、官方转录形状和模型上游夹具 |

### 7.2 历史验收与迁移覆盖

`bun run tools/harness.ts parity` 执行活动 Bun 验收和规范中的 MRL 一致性用例，并核对归档的 Python 验收。每个已验证等价的历史用例要记录完整的文件与函数名，以及 Bun 验收文件和用例名称。部分覆盖的用例继续待迁移。退出码 0 需要历史规范与用例映射一致、所有归档验收有通过的等价证据、MRL 一致性与内核覆盖通过、代码策略迁移完成。

| 历史验收 | Bun 文件 | Bun 用例 | 验证 |
|---|---|---|---|
| `removed/python/tests/acceptance/m1/test_m1_01_foundation.py::test_canonical_json_sorting_and_separators` | `backend/tests/legacy-core.test.ts` | `serializes sorted compact UTF-8 JSON and hashes the known empty payload` | 字段排序、紧凑分隔符、序列与非 ASCII 内容 |
| `removed/python/tests/acceptance/m1/test_m1_01_foundation.py::test_canonical_bytes_is_utf8` | `backend/tests/legacy-core.test.ts` | `serializes sorted compact UTF-8 JSON and hashes the known empty payload` | 规范 JSON 的 UTF-8 字节 |
| `removed/python/tests/acceptance/m1/test_m1_01_foundation.py::test_digest_bytes_known_vector` | `backend/tests/legacy-core.test.ts` | `serializes sorted compact UTF-8 JSON and hashes the known empty payload` | 空字节串的 SHA-256 已知向量 |
| `removed/python/tests/acceptance/m1/test_m1_01_foundation.py::test_digest_file_matches_bytes_and_streams` | `backend/tests/legacy-core.test.ts` | `streams file digests and keeps one empty blob at its exact CAS path` | 大于 1 MiB 的文件摘要匹配字节摘要 |
| `removed/python/tests/acceptance/m1/test_m1_01_foundation.py::test_utc_iso_formats_and_truncates` | `backend/tests/legacy-core.test.ts` | `checks duration parsing, UTC formatting, fake time and independent counters` | 毫秒精度及 UTC 偏移转换 |
| `removed/python/tests/acceptance/m1/test_m1_01_foundation.py::test_parse_duration_table` | `backend/tests/legacy-core.test.ts` | `checks duration parsing, UTC formatting, fake time and independent counters` | 毫秒、分钟、天和前导零 |
| `removed/python/tests/acceptance/m1/test_m1_01_foundation.py::test_parse_duration_invalid` | `backend/tests/legacy-core.test.ts` | `checks duration parsing, UTC formatting, fake time and independent counters` | 八种非法时长的 `DURATION_INVALID` |
| `removed/python/tests/acceptance/m1/test_m1_01_foundation.py::test_fake_clock` | `backend/tests/legacy-core.test.ts` | `checks duration parsing, UTC formatting, fake time and independent counters` | 起点、读取、推进、睡眠和负数拒绝 |
| `removed/python/tests/acceptance/m1/test_m1_01_foundation.py::test_counter_ids` | `backend/tests/legacy-core.test.ts` | `checks duration parsing, UTC formatting, fake time and independent counters` | 按前缀独立编号 |
| `removed/python/tests/acceptance/m1/test_m1_01_foundation.py::test_id_prefix_validation` | `backend/tests/legacy-core.test.ts` | `checks duration parsing, UTC formatting, fake time and independent counters` | 拒绝大写前缀 |
| `removed/python/tests/acceptance/m1/test_m1_01_foundation.py::test_database_pragmas_and_rows` | `backend/tests/legacy-core.test.ts` | `uses SQLite WAL, commits rows and rolls back outer and nested transactions` | WAL 模式和提交后的行查询 |
| `removed/python/tests/acceptance/m1/test_m1_01_foundation.py::test_tx_commit_and_rollback` | `backend/tests/legacy-core.test.ts` | `uses SQLite WAL, commits rows and rolls back outer and nested transactions` | 外层异常撤销插入 |
| `removed/python/tests/acceptance/m1/test_m1_02_snapshot.py::test_epoch_is_tree_hash` | `backend/tests/legacy-snapshot.test.ts` | `matches an independent tree hash across directories and includes non-Spec files` | 格式和独立算出的 SHA-256 树摘要一致 |
| `removed/python/tests/acceptance/m1/test_m1_02_snapshot.py::test_epoch_ignores_absolute_path_and_changes_with_content` | `backend/tests/legacy-snapshot.test.ts` | `matches an independent tree hash across directories and includes non-Spec files` | 复制到其他目录得到相同 epoch，添加非规范文件后摘要改变 |
| `removed/python/tests/acceptance/m1/test_m1_02_snapshot.py::test_notes_registry_is_clean` | `backend/tests/legacy-snapshot.test.ts` | `indexes the kernel type closure and resolves current non-retired minors` | 校验真实 Registry 可加载且无错误和警告 |
| `removed/python/tests/acceptance/m1/test_m1_02_snapshot.py::test_key_doc_spec_and_deep_copy` | `backend/tests/legacy-snapshot.test.ts` | `indexes the kernel type closure and resolves current non-retired minors` | 校验文档键、查找、深拷贝与未知键错误 |
| `removed/python/tests/acceptance/m1/test_m1_02_snapshot.py::test_type_key_latest_and_retired` | `backend/tests/legacy-snapshot.test.ts` | `indexes the kernel type closure and resolves current non-retired minors` | 主版本解析到最新有效次版本，退役次版本回退 |
| `removed/python/tests/acceptance/m1/test_m1_02_snapshot.py::test_is_subtype_matches_kernel` | `backend/tests/legacy-snapshot.test.ts` | `indexes the kernel type closure and resolves current non-retired minors` | 读取内核关系的自反、父类型和未知类型断言 |
| `removed/python/tests/acceptance/m1/test_m1_02_snapshot.py::test_load_rejects_invalid_registry` | `backend/tests/legacy-snapshot.test.ts` | `recovers an older unfrozen epoch only from matching bytes and rejects invalid capture` | Registry 格式错误时拒绝捕获且保留历史快照 |
| `removed/python/tests/acceptance/m1/test_m1_02_snapshot.py::test_compiled_covers_sites_and_envs` | `backend/tests/legacy-snapshot.test.ts` | `looks up typed predicates and templates by subject before and after reopening` | 全部表达式位置的源文本和按主体查找，属性、标量与列表验收、审批、配方和运行时模板 |
| `removed/python/tests/acceptance/m1/test_m1_02_snapshot.py::test_compiled_unknown_subject` | `backend/tests/legacy-snapshot.test.ts` | `looks up typed predicates and templates by subject before and after reopening` | 未知表达式主体返回 `SUBJECT_UNKNOWN` 与原主体 |
| `removed/python/tests/acceptance/m1/test_m1_02_snapshot.py::test_facts_validators_properties_always` | `backend/tests/legacy-snapshot.test.ts` | `indexes the kernel type closure and resolves current non-retired minors` | 校验继承事实、validator 参数与副本、必然属性及继承谓词的真假结果 |
| `removed/python/tests/acceptance/m1/test_m1_01_foundation.py::test_nested_tx_uses_savepoints` | `backend/tests/legacy-core.test.ts` | `uses SQLite WAL, commits rows and rolls back outer and nested transactions` | 内层回滚保留外层事务 |
| `removed/python/tests/acceptance/m1/test_m1_03_warehouse.py::test_put_file_cas_layout_and_idempotent` | `backend/tests/legacy-core.test.ts` | `streams file digests and keeps one empty blob at its exact CAS path` | 空 Blob 的摘要、仓库层级与重复写入 |
| `removed/python/tests/acceptance/m1/test_m1_03_warehouse.py::test_bean_id_and_add_get` | `backend/tests/legacy-core.test.ts` | `preserves Bean identity and rejects invalid paths, types, digests and IDs` | Bean 身份、元数据副本、媒介类型及首次创建时间 |
| `removed/python/tests/acceptance/m1/test_m1_03_warehouse.py::test_payload_path_and_type_rejected` | `backend/tests/legacy-core.test.ts` | `preserves Bean identity and rejects invalid paths, types, digests and IDs` | 拒绝非法路径、类型、摘要和未知 ID |
| `removed/python/tests/acceptance/m1/test_m1_03_warehouse.py::test_seal_many_transaction_and_conflict` | `backend/tests/legacy-core.test.ts` | `rolls back a failed Bean seal and keeps facts stable across resealing` | 批次封存回滚、事实冲突及等价事实复封 |
| `removed/python/tests/acceptance/m1/test_m1_03_warehouse.py::test_lineage_up_and_down` | `backend/tests/legacy-core.test.ts` | `stores, seals, materializes and traces Bean lineage` | 双向血缘及未知 Bean 导致写入回滚 |
| `removed/python/tests/acceptance/m1/test_m1_03_warehouse.py::test_materialize_layouts` | `backend/tests/legacy-core.test.ts` | `materializes both warehouse layouts with identical canonical Bean metadata` | 两种落盘布局的字节和规范 Bean JSON |
| `removed/python/tests/acceptance/m1/test_m1_03_warehouse.py::test_list_and_verify` | `backend/tests/legacy-core.test.ts` | `filters sealed and unsealed Beans and verifies modified blobs and identities` | 列表过滤及 Blob 和 Bean 身份损坏检测 |
| `removed/python/tests/acceptance/m1/test_m1_04_sandbox.py::test_build_run_argv_locks_security_and_order` | `backend/runtime/podman.test.ts` | `emits the ordered Podman run contract and protects fixed environment entries` | Podman 的完整启动参数顺序、安全标志和固定环境变量 |
| `removed/python/tests/acceptance/m1/test_m1_04_sandbox.py::test_build_run_argv_rejects_egress` | `backend/runtime/podman.test.ts` | `rejects egress until the managed proxy is enabled` | 默认拒绝无代理出网并返回对应错误细节 |
| `removed/python/tests/acceptance/m1/test_m1_04_sandbox.py::test_build_run_argv_egress_switch_is_not_host` | `backend/runtime/podman.test.ts` | `builds a deterministic hardened argv` | 显式本地出网仍使用独立网络及沙箱限制 |
| `removed/python/tests/acceptance/m1/test_m1_04_sandbox.py::test_build_run_argv_rejects_docker_sock` | `backend/runtime/podman.test.ts` | `rejects mounted engine sockets, invalid names and mutable image tags` | 拒绝引擎套接字挂载和非法沙箱名称 |
| `removed/python/tests/acceptance/m1/test_m1_04_sandbox.py::test_image_ref_invalid` | `backend/runtime/podman.test.ts` | `rejects mounted engine sockets, invalid names and mutable image tags` | 镜像只接受合法固定摘要，校验先于引擎调用 |
| `removed/python/tests/acceptance/m1/test_m1_04_sandbox.py::test_local_provider_rewrites_virtual_paths` | `backend/tests/legacy-sandbox.test.ts` | `maps argv, cwd and control environment and rejects execution after close` | 实际 Bun 子进程确认路径、工作目录、隔离环境及关闭语义 |
| `removed/python/tests/acceptance/m1/test_m1_05_factoryctl.py::test_poll_once_writes_response_and_skips_done` | `backend/tests/legacy-mailbox.test.ts` | `dispatches a complete check once and skips both answered and partial requests` | 写入完整回执并跳过已响应请求和暂存文件 |
| `removed/python/tests/acceptance/m1/test_m1_05_factoryctl.py::test_poll_once_rejects_invalid_request` | `backend/tests/legacy-mailbox.test.ts` | `rejects malformed request files and request paths that are directories` | 缺操作和 ID 不匹配的请求在转交前拒绝 |
| `removed/python/tests/acceptance/m1/test_m1_05_factoryctl.py::test_poll_once_defers_when_handler_returns_none` | `backend/tests/legacy-mailbox.test.ts` | `defers an unanswered check, publishes its exact reply once and ignores partial files` | 未决请求无回执，后续轮询返回回复 |
| `removed/python/tests/acceptance/m1/test_m1_05_factoryctl.py::test_factoryctl_harvest_places_single_match` | `backend/tests/legacy-harvest.test.ts` | `harvests virtual absolute paths and preserves an output already in place` | CLI 的单匹配收货、输出文件和完整报告 |
| `removed/python/tests/acceptance/m1/test_m1_05_factoryctl.py::test_factoryctl_harvest_ambiguous_empty_and_submitted` | `backend/tests/legacy-harvest.test.ts` | `reports missing and ambiguous outputs, sorts list matches and honors submissions` | CLI 处理歧义、空匹配、可选项、列表和已有提交 |
| `removed/python/tests/acceptance/m1/test_m1_05_factoryctl.py::test_factoryctl_fail_and_log` | `backend/tests/legacy-mailbox.test.ts` | `runs the check, log, fail and contract commands at the mailbox boundary` | 两条日志、失败报告和缺少邮箱或契约时的退出码 |
| `removed/python/tests/acceptance/m1/test_m1_05_factoryctl.py::test_factoryctl_submit_and_inputs` | `backend/tests/legacy-sandbox.test.ts` | `submits file metadata and exposes inputs through the sandbox CLI` | 真实 CLI 提交、元数据、来料、预算及无效参数退出码 |
| `removed/python/tests/acceptance/m1/test_m1_05_factoryctl.py::test_check_roundtrip_with_server` | `backend/tests/legacy-mailbox.test.ts` | `runs the check, log, fail and contract commands at the mailbox boundary` | CLI 发送指定参数并输出宿主的检验结果 |
| `removed/python/tests/acceptance/m1/test_m1_06_gate.py::test_check_inputs_rejects_unsealed` | `backend/tests/legacy-gate.test.ts` | `checks missing, unsealed, wrong type, properties and ordered input cardinality without opening a sandbox` | 拒绝未封存与未知物料，来料检查不创建沙箱 |
| `removed/python/tests/acceptance/m1/test_m1_06_gate.py::test_check_inputs_type_requires_cardinality` | `backend/tests/legacy-gate.test.ts` | `checks missing, unsealed, wrong type, properties and ordered input cardinality without opening a sandbox` | 区分类型、属性和端口基数三类错误 |
| `removed/python/tests/acceptance/m1/test_m1_06_gate.py::test_check_inputs_ok_and_list` | `backend/tests/legacy-gate.test.ts` | `checks missing, unsealed, wrong type, properties and ordered input cardinality without opening a sandbox` | 标量与列表接收、封存状态、输入顺序和沙箱开销 |
| `removed/python/tests/acceptance/m1/test_m1_06_gate.py::test_admit_external` | `backend/tests/legacy-gate.test.ts` | `admits an external card without validators and explains missing required metadata` | 外部物料入库但尚未封存，缺少元数据时拒绝 |
| `removed/python/tests/acceptance/m1/test_m1_06_gate.py::test_report_json_and_digest` | `backend/tests/legacy-gate.test.ts` | `checks output structure, measured facts, guarantees, acceptance and metadata` | 报告字段、检查阶段和确定的规范摘要 |
| `removed/python/tests/acceptance/m1/test_m1_07_operator.py::test_parse_transcript_result_and_unknown` | `backend/tests/legacy-operator.test.ts` | `counts unknown or malformed Claude events and uses the last typed result` | 统计陌生合法事件并提取费用、轮数和结果 |
| `removed/python/tests/acceptance/m1/test_m1_07_operator.py::test_parse_transcript_malformed_and_last_result_wins` | `backend/tests/legacy-operator.test.ts` | `counts unknown or malformed Claude events and uses the last typed result` | 异常行计数、最后结果及非法字段类型 |
| `removed/python/tests/acceptance/m1/test_m1_07_operator.py::test_parse_transcript_errors` | `backend/tests/legacy-operator.test.ts` | `counts unknown or malformed Claude events and uses the last typed result` | 拒绝非法格式和缺失文件，区分空转录和未完成转录 |
| `removed/python/tests/acceptance/m1/test_m1_07_operator.py::test_render_brief_contract_and_feedback` | `backend/tests/legacy-operator.test.ts` | `renders a brief and script invocation from the Registry snapshot` | 脚本工位的完整契约、反馈和尝试次数校验 |
| `removed/python/tests/acceptance/m1/test_m1_07_operator.py::test_render_brief_contract_and_feedback` | `backend/tests/legacy-operator.test.ts` | `renders the configured agent's brief, model, turn limit and budget from Registry data` | Agent 工位的任务、输入路径和验收条件 |
| `removed/python/tests/acceptance/m1/test_m1_07_operator.py::test_build_invocation_script_and_agent` | `backend/tests/legacy-operator.test.ts` | `renders a brief and script invocation from the Registry snapshot` | 脚本工位参数、环境和超时 |
| `removed/python/tests/acceptance/m1/test_m1_07_operator.py::test_build_invocation_script_and_agent` | `backend/tests/legacy-operator.test.ts` | `renders the configured agent's brief, model, turn limit and budget from Registry data` | Agent 工位参数、环境、转录格式和超时 |
| `removed/python/tests/acceptance/m1/test_m1_08_runner.py::test_terminal_states` | `backend/tests/legacy-runner.test.ts` | `treats only SEALED and DEAD jobs as terminal` | 仅 `SEALED` 和 `DEAD` 为终态 |
| `removed/python/tests/acceptance/m1/test_m1_08_runner.py::test_submit_rejects_unsealed` | `backend/tests/legacy-runner.test.ts` | `rejects unsealed input before creating a job` | 未封存入参在创建任务前报 `INTAKE_REJECTED` |
| `removed/python/tests/acceptance/m1/test_m1_08_runner.py::test_gate_reject_records_feedback` | `backend/tests/legacy-runner.test.ts` | `records gate rejection feedback and reaches DEAD without sealing any outputs` | Gate 退回时保留反馈和 `REJECTED` 事件，并结束任务 |
| `removed/python/tests/acceptance/m1/test_m1_08_runner.py::test_operator_fail_retries_then_dead` | `backend/tests/legacy-runner.test.ts` | `retries operation failures to the configured limit and retains both attempt logs` | 执行失败按上限重试并保留每次尝试的日志 |
| `removed/python/tests/acceptance/m1/test_m1_08_runner.py::test_budget_exceeded_does_not_seal_or_retry` | `backend/tests/legacy-runner.test.ts` | `harvests official-shaped Harness outputs and stops over-budget jobs before sealing` | 超预算任务停止封存和重试 |
| `removed/python/tests/acceptance/m1/test_m1_08_runner.py::test_writer_harvests_and_seals` | `backend/tests/legacy-runner.test.ts` | `harvests official-shaped Harness outputs and stops over-budget jobs before sealing` | Runner 采集官方 Harness 形状的产物并封存正常任务 |
| `removed/python/tests/acceptance/m1/test_m1_09_cli.py::test_bean_lineage_json` | `backend/tests/cli.test.ts` | `imports material, seals a scripted job and returns its lineage through Bun CLI` | CLI 封存后向上追溯，结果只包含原输入 Bean |
| `removed/python/tests/acceptance/m2/test_m2_01_runplan.py::test_templated_params_recorded` | `backend/tests/legacy-runplan.test.ts` | `orders the example by dependency layer and records parameters and budgets` | 归档示例产线的 `to_s3.prefix` 保留模板原文和模板字段标记 |
| `removed/python/tests/acceptance/m2/test_m2_03_line_runner.py::test_epoch_mismatch` | `backend/tests/legacy-snapshot.test.ts` | `rejects a Run tick from a LineRunner bound to a different epoch` | 不同 Registry 快照下拒绝推进同一个 Run |
| `removed/python/tests/acceptance/m4/test_m4_01_dynamic.py::test_m2_compiler_still_rejects` | `backend/tests/legacy-runplan.test.ts` | `rejects missing inputs, incompatible contracts, cycles and unsupported constructs` | 静态入口对 Map 返回 `LINE_UNSUPPORTED` |
| `removed/python/tests/acceptance/m4/test_m4_01_dynamic.py::test_plain_line_digest_matches_m2` | `backend/tests/dynamic-switch.test.ts` | `compiles ordered switch cases and preserves the static plan document` | 示例静态 Line 的两个编译入口生成相同计划 |
| `removed/python/tests/acceptance/m4/test_m4_01_dynamic.py::test_literal_map_order` | `backend/tests/dynamic-map-literal.test.ts` | `compiles literal Map families and persists ordered, single-submission execution` | 使用类型正确的脚本工位和隐式转换替代缺少字幕输入的历史配音夹具，验证计划实例、模板及有序产物 |
| `removed/python/tests/acceptance/m4/test_m4_01_dynamic.py::test_dynamic_overflow_reject` | `backend/tests/dynamic-map-literal.test.ts` | `rejects a literal Map overflow before creating instances` | 计划只保留控制节点，运行时保存 `DYNAMIC_OVERFLOW` |
| `removed/python/tests/acceptance/m4/test_m4_01_dynamic.py::test_truncate_keeps_prefix` | `backend/tests/dynamic-map-literal.test.ts` | `compiles and executes only the original literal Map prefix when truncating` | 使用相同合规脚本夹具验证仅编译和运行原列表前两个元素 |
| `removed/python/tests/acceptance/m4/test_m4_01_dynamic.py::test_switch_first_match` | `backend/tests/dynamic-switch.test.ts` | `commits the first true choice, skip and submission in one tick across restart` | 两个真条件按声明顺序选择第一个，并持久化控制节点 |
| `removed/python/tests/acceptance/m9/test_m9_01_dynamic_runtime.py::test_switch_selects_one_branch` | `backend/tests/dynamic-switch.test.ts` | `runs one selected branch and skips the alternative with no job` | 两种物料分别选择对应分支，控制节点无 Job，另一个分支跳过 |
| `removed/python/tests/acceptance/m9/test_m9_01_dynamic_runtime.py::test_until_stops_when_condition_holds` | `backend/tests/dynamic-until.test.ts` | `stops on the first true until result after restoring a persisted iteration` | 各轮为独立 Job，第二轮为真时只输出第二轮结果且无第三轮 |
| `removed/python/tests/acceptance/m9/test_m9_01_dynamic_runtime.py::test_until_exhausted_fails_run` | `backend/tests/dynamic-until.test.ts` | `fails when until is exhausted without passing the final iteration to dependants` | 两轮完成后聚合失败并保存 `UNTIL_EXHAUSTED`，Run 无产物 |
| `removed/python/tests/acceptance/m9/test_m9_01_dynamic_runtime.py::test_subline_runs_child_in_parent_epoch` | `backend/tests/dynamic-subline.test.ts` | `runs one frozen child in the parent epoch and propagates the same output after restart` | 唯一子 Run 继承父 epoch，父节点无 Job 且共享同一封存产物 |
| `removed/python/tests/acceptance/m9/test_m9_01_dynamic_runtime.py::test_map_expands_runtime_list` | `backend/tests/dynamic-map.test.ts` | `expands every runtime list item and aggregates outputs in source order` | 运行期产生三个顺序实例，聚合节点和产线输出共享同一有序物料列表 |
| `removed/python/tests/acceptance/m9/test_m9_01_dynamic_runtime.py::test_map_overflow_rejects` | `backend/tests/dynamic-map.test.ts` | `rejects runtime Map overflow before creating any instances` | 来源完成后拒绝超限列表且不创建实例 |
| `removed/python/tests/acceptance/m9/test_m9_01_dynamic_runtime.py::test_map_truncate_keeps_upstream_order` | `backend/tests/dynamic-map.test.ts` | `truncates Map overflow to the upstream prefix` | 截取原列表前两个元素，保留物料顺序 |
| `removed/python/tests/acceptance/m9/test_m9_01_dynamic_runtime.py::test_map_max_parallel_limits_submissions` | `backend/tests/dynamic-map.test.ts` | `limits new Map station jobs per tick and keeps other instances waiting` | 首轮仅提交索引零，其余等待并最终完成 |
| `removed/python/tests/acceptance/m3/test_m3_01_epochs.py::test_init_sets_active_pointer_and_ref` | `backend/tests/registry-epochs.test.ts` | `initializes the active Git epoch in SQLite and refs/factory exactly once` | active 指针、Git 引用、SQLite 行和物化 epoch 一致 |
| `removed/python/tests/acceptance/m3/test_m3_01_epochs.py::test_init_twice_rejected` | `backend/tests/registry-epochs.test.ts` | `initializes the active Git epoch in SQLite and refs/factory exactly once` | 重复初始化返回 `EPOCH_STATE_INVALID` |
| `removed/python/tests/acceptance/m3/test_m3_01_epochs.py::test_build_writes_frozen_resolutions_once` | `backend/tests/registry-epochs.test.ts` | `freezes a committed Registry and its nonempty resolutions once` | 校验提交归档和预计算解析，重复构建保留写入过的解析文件 |
| `removed/python/tests/acceptance/m3/test_m3_01_epochs.py::test_build_rejects_invalid_registry` | `backend/tests/registry-epochs.test.ts` | `rejects a malformed committed Registry without publishing an epoch or stage` | 格式错误不产生构建行或目录，active 保持原提交 |
| `removed/python/tests/acceptance/m3/test_m3_01_epochs.py::test_rollback_moves_pointers_only` | `backend/tests/registry-epochs.test.ts` | `rolls back to a built commit by changing epoch pointers and Git refs` | 回滚同时核对 SQLite 指针、Git 引用、主分支和旧提交 |
| `removed/python/tests/acceptance/m3/test_m3_01_epochs.py::test_rollback_target_must_be_built` | `backend/tests/registry-epochs.test.ts` | `rolls back to a built commit by changing epoch pointers and Git refs` | 无 previous、未构建目标和当前 active 返回各自错误 |
| `removed/python/tests/acceptance/m3/test_m3_01_epochs.py::test_adopt_requires_constitution_only_signed_commit` | `backend/tests/registry-epochs.test.ts` | `rejects mixed and unsigned constitution adoptions` | 拒绝含非宪法路径或未经签名的提交 |
| `removed/python/tests/acceptance/m3/test_m3_01_epochs.py::test_retain_rules` | `backend/tests/registry-epochs.test.ts` | `retains activation pointers and unfinished Run, Job and ChangeSet dependencies` | 指针和未完成依赖决定 retain，未保留的目录仍存在 |
| `removed/python/tests/acceptance/m3/test_m3_01_epochs.py::test_registry_not_configured` | `backend/tests/registry-epochs.test.ts` | `reports an unconfigured Registry before creating any epochs` | 未配置时打开 Git epoch 返回 `REGISTRY_NOT_CONFIGURED` |
| `removed/python/tests/acceptance/m3/test_m3_02_changesets.py::test_propose_creates_control_plane_commit` | `backend/tests/changesets.test.ts` | `proposes a distinct control-plane commit and persists draft history` | 核对草案、候选提交、树、父提交、专用引用和主分支 |
| `removed/python/tests/acceptance/m3/test_m3_02_changesets.py::test_constitution_change_forbidden` | `backend/tests/changesets.test.ts` | `rejects constitution edits and a stale base before publishing a draft` | 拒绝宪法路径，数据库和候选引用均无草案 |
| `removed/python/tests/acceptance/m3/test_m3_02_changesets.py::test_stale_base_rejected` | `backend/tests/changesets.test.ts` | `rejects constitution edits and a stale base before publishing a draft` | 基线与 active 不一致时拒绝写入草案 |
| `removed/python/tests/acceptance/m3/test_m3_02_changesets.py::test_freeze_and_rate_limit` | `backend/tests/changesets.test.ts` | `rejects frozen, rate-limited, empty and malformed proposals` | 固定时间下封版和 24 小时推广次数限制分别拒绝 |
| `removed/python/tests/acceptance/m3/test_m3_02_changesets.py::test_unscored_is_not_low` | `backend/tests/changesets.test.ts` | `classifies MRL risk without defaulting unscored facts to a low tier` | 未知字段和结构无法评级，Machine 镜像字段风险为 T2 |
| `removed/python/tests/acceptance/m3/test_m3_02_changesets.py::test_limits_change_is_t1` | `backend/tests/changesets.test.ts` | `verifies a T1 Station with contract results and rejects a shadow regression` | 工位限额变更经差分合同验证进入 T1，报告记录变更工位 |
| `removed/python/tests/acceptance/m3/test_m3_02_changesets.py::test_risk_ignores_proposer_claim` | `backend/tests/changesets.test.ts` | `rejects undeclared, immutable and backward-incompatible Registry changes` | 声称 T0 的机器镜像变更按规则归为 T2 并要求复核 |
| `removed/python/tests/acceptance/m3/test_m3_02_changesets.py::test_declared_changes_must_match` | `backend/tests/changesets.test.ts` | `rejects undeclared, immutable and backward-incompatible Registry changes` | 报告未声明的 Machine 变更 |
| `removed/python/tests/acceptance/m3/test_m3_02_changesets.py::test_in_place_edit_is_immutable` | `backend/tests/changesets.test.ts` | `rejects undeclared, immutable and backward-incompatible Registry changes` | 原地修改返回 `V3_IMMUTABLE` 及原键 |
| `removed/python/tests/acceptance/m3/test_m3_02_changesets.py::test_minor_must_be_compatible` | `backend/tests/changesets.test.ts` | `rejects undeclared, immutable and backward-incompatible Registry changes` | Type minor 改变必需 payload 被拒绝 |
| `removed/python/tests/acceptance/m3/test_m3_02_changesets.py::test_v4_failure_rejects` | `backend/tests/changesets.test.ts` | `verifies a T1 Station with contract results and rejects a shadow regression` | 合同失败后拒绝并保存失败结果 ID |
| `removed/python/tests/acceptance/m3/test_m3_03_rollout.py::test_shadow_failure_rejects` | `backend/tests/changesets.test.ts` | `verifies a T1 Station with contract results and rejects a shadow regression` | 影子回归拒绝、清理候选指针，active 指向基线 |
| `removed/python/tests/acceptance/m2/test_m2_05_cache.py::test_not_cacheable_roles` | `backend/tests/legacy-cache.test.ts` | `uses result-affecting inputs without depending on epoch or descriptive metadata` | Sink 的缓存键请求返回 `CACHE_NOT_CACHEABLE` |
| `removed/python/tests/acceptance/m2/test_m2_08_contract.py::test_validator_accept_and_reject` | `backend/tests/legacy-contract.test.ts` | `runs validator accept/reject samples and audits removed negative fixtures` | 正样例被接受，反样例被拒绝，报告为通过 |
| `removed/python/tests/acceptance/m2/test_m2_08_contract.py::test_reject_fixture_that_passes_fails` | `backend/tests/legacy-contract.test.ts` | `fails when declared negative fixture bytes are accepted` | 保持反样例路径，写入正样例字节后返回 `CONTRACT_FIXTURE_ACCEPTED` |
| `removed/python/tests/acceptance/m2/test_m2_08_contract.py::test_fixture_set_removed` | `backend/tests/legacy-contract.test.ts` | `runs validator accept/reject samples and audits removed negative fixtures` | 报告删除的反样例路径，只新增正样例路径时通过 |
| `removed/python/tests/acceptance/m2/test_m2_08_contract.py::test_golden_within_baseline` | `backend/tests/legacy-contract.test.ts` | `executes GoldenSet jobs with caller timeouts and applies only declared Baseline thresholds` | 费用在 Baseline 最大值内，门槛报告通过 |
| `removed/python/tests/acceptance/m2/test_m2_08_contract.py::test_golden_exceeds_written_max` | `backend/tests/legacy-contract.test.ts` | `executes GoldenSet jobs with caller timeouts and applies only declared Baseline thresholds` | 费用越过显式最大值，返回 `CONTRACT_BASELINE_EXCEEDED` |
| `removed/python/tests/acceptance/m2/test_m2_08_contract.py::test_baseline_record_keys_are_not_gates` | `backend/tests/legacy-contract.test.ts` | `executes GoldenSet jobs with caller timeouts and applies only declared Baseline thresholds` | `p50` 与 `mean` 只记录，不生成门槛 |
| `removed/python/tests/acceptance/m2/test_m2_08_contract.py::test_unknown_metric_fails` | `backend/tests/legacy-contract.test.ts` | `executes GoldenSet jobs with caller timeouts and applies only declared Baseline thresholds` | 校验 Baseline 指标时发现 `judge_score`，返回 `CONTRACT_METRIC_MISSING` |
| `removed/python/tests/acceptance/m2/test_m2_08_contract.py::test_golden_budget_required` | `backend/tests/legacy-contract.test.ts` | `executes GoldenSet jobs with caller timeouts and applies only declared Baseline thresholds` | GoldenSet 缺少预算时抛出 `CONTRACT_BUDGET_REQUIRED` |
| `removed/python/tests/acceptance/m2/test_m2_08_contract.py::test_equivalence_holds` | `backend/tests/legacy-contract.test.ts` | `checks converter equivalence and compares shared facts against the prior version` | 封存正样例并检验等价表达式，恒假表达式返回 `CONTRACT_EQUIVALENCE_FAILED` |
| `removed/python/tests/acceptance/m2/test_m2_08_contract.py::test_converter_reject_not_sealed` | `backend/tests/legacy-contract.test.ts` | `checks converter equivalence and compares shared facts against the prior version` | 转换器反样例没有已封存输出 |
| `removed/python/tests/acceptance/m2/test_m2_08_contract.py::test_diff_regression` | `backend/tests/legacy-contract.test.ts` | `checks converter equivalence and compares shared facts against the prior version` | 旧版本封存而新版本未封存，返回 `CONTRACT_DIFF_REGRESSION` |
| `removed/python/tests/acceptance/m2/test_m2_08_contract.py::test_diff_facts_exact` | `backend/tests/legacy-contract.test.ts` | `checks converter equivalence and compares shared facts against the prior version` | 两版本都封存时精确比较共有 fact，返回 `CONTRACT_DIFF_FACTS` 与差异字段，差分不使用 epsilon |
| `removed/python/tests/acceptance/m2/test_m2_08_contract.py::test_diff_skipped_without_previous` | `backend/tests/legacy-contract.test.ts` | `checks converter equivalence and compares shared facts against the prior version` | 不存在上一版本时通过并返回 `no_previous` |
| `removed/python/tests/acceptance/m2/test_m2_08_contract.py::test_sink_dry_run_no_receipt` | `backend/tests/legacy-contract.test.ts` | `runs an automatically approved Sink proposal without publishing or creating a receipt` | 批准后仍停在 propose，未写入回执和发布目标 |
| `removed/python/tests/acceptance/m2/test_m2_08_contract.py::test_sink_without_propose_refuses` | `backend/tests/legacy-contract.test.ts` | `runs an automatically approved Sink proposal without publishing or creating a receipt` | 拒绝单阶段 Sink 并确认未创建 Job 或回执 |
| `removed/python/tests/acceptance/m2/test_m2_08_contract.py::test_timeout_required` | `backend/tests/legacy-contract.test.ts` | `checks converter equivalence and compares shared facts against the prior version` | 工位与调用方均无超时，抛出 `CONTRACT_TIMEOUT_REQUIRED` |
| `removed/python/tests/acceptance/m2/test_m2_12_scheduler.py::test_executor_methods_match_lines` | `backend/tests/legacy-scheduler.test.ts` | `drives a Line to completion through ScheduledExecutor dispatch and wait` | LineRunner 通过队列派发并等待，原 Run 成功终止 |
| `removed/python/tests/acceptance/m2/test_m2_12_scheduler.py::test_dispatch_does_not_run_inline` | `backend/tests/legacy-scheduler.test.ts` | `persists the queue, reserves resources and dispatches jobs in ID order` | 派发返回时 Job 仍待执行 |
| `removed/python/tests/acceptance/m2/test_m2_12_scheduler.py::test_config_missing` | `backend/tests/legacy-scheduler.test.ts` | `detects missing configuration and leaves jobs queued for absent capacity` | 缺少配置文件时返回 `SCHEDULER_CONFIG_MISSING` |
| `removed/python/tests/acceptance/m2/test_m2_12_scheduler.py::test_capacity_unset_does_not_default` | `backend/tests/legacy-scheduler.test.ts` | `detects missing configuration and leaves jobs queued for absent capacity` | 缺少 CPU 容量时保留队列并记录错误，显式 `null` 后执行 |
| `removed/python/tests/acceptance/m2/test_m2_12_scheduler.py::test_pool_limits_parallel_admission` | `backend/tests/legacy-scheduler.test.ts` | `persists the queue, reserves resources and dispatches jobs in ID order` | 按资源容量和 ID 依次准入，第一项执行期间不释放认领 |
| `removed/python/tests/acceptance/m2/test_m2_12_scheduler.py::test_compartment_does_not_borrow` | `backend/tests/legacy-scheduler.test.ts` | `enforces Line compartment quotas and picks higher priority under global pressure` | 用当前的产线隔舱配置断言 upper 与 idle 的需求及池归属，并验证配额不借用 |
| `removed/python/tests/acceptance/m2/test_m2_12_scheduler.py::test_wip_backpressure` | `backend/tests/legacy-scheduler.test.ts` | `enforces a zero WIP limit by station name until it is removed` | 裸工位名的零在制品上限阻止执行，删除上限后恢复 |
| `removed/python/tests/acceptance/m2/test_m2_12_scheduler.py::test_breaker_unarmed` | `backend/tests/legacy-scheduler.test.ts` | `leaves breakers and automatic quarantine idle when these controls are unconfigured` | 空熔断列表下连续失败也不建立熔断状态或阻止准入 |
| `removed/python/tests/acceptance/m2/test_m2_12_scheduler.py::test_breaker_uses_configured_rate` | `backend/tests/legacy-scheduler.test.ts` | `opens only above the configured failure rate and persists capped half-open probes` | 等于阈值时准入，超出配置阈值时打开熔断并阻止排队 Job |
| `removed/python/tests/acceptance/m2/test_m2_12_scheduler.py::test_half_open_probe_limit` | `backend/tests/legacy-scheduler.test.ts` | `opens only above the configured failure rate and persists capped half-open probes` | 配置时钟到期后进入半开，并在探针运行期间验证并发准入上限 |
| `removed/python/tests/acceptance/m2/test_m2_12_scheduler.py::test_run_cap` | `backend/tests/legacy-scheduler.test.ts` | `applies a common run cap across Bean and cron triggers` | 零上限不建 Run，Bean 和 cron 共享非零上限 |
| `removed/python/tests/acceptance/m2/test_m2_12_scheduler.py::test_cron_every` | `backend/tests/legacy-scheduler.test.ts` | `fires a one-minute cron at its configured interval and diagnoses input-bound lines` | 首次立即触发，时钟满一分钟后创建新 Run |
| `removed/python/tests/acceptance/m2/test_m2_12_scheduler.py::test_cron_requires_empty_interface` | `backend/tests/legacy-scheduler.test.ts` | `fires a one-minute cron at its configured interval and diagnoses input-bound lines` | 有接口输入的 cron 产线不建 Run，并记录 `CRON_INPUT_REQUIRED` |
| `removed/python/tests/acceptance/m2/test_m2_12_scheduler.py::test_dlq_not_redispatched` | `backend/tests/legacy-scheduler.test.ts` | `reports dead jobs without writing history and never redispatches them` | 读取新旧死信不改记录，再次派发既不执行也不增加尝试次数 |
| `removed/python/tests/acceptance/m2/test_m2_12_scheduler.py::test_quarantine_visible_to_cache` | `backend/tests/legacy-scheduler.test.ts` | `shares scheduler quarantine with cache lookup and restores hits after release` | 调度隔离同时改变缓存命中结果，释放隔离后恢复 |
| `removed/python/tests/acceptance/m2/test_m2_12_scheduler.py::test_toxic_unarmed` | `backend/tests/legacy-scheduler.test.ts` | `leaves breakers and automatic quarantine idle when these controls are unconfigured` | 空阈值在连续失败后保持隔离关闭 |
| `removed/python/tests/acceptance/m2/test_m2_12_scheduler.py::test_memory_unit` | `backend/tests/legacy-scheduler.test.ts` | `parses Gi memory demand in MiB and rejects nonzero unitless sizes` | `8Gi` 解析为 8192 MiB，拒绝非零无单位和错误单位 |
| `removed/python/tests/acceptance/m2/test_m2_04_triggers.py::test_bean_trigger_starts_run_once` | `backend/tests/legacy-triggers.test.ts` | `deduplicates sealed events, resumes the cursor and detects late labels` | 同一已封存 Bean 只触发一次 |
| `removed/python/tests/acceptance/m2/test_m2_04_triggers.py::test_label_filter` | `backend/tests/legacy-triggers.test.ts` | `deduplicates sealed events, resumes the cursor and detects late labels` | 标签由不匹配变为匹配后触发 |
| `removed/python/tests/acceptance/m2/test_m2_04_triggers.py::test_trigger_invalid_interface` | `backend/tests/legacy-triggers.test.ts` | `records TRIGGER_INVALID for a matching rule with multiple interface inputs` | 双输入产线记录 `TRIGGER_INVALID` 且不创建 Run |
| `removed/python/tests/acceptance/m2/test_m2_04_triggers.py::test_cursor_survives_restart` | `backend/tests/legacy-triggers.test.ts` | `preserves recorded trigger cursors across database reopening` | 重开 SQLite 后复用事件游标与已有触发记录 |
| `removed/python/tests/acceptance/m2/test_m2_10_sink.py::test_publish_and_verify` | `backend/tests/legacy-connectors.test.ts` | `Sink verifies once, suppresses duplicate jobs and can be reaudited or voided` | 真实发布、回执核验与 Job 封存 |
| `removed/python/tests/acceptance/m2/test_m2_10_sink.py::test_duplicate_suppressed` | `backend/tests/legacy-connectors.test.ts` | `Sink verifies once, suppresses duplicate jobs and can be reaudited or voided` | 已核验回执阻止再次创建发布沙箱 |
| `removed/python/tests/acceptance/m2/test_m2_10_sink.py::test_void_allows_republish` | `backend/tests/legacy-connectors.test.ts` | `Sink receipt claims reject an unresolved older version and release after voiding` | 作废占键回执后恢复认领 |
| `removed/python/tests/acceptance/m2/test_m2_10_sink.py::test_outcome_unknown_no_retry` | `backend/tests/legacy-connectors.test.ts` | `Sink preserves the claim and prevents automatic retries after an uncertain result` | 外部结果未知时停止重试并保留回执 |
| `removed/python/tests/acceptance/m2/test_m2_10_sink.py::test_conflict_on_claimed` | `backend/tests/legacy-connectors.test.ts` | `Sink preserves the claim and prevents automatic retries after an uncertain result` | 占键回执阻止同键重跑并报告 `IDEMPOTENCY_CONFLICT` |
| `removed/python/tests/acceptance/m2/test_m2_10_sink.py::test_receipt_template` | `backend/tests/legacy-connectors.test.ts` | `Sink renders recipe fallback from inputs and parameters and rejects an invalid receipt file` | 渲染模板并保存目标和地址 |
| `removed/python/tests/acceptance/m2/test_m2_10_sink.py::test_receipt_invalid` | `backend/tests/legacy-connectors.test.ts` | `Sink renders recipe fallback from inputs and parameters and rejects an invalid receipt file` | 拒绝缺少目标的回执和无效地址 |
| `removed/python/tests/acceptance/m2/test_m2_10_sink.py::test_verify_failed` | `backend/tests/legacy-connectors.test.ts` | `Sink verifies once, suppresses duplicate jobs and can be reaudited or voided` | 删除发布文件后重新核验并记录失败 |
| `removed/python/tests/acceptance/m2/test_m2_10_sink.py::test_lineage_down_shows_receipt` | `backend/tests/legacy-connectors.test.ts` | `Sink verifies once, suppresses duplicate jobs and can be reaudited or voided` | Bean 下游血缘的 CLI 输出包含回执 |
| `removed/python/tests/acceptance/m2/test_m2_11_source.py::test_source_ingests_and_seals` | `backend/tests/legacy-connectors.test.ts` | `Source seals local input through Gate and records provenance for repeated content` | 从目录摄入，封存并记录外部来源事件 |
| `removed/python/tests/acceptance/m2/test_m2_11_source.py::test_source_rejects_inputs` | `backend/tests/legacy-connectors.test.ts` | `Source seals local input through Gate and records provenance for repeated content` | 提交额外输入返回 `INTAKE_REJECTED` |
| `removed/python/tests/acceptance/m2/test_m2_11_source.py::test_untrusted_input_gated` | `backend/tests/legacy-connectors.test.ts` | `Source seals local input through Gate and records provenance for repeated content` | 外部空文件检验失败且无新 Bean 封存 |
| `removed/python/tests/acceptance/m2/test_m2_11_source.py::test_same_content_same_bean` | `backend/tests/legacy-connectors.test.ts` | `Source seals local input through Gate and records provenance for repeated content` | 内容重复时复用同一个 Bean ID |
| `removed/python/tests/acceptance/m2/test_m2_11_source.py::test_service_form_unsupported` | `backend/tests/legacy-connectors.test.ts` | `rejects service Source execution and incomplete Source Run context before launching` | 服务型 Source 执行前返回 `FORM_UNSUPPORTED` |
| `removed/python/tests/acceptance/m2/test_m2_11_source.py::test_policy_violation_defense` | `backend/tests/legacy-connectors.test.ts` | `rejects service Source execution and incomplete Source Run context before launching` | Run 上下文缺字段时拒绝创建 Source Job |
| `removed/python/tests/acceptance/m7/test_m7_04_backup.py::test_restore_refuses` | `backend/tests/backup.test.ts` | `reports corrupt and missing blobs, extra files, and unsafe paths before restore` | 拒绝损坏备份和非空恢复目录，原 CAS 保留 |

`parity --json` 输出逐条差异。历史 Python 验收文件只作为需求和证据索引读取。普通验证检查活动源码的依赖边界、Shell 与表达式调用约束，以及 Python 代码归档。完整代码策略中尚未迁移的检查会在 `parity` 中列出。通过的 Registry 用例会按规则文件和行号记录实际生成诊断的规则及触发用例。未触发的诊断规则仍列入覆盖缺口。

Bun 使用工作区固定版本的 TypeScript 编译器解析生产源码。校验检查字符串和模板片段中的测试基础设施路径，以及示例和测试 Registry 实际声明的工位、产线、机器、GoldenSet 和 Baseline 名称。扫描范围是活动后端、运行时和 MRL 的非测试 TypeScript 文件。`verify:bun` 运行 `tools/registry-policy.test.ts`，校验 Registry 声明发现、定位和正反例。

Bun MRL 表达式检查标准库参数、宏短路、集合顺序和嵌套深度。规则检查覆盖模块归属、关系签名、变量绑定、宿主事实类型、分层否定及聚合。Registry 事实交给规范中的 `kernel/refs.mrl`、`kernel/types.mrl`、`kernel/stations.mrl` 和 `kernel/lines.mrl`。内核检查业务引用、类型继承、工位契约和产线绑定，并生成转换解析表。测试可按完整 Line 键或省略 `Line:` 的键匹配解析条目。Registry 的宪法在同一规则程序上执行，编译失败时仍会返回内核诊断。完整 int64 精度和整数值与同值浮点数的身份尚需迁移。超过 JavaScript 安全整数范围的 YAML 用例会报告证据缺口。规范用例以 `spec/mrl/conformance/` 为准。

下表记录 `removed/python/tests/acceptance/m1/test_m1_10_e2e.py` 中的历史验收。

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

```ts
// ✖ 用替身结果绕过业务运行时
runner.run = () => sealed;
// ✔ 使用 Bun fixture 和真实 ContainerProvider 边界
await runBusinessUnit({ provider, fixture: "upper" });
```

**8.2 把占位 digest 当成已构建**

```ts
// ✖ 占位值在本机并不存在
const image = "registry.local/machines/toolbox@sha256:dd22...";
// ✔ 测试用 imageIds 替换后再运行
await renderRegistry(dest, { images: await imageIds() });
```

**8.3 修改夹具来让测试变绿**

```text
✖ 改 upper.yaml 去掉验收条件
✔ 修实现。夹具与替身受保护
```

---

## 9. 风险与注意事项

- **R13**：容器验收使用 Podman 或 Kiri 的实际运行时，依赖缺失进入失败状态，不把 Local fixture 结果冒充容器证据。
- **R5**：构建上下文必须把 CRLF 换成 LF，否则 Linux 镜像里的 shebang 无法执行。
- **S4**：digest 以本机 inspect 为准，写进报告，不写死在受保护的 yaml 里。
