# 里程碑

> 每个里程碑写明：目标、范围（做什么、不做什么）、交付物、任务、退出条件。
> 退出条件的可执行版本是 `plan/gates.yaml` 中对应的阶段门。
> 任务的依赖与验收项以 `plan/tasks.yaml` 为准。

```
M0 结构编译 ──▶ M1 单工位跑通 ──▶ M2 产线与外部世界 ──▶ M3 治理与厂长 ──▶ M4 动态结构与生产化
  （已完成）
                                                                               │
M7 运维与生产化 ◀── M6 真实接入 ◀── M5 自治闭环（含 M1–M4 纠偏） ◀──────────────────┘
   │
   ▼
M8 交付审查纠偏 ──▶ M9 闭环审查纠偏 ──▶ M10 接线审查纠偏
M10 接线审查纠偏 ──▶ M11 委托与能力 ──▶ M12 常驻机器与入站 ──▶ M13 常驻 Agent ──▶ M14 跨端口来料约束 ──▶ M15 人与厂长的持久对话 ──▶ M16 工厂资产与可替代性
```

每个里程碑结束时，系统都处于可以使用、可以交接的状态，而不是半成品。

---

## M0 结构编译（已完成）

**目标**：让“结构是否合法”成为可以运行、可以验证的判断，而不只是文档里的描述。

**交付物**：
- MRL 1.0：表达式、模板、规则语言。参考实现在 `src/mrl/`，语言规范是 `spec/mrl/mrl-1.0.md`。
- 工厂 profile：Registry → JSON Schema → 事实 → 内核 + 宪法 → 诊断。规范在 `spec/mrl/factory-profile.md`，内核在 `spec/mrl/kernel/`，宪法在 `spec/mrl/constitution/`。
- Spec 的 JSON Schema（`spec/schemas/`），以及示例 Registry（`spec/examples/registry/`，对应架构文档 13.1）。
- 一致性用例 702 个（`spec/mrl/conformance/`），诊断码与内核规则覆盖率均为 100%。
- `mrl` 命令行：`eval`、`template`、`check`、`run`、`conformance`、`coverage`、`registry check|facts|resolutions`、`resolve`、`explain`。
- harness（`tools/harness.py`）、计划文档、场景（架构文档 13.2–13.5 的静态部分）。

**任务**：M0-01 至 M0-07，均已完成，用于记录证据。

**退出条件**（阶段门 M0）：
- 一致性用例全部通过，`total >= 702`；
- 覆盖率没有缺口；
- 示例 Registry `--strict` 通过；
- 4 个场景通过；
- 全量 pytest 零失败；
- lint、格式、文档一致性、受保护文件检查都通过。

---

## M1 单工位跑通

**目标**：给定一个通过检查的 Registry 和已封存的输入 Bean，能在 Docker 沙箱里运行**一个**工位，经过出厂检验，把产物封存进仓库，并能追溯血缘。

工位覆盖两种形态：
- C 形态：脚本工位，例如 ffmpeg 类工具；
- A 形态：agent 工位，由 OperatorRuntime 启动的 CLI 驱动工作区。

**范围**：
- 做：
  - 基础设施（规范 JSON、时钟与 ID 注入、错误码、SQLite 迁移）；
  - Registry 快照与运行期求值环境；
  - 仓库（CAS、Bean、封存、血缘、完整性校验）；
  - 沙箱（Docker / Local）；
  - `factoryctl` 与控制通道；
  - 来料检验与出厂检验（V5：结构、validator、属性、accept、meta_schema）；
  - OperatorRuntime 调用与 Station Brief；
  - Job 状态机（重试、运行中审批、事后预算结算）；
  - `factory` 命令行；
  - 端到端测试（Local 与 Docker）。
- 不做：
  - 产线执行、缓存、Source/Sink；
  - 出网代理与模型代理、秘密注入；
  - 两阶段工位、judge、`ref<T>` 与 `any` 产物。

  这些分别属于 M2 到 M4。

**交付物**：`src/mao/`；`tests/system/`（实现者的单元测试）；`tests/acceptance/m1/` 全部通过。

**任务**（依赖关系）：

```
M1-01 基础设施 ──┬─▶ M1-02 快照与求值环境 ──┐
                 ├─▶ M1-03 仓库 ────────────┼─▶ M1-06 检验 ──┐
                 └─▶ M1-04 沙箱 ─▶ M1-05 factoryctl ─┤        │
                                  M1-02 + M1-05 ─▶ M1-07 操作工与 Brief
                    M1-03 + M1-05 + M1-06 + M1-07 ─▶ M1-08 Job 与 Runner ─▶ M1-09 命令行 ─▶ M1-10 端到端
```

| 任务 | 规范 | 验收测试 |
|---|---|---|
| M1-01 基础设施 | `spec/system/foundation.md` | `tests/acceptance/m1/test_m1_01_foundation.py` |
| M1-02 快照与求值环境 | `spec/system/snapshot.md` | `test_m1_02_snapshot.py` |
| M1-03 仓库 | `spec/system/warehouse.md` | `test_m1_03_warehouse.py` |
| M1-04 沙箱 | `spec/system/sandbox.md` | `test_m1_04_sandbox.py` |
| M1-05 factoryctl 与控制通道 | `spec/system/factoryctl.md` | `test_m1_05_factoryctl.py` |
| M1-06 来料与出厂检验 | `spec/system/gate.md` | `test_m1_06_gate.py` |
| M1-07 操作工与 Brief | `spec/system/operator.md` | `test_m1_07_operator.py` |
| M1-08 Job 与 Runner | `spec/system/runner.md` | `test_m1_08_runner.py` |
| M1-09 命令行 | `spec/system/cli.md` | `test_m1_09_cli.py` |
| M1-10 端到端 | `spec/system/testing.md` | `test_m1_10_e2e.py` |

**退出条件**（阶段门 M1）：
- M0 的全部检查；
- `tests/acceptance/m1` 在 `MAO_REQUIRE_DOCKER=1` 下零失败、零跳过；
- 单元测试零失败；
- 静态规则通过；
- 每个任务的越界改动经人审查。

**主要风险**：
- R5 Windows；
- R7 安全基线；
- R13 Docker 测试被跳过；
- S1 CLI 输出格式；
- S4 本地镜像 digest。

详见 `plan/risks.md`。

---

## M2 产线与外部世界

**目标**：
- 按一条 Line 运行多个工位：产物沿边流转，不兼容时插入转换工位；
- 接上外部世界：Source 摄入，Sink 发布并核验回执；
- 补上缓存、审批、出网控制，以及 V4 的契约验证（V3 演进校验随 ChangeSet 流水线放在 M3）。

**范围**：
- 产线运行时：运行记录，按解析表执行 DAG，运行期插入转换 job，运行预算；
- 缓存：`job_key`、`variant`、转换缓存；
- Source/Sink：幂等键、回执与核验、dry-run；
- 两阶段工位与审批路由（auto / foreman / review、超时）；
- 出网代理（域名 × 方法白名单）、模型代理（注入密钥、实时计量预算）、秘密注入；
- V4 契约测试（validator 正反样例、黄金样例与基线、转换的 equivalence 与差分、Sink dry-run）；
- 调度：资源队列、配额、隔舱、背压、熔断、死信队列。

**规范**：
- `spec/system/lines.md`、`cache.md`、`connectors.md`、`approvals.md`；
- `spec/system/egress.md`、`contract-tests.md`、`scheduler.md`。

**任务与退出条件**：见 `plan/tasks.yaml` 中的 M2 任务和 `plan/gates.yaml` 中的阶段门 M2。

---

## M3 治理与厂长

**目标**：结构可以安全地演进，厂长（外部 agent CLI）可以在确定性的护栏内维护系统。

**范围**：
- epoch（Registry 的 Git commit、当前指针、回滚）；
- ChangeSet 流水线，含 V3 演进校验（不可变、兼容、退役引用）；风险等级由 diff 事实经受保护的 `spec/mrl/governance/risk.mrl` 确定性地计算；
- 影子运行、金丝雀、按 SLO 自动回滚；
- 变更速率与封版窗口；
- 调和器与不变量审计（V6）、漂移监测；
- 厂长的唤醒与操作面（只读查询与 ChangeSet 提交）；
- 跨 runtime 复核；
- ConversionRequest 的处理；
- 维护产线自举。

**规范**：`spec/system/governance.md`、`reconciler.md`、`foreman.md`。

**任务与退出条件**：见 `plan/tasks.yaml` 中的 M3 任务和 `plan/gates.yaml` 中的阶段门 M3。

---

## M4 动态结构与生产化

**目标**：
- 运行期的动态结构（map / switch / until / subline / plan）按编译期同样的规则展开和约束；
- 接入服务型机器（B 形态）；
- 存储后端可替换为生产级实现。

**范围**：
- 动态结构运行时，以及 planner 产出的 Plan 在执行前的编译检查（V1/V2 + `plan_bounds`）；
- B 形态网关（OpenAPI → MCP，提交 → 轮询 → 取回）；
- S3/MinIO 的 CAS、Postgres 状态库，以及数据迁移。

**规范**：`spec/system/dynamic.md`、`gateway.md`、`backends.md`。

**任务与退出条件**：见 `plan/tasks.yaml` 中的 M4 任务和 `plan/gates.yaml` 中的阶段门 M4。

---

## M5 自治闭环（含 M1–M4 纠偏）

**目标**：
- 纠正 M1–M4 交付中的偏移（`plan/drift.md`），让安全网在生产路径上真正生效；
- 让“外部机器人维护整个流程”在没有人值守时也能发生：信号 → 唤醒票 → 现成 agent CLI 的厂长会话 → ChangeSet → 复核 → 发布。

**范围**：
- 删除面向测试的特例，补齐缺省路径、调和器不变量与迁移编号（M5-01）；
- 规范列出的全部命令行（M5-02）；持久化执行与租约（M5-03）；
- 守护进程 `factory serve` 与信号泵（M5-04）；MCP 操作面（M5-05）；
- 厂长值守：为每张唤醒票在沙箱里启动 Claude Code / Codex 等现成 CLI，交叉复核（M5-06）；
- 端到端：`serve` 托管值守（M5-07）。

**不做**：系统不实现任何 agent，也不调用模型 API；验收用 `tests/fakes/fake_agent.py`。

**规范**：`spec/system/corrections.md`、`commands.md`、`durable.md`、`daemon.md`、`mcp.md`、`supervisor.md`、`autonomy.md`；偏移清单 `plan/drift.md`。

**任务与退出条件**：见 `plan/tasks.yaml` 中的 M5 任务和 `plan/gates.yaml` 中的阶段门 M5。

---

## M6 真实接入

**目标**：用真实的 agent CLI（固定版本，运行在 Docker 里）跑通工位 job、厂长会话与复核会话，证明 M5 的闭环不依赖替身。

**范围**：
- 控制面变量、codex transcript、模型代理与出网代理的归属与绑定地址、`mao-egress` 网络（M6-01）；
- 厂长为上游产品构建镜像（M6-02）；
- 现场镜像与端到端（M6-03）：模型上游是 `tests/fakes/fake_llm.py`，不调用真实模型。

**规范**：`spec/system/runtimes.md`、`images.md`、`live.md`；参考 runtime 文档 `spec/runtimes/`。

**任务与退出条件**：见 `plan/tasks.yaml` 中的 M6 任务和 `plan/gates.yaml` 中的阶段门 M6。M6-03 需要 Docker 与现场镜像。

---

## M7 运维与生产化

**目标**：无人值守长期运行时，人能看见状态、能限制增长、能备份恢复。

**范围**：
- 状态、Prometheus 指标、事件流（M7-01）；
- 本机只读 Web 控制台（M7-02）；
- 保留期、墓碑与回收（M7-03）；
- 备份、校验与恢复（M7-04）。

**不做**：推送告警、远程访问、Registry 中的保留期字段（`plan/drift.md` D-32，需要人改宪法）。

**规范**：`spec/system/observability.md`、`console.md`、`retention.md`、`backup.md`。

**任务与退出条件**：见 `plan/tasks.yaml` 中的 M7 任务和 `plan/gates.yaml` 中的阶段门 M7。

---

## M8 交付审查纠偏

**目标**：M5–M7 的阶段门通过之后，审查（`plan/review-m5-m7.md`）发现出网代理是空壳、无人值守的值守循环不存在。M8 让这两项核心能力在生产路径上真实可用，并修正其余 7 处问题。

**范围**：出网代理按 `egress.md` 5.4 转发与拒绝；跨进程的出网计划登记；命令发现守护进程的代理；Docker Desktop 转发容器的固定与清理；常驻值守与 `serve` 托管；提议失败后重开转换需求；回收竞态；`serve` 的韧性与信号处理；值守会话的网络。

**规范**：`spec/system/hardening.md`；审查清单 `plan/review-m5-m7.md`。

**任务与退出条件**：见 `plan/tasks.yaml` 中的 M8-01 和 `plan/gates.yaml` 中的阶段门 M8（继承 M7 的全部检查）。

---

## M9 闭环审查纠偏

**目标**：阶段门 M8 通过之后的闭环审查（`plan/review-closure.md`）发现，M4 的动态结构、Plan、B 形态网关、生产后端只有被验收直接调用的纯函数，运行期没有调用者，部分实现按验收字面值写死。M9 让这四项能力在生产路径上真正运行。

**范围**：`LineRunner.tick` 执行动态构造；planner 的出厂检验、Plan 挂接与执行；`Runner` 经 `ServiceGateway` 执行服务型工位且生产路径都装上网关；S3 SigV4、命令按 `backend-active.json` 打开后端、Postgres 只经 `psql`、运行期 SQL 可移植。

**规范**：`spec/system/closure.md`；审查清单 `plan/review-closure.md`。

**任务与退出条件**：见 `plan/tasks.yaml` 中的 M9-01 至 M9-04 和 `plan/gates.yaml` 中的阶段门 M9（继承 M8 的全部检查）。

---

## M10 接线审查纠偏

**目标**：阶段门 M9 通过之后按文档演练产线时发现（`plan/review-wiring.md`），Sink 回执只被占用、从不发布和核验，`connectors.verify` 是桩；`factoryctl receipt`、`factory receipt`、`factory source run`、`factory run` 缺失；生产路径的产线运行器不带缓存；几处 `Runner` 没有网关。M10 把这些接上，并让运行器只在一处装配。

**范围**：Sink 的取回执、发布、核验、结算与 `SINK_OUTCOME_UNKNOWN`；`factoryctl receipt`；回执、Source、单工位运行三组命令；`open_line_runner` 与构造点收敛。

**规范**：`spec/system/wiring.md`；审查清单 `plan/review-wiring.md`。

**任务与退出条件**：见 `plan/tasks.yaml` 中的 M10-01 至 M10-03 和 `plan/gates.yaml` 中的阶段门 M10（继承 M9 的全部检查）。

---

## M11 委托与能力

**目标**：让厂长能自主接入一台新机器（例如 B 站投稿：镜像、Playwright 脚本、Machine、Station、Policy、产线），人可以用配置把审批全部交给厂长（`bypass`），厂长与操作工都有独立的申请渠道，秘密由人填写并在保存前核验，验证码交给人处理。同时修掉演练时发现的接线问题：带出网的工位到不了沙箱、秘密没有注入、`Runner` 的审批绕过 `Approvals`、出网只靠请求头识别 job、新产线在金丝雀阶段无法运行（`plan/design-delegation.md` 第 1 章）。

**范围**：人工收件箱与通知钩子；授权模式、审批委托与改道、定向申请、ChangeSet 签核；出网令牌、通配域名、按需放行、监控规则；秘密申请与核验；人工协助；沙箱资源与镜像构建网络。

**规范**：`spec/system/inbox.md`、`spec/system/delegation.md`、`spec/system/capabilities.md`；设计 `plan/design-delegation.md`。

**任务与退出条件**：见 `plan/tasks.yaml` 中的 M11-01 至 M11-09 和 `plan/gates.yaml` 中的阶段门 M11（继承 M10 的全部检查）。

---

## M12 常驻机器与入站

**目标**：补齐机器的实例模式。M1 起 `instance` 只有 `per_job` 能用：每个 job 新建再销毁容器，浏览器登录态、长连接、本地缓存都留不住，需要长期运行的服务也无处安放。M12 让机器以 `pooled`（复用容器，每个 job 之后 `reset`）或 `resident`（长期运行的主进程，崩溃后退避重启）运行；常驻机器能主动产出 Bean（`factoryctl emit`）；外部推送经入站网关进入工厂，投成 Bean 或写进常驻实例的收件目录（`plan/design-resident.md` 第 1 章）。

**范围**：`SandboxSpec.command`、`alive`、`attach`；实例配置与静态检查；租借、`reset`、拷入拷出、准入；调和、恢复、命令行、`Daemon` 接线；`Runner.adopt`、`emit`、`factoryctl emit`；入站网关与 `serve` 装配。

**规范**：`spec/system/instances.md`、`spec/system/ingress.md`；设计 `plan/design-resident.md`。

**任务与退出条件**：见 `plan/tasks.yaml` 中的 M12-01 至 M12-05 和 `plan/gates.yaml` 中的阶段门 M12（继承 M11 的全部检查）。

---

## M13 常驻 Agent

**目标**：长期收集信息、接收不定期消息这类工作不再被打散成许多次一次性会话。常驻 Agent 的容器长期存活，对话由 CLI 自己保存并在每批消息到来时续接（`invoke.resume`），上下文在 Agent 身上，而不是每次从数据库拼出来。消息来自定时、新 Bean、入站、收件箱回答、人和别的 Agent；轮次有预算、超时、失败重试与丢弃、恢复和上下文轮换。厂长可以切到常驻模式，复核仍是一次性会话。

**范围**：`Agent` kind 与 `agent_config`；邮箱与订阅；轮次、沙箱、预算、恢复、命令行与 `serve` 装配；上下文轮换与 `agent` 角色的 MCP 工具；厂长常驻模式。

**规范**：`spec/system/agents.md`；设计 `plan/design-resident.md`。

**任务与退出条件**：见 `plan/tasks.yaml` 中的 M13-01 至 M13-05 和 `plan/gates.yaml` 中的阶段门 M13（继承 M12 的全部检查）。

---

## M14 跨端口来料约束

**目标**：让工位声明“几个输入之间要相互匹配”，例如固件的目标车型、平台必须与这台车一致。不匹配的物料在建 job 之前被拒收，而不是工位跑完后才在出厂 `accept` 里发现（`plan/design-admit.md` 第 1 章）。

**范围**：工位 `admit` 段；抽取与表达式位置；`W-ADMIT-META` 与无输入工位的 `E-PORT`；一致性用例；`Gate.check_inputs` 求值 admit，`Runner.submit` 传参。

**规范**：`spec/system/admit.md`；设计 `plan/design-admit.md`。

**任务与退出条件**：见 `plan/tasks.yaml` 中的 M14-01、M14-02 和 `plan/gates.yaml` 中的阶段门 M14（继承 M13 的全部检查）。

---


## M15 人与厂长的持久对话

**目标**：给人与常驻厂长一条工厂级线程。对话是意图通道，卡片是命令。控制面不调用模型。人的话进入线程和厂长邮箱；厂长用 `talk_reply` 写回。配置仍走 `changeset_propose`。产出产线先记运行卡片，人确认后控制面才 `start_run`（`plan/design-talk.md`）。

**范围**：表 `talk_messages`；`post_human`、`talk_reply`、`factory talk ls/send/show`；未回复和邮箱丢弃时开收件箱事项；常驻厂长轮次里的 ChangeSet、上报、秘密申请卡片及后来的状态；`talk_propose_run`（含通知）与 `factory talk answer --confirm`（含 `EpochRouter`）。

**不做**：Web 与控制台路由；对话里改宪法、`factory.json`、合入、回滚；模型直接 `start_run`；把全厂 `bean.sealed` 灌进线程。

**规范**：`spec/system/talk.md`；设计 `plan/design-talk.md`。

**任务与退出条件**：见 `plan/tasks.yaml` 中的 M15-01、M15-02、M15-03 和 `plan/gates.yaml` 中的阶段门 M15（继承 M14 的全部检查，`requires_gates: [M14]`）。

---

## M16 工厂资产与可替代性

**目标**：完整业务以数据闭包交付，经验有证据才能成为正式知识，新岗位能从显式交接继续真实工作。保留不可变原件、固定领域标准和厂长的自主组织空间；不建设影子执行、周期模型压缩或持续双跑。

**范围**：资产目录与依赖保留、授权契约与真实证据、CAS 正式知识、安全交接与全新私有工作区接管、SQLite/本地 CAS 的完整冷检查点、披露审核后的移交包与买家绑定、CLI/MCP。全部业务语义来自领域数据与原有 Gate，不硬编码视频/量化/制造指标。

**不做**：市场/交易/支付、自动发布、自动识别所有隐私、在线/远程后端一致快照、设备现场复制、自动周期全厂复测。旧结构治理和生产确认规则不变。

**规范**：`spec/system/assets.md`；设计 `plan/design-assets.md`。M16 显式启用，旧工厂的 API 与运行默认不变。

**任务与退出条件**：M16-01 至 M16-06 顺序实施；阶段门 M16 继承 M15 全部检查，`requires_gates: [M15]`，M16 验收全部通过且零跳过。设计交付不登记实现完成。移交包的静态 verify 不能替代买家领域能力验收。

---

## 里程碑之间的交接规则

1. **退出前提**：阶段门通过，才能开始下一个里程碑的任务。harness 用 `requires_gates` 和任务依赖强制这一点。
2. **规范冻结**：一个里程碑开始后，它的规范只能由人修改。修改后要重新确认受影响的任务，已完成的任务要重新验收。
3. **检查项继承**：每个阶段门都继承前一个阶段门的全部检查项，门槛只升不降。
4. **交接文件**：里程碑结束时，在 `plan/journal/` 写一份交接记录，内容包括：
   - 完成了什么；
   - 偏离了什么、为什么；
   - 留下了哪些已知问题；
   - 下一个里程碑要先读什么。

---

## M17 持续自治与故障闭环

**目标**：修复 M15 冻结基线中已复现的驱动、治理、服务与预算问题，并让工厂从一次宏观方向出发，由厂内 Agent 发现具体工作、执行、评价和继续观察。运行常驻进程或一次产线成功不构成此目标的证明。

**边界**：先完成调查与统一设计，当前不实施修复；实现依赖 M16-06 和阶段门 M16。独立 worktree 基线为 c538768，不干预 M16 进行中的工作区。结构扩展继续 ChangeSet，正常业务无需外层 Agent 改内核或代签；控制面不调用模型。

**规范与证据**：`spec/system/continuity.md`、`spec/system/operability.md`、两个M17 design文档、`closure.yaml`、`plan/evidence/m17/README.md`及分域报告。

**退出条件**：M17-01至M17-12共12任务全部完成（08为最终任务，依赖07/09/10/11/12）；46个已登记问题/别名/要求的设计映射完整、至少81个精确离线用例零失败/错误/跳过，旧阶段门保持。原C10与新增两领域×两native runtime四格真实witness均通过，各格至少四周期、两个自主工作、idle与失败调整，历史机器库存实际产品资格、权限和介入账本完整。缺任何必选真实资源/节点/证据不能通过，完整性校验不替代真实能力。当前仅完成设计维护，不标记已修复或done。
