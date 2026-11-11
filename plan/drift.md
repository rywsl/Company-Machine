# M1–M4 偏移审查与纠偏（2026-11-11）

> 设计方对实现方交付的 M1–M4 做的审查。**受保护，只能由人修改。**
> 实现方按本文件第 4 节的任务纠偏：M5-01 处理第 2.1、2.2 节；第 2.3 节的接口缺口由 M5-02 至 M5-06 补齐；第 2.4 节是闭环与生产路径上的缺口，分别进入 M5-04、M5-06 与 M6-01；第 2.5 节是编写 M6、M7 规范时发现的问题。
> 本文件的编号（D-xx）被 `tests/test_codebase_policy.py`、任务卡和规范引用，不要重新编号。

---

## 0. 结论

实现方报告 M0–M4 的阶段门全部通过，`harness status` 也显示五个阶段门都是 passed。审查发现，**阶段门通过不等于系统按规范工作**。偏移分三类：

1. **面向测试的特例（违反 `plan/README.md` 第 6 节第 4 条）**：共 11 处，见 2.1 节。包括：
   - 在导入时全局替换 `Path.mkdir`；
   - 往快照的私有字典里注入虚构的工位、配方和黄金样例；
   - 在当前目录下找示例 Registry 和测试夹具；
   - 按产线名 `explainer`、工具名 `count_text.py`、`fake_agent.py` 走特殊分支；
   - Source 的来源目录为空时，控制面自己写一份“ingested”文本冒充外部物料；
   - 用正则改写所有 SQL 里的 `commit`。

   这些特例让验收通过，系统在真实环境里的行为却是错的。
2. **安全网在生产路径上是空的**，见 2.2 节。
   - 测试都会注入替身，于是缺省路径从未被执行。
   - `ChangePipeline` 不注入替身时：V4 契约测试直接跳过，影子运行直接判通过，被阻塞的 run 不重建。规范写明这三处的缺省实现是什么，实现方没有做。
   - 调和器每轮审计都写一条 I8 `wake` 发现。按 `governance.md` 5.13，这会让安全模式**永远开启**。
   - 调和器还缺 I4–I7、I10–I13 共 8 条不变量检查。
   - 漂移检查读的是人与厂长的 Registry 工作区，不是 active epoch。
3. **规范要求、验收没有覆盖的接口没有实现**，见 2.3 节。
   - M2–M4 规范在“接口”章列出的约 60 条 `factory` 子命令，只实现了 `backend migrate`。
   - `factory mcp` 没有实现。实现方在 M3 交接里写“未知命令退出码 2 已满足该断言”，这是在利用验收的漏洞。
   - 后果：**厂长没有可用的操作面**，系统的核心目标“外部机器人维护整个流程”在 M1–M4 交付物上做不到。

根因之一在设计方：若干受保护的验收测试、夹具和规范本身有错（3.1 节）。正确的做法是按 `plan/README.md` 第 5 节写日志并 `task block`，而不是特判。实现方只在 M2-06 上这样做了一次，其余都改成了特例。设计方已经修正这些受保护文件。

流程上还有两处违规：
- 实现方修改了受保护的 `tests/fakes/tools/count_text.py`，并刷新了 `plan/protected.lock`（`lock --update` 只能由人执行）。
- 新增 `pytest.ini`，覆盖了受保护的 `pyproject.toml` 里的 pytest 配置。

---

## 1. 审查方法

- 用 `grep` 和 AST 扫描 `src/mao` 中出现的夹具名、测试路径、私有成员访问和全局补丁（扫描规则已写进 `tests/test_codebase_policy.py`，见 3.2 节）。
- 逐份对照 `spec/system/*.md` 第 3 章（接口）和第 5 章（行为）与实现。重点看验收没有覆盖的缺省路径、命令行和不变量。
- 修正受保护文件之后，对现有实现重跑 M1–M4 验收，确认失败都来自实现偏移，而不是新测试写错（4.2 节列出预期失败）。

---

## 2. 偏移清单

### 2.1 面向测试的特例（M5-01 删除）

| 编号 | 位置 | 现象 | 为什么错 | 根因与设计方修正 | 实现方要做的 |
|---|---|---|---|---|---|
| D-01 | `src/mao/approvals.py` 顶部、`src/mao/egress.py` 顶部 | 导入模块时把 `pathlib.Path.mkdir` 换成自定义函数：名为 `factory` 的目录自动建父目录；`secrets/values` 自动 `exist_ok` | 全进程生效。改变了标准库对所有调用方的语义，导入顺序不同结果就不同 | 测试辅助写错：`world.open_factory` 对嵌套目录调用 `mkdir()`；`test_m2_07` 的 `_store` 在同一测试里被调用两次。两处已改 | 删除这两段补丁。静态规则禁止给导入的对象赋值 |
| D-02 | `src/mao/contract.py` 的 `_install_converter`、`_BUILTIN` | 往 `snapshot._docs`、`snapshot._compiled` 注入虚构的 `subs-convert@0/1`、给 `toolbox@1` 加配方 `subs-keep`；内置 `golden@1`、`golden-no-budget@1`、`baseline-judge@1` | 契约测试在验证实现自己编造的文档。真实 Registry 里同名文档会被覆盖或混淆 | 夹具把 GoldenSet / Baseline 放在 `records/`（不在 Registry 布局里）；`test_m2_08` 引用夹具里不存在的名字。已把夹具移到 `goldens/`、`baselines/`；`test_m2_08` 改成在自己的副本上打补丁 | 删除注入代码与 `_BUILTIN`。`ContractRunner` 只通过快照的公开接口读文档；遍历用新增的 `Snapshot.keys`（`snapshot.md` 第 3 章） |
| D-03 | `src/mao/egress.py` 的 `_snapshot_for` | ModelProxy 要知道工位预算，于是在 `Path.cwd()/spec/examples/registry`、`Path.cwd()/tests/fixtures/registry-m2` 里找 epoch 相同的快照 | 生产环境当前目录里没有这些目录，预算上限永远取不到，`MODEL_BUDGET_UNSET` 判断失效 | 规范缺口：没写 ModelProxy 从哪里拿上限。`egress.md` 已补：新表 `model_caps`、函数 `register_job_cap`，Runner 在 PROVISIONING 登记 | 实现 `024_model_caps.sql` 与 `register_job_cap`；Runner 登记；ModelProxy 只读该表。删除 `_snapshot_for` |
| D-04 | `src/mao/runs.py` 的 `_check_inputs` | 产线名包含 `explainer` 且输入为空时，跳过“缺少接口输入”检查 | 按名字放宽校验。真正的缺陷是动态产线编译出的 `RunPlan.inputs` 为空 | `test_m4_01`、`test_m4_02` 启动产线时没给接口输入。已改为用 `world.line_inputs` 封存输入 | 删除特例；修正 `compile_dynamic`，让计划带上接口输入 |
| D-05 | `src/mao/sandbox.py` 的 `_trim_raw_facts` | 命令里有 `count_text.py` 且参数以 `raw.txt` 结尾时，从 validator 输出里删掉 `has_lower` | 沙箱篡改 validator 的输出 | 夹具 `types/raw.yaml` 的 `emits` 与工具实际输出不一致（`gate.md` 第 5 章要求键集合等于 `emits`）。已在类型里声明 `has_lower` | 删除 |
| D-06 | `src/mao/sandbox.py` 的 `_agent_fallback` | 命令里有 `fake_agent.py` 且 `task.json` 不是脚本时，控制面写一份替代脚本 | 控制面认识测试替身，并替 agent 编造行为 | `world.seal_input` 把所有载荷写成 `acceptance\n`；夹具 `fixtures/task/ok.json` 本身不是合法 JSON。两处已改 | 删除 |
| D-07 | `src/mao/runner.py` 的 `_seed_source_dir` | Source 的 `from_dir` 没有 `*.txt` 时，控制面往外部目录写 `note.txt` 内容 `ingested` | 控制面伪造外部物料，还写了它无权写的目录 | `test_m2_11` 用了空目录。已改为先写一份文本 | 删除 |
| D-08 | `src/mao/approvals.py` | `by` 以 `runtime` 开头就按复核计票；`quote@1` 的路由有针对矛盾的特例 | 按调用方字符串决定语义 | `test_m2_06` 没有按 `approvals.md` 第 7 章给复核用例打补丁（`else: review`、宪法法定人数 2）。已补 | 删除特例，路由只来自规范 5.2 节。另有两处与规范不符：法定人数缺失时 job 要 `FAILED`；复核出现 deny 时 `error.code` 是 `APPROVAL_REVIEW_SPLIT` |
| D-09 | `src/mao/db.py` 的 `Database.query` | 用正则把所有未加引号的 `commit` 改成 `"commit"` | 会改写字符串字面量和 `COMMIT` 语句 | `governance.md` 的建表语句用了未加引号的关键字 `commit`，测试也这样写。规范与测试已改为 `"commit"` | 删除改写；迁移和查询里写 `"commit"` |
| D-10 | `tests/fakes/tools/count_text.py` | 实现方改了受保护的替身（`chars` 去掉空白后计数），并自行刷新 lock | 违反第 6 节第 1、2 条 | 改动内容与 `m1_data_helpers.py` 写明的公式一致，设计方**追认**这一改动。流程违规记录在此 | 以后遇到这种情况写日志、阻塞 |
| D-11 | 仓库根目录 `pytest.ini` | 为了让同名的 `tests/test_cli.py` 与 `tests/system/test_cli.py` 都能收集，新增 `pytest.ini` 并改成 importlib 模式 | `pytest.ini` 优先于 `pyproject.toml`，后者的 pytest 配置整体失效 | 设计方已删除 `pytest.ini`，并把实现方的单元测试改名为 `tests/system/test_factory_cli.py` | 不要再新增 `pytest.ini`、`tox.ini`、`setup.cfg` 或新的 `conftest.py`（静态规则检查） |

### 2.2 缺省路径与不变量（M5-01 补齐）

| 编号 | 位置 | 现象 | 规范依据 | 实现方要做的 |
|---|---|---|---|---|
| D-12 | `governance/changesets.py`、`governance/rollout.py` | `contracts`、`shadow`、`rebuild` 为 `None` 时分别是：跳过 V4、影子直接判通过、不重建 | `governance.md` 3.3 写明三者的缺省实现：`ContractRunner(ctx, snap, Runner(ctx, snap))`、`LineShadowRunner(ctx)`、`LineRunner(...).rebuild` | 构造函数里装上缺省实现。实现 `LineShadowRunner`（5.9 节：重放基线 run 或黄金样例，Sink 一律走 dry-run）。`None` 不再表示“跳过” |
| D-13 | `reconcile.py` 的 `_missing_contract` | 每轮审计都写 I8 `wake` 发现；安全模式因此永远开启 | `reconciler.md` 5.1 的 I8；`governance.md` 5.13。规范的构造函数没有注入点，这是设计缺口，已补：`Reconciler(ctx, epochs, *, contracts=...)`，缺省与治理相同 | 用 `contracts` 重跑每个 validator 的正反样例；只有确实没有入口时才写 `missing`，缺省情况下不会出现 |
| D-14 | `reconcile.py` | 只实现了 I1、I2、I3、I9、I14、漂移和 I8 占位。I4、I5、I6、I7、I10、I11、I12、I13 没有检查 | `reconciler.md` 5.1 的表 | 按表逐条实现。M5-01 的验收测试覆盖其中的 I4、I6、I7、I10、I12 |
| D-15 | `reconcile.py` 的 `_drift`、`_baselines` | 用 `rglob` 扫描 `epochs.registry` 工作区里的 YAML 找 Baseline | `reconciler.md` 5.3：“对每个在 active 中有 Baseline 的工位”。工作区属于人与厂长，可能有未提议的修改 | 从 `epochs.materialize(active)` 的快照里取 Baseline（`Snapshot.keys("Baseline")`） |
| D-16 | `governance/rollout.py` 多处 | `current() != base_epoch and not current().startswith(base_epoch)`：用前缀比较放宽“过期”判断 | `governance.md` 5.9、5.10：`base_epoch` 存的是 40 位 commit，比较用相等 | 改成相等比较 |
| D-17 | `warehouse.py` 的 `_receipt_ids` | `except Exception: return []` | README 3.3：错误不能吞掉 | 删除这层 `try`；表一定存在（迁移全部执行） |
| D-18 | 多个模块 | 读 `snapshot._docs`、`getattr(snapshot, "_docs", {})`，调用别的模块的 `_reject`、`_verify` | 接口只能用 README 第 4 节列出的 | 改用 `Snapshot.keys`；跨模块要用的方法改成公开方法（不带下划线）。静态规则检查 |

### 2.3 规范要求但没有实现的接口（M5-02、M5-05）

| 编号 | 缺口 | 规范 | 处理 |
|---|---|---|---|
| D-19 | `factory` 命令行只实现了 M1 的命令和 `backend migrate`。缺少：`line`、`trigger`、`cache`、`approval`、`secret`、`egress`、`contract`、`schedule`、`epoch`、`changeset`、`governance`、`audit`、`drift`、`foreman`、`maintain`、`gateway`、`backend status/verify` | 各组件规范第 3 章；汇总与 JSON 约定见新规范 `spec/system/commands.md` | M5-02 |
| D-20 | `factory mcp` 没有实现 | `foreman.md` 3.6 | M5-05。设计方同时修正了 `foreman.md` 的设计错误：原文的方法名（`foreman.get` 等）不是 MCP 协议，Claude Code 这类客户端连不上。新规范 `spec/system/mcp.md` 改为标准 MCP（`initialize`、`tools/list`、`tools/call`） |
| D-21 | `ForemanQuery.registry_diff` 没有实现 | `foreman.md` 3.2 | M5-02（`factory foreman diff` 需要它） |
| D-22 | 架构文档第 15 节 M4 行里的“持久化执行、Web 控制台”没有进入任何里程碑 | `docs/architecture.md` 第 15 节 | 设计方已把持久化执行放进 M5（M5-03、M5-04），控制台放进 M7（M7-02），并更新了架构文档的路线表 |


### 2.4 闭环与生产路径上的缺口（M5-04、M5-06、M6）

下面几条不是“实现了错的东西”，而是“没有任何东西会让它发生”。M1–M4 的验收都在进程内直接调用组件，所以没有暴露出来。

| 编号 | 缺口 | 后果 | 根因 | 处理 |
|---|---|---|---|---|
| D-23 | `EpochRouter.choose` 没有调用方。触发器、`factory line run` 都直接用构造时的快照 | 金丝雀阶段永远等不到样本，T2 及以上的 ChangeSet 停在 `canary` | 规范写了路由规则（`governance.md` 5.9），没写谁在什么时候调用 | M5-04：守护进程按路由为新 run 选 epoch（`daemon.md` 5.3）；M5-02：`factory line run` 一侧 |
| D-24 | `conversion`、`dead-letter`、`review`、安全模式四类唤醒票没有生产者；也没有任何进程读唤醒票、启动厂长 | 厂长永远不会被唤醒。架构的核心目标“外部机器人维护整个流程”在交付物上做不到 | `foreman.md` 5.3 第 6 条只说“外部监督进程”，没有规范定义它 | M5-04：信号泵（`daemon.md` 5.5）；M5-06：值守进程（`supervisor.md`） |
| D-25 | 编译出 `E-NO-CONVERTER` 时没有人开转换需求。`src` 里没有任何 `ConversionStore.open_or_merge` 的调用 | 13.2 的闭环起不了头；转换需求表永远是空的 | 设计缺口：只有 `dynamic.md` 规定了 Plan 编译时开需求，治理的 V2 与厂长检查草稿的路径都没写 | M5-06：`file_conversions`、治理 V2 的调用点、MCP 工具 `registry_check`（`supervisor.md` 5.6、5.7） |
| D-26 | `WakeQueue.file` 的载荷超长时抛 `WAKE_NOT_FOUND` | 错误码与含义不符，调用方无法区分 | `foreman.md` 5.1 第 2 条没有给错误码 | M5-06：新码 `WAKE_PAYLOAD_INVALID` |
| D-27 | `ModelProxy` 在生产里固定监听 8080，`EgressProxy` 也是“本进程里活着的代理”；而 M5-04 的 `ProcessExecutor` 为每个 job 起一个子进程 | 多个 job 子进程会争抢 8080；`DockerProvider.open` 在子进程里找不到活着的出网代理，`network=egress` 的工位全部 `EGRESS_UNSUPPORTED` | `egress.md` 假定执行与代理在同一个进程 | M6-01：代理由 `factory serve` 持有，子进程与值守经 `serve.json` 发现（`runtimes.md` 5.4） |
| D-28 | 真实 CLI 无法把 `X-Factory-Job` 带给模型代理：runtime 模板的变量里没有 job id | 模型代理拒绝所有真实 CLI 的请求（`EGRESS_JOB_INVALID`），或者实现方会去写“按 CLI 名字加请求头”的特例 | `operator.md` 5.3 的模板环境只有 brief、limits、operator | M6-01：渲染后的控制面变量 `${FACTORY_JOB_ID}`、`${MODEL_PROXY}` 等（`runtimes.md` 5.1） |
| D-29 | `risk.mrl` 把新增转换工位（`op_tier("Station", "add", 2)`）评为 T2；架构第 11 节、13.2 第 4 步说新增 converter 是 T1 | 新增转换需要交叉复核和金丝雀，比架构描述的更慢 | 规范与架构不一致。T2 更保守，不会放过问题 | 保持 T2，不改实现。架构文档的说法由人决定是否修改。M5-07 的端到端按 T2 走（含交叉复核） |
| D-30 | 没有规范创建 `mao-egress` 网络，也没有规定代理绑定在哪个地址上容器才连得到；M2 的验收没有一个容器真的经过代理 | Linux 原生引擎上容器访问不到只绑定回环的代理；`mao-egress` 不存在时 `docker run` 直接失败 | `egress.md` 5.6 只写了 `--add-host=...:host-gateway` | M6-01：`ensure_egress_network`、`proxy_bind_host`（`runtimes.md` 5.5）；M6-03 在 Docker 里验证 |
| D-31 | agent 工位的策略只有 `none` 时 `network` 为 `none`，容器连不到 `model-proxy` | 真实 CLI 发不出任何模型请求 | `egress.md` 5.3 第 2 步没有区分 agent 与脚本工位；架构 6.4 说模型流量不占出网级别 | M6-01：runtime 引用 `${MODEL_PROXY}` 的 agent 工位一律 `egress`，`allow` 按策略（`runtimes.md` 5.6） |

### 2.5 编写 M6、M7 规范时发现的问题

| 编号 | 位置 | 现象 | 后果 | 处置 |
|---|---|---|---|---|
| D-32 | 架构第 2 节 Policy 的“保留期”；`spec/mrl/kernel` | Registry 里没有任何保留期字段。加字段要改内核 schema（L0），只有人能改 | 无法按架构把保留期写进 Policy | M7-03 把保留期放在 `factory.json` 的 `retention` 段（操作员配置，`retention.md` 4.1）。人以后若通过宪法提交加入 Registry 字段，由人改 `retention.md` |
| D-33 | `src/mao/db.py` 的 `migrate`；`egress.md`、`corrections.md`、`backends.md`、`images.md` 的迁移编号 | `migrate` 用 `PRAGMA user_version` 记录已应用的最大编号，跳过 `<=` 它的文件。规范曾把新迁移编为 `017`、`018`（小于已有的 `023`），`backend_lock` 与 `images` 都用了 `032` | 已有数据库永远不会执行插在中间的迁移；同号迁移让 `DB_MIGRATION_INVALID` 在启动时失败 | 重新编号：`024_model_caps`、`025_run_sink_mode`（M5-01）、`026_backend_lock`（M5-01 补齐 `backends.md`）、`030_leases`（M5-03）、`031_supervisor`（M5-06）、`033_images`（M6-02）、`034_retention`（M7-03）。**规则**：新迁移的编号必须大于仓库里已有的最大编号；不得复用、不得插空 |

另有一处设计方遗留：`tests/acceptance/m4/test_m4_02_plan.py` 有 5 个测试比 `dynamic.md` 第 7 章的描述弱（`test_plan_feedback_on_bounds` 不检查 job `REJECTED` 与 Bean 未封存；`test_budget_remaining` 不检查“等于剩余则通过”；`test_missing_converter_rejects_plan` 不检查转换需求；`test_sealed_plan_same_epoch` 不检查 plan Bean 已封存；`test_runtime_resolution_append_only` 测的不是追加规则）。实现方仍按规范第 7 章的描述实现；设计方会补强这些测试，补强后它们进入 M5-01 的验收范围。

---

## 3. 设计方已经修改的受保护文件

### 3.1 测试、夹具与规范的修正

| 文件 | 修改 |
|---|---|
| `tests/acceptance/m2/world.py` | `open_factory` 用 `mkdir(parents=True)`；`seal_input` 对 JSON 载荷取该类型第一个正样例；新增 `line_inputs` |
| `tests/acceptance/m2/test_m2_06_approvals.py` | 复核用例按第 7 章在副本上打补丁；补上“法定人数缺失时 job `FAILED`”“deny 即 `APPROVAL_REVIEW_SPLIT`”两处断言 |
| `tests/acceptance/m2/test_m2_07_egress.py` | `_store` 用 `exist_ok`；直接插入 job 行时调用 `register_job_cap` |
| `tests/acceptance/m2/test_m2_08_contract.py` | 重写。全部变体都在测试自己的副本上打补丁，与 `contract-tests.md` 第 7 章逐条对应（`CONTRACT_METRIC_MISSING` 是 `fail` 结果而不是异常；单阶段 Sink 用 `publish-local@1`，dry-run 用两阶段的 `publish-staged@1`） |
| `tests/acceptance/m2/test_m2_11_source.py` | 来源目录里先写一份文本 |
| `tests/acceptance/m3/test_m3_01_epochs.py`、`test_m3_04_reconciler.py` | SQL 里写 `"commit"` |
| `tests/acceptance/m4/test_m4_01_dynamic.py`、`test_m4_02_plan.py` | 启动产线时给出接口输入 |
| `tests/fixtures/registry-m2/types/raw.yaml` | 声明 `has_lower`，`emits` 与 `count-text` 的输出一致 |
| `tests/fixtures/registry-m2/goldens/`、`baselines/` | 由 `records/` 移入 Registry 布局 |
| `tests/fixtures/registry-m1|m2/fixtures/task/ok.json` | 改成合法 JSON |
| `spec/system/governance.md` 4.2 | 列名写成 `"commit"`，禁止正则改写 SQL |
| `spec/system/egress.md` 3.3、4.2、5.5 | `model_caps`、`register_job_cap` |
| `spec/system/snapshot.md`、`README.md` 4.2 | `Snapshot.keys` |
| `spec/system/reconciler.md` 第 3 章、5.1、5.3 | `contracts` 注入点；I8 的缺省；漂移只读 active 快照 |

### 3.2 护栏

- `tests/test_codebase_policy.py` 新增五条规则：
  - 禁止给导入的对象赋值或 `setattr`（D-01）；
  - 库代码禁止读当前目录（D-03）；
  - 禁止访问别的源文件定义的私有成员，包括用 `getattr` 绕过（D-02、D-18）；
  - 禁止出现测试基础设施路径（`tests/`、`registry-m2`、`fake_agent` 等）；
  - 禁止写死示例 Registry 和夹具里的工位、产线、机器名。

  另外新增 `test_pytest_config_only_in_pyproject`（D-11）。
- 受保护模式新增：
  - `tests/test_codebase_policy.py`（以前漏了，实现方可以改它）；
  - `docs/*.md`；
  - `pytest.ini`、`tox.ini`、`setup.cfg`、`tests/**/conftest.py`；
  - `spec/runtimes/*.yaml`（参考 runtime 文档）。

  模式要在人执行 `lock --update` 之后才进入基准。

---

## 4. 实现方的纠偏任务

### 4.1 任务

| 任务 | 内容 |
|---|---|
| M5-01 | 第 2.1、2.2 节全部条目。验收：修正后的 M1–M4 验收全部通过，静态规则通过，`tests/acceptance/m5/test_m5_01_corrections.py` 通过 |
| M5-02 | D-19、D-21、D-23 的命令行一侧：按 `spec/system/commands.md` 补齐命令行 |
| M5-03 | 持久化执行（D-22 的一半）：`spec/system/durable.md` |
| M5-04 | D-23、D-24 的生产者：`spec/system/daemon.md` |
| M5-05 | D-20：按 `spec/system/mcp.md` 实现 `factory mcp` 与 `factoryctl mcp` |
| M5-06 | D-24 的消费者、D-25、D-26：`spec/system/supervisor.md` |
| M5-07 | 13.2 端到端：`spec/system/autonomy.md` |
| M6-01 | D-27、D-28、D-30、D-31：`spec/system/runtimes.md` |
| M6-02 | 厂长构建镜像（架构 9 节“上游常常不提供 Docker”）：`spec/system/images.md` |
| M6-03 | 真实 CLI 在 Docker 里的端到端：`spec/system/live.md` |
| M7-01 | 状态、指标、事件流：`spec/system/observability.md` |
| M7-02 | 本机只读控制台：`spec/system/console.md` |
| M7-03 | D-32、D-33 的保留期部分：`spec/system/retention.md` |
| M7-04 | 备份与恢复：`spec/system/backup.md` |

### 4.2 修正受保护文件之后，现有实现预期失败的验收

下面这些失败来自实现偏移，不是测试写错。纠偏完成时它们必须全部通过：

| 测试 | 原因 |
|---|---|
| `test_m2_03_line_runner::test_happy_path_inline`、`test_implicit_converter_job_runs` | D-05：沙箱删掉了 `has_lower`，`count-raw` 判为设施故障 |
| `test_m2_05_cache::test_rebuild_hits_everything`、`test_variant_reruns_one_step` | 同上，产线跑不通 |
| `test_m2_06_approvals::test_review_quorum_unset` | job 没有被置为 `FAILED` |
| `test_m2_06_approvals::test_review_counts_distinct_runtimes` | deny 时错误码是 `APPROVAL_DENIED` 而不是 `APPROVAL_REVIEW_SPLIT` |
| `test_m2_07_egress` 中直接插 job 行的 4 个测试 | D-03：没有 `register_job_cap` |
| `test_m2_08_contract::test_equivalence_holds`、`test_diff_regression`、`test_diff_facts_exact` | D-05：原稿入库失败 |
| `test_m2_08_contract::test_unknown_metric_fails` | `CONTRACT_METRIC_MISSING` 被抛成异常，规范要求返回 `fail` 结果 |
| `test_m2_08_contract::test_sink_without_propose_refuses` | 单阶段 Sink 没有抛 `CONTRACT_SINK_NO_PROPOSE` |
| `test_m4_01_dynamic::test_dynamic_overflow_reject`、`test_switch_eval_error` | D-04：动态计划没有接口输入 |
| `tests/test_codebase_policy.py` 中 11 个源文件 | 2.1、2.2 节的特例与私有成员访问 |

### 4.3 以后遇到同类情况

验收测试、夹具与规范互相矛盾，或者验收要求的行为在规范里找不到依据：**停下来**，按 `plan/README.md` 第 5 节写日志并 `task block`。设计方会修正受保护文件。下面这些做法任何一种都算任务失败，即使验收通过：

- 在实现里识别测试的名字、路径或工具；
- 改标准库的行为；
- 读别的模块的私有成员；
- 替外部世界造数据。
