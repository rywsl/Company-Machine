# M17 已知问题整体关闭设计维护

- 时间：2026-11-16；工作区：m17-autonomy-audit；分支codex/m17-autonomy-audit；冻结基线c538768。
- 本日志阶段的历史授权：修改M17规范和计划，围绕当前发现完整解决；不是开始生产实施。不写M16工作区，不task start/accept/gate，不提交或推送。

## 设计结果

continuity.md与operability.md共同覆盖可靠驱动、所有权、治理、真实服务/操作工、成本/结果/持续目标，以及有界网络、真实CLI私有历史/契约/工具资格、产品身份、声明式非破坏部署、受限外部操作和help/docs/skill。新增09–12前置，08仍是全体12任务的最终验收，不产生核心外的场景补丁链。

closure.yaml逐项固定46个登记项（含别名、能力要求与R01），各有证据级别、规范章节、任务和精确节点。design_baseline_status只描述冻结审计，不替代后续harness任务完成证据。HC01规范引用已纠正为真实宿主装配，涉及最终live的责任也显式纳入08。

R01：原旧池用例在pristine基线失败；其路径不等断言不能单独证明实际内容隔离，与M9端口布局/M12池复用语义有矛盾。§5.9冻结真实两个job、stage/reset、内容/上下文归属与旧handle fencing，限定未来维护原节点夹具并加强断言；本轮未改旧测试或生产代码。R01与独立新池测试都进入精确门，不能漏在日志中。

部署预检接入真实serve，任务路径已覆盖；首次资格可用manifest构建临时环境，不依赖生产serve。签名文件邮箱保留D7、不新开控制端口；持久request_id与业务key分别去重，崩溃后能查询/重放响应。费用继续使用既有账本，不创建非法的非唯一operation外键。受限操作者不能借by标签或本地root回退取得管理权限。

## 本轮实测与边界

- 设计映射：46项、零覆盖问题。
- 设计/证据工具回归：43 passed，0 failed/errors/skipped；缺行为节点在隔离clone中由真实runner/harness执行失败，实施后测试仍有效。
- 执行计划：74个精确离线节点、至少77个行为用例；26个新operability节点、原诊断/目标/核心集成及R01均在内。只是计划，未通过行为验收。
- m17-contracts最低88用例：原45加新43；原目标13项在冻结基线缺M16，不得宣称整个门已通过。
- 缺未来09节点和缺live index的实际命令均exit1；零用例报告不能被当成功。
- doccheck/lint通过；最终lock、format、diff和fast检查结果写design-validation.json。只同步授权文件哈希，不运行lock --update，不登记无关变更。
- 原C01–C10不削减；新增两领域×两native runtime四格live，各四周期、idle/新工作/失败调整、真实历史产品和零外部业务接续；真实witness与只读完整性校验缺一不可。本轮未运行live或付费资源。

最终检查：check --fast通过（lint/format/lock/doccheck、715一致性、完整规则覆盖、严格Registry、4场景）；evidence专项ruff与全276文件格式、git diff --check通过。27个已审阅设计/证据文件定向同步哈希，一致性用例与无关哈希保留。回写摘要后只同步摘要/本日志两个哈希并再次lock --verify。生产源码、旧/M16验收、plan/state.json及M16原工作区未改，无提交推送。


## 后续交接授权

用户随后明确要求运行lock --update、提交和推送M17设计。上述“不提交或推送”和43/88结果是本日志原审阅阶段的快照；最新交接、迁移顺序及49/94门槛见2026-11-16-M17-handoff-audit.md与design-validation.json。本轮仍不开始生产实现、不干预M16。
