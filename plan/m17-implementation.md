# M17 实施入口（交给 Grok）

先读本页，再读对应规范和完整任务卡。当前分支交付的是设计与证据，尚无 M17 生产修复；不能将设计检查通过写成任务完成。完整规范为 `spec/system/continuity.md` 与 `spec/system/operability.md`，问题追踪为 `plan/evidence/m17/closure.yaml`。

基础跑单及当前live占位问题先读 [基础订单与真实见证交接](m17-live-readiness.md)；基础script订单无需四个视频产品，完整M17真实门另行证明。

## 1. 开工条件与目标

M16 正在其他工作区实现。只有 M16-06 已完成、M16 阶段门实际通过后，才在 M17 分支整合经过验证的 M16 最终提交；核对 `plan/state.json` 的任务、命令结果和阶段门证据，不凭提交标题认定完成。不在 M16 工作区修改、合并或重置文件；本次设计交付也不提前合并未完成的 M16。

整合后先核对 M16 已包含的修复，尤其 LocalProvider 的宿主命令路径；重放有关诊断，保留回归而不重复打补丁。若接口与冻结设计冲突，按 AGENTS.md 的设计维护/受阻流程处理，不能回退 M16、改旧验收或手工标记 state 为 done。

完成目标是：人给宏观方向及一次长期授权后，常驻厂内岗位自主观察、发现候选、选择工作、制定计划、治理业务扩展、派工、生产、领域评价、调整并继续。外部 Agent 负责交接目标、查看状态、转交真正需要人的事项；具体业务接续、修平台内核、代签和手动重试不计自治成功。控制面只做确定性权限、状态、调度、校验和计量，推理与工具选择由原生 CLI 内的 Agent 承担。

## 2. 固定实施顺序与入口

推荐单线程顺序：**01 → 02 → 03 → 04 → 05 → 06 → 07 → 09 → 10 → 11 → 12 → 08**。08 是最终总验收；编号小不意味着提前做。完整依赖和允许写入的文件由 `plan/tasks.yaml` 决定，表中源码路径是阅读入口，不扩大任务 paths。

| 任务 | 先看的实际入口 | 本项交付边界 |
|---|---|---|
| 01 | `agents.py` 的 Mailbox、`_schedules/_wakes/_pending`；`foreman.py` 的 WakeQueue；`durable.py` 的 LeaseStore | 事务内领取/投递/水位/去重、持久顺序、租约代数、固定效果 outbox；迁移043 |
| 02 | `commands/serve.py`、`daemon.py`；AgentHost 的 `_work/_run_cli/_handoff`、`_turn_outcome`；`operator.py` 的 parse_transcript；`mcp.py` | 真正 CLI 启动已装配的 AgentHost；统一异常结案和有效 result/session；每次可控副作用检查 fence |
| 03 | `governance/changesets.py` 的 advance/验证/diff；`governance/rollout.py` 的各阶段推进、推广与 rebuild；`governance/epochs.py` | 统一 safe-mode/发布槽、原子数据库决议、refs/rebuild 可恢复投递、所有 Registry kind 和 Agent 风险事实 |
| 04 | `ctl/factoryctl.py`、`gateway.py`、`runner.py`、`operator.py`、`instances.py`、`sandbox.py` | 真实客户端→声明的操作工→服务→产物；已知 task 续取与未知结果；路径来源；真实池隔离；迁移044 |
| 05 | `egress.py` 的 ModelProxy、Runner/AgentHost 成本记账、真实 factoryctl budget | 单一持久预算账本和余额反馈；重启/并发/未知费用，不另建目标成本副本 |
| 06 | `agents.py` 的 agent_tool/run_start、`runs.py`/`runplan.py` 终态路径、ForemanQuery；新增 results.py；M16 learning.py | 稳定 run 身份、结果查询与可靠回信、确定性指标/评价助手、通用路由消费；此时不创建目标表或伪造长期授权 |
| 07 | 新增 objectives.py；06 的 results.py；AgentHost/MCP/daemon；M16 assets/learning/retention/backup | 目标资产、长期授权、可信来源与派工、目标预算、周期收尾/恢复和受控候选试运行；迁移045 |
| 09 | 新增 transport.py；egress/gateway/真实 factoryctl；原有预算/执行来源 | connect/idle/total、CONNECT 有界转发/TLS 方法控制、未知请求与费用保留；迁移046（必须晚于045） |
| 10 | 新增 qualification.py/commands/qualification.py；operator/AgentHost/serve、gateway/Runner | 两种原生 CLI 的真实会话/任务书/工具协议资格、产品身份和通用服务地址恢复；收据变更失效 |
| 11 | 新增 deployment.py/operations.py、commands/deployment.py/access.py；base/context、serve/daemon | 只读预检、幂等非破坏 plan/apply、管理/operate 文件邮箱隔离、可信凭据、治理最低证据 |
| 12 | cli.py 的真实注册树、commands/；新增 command_help.py、skills/mao-factory；对应使用文档 | help/guide/capabilities/错误恢复一致、可发现且版本匹配的 skill；外部只交接目标，厂内做业务 |
| 08 | `cases.yaml`、`operability-cases.yaml`、所有实现任务结果 | 全离线精确节点、原 C10 真实 witness、额外两领域×两运行时四格真实 witness 和完整性校验 |

以上源码路径均相对于 `src/mao/`。治理实现位于 `governance/` 包，不是 `governance.py`。真实 `ctl/factoryctl.py` 是供工位安装使用的标准库客户端，不能要求工位为修客户端额外安装宿主 mao 包。新模块接口严格按 spec 第3章；兼容现有公开接口和调用方，不自行设计替代 API。

02 保留现有 TranscriptSummary/parse_transcript 的兼容返回结构，可靠识别终态；旧脚本、支持与不支持 resume 的 runtime 分别遵守规范，不能将空输出或假 session 当成功。07 才将目标周期/授权、目标来源和受限 candidate 权限接入06的通用结果路径；06可先完成按既有权限的路由、稳定 epoch 与去重，07再补完整目标内候选正反例，不能在06引用未创建的目标表。

## 3. 迁移只追加，并按责任任务落地

当前逻辑槽固定为：

| 迁移 | 创建者 | 内容 |
|---|---|---|
| 043_delivery.sql | 01 | 消息序号、turn owner/generation、lease_generations、delivery_keys、effect_outbox、rollout_owner |
| 044_execution.sql | 04 | service_operations、budget_entries、run_requests（cycle/task 可空） |
| 045_objectives.sql | 07 | execution_origins、objective_bindings、objectives、objective_cycles、objective_dispatches、turn 的目标/周期/任务字段 |
| 046_operability.sql | 09 | transport_attempts、qualifications、deployment_operations、operation_credentials、operation_requests、operation_request_keys |

05/06消费044，10/11消费046，不回改已交付迁移。数据库按 `PRAGMA user_version` 跳过低号迁移；因此不能先创建046再补045，也不能让下游只因自己缺表而修改旧SQL。

合并 M16 后若尾号冲突，**在运行任何 M17 迁移前**，按最大已部署编号 N 把这四个逻辑槽整体映射至 N+1…N+4，作为限定设计维护同步 M17 的规范、任务路径、卡片和对应哈希。没有冲突不重编号。任何已经运行的物理迁移都不允许这样重排；需另行追加兼容迁移设计。具体字段/约束以 continuity §4.1 与 operability §4.1 为准。

## 4. 每个任务如何工作与验收

读 AGENTS.md、plan/README.md 第6节、该任务引用的规范全文和完整任务卡后，依次执行：

```bash
uv run python tools/harness.py doctor
uv run python tools/harness.py status
uv run python tools/harness.py task next
uv run python tools/harness.py task show M17-01
uv run python tools/harness.py task start M17-01
```

将示例中的 M17-01 替换为本次依赖已满足的任务。先运行本卡验收看失败，再在允许路径实现并补真实正反例。单项结束运行：

```bash
uv run python tools/harness.py accept M17-01
```

不要绕过 start 的基准记录、accept 的真实命令或旧测试。规范不清、与验收冲突或同因连续失败5次时，停止受影响实现，在 `plan/journal/` 记命令/错误/条款/建议，并 `task block <ID> --reason ...`。允许继续不依赖它的工作。冻结设计本身有问题时先做获授权的设计维护，不能猜接口、削弱测试、改计数或直接写 state。

四类证据须分别报告：

- **基线红测**：原25例在 c538768 上24失败/1通过；两条宿主路径专项另计。实现后需逐项真实转绿，不改红测定义。
- **设计/完整性工具**：verify_plan 检查46个登记项的规范/任务/精确节点；52个工具用例和原风险/合成live校验可在尚未实现生产功能时通过，不能充抵业务行为。
- **未来离线行为**：`run_contracts.py --all` 必须逐节点执行78个精确节点、至少81个参数化用例；缺模块/节点、skip/xfail或空收集均失败。协议子进程夹具必须明确标记，只证明接线，不证明真实模型能力。
- **真实能力**：原C10独立 witness和额外四格 witness 均实际运行，再校验报告/一致性快照；缺 CLI、模型、Docker、产品或预算即 blocked，不能用离线结果代替。

设计与执行入口：

```bash
uv run python plan/evidence/m17/verify_plan.py
uv run python plan/evidence/m17/run_contracts.py --plan
uv run python plan/evidence/m17/run_contracts.py --task M17-09
uv run python plan/evidence/m17/run_contracts.py --all
```

前两条只验证映射/展示计划。后两条才执行行为测试；第3条示例仅用于09实现后，本卡其他验收和旧回归仍需运行。只有12项任务均done、完整旧回归与所有真实门实际通过后才能 `uv run python tools/harness.py gate M17`。

## 5. 部署与真实验收顺序

用冻结 manifest 和固定资源在专用临时工厂运行 runtime/machine qualification（`--manifest`），不依赖生产 serve 或生产 root 已初始化。offline资格仅用于local-test；resident实际使用的运行时/产品需要live资格，可能收费时必须给显式许可、预算和可强制的费用上界。

然后可信管理面执行 init plan → 审查冲突/要求 → 同一摘要 apply → 建立宏观目标/固定领域标准并一次授权 → 运行受限外部凭据的serve预检/启动流程。具体语法见 operability §3 和 CLI help；init不自动授权、不代签、不发业务任务。平台源码/adapter/声明/CLI/模型摘要变化使资格失效，最终验收前必须刷新匹配版本的live资格。

08需先运行 cases.yaml 的原C10 witness及 verify_live.py，再运行 operability-cases.yaml 的四格 witness和 verify_plan.py --live-index；两个live节点都不可省略。四格入口固定如下，实际领域和runtime标签在witness开始前固定：

| | Runtime A | Runtime B |
|---|---|---|
| 领域A | help_only | skill |
| 领域B（至少一个领域非媒体） | skill | help_only |

先做help_only两格，再做skill两格。每格独立外部操作者会话、原生CLI会话、凭据和工厂根，不共享业务文件/历史；help_only不加载skill正文。每格至少四周期、有idle后新机会、两个从不同真实资料导出的工作、失败后调整；report与index记录相同entry_mode。资料源和领域标准可预置，不能预写候选、派工或业务行动序列。

历史PR1的四个必选Machine库存见closure.yaml的required_live_products，每个均需本次witness的真实调用、产物和产品资格；缺资源不得改名或用自写脚本冒名通过。整次witness固定同一平台源码摘要；资料/治理业务变化可被追踪，但不得途中补平台源码。观察、故障注入、合法人类事项转交均记录；外部代编排、业务修改、代签或手动重试使对应格失败。

## 6. 设计维护的有限授权

默认不修改 protected.lock 保护的 spec、计划、证据、旧验收或 harness。当前已冻结的维护例外只有：

- 04按continuity §5.9对R01原池测试单一节点/专用夹具做更强的真实两job生命周期证明，保留节点和隔离意图，新增独立反例；不得只改期望值或改其他旧测试。
- 12按任务paths维护本次CLI/skill对应使用文档，不能扩展为重写架构、规范或验收。
- M16完成整合且确有编号冲突时，按第3节在M17首次运行前整体重映射四份新迁移。

这些维护都需先审阅差异，仅同步授权文件哈希并lock --verify，不重登记无关改动。用户对本次设计交付明确指定的 `lock --update`、提交和推送，不是以后实现时可随意刷新锁/提交的长期许可。没有新授权时仍遵守AGENTS.md和任务卡；业务Agent更不拥有设计维护或平台源码修改权。
