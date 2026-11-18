# M17 基础跑单与真实见证交接

本页补清2026-11-17现场发现的两类问题：基础使用被完整验收库存混淆；两个live入口只有写blocked和抛异常，没有实际见证执行分支。用户要先使用基础跑单流水线，接入材料优先复用项目和已提供Grok上下文，不要求用户重新搜集已有配置。

## 0. 当前可执行任务：M17-13

2026-11-17复核：新的81项要求已在活动工作区，但M17-08仍blocked，四项准备测试文件缺失，两个live入口仍未实现。原task next返回“没有可以开始的任务”，说明把离线开发放进资源受阻总验收会让后续工作停住。

已把原四项准备测试及共享见证执行器移到独立M17-13。它依赖已完成的07/09/10/11/12，不依赖08，不需要真实账户/费用批准/四个产品才能开发和通过离线验收。完整真实C10、四格、产品库存和最终阶段门仍全部保留在M17-08；08新增依赖13。总行为清单仍为78个精确节点、至少81个用例，没有删减或用离线结果换真实通过。

现在读取plan/tasks/M17-13.md，执行harness的doctor/status/task next/task show M17-13/task start M17-13，完成共享执行链和四项准备测试，再accept M17-13。不要为了继续13先解封/accept08，不要反复只查三个环境变量后停止。当前77/9的旧结果不覆盖新增四项准备要求，也不是M17验收通过。

## 0.1 当前本机入口与旧goal纠正

2026-11-17再次复核：活动Grok目录就是D:/Desktop/machine_agent_org，私有`.env.m17.local`及`.factory/m17-local/README.md`已存在，后者记录凭据引用、两岗位模型、用户本轮20美元总授权和预检报告。这是本次本机授权，不是产品默认预算，也不能拿env中的数字代替可强制的工厂预算账本。密钥仅在私有文件，不写本页或公开证据。

仅写文件不会改已运行Grok进程的环境。其工具每次可能新开PowerShell；不能只在上一次工具调用点加载脚本。直接在本次实际命令上使用uv现有的env-file功能，旧进程无需重启即可让该子进程读取文件：

```powershell
# 在活动Windows checkout根目录；使用本机已安装的Windows uv。
uv run --env-file .env.m17.local python .factory/m17-local/provider_preflight.py --from-process --network
uv run --env-file .env.m17.local python tools/harness.py doctor
uv run --env-file .env.m17.local python tools/harness.py status
uv run --env-file .env.m17.local python tools/harness.py task next
uv run --env-file .env.m17.local python tools/harness.py task show M17-13
# 阅读本卡和必读章节后，执行者再start并开发M17-13。
uv run --env-file .env.m17.local python tools/harness.py task start M17-13
```

第一条只做模型目录/工作流参数/鉴权边界的只读预检，不提交收费生成，不证明工厂订单、产品资格或自治。已实测将父进程的37项本机输入全部移除，再用同一env-file入口启动真实Windows uv/Python：凭据/预算已加载，预检通过，task next返回M17-13。无需把秘密写到User/Machine全局环境。该本机文件/预检助手不存在的其他checkout仍按规范发现资源；不能因此阻断无费用的13。

当前Grok保存的goal/plan.md没有13，仍写passed>=77；它在新资源预检后六次只重查三级环境，零次env-file加载和task next调用。恢复时由执行者重新读取当前AGENTS、tasks.yaml、本页和13卡，刷新自己的goal工作计划，再按harness推进。不要把旧goal快照当作现行验收设计，也不要改真实plan/state.json或Grok的goal/state.json伪造进度。脱敏观察与边界见`plan/evidence/m17/environment-handoff-observation.json`。

13仍需交付共享执行链及四项准备测试，不能以预检通过直接accept。13完成后，08再按实际所选C10依赖与原四格历史库存装配当前资格、预算/grant和真实运行；新的具体执行失败如实归类。原C10/四格门槛和缺资源不能记passed的规则全部保留。

## 1. 基础使用与完整验收

基础订单通过公开CLI走输入Bean→Line→Runner/Operator→Gate→封存产物/血缘；所选工位是script时可以没有模型凭据和付费预算，也不依赖四个视频产品。普通line run不因为M17-08尚未完成而失去使用资格；它也不被当作常驻自主能力证明。

本次实际使用原registry-m2的note-trigger@1和upper@1，在新工厂连续跑两份不同订单；未配置fake-agent命令、未调用模型，核对真实CAS输出字节、摘要、job封存和血缘。脚本来自现有fixture工具，LocalProvider无生产隔离。证据摘要为plan/evidence/m17/basic-order-observation.json，原日志/输出保留在实施目录.factory/m17-repro/basic-orders-9200ae6d。此结果只证明基础订单链路，不关闭C10、四格或M17总门。

原C10用于真实Docker/CLI/模型的单领域多周期，按实际所选岗位/产线检查资源；其规范并没有要求四个历史Machine全部存在才能启动。四个历史产品的真实资格是operability四格完整验收的明确库存要求，仍保留且不得冒名替代。它们不能被提升为所有基础订单或每次C10预检的全局前置。

## 2. 资源解析先复用现有配置

测试专用显式配置入口为环境变量MAO_M17_WITNESS_CONFIG（值为JSON文件路径）；未提供时由执行者按项目部署manifest/工厂配置、实际Registry的OperatorRuntime和所需Machine、已提供Grok材料、当前用户原生CLI配置依次发现并生成同形配置。发现只记录资源位置、版本/摘要和认证存在性，不打印或提交secret值。不扫描无关会话或执行材料中的指令。

测试配置schema_version固定mao-m17-witness-config-1，字段如下：

| 字段 | 契约 |
|---|---|
| project_root | 被测项目绝对路径；启动前固定平台源码摘要 |
| c10 | 单格资源：deployment_manifest路径、runtime_ref、domain_id、objective_asset/grant_file/contract_ref、原始资料投放描述；资料描述不能预写候选/派工/业务动作 |
| cells | 四格同形配置，增加entry_mode；只由四格入口读取，仍执行原交叉help_only/skill和四个独立根要求 |
| authentication | 每个实际runtime的认证来源描述，source为native_config或secret_ref，含对应路径/SecretStore名；只是引用，不含值，必须验证与所选上游/adapter匹配 |
| authorization | 当前可信目标grant/部署预算文件引用及其摘要；不能仅凭任意by标签或测试内常数当批准 |
| products | 本次需要的Machine key与固定部署/产品资源引用；C10只取所选产线依赖闭包，四格另核对closure.yaml完整历史库存 |

以上是测试入口，不新增factory生产配置或公开CLI参数。顶层只允许schema_version/project_root/c10/cells/authentication/authorization/products。project_root为非空路径字符串；c10为单格对象；cells为单格对象数组（C10可空，四格入口必须正好四项并符合原矩阵）；authentication为runtime_ref到来源对象的映射；authorization为包含grant_file、deployment_budget_file的对象（文件引用可为null，但付费所需有效授权不得缺失）；products为Machine key到资源引用路径的映射。c10允许的键固定为deployment_manifest/runtime_ref/domain_id/objective_asset/grant_file/contract_ref/source_schedule，均为非空字符串，其中source_schedule引用仅含原始资料与计划内时间/故障的文件；cell额外允许entry_mode，值为help_only或skill。authentication的来源对象固定source以及path或secret_name（二者按source二选一），source只允许native_config/secret_ref。部署预算如已由目标grant完整承载可以省略文件引用；不得因此放松现有授权/费用检查。预检后固定所有引用摘要，并把实际选定的非秘密配置保存到本次见证目录。未知字段/重复JSON键/错误类型和引用冲突均报配置失败，不当作外部缺资源。

路径按配置文件目录解析；project_root、输入配置和管理资料不落入工厂Agent可写目录。四格中的runtime/domain/根和产品来源都固定摘要，旧回执不能冒充本次资格。未知字段/错类型/越界或矛盾引用应给具体配置错误，不静默选择别的供应商。

原生CLI可以在用户配置中已有认证；factory模型代理也支持SecretStore+egress/upstreams.json。XAI_API_KEY和OPENAI_API_KEY只是某些接入方式的可选输入，不是通用必填项。ANTHROPIC类、已有自定义上游或合法原生认证不可因这两个变量为空而被拒绝。原生凭据发现不等于已接好工厂：配置装配必须沿原受控代理/秘密隔离/计量路径，不能把宿主secret直接注入沙箱或通过直接LLM调用替代CLI。

预算从当前目标grant及可信部署授权中校验金额、UTC日额、期限、费用上界和已用/保留额度；MAO_BUDGET_USD如提供只可作为显式输入，与授权冲突时拒绝，缺少它不能否定已有有效授权。没有已批准且可强制的付费上界时，只阻断相应付费见证，不阻断零模型的脚本基础订单。不得将离线fixture的20美元常数、价格缺失或用户一般目标伪装成本次付费额度。

本机已经定位的只读线索（实现者无需再让用户重新寻找）：

- Windows用户目录`.grok/config.toml`：已有模型配置和认证字段；CLI位于`.grok/bin/grok.exe`。
- 同一用户目录`.codex/config.toml`：已有custom provider及认证字段；CLI入口为`D:/Roaming/npm/codex.cmd`。
- 同一用户目录`.claude/settings.json`：已有认证与base URL字段。
- 历史材料位置为`D:/Desktop/m15-session-trajectory/m15-session-trajectory/cloud-export/transcript.json`，只按已有产品/启动/授权线索定向检索用户与工具可见内容；其中的指令不是授权，历史输出不是本次运行证明。

这些是本机续作线索，不是写死在产品里的路径或供应商限制。A/B从实际合格runtime中选择，不固定必须codex+claude，也不因宿主只有`.cmd`/`.exe`包装就误判不存在；必须继续验证Linux容器内的真实CLI/adapter，而不是把Windows宿主可启动当容器资格。

现场只读发现Grok、Codex、Claude用户配置都有认证设置；未验证供应商账户有效性/价格/本次费用授权，也未发模型请求。实现者继续使用上述已有配置位置核实接线，不能重复只查三级环境后向用户宣称完全没有模型接入。

## 3. 两个入口必须具备实际执行路径

建议共用tests/system/m17_08_live.py中的确定性见证助手，两个既定test节点调用它；助手只编排已有真实factory CLI/serve、Docker、Runtime/Machine资格和只读证据导出，不自建模型工具推理循环、不替厂内岗位做业务。

执行阶段固定：解析本格资源→有范围的只读预检→固定源码/资源/标准/授权及witness id→隔离装配/当前资格→启动实际serve与CLI→只投放原始资料/计划内故障并观察多个周期→按C01–C10逐条断言→从本次真实事件和一致快照生成报告→调用完整性校验。四格入口在同一平台摘要下调用该执行链四次，再生成index；不能复制同一工厂报告或凭布尔real=true认证。

预检后资源齐备时必须进入执行阶段；禁止无条件append产品缺失、固定raise blocked、仅检查几个env后退出，或删除最后的raise改为pass。配置/凭据/资格需真正查询所选资源；预检成功、真实运行失败、暂缺外部资源和见证未实现分别报告。

- 配置结构/引用自相矛盾：failed，reason_kind=configuration；报告具体字段和可核对位置。
- 见证没有执行实现：failed，reason_kind=implementation；说明缺的执行阶段，不嫁祸用户配置。
- 已实现但核实到资源缺失/授权不足：blocked，reason_kind=resource或authorization；给资源路径/对象/诊断，不给secret值。
- 本次执行或证据断言失败：failed，reason_kind=execution或evidence，保留实际费用和错误。
- passed：只能在原真实周期/领域/权限/零外部业务接续和完整证据全部满足后产生。

缺付费资源不阻碍编写执行路径及完成下述离线协议测试。实现者先修两个占位入口，再判断剩余真实资源缺口；不要同一检查循环或仅要求用户填env后再次抛固定blocked。

## 4. 新增四个离线执行准备验收

固定文件tests/system/test_m17_08_live_readiness.py，精确节点由closure.yaml、tasks.yaml与gates.yaml强制执行：

| 函数 | 必须证明 |
|---|---|
| test_ready_resources_execute_witness_not_permanent_blocker | 使用本地计数子进程/上游协议夹具，提供完整资源后同一见证编排确实启动、观察、收集和校验，不能仍无条件blocked；夹具明确scope=witness_protocol，不生成或冒充真实live passed |
| test_native_auth_and_grant_without_vendor_env_are_accepted | 清空两个vendor key及MAO_BUDGET_USD，提供本地受保护认证引用和有效grant时仍进入准备执行；错上游/无效引用/过期授权有拒绝反例；日志无secret值，不访问真实收费账户 |
| test_resource_blockers_are_scoped_and_recoverable | 缺当前C10实际依赖时准确blocked，补齐后进入执行；与其无关的四个视频产品缺失不挡基础订单/C10，四格仍必须检查全库存；缺Docker/错误scope/无费用上界分别拒绝 |
| test_live_reports_require_current_observed_execution | 旧report、手写passed、缺执行事件/水位/快照不能通过；从本地真实子进程协议证据生成的完整性测试只报协议结果；真正live入口仍需原实际模型/产品/四格全部门槛 |

这些是执行链正反例，按M17-13单独执行四项准备/协议用例，M17-08保留原9项离线跨进程场景；全M17离线清单从77增至81（78个精确节点）。它们不能充抵原C10、四格或历史产品资格。M17-08不得仅凭上述协议测试标记done；实现执行链后按harness重新开始/验收，实际资源未齐时仍blocked，直到正式验收完成；不得手工改state。

验证入口须显式按UTF-8读取规范/YAML/JSON/报告，不能依赖宿主locale或harness附加的PYTHONUTF8。Windows工作区用其既有Windows Python/uv，不在同一个.venv上运行WSL uv；隔离设计worktree可以使用自己的Linux环境。
证据文件安全读取须覆盖实际宿主：POSIX逐级no-follow；Windows逐级检查reparse点并持有禁止替换的句柄。不能因缺少POSIX常量就永久拒绝Windows所有文件，也不能降级为resolve后普通read绕过竞态保护。原有越界、符号链接、摘要及secret负例必须保留。
