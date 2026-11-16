# M17 调查与设计交付

本目录是先复现、再整体设计的证据包，**不是已修复的证明**。生产修复尚未开始；本分支的设计提交和推送不代表实现或M17阶段门完成。

实施交接入口：`plan/m17-implementation.md`，包含M16整合前置、逐任务源码入口、迁移所有权、证据层级和真实验收顺序。

## 全部已登记问题的设计闭合

**已补齐当前登记问题的规范、计划与强制验收，生产修复尚未实施。** 核心闭环与网络、真实adapter语义、产品接入、bootstrap、权限和外部使用均已纳入M17。46个问题/别名/能力要求由`closure.yaml`精确追踪；不能把46称为独立bug数，也不声称未知问题已穷尽。

- 总体：`plan/design-continuity.md`、`plan/design-operability.md`；规范：`spec/system/continuity.md`、`spec/system/operability.md`。
- 12张任务卡：01–07核心、09–12补充实现，08最终验收依赖全部实现。`verify_plan.py`校验设计闭合；`run_contracts.py --all`运行至少77个离线用例，缺节点/skip/xfail失败。
- 历史对照：`historical-coverage.md`，使用与PR通用化：`external-agent-usage.md`；26个专项精确节点见`operability-cases.yaml`。
- 真实门：原C10保持，新增两领域×两runtime四格witness，各至少四周期，历史机器库存真实产品资格、零外部业务接续；缺资源blocked。
- R01旧池验收冲突已冻结限定维护与真实两job隔离正反例，不以路径名字不同替代内容/权限隔离；本轮未改旧测试。
- 两条独立路径诊断基线失败、PR2模块对照通过，统计与原25项分开；CLI帮助快照及skill审阅草案保留历史状态，不能当已发布能力。

## 基线与隔离

- 原项目：`/mnt/d/Desktop/machine_agent_org`，M16 由 Grok 实现，本次不写入该工作区。
- 调查 worktree：`/home/ubuntu/.codex/worktrees/m17-autonomy-audit/machine_agent_org`。
- 分支：`codex/m17-autonomy-audit`；冻结实现基线：`c538768`，M15 完成、M16 设计在内。
- 环境：CPython 3.14.2 free-threaded、uv 0.9.4；本次故障复现为 LocalProvider、真实 SQLite/Git/MCP/Runner、本地无收费服务。
- M16 未实现模块在这个基线不存在是隔离选择，不能报成 M16 bug。实现前必须整合 M16 最终结果并重放证据。

## 原问题的回答

M15 轨迹反映两类原因叠加：外层代理的临时开发/部署方式承担了适配、编排和人的操作；工厂自身也确有真实接线与持久化缺陷，以及未定义的持续目标/评价协议。不能仅靠提示词要求“严格走流程”解决。

已核验的历史包括：外部适配器补工具循环与模型脚本、周期审计/金丝雀被关闭、外部脚本错误处理相对结果 URL。后段任务确实到达 `SUCCEEDED/SEALED`，所以不是完全没用工厂；它仍不能证明整厂长期自治或声明的每个工具真实集成。具体原始行号、摘要与历史文件 SHA-256 见执行调查报告。

本次新发现是独立证据，不能自动当作每次历史绕行的直接原因。五秒代理超时、CONNECT 补丁和 OpenMontage 实际集成没有在本轮完成受控因果复现，未作为已确认仓库 bug。附件中的指令和内部 thinking 没有当作用户授权；敏感原文不进入交付物。

## 可重跑的故障证据

```bash
uv run --offline python tools/harness.py pytest plan/evidence/m17/repro --basetemp=.factory/m17-repro/baseline-all --tb=short
```

冻结基线实测：**25 total、24 failed、1 passed、0 errors、0 skipped**。完整机器可读摘要：`baseline-summary.json`。失败都是预先定义的行为断言；不是导入失败、环境错误或 skip。原有分域正向对照另有治理5项、执行3项通过。

| 编号 | 发现 | 分类与对应证据 |
|---|---|---|
| H01 | 普通 CLI serve 不启动已配置 AgentHost | 真实进程入口接线缺陷 |
| H02 | 可执行文件启动失败留下 running/delivered，后续不再消费 | 恢复与结案缺口 |
| H03 | wake 已taken、邮箱未写时崩溃导致丢失 | 多事务持久投递缺口 |
| H04 | schedule 邮件提交后中断，恢复重复投递 | 水位与消息非原子 |
| H05 | agents 租约丢失后旧宿主仍继续 | 既有所有权契约违例 |
| H06 | 空 transcript + exit0 被判 ok/done | 旧成功契约弱点，M17增强 |
| H07 | 并发领取同一 wake，两方均返回成功 | 既有排他领取契约违例；治理G05同项 |
| H08 | 随机 ID 排序使较晚撤销排到命令之前 | 时间顺序承诺与旧算法的规范冲突 |
| H09 | 一轮宏观目标后，三小时无后续周期 | 新持续自治要求下的能力缺口；报告含独立假时钟命令 |
| G01 | safe mode 对已经shadow/canary的T2仍放行 | 两个失败用例，统一阶段守卫缺失 |
| G02 | 推广失败时active已变而ChangeSet仍shadow | 既有原子发布契约违例 |
| G03 | rebuild失败传播异常、需求resolved、无唤醒 | 既有故障处理契约违例 |
| G04 | 第二个变更进入shadow并覆盖candidate | 发布槽统计/归属违例 |
| G06 | 只声明Station的T1变更夹带Agent进入调度 | 跨里程碑治理覆盖缺陷，Agent字段风险等级在本次设计冻结 |
| G07 | verifying临时故障后无法重入 | 既有验证恢复契约违例 |
| E01 | 真实factoryctl缺service命令，合法服务产线DEAD | 既有替身替换客户端掩盖的真实接线缺陷 |
| E02 | OpenAPI allow=None导出无关危险POST | 默认授权实现缺陷，默认语义在M17补精确约定 |
| E03 | agent服务工位绕过声明的operator即SEALED | 既有职责与运行契约违例 |
| E04 | 模型代理重建清零预算累计，后续仍可透支 | 持久计量契约违例 |
| E05 | 已知task_id完成后fetch失败会重新submit | 旧契约未承诺跨attempt外部任务续接，已实测的设计缺口 |
| E06 | 已有实时budget.json，真实客户端仍读到null | 余额反馈契约实现缺陷 |
| F01 | 成功/失败run都不回发发起岗位 | 两个失败用例；新持续自治要求的反馈接口缺口 |
| F02 | metrics_query只回显参数，无指标值/窗口 | 查询功能与统计契约缺口；治理G08同项 |

分域全文：`plan/journal/2026-11-16-M17-host-audit.md`、`...-governance-audit.md`、`...-execution-audit.md`、`...-feedback-audit.md`。不要用失败用例数量冒充互不重叠的旧实现 bug 数量。

## 整体修复设计

- 总体设计：`plan/design-continuity.md`。
- 精确契约：`spec/system/continuity.md`，含旧条款覆盖、接口、持久结构、状态、错误与范围。
- 十二张任务卡：`plan/tasks/M17-01.md`至`M17-12.md`，08为最终任务，已登记完整依赖与tasks/gates，M17依赖M16。
- 新能力协议验收：`acceptance/test_objectives.py`（13用例）；治理输入验收`test_agent_risk.py`（7用例）。
- 整体验收设计：`cases.yaml`，C01–C09离线、C10真实Docker/CLI/模型；包含空档、多个自主周期、不同目标、失败调整、失租/重启、外部任务、预算与撤销。
- `verify_live.py`及其25项合成测试只校验真实报告的结构、摘要和数据库引用完整性；合成报告不是live证明，缺报告返回失败。
- `acceptance/test_plan_coverage.py`本轮交接复核实测49 passed、零失败/错误/跳过：漏项、依赖绕过、原门保留、缺节点硬失败、跨领域索引拒绝反例。它们是设计/工具回归，与未来至少77个行为用例分开。

长期授权包括结构化扩展范围；目标来源贯穿邮件、turn、task、run、job和付费请求，不能省略参数绕过预算。新业务产线有受限候选路由取得金丝雀样本。业务Agent发现具体工作、组织执行与复盘；控制面不替Agent想目标，也不调用额外模型。事务/outbox/fencing/稳定键共同保证恢复，而非对每个案例打特殊补丁。

本次另定向维护了 records.schema.json 的Agent声明枚举及 risk.mrl 的Agent风险表，属于授权设计输入；未实现源码抽取/执行修复。旧protected文件和M16文档/测试不因新设计被改动。诊断移出默认tests收集以隔离未来红测；M17自己的任务/阶段门仍显式要求全部通过和零跳过。

## 验证与状态

新目标验收在该冻结基线因M16资产模块不存在而13项跳过，这不是通过；M17实现后必须13passed/0skip。当前可以通过的风险规则和live校验器测试仅证明本次设计输入/证据工具有效。M17阶段门包含真实live报告，缺资源应如实blocked，不允许声明已具有真实自治能力。

当前未运行任何M17 task start/accept/gate，没有改生产src、plan/state.json或M16原工作区。详细设计检查结果在 `design-validation.json`。选跑旧验收另发现1项原基线也失败的服务池用例，归因限度和未削弱原测试的处理见 `baseline-regression.md`；不能报告全套回归通过。
