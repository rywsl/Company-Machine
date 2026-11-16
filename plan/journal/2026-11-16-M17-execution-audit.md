# M17 执行链审计：操作工、服务网关与可恢复计量

- 范围：冻结提交 `c538768` 的 M0–M15 已实现路径；M16 只有冻结设计在基线内，M16 实现不在本次审计范围。没有修改生产源码。
- 环境：隔离 worktree，Linux，CPython 3.14.2 free-threaded；`uv sync --frozen --offline` 使用已有依赖。HTTP 上游全部为本地进程，未请求付费服务。
- 六项测试均断言应有行为，基线预期失败，是诊断证据而非通过报告。正向对照中的服务命令替身只用于隔离 E03、E05；E01 刻意使用**真实安装的 factoryctl**。
- 临时目录不能放系统 `/tmp`：基线 local provider 会误改该宿主路径，造成假诊断。本轮统一放在 worktree 的 `.factory/m17-repro/` 下。

复现：

```bash
uv run --offline python tools/harness.py pytest plan/evidence/m17/repro/test_execution.py --basetemp="$PWD/.factory/m17-repro/execution"
```

实际汇总：`total=6, passed=0, failed=6, errors=0, skipped=0`，退出码 1。每一项都命中下文写明的目标断言，非收集或夹具错误。

正向对照：

```bash
uv run --offline python tools/harness.py pytest \
  tests/acceptance/m9/test_m9_03_gateway_runtime.py::test_line_runs_service_station \
  tests/acceptance/m4/test_m4_03_gateway.py::test_openapi_allowlist \
  tests/acceptance/m2/test_m2_07_egress.py::test_model_proxy_meters \
  --basetemp="$PWD/.factory/m17-repro/controls"
```

实际汇总：`total=3, passed=3, failed=0, errors=0, skipped=0`。这只证明这些既有场景成立，不能证明以下真实接线场景成立。

## E01：真实 factoryctl 缺服务命令（已复现的实现缺陷）

- 契约：`spec/system/gateway.md:83`、`:179` 要求沙箱内真实 `factoryctl service {openapi,submit,poll,fetch}` 完成服务交互。
- 现场：E01 首先直连本地假服务 `/openapi.json`，HTTP 200；完整产线随后使用 `Context.open` 安装的、未修改的 `src/mao/ctl/factoryctl.py`。结果 `job=DEAD`，错误 `SERVICE_UNHEALTHY`；真实 stderr 为 `argument cmd: invalid choice: 'service'`。
- 根因：`src/mao/ctl/factoryctl.py:528` 的 parser 不登记 `service`，但 `src/mao/gateway.py:144`、`:153`、`:271` 都调用它。`_health` 把命令缺失归类为服务不健康，掩盖了根因。
- 覆盖缺口：`tests/acceptance/m9/test_m9_03_gateway_runtime.py:30` 的 `FAKE_CTL` 替换了全部 `service` 子命令，旧服务产线测试因此可通过，未验证实际安装的工具。
- 自治影响：厂长提供了合法的服务型机器和产线，生产仍停在第一步；外层 agent 必须发现并提供临时代码才能继续。
- 修复约束：补完整真实沙箱内命令及错误分类；至少用真实安装工具、本地 HTTP 服务跑完整生产线，同时保留权限、出网和原生脚本兼容边界。

## E02：默认导出无关 OpenAPI 操作（已复现的实现缺陷及契约缺口）

- 契约：`spec/system/gateway.md:168`–`:175` 明确 `allow=None` 不能导出整个 API，只能导出长任务相关操作；显式 allow 必须限制工具范围。
- 现场：相同 OpenAPI 文档中，显式 allow 得到 `submitVideo`、`pollVideo`，作为正向对照；传 `allow=None` 实际还返回了无关危险 POST `wipeEverything`。
- 根因：`src/mao/gateway.py:57`–`:72` 对 `None` 不过滤。另一个设计问题是公开函数只接收 `document, allow`，没有长任务路径，无法可靠推知哪些操作属于当前任务，需在 M17 明确安全的上下文传递或默认空集语义。
- 自治影响：当工具清单实际被接给 agent 时，会扩大工具授权面；本复现只证明列表生成错误，不声称当前已完整接通或发生了真实调用。
- 修复约束：明确工具来源、显式白名单、默认失败关闭以及无关操作不可调用；保留现有安全策略，不能仅靠系统提示让模型不选危险工具。

## E03：服务工位绕过声明的 agent 操作工（已复现的实现缺陷）

- 契约：`spec/system/gateway.md:189` 要求 agent 工位启动 operator CLI；脚本工位才不用 agent。
- 现场：测试将服务工位 runtime 改为 `fake-agent@1`，映射到写启动标记的可执行脚本；服务交互采用既有受控替身以隔离 E01。产线 `SUCCEEDED`、job `SEALED`，但启动标记不存在。
- 根因：`src/mao/runner.py:391`–`:393` 直接进入 `_execute_service`；`:489`–`:508` 调网关后直接转 `GATING`，没有创建或运行声明的 operator，也没有 operator transcript。
- 自治影响：结果外观成功，但产线没有执行交给操作工的任务；调用者无法从成功状态判断实际分工成立。
- 修复约束：在服务工位正确接通 operator、任务书、受限工具、预算、失败和 gate；测试须验证真实 operator 可观察地工作、失败不被封存，不能把仅提交服务当作 agent 完成。

## E04：模型代理重建后预算累计丢失（已复现的实现缺陷）

- 契约：`spec/system/egress.md:306`–`:321` 要求每个 job 按 `model_usage` 的 `sum(cost_usd)` 判断预算，超限后写 stop，之后请求拒绝。
- 现场：真实本地 HTTP 上游每次报告成本 0.6，job cap=1.0。第一次后重建真实 ModelProxy，第二次后数据库合计 1.2，但 `budget_stops=[]`；第三次请求仍返回 HTTP 200。
- 根因：`src/mao/egress.py:1001`–`:1007`、`:1024`、`:1099`–`:1103` 用进程内 `_spent` 算累计值，重建即归零；`:1188` 只记录单笔，没有用持久合计决策。
- 自治影响：服务重启或进程内代理重建后同一任务可以继续透支，直接影响长期无人值守成本边界。
- 修复约束：持久化合计和 stop 必须可在重启后恢复，并考虑多请求并发的事务一致性；验证每笔可追溯、达到既有的“严格大于”上限语义后后续请求拒绝，不封存。

## E05：已知外部任务完成后，取回失败会重复提交（已复现的契约缺口）

- 已有边界：`gateway.md:179`–`:189` 有 submit、poll、fetch 与 `service.submit` 事件，但没有规定跨 attempt 续接外部任务。`durable.md:189`–`:201` 在进程中断后按重试策略新建 attempt。因而这是**现有设计缺口**，不声称旧规范已经保证 exactly-once。
- 现场：服务工位配置 `failed=1, mode=resume`。假服务已完成，首次 fetch 受控失败；第二次成功后 run `SUCCEEDED`，但本地假服务收到两次提交，DB 有两条 `service.submit`。模拟的首次失败标记确实存在。为了隔离 E01，使用原 M9 测试的 factoryctl service 替身；不能将它描述为已验证真实服务生产链。
- 根因：`src/mao/gateway.py:144`–`:172` 每次 `run_recipe` 都提交，没有从已记录的 task_id 恢复；`src/mao/runner.py:1227`–`:1237` 把失败转为新的 attempt。
- 自治影响：下载或轮询抖动可能重复付费、产出不同结果或重复触发外部副作用，要求外层人工定位和重试。
- 修复约束：已获 task_id 的可恢复机器必须保留可验证的任务身份与输出阶段，优先恢复 poll/fetch；对发送成功但响应丢失的未知结果要以 outcome-unknown、对账或供应商幂等机制处理。不能对不支持幂等或查询的供应商承诺普遍 exactly-once。

## E06：操作工读不到已提供的实时预算（已复现的实现缺陷）

- 契约：`spec/system/egress.md:130`–`:136` 规定读取 `/factory/ctl/budget.json`；`:319` 要求计量后更新预算文件。
- 现场：手动提供有效 `job.json` 与 `budget.json`（spent 0.6、remaining 0.4），真实 `factoryctl budget --json` 仍返回 `spent_usd: null` 和“M1 结束后结算”。这是隔离客户端读取的测试，不证明代理已正确产生文件。
- 根因：`src/mao/ctl/factoryctl.py:106`–`:119` 固定返回 M1 时代的空余额；无 `--json` 时直接退出 2。`src/mao/egress.py:1188`–`:1202` 也只写 usage 表，尚未在此测试中验证预算文件和 `model.metered` 的发布路径。
- 自治影响：操作工无法根据实际余额决定继续、降级或求助，厂长无法据此形成可解释的持续预算反馈。
- 修复约束：明确并兼容 M1/M2 命令输出、文件不存在及实时文件更新的优先规则；测试从真实模型代理计量到真实 factoryctl 返回值及预算事件的完整链路，覆盖重启和并发。

## 诊断边界与统一设计输入

- 本报告的 E01、E02、E03、E04、E06 为相对于所引规范的实现缺陷；E05 是已实测的能力/契约缺口。Grok 相对 URL 取回失败是历史会话中外部临时适配器的错误，不是冻结基线内的 `mao.gateway` 缺陷；此处不重复作为本仓库复现。
- 不把五秒模型代理上游超时或历史 CONNECT 修改的根因当成本轮已复现结论；这两项需单独受控实验才能确定。`runtimes.md:230` 起对 SSE 用量有更新规则，不能直接把旧 `egress.md` 的“流式加总”作为唯一依据。
- 统一目标不是为六个例子逐条打补丁：应同时定义服务工具权限与 agent 执行边界、可恢复任务状态/未知结果、持久预算及操作工反馈，并用两个以上无人接管的生产周期和失败恢复证明闭环。保留工厂现有预算、审批、审计与 Gate 约束。

## 历史会话对照（只读，脱敏）

- 对照文件：`/mnt/d/Desktop/m15-session-trajectory/m15-session-trajectory/cloud-export/transcript.json`；SHA-256 为 `39b189532e0d252de5b5ac12e6c4071df66607fccc62f95cb2c5faa327720fc7`，共 1987 条记录。只核验用户消息、可见答复、工具参数和工具结果；没有读取或复述 `thinking` 字段。原文件包含敏感信息，不把它的正文或凭据复制进本报告。
- 第 46673 行：用户明确要求通过工厂与厂长交流，由厂长编写产线，operator 与 machine 协作。这是用户目标的历史证据。
- 第 53470 行：外层临时适配器增加厂长的工具循环及 `_dance`，并在自写操作工中调用旁边的 MiniMax 脚本。这证明当时有外部开发和接线；它不是本报告 E03 的基线复现，二者各自独立。
- 第 40570 行：部署配置补丁把 `audit_every_s` 和 `canary_every_s` 设置为 `None`。这是当次环境中关闭这两项周期检查的证据，不证明本报告的本地服务试验依赖相同配置。
- 第 70453 行：历史 Grok 服务返回 `status=done`，结果 URL 是 `/v1/videos/.../content` 的相对路径；第 70541 行的外部脚本补丁将此类路径拼接来源地址。其失败发生在当次新增适配器，不能归因到基线 `mao.gateway`；它为 E05 所描述的“已完成任务不应因取回故障重复提交”提供真实场景线索。
- 第 70730 行的轮询输出显示重新运行后 `SEALED SUCCEEDED`；第 70780 行分别记录 run `SUCCEEDED`、job `SEALED`。这能证明当次工厂任务进入成功终态，不能单独证明真实 OpenMontage 集成、长期无人介入或质量指标已经成立。
