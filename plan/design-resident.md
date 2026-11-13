# 常驻机器与常驻 Agent 设计（2026-11-13）

> 设计方根据用户对“常驻 agent”和“常驻 / once 机器”的要求所做的设计与原型验证。**受保护，只能由人修改。**
> 任务是 **M12-01 至 M12-05**（规范 `spec/system/instances.md`、`spec/system/ingress.md`，验收 `tests/acceptance/m12/`）与 **M13-01 至 M13-05**（规范 `spec/system/agents.md`，验收 `tests/acceptance/m13/`）。
> 本文件的编号（G-xx、D-xx）被规范与任务卡引用，不要重新编号。

---

## 0. 结论

用户指出两件事：

1. 能长期运转的只有“重复触发”：每张唤醒票启动一次新的厂长会话，处理完就退出，下一张票不继承对话。长期收集信息、接收不定期消息这类工作因此被打散成许多次会话，状态散落在数据库、事件和文件里，每次再拼成新会话的上下文。上下文应当在 agent 身上，而不是每次由系统重新拼出来。
2. 常驻机器和 once 机器的实现不完整。

逐项核对现有代码，第 2 点比看上去更严重（第 1 章）：`pooled` 从来没有实现，服务型机器在 Docker 下根本没有启动服务进程，常驻机器与入站推送都不存在。

用户的四个选择（2026-11-13）：

| 问题 | 选择 |
|---|---|
| 常驻 agent 的进程模型 | **常驻容器 + 续接会话**：容器长期存活，CLI 的会话目录在持久目录上；每批消息用 runtime 的 `invoke.resume` 在同一段对话里续一轮。上下文在 agent 自己的对话里，由 CLI 原生压缩；进程崩溃不丢上下文 |
| 常驻 agent 的身份 | **新增 `Agent` 资源**：Registry 的新 kind，经 ChangeSet 治理；可以有多个。厂长是内置的一个常驻 Agent（`foreman.mode = "resident"` 时）；复核者仍是一次性会话 |
| 外部推送怎么进来 | **两条都做**：`factory serve` 内置入站网关（宿主一个端口，按路由的秘密认证，变成 Bean、送进常驻机器或 Agent 邮箱，机器端口仍不发布）；常驻机器也可以经出网代理主动长连接，用 `factoryctl emit` 投递 |
| 里程碑 | **M12 机器 + M13 Agent**：M13 依赖 M12 的常驻沙箱与事件投递 |

---

## 1. 现状差距

| 编号 | 问题 | 后果 | 处理 |
|---|---|---|---|
| G-01 | `src/` 里没有任何代码读 `runtime.instance`。`Runner` 每个 attempt 都开新容器 `mao-{job}-{attempt}`，结束即关。M4 的 `test_pooled_reset_between_jobs` 把同一个沙箱对象传给网关两次，所以通过了 | `pooled` 不存在；`reset` 每次在全新容器里执行，没有意义 | `instances.md` 5.2–5.4 |
| G-02 | `DockerProvider.build_run_argv` 把容器命令固定为 `sh -c "mkdir -p /tmp/home && exec sleep 2147483647"`，覆盖了镜像自己的启动命令；`runtime.service` 没有启动命令字段 | 生产上服务型机器从来没有服务进程，健康检查只在测试的假服务器上通过 | `instances.md` 5.1 |
| G-03 | 没有不属于任何 job 的机器。`egress.md` 第 1 章把“服务型机器的入站 webhook 监听”推到 M4，`gateway.md` 没有写 | 无法长期保持登录、监听推送；外部事件只能靠 cron 触发 Source 去拉 | `instances.md` 5.5–5.7、`ingress.md` |
| G-04 | 厂长每张票一次会话（`supervisor.md` 5.1），会话串行、处理完退出，不继承对话 | 长期职责被打散，上下文要从散落的状态里重新拼 | `agents.md` |
| G-05 | `gateway.md` 5.1 说 `pooled` “按机器键复用容器” | 同一台机器的不同工位有不同的策略、秘密和出网身份。按机器键共用容器会让一个工位看见另一个工位的秘密 | `instances.md` D-02 |

---

## 2. 设计决定

### 2.1 机器（M12）

| 编号 | 决定 |
|---|---|
| D-01 | `runtime.instance` 取 `once`（缺省）、`pooled`、`resident`。`per_job` 是 `once` 的旧名，继续接受。三种模式适用于所有形态（`workspace`、`tool`、`service`），不再只属于服务型 |
| D-02 | 实例按**工位键**区分，不按机器键。`pooled` 每个工位最多 `runtime.pool_size`（缺省 1）个实例；`resident` 每个工位恰好一个 |
| D-03 | 新字段 `runtime.command`（参数列表）：容器的主进程。不写时与现在相同（`sleep`）。服务型机器靠它真正启动服务 |
| D-04 | 实例的宿主目录 `$FACTORY_ROOT/instances/<实例 id>/` 挂到容器 `/factory/instance`。容器建好后不能再加挂载，所以 job 的 `in`、`out`、`ctl` 放在 `/factory/instance/jobs/<job>-<attempt>/` 下：执行前从 attempt 目录拷入，执行后把 `out` 拷回。`Runner` 的收货、回执、出厂检验都不变 |
| D-05 | 常驻实例的持久状态目录 `$FACTORY_ROOT/instances/state/<工位名>/` 挂到 `/factory/state`。按工位**名**（不含版本），升级版本或重启容器后登录状态仍在 |
| D-06 | `reset` 只用于 `pooled`，在 job 释放实例时执行；失败就销毁实例。`resident` 不允许写 `reset`。`Runner` 调网关时不再让网关自己 `reset` |
| D-07 | 常驻实例由 `factory serve` 的新步骤维护（`Instances.reconcile`）：按 active 快照启动、按摘要替换、排空后停止、健康检查、指数退避重启；连续失败到上限后进入 `crashed`，给厂长开 `instance` 唤醒票 |
| D-08 | 调度准入限制并发：`pooled` 工位同时运行的 job 不超过 `pool_size`，`resident` 不超过 `runtime.concurrency`（缺省 1）。实例不够时 job 留在队列里，不消耗 attempt |
| D-09 | `factoryctl emit`：常驻实例的主进程把文件作为工位输出端口的 Bean 封存。每次 emit 生成一个 `DONE` 的 emit job，作为 Bean 的来源证明；只做类型校验，不跑 `gate.judge`。只有 `role: source` 的工位能 emit |
| D-10 | 每个 job 的 `exec` 带自己的出网凭据（M11 的 attempt 令牌放在 exec 环境里），常驻实例的主进程用实例主体 `inst-<id>` 的令牌 |

### 2.2 入站（M12）

| 编号 | 决定 |
|---|---|
| D-11 | `factory.json` 的 `ingress` 段（人维护）开启宿主上的一个 HTTP 端口，由 `factory serve` 的线程 `mao-ingress` 运行。没有这一段就不监听 |
| D-12 | 路由写在 Registry：工位的 `ingress.<路由名>`（M13 起 Agent 的 `subscribe.ingress` 也可以）。路由名在 active 快照里唯一。厂长可以经 ChangeSet 加路由，但认证用的秘密只能由人设置 |
| D-13 | 认证：`bearer`（`Authorization: Bearer`）或 `hmac-sha256`（请求体的 HMAC，放在指定请求头）。比较用常量时间。秘密不存在时回 503，不放行 |
| D-14 | 投递：`bean`（按 D-09 的方式封存为路由端口的 Bean）、`instance`（原子写入常驻实例的 `/factory/instance/inbox/`）、`agent`（M13，进 Agent 邮箱） |
| D-15 | 事件只记录路由、字节数、投递目标与 id，不记录请求体与请求头 |

### 2.3 常驻 Agent（M13）

| 编号 | 决定 |
|---|---|
| D-16 | 新 kind `Agent`（`agents/*.yaml`）：`runtime`（agent-cli 的 OperatorRuntime）、`model`、`charter`（职责说明）、`policy`、`subscribe`（`schedule`、`ingress`、`beans`）、`lines`（允许启动的产线）、`budget`、`context` |
| D-17 | 每个 Agent 一个常驻沙箱 `mao-agent-<名>`。`$FACTORY_ROOT/agents/<名>/home` 挂到 runtime 的 `HOME`，CLI 自己的会话存储因此跨容器重启保留；`notebook/` 是 Agent 自己的笔记 |
| D-18 | 邮箱 `agent_messages`：定时、入站、新 Bean、收件箱回答、人发来的消息、自己预约的提醒、（厂长）唤醒票。消息体在写入时打码 |
| D-19 | 一轮（turn）：值守取出最多 `context.batch` 条待处理消息，拼成一段提示，在同一段对话里续一轮。对话的第一轮用 `invoke.argv`，之后用新字段 `invoke.resume`（模板里有 `{{ session.id }}`）。会话 id 从 transcript 的 `session_id` 取 |
| D-20 | 上下文在 Agent 自己的对话里，由 CLI 原生压缩。对话轮数或上一轮的输入 token 超过上限时**轮换**：先在旧对话里请 Agent 把交接写进 `notebook/HANDOFF.md`，再开新对话，第一轮提示引用它 |
| D-21 | 预算：每轮 `register_job_cap`（模型代理强制），每日上限按最近 24 小时的轮次成本求和。用尽时消息留在邮箱，不丢 |
| D-22 | 恢复：`running` 的轮次在值守重启时记为 `interrupted`，消息退回待处理；同一批消息连续失败 3 轮后记为 `dropped` 并给人开收件箱事项 |
| D-23 | `foreman.mode = "resident"` 时，厂长的唤醒票（复核票除外）不再起一次性会话，而是投进内置 Agent `foreman` 的邮箱；票按投递它的那一轮判定（沿用 `supervisor.md` 5.5 的表）。缺省 `session`，M5 的行为不变 |
| D-24 | Agent 的 MCP 身份是 `agent:<名>`：只读查询、启动 `lines` 里列出的产线、向人提问、给自己预约提醒、给另一个 Agent 发消息。不能提议 ChangeSet（厂长除外），不能做审批 |

---

## 3. 不变的边界

- `factory serve` 的主循环仍然确定，不调用模型。模型只在 Agent 沙箱里的 CLI 进程中运行，经模型代理计费。
- 机器端口不发布到宿主。入站只经 `mao-ingress` 一个端口，由人决定是否暴露到公网。
- 沙箱参数仍由 `build_run_argv` 产生，不加 `-p`、`--network host`、`--privileged`。
- 秘密值不进入事件、日志、job 行、邮箱消息、MCP 返回与控制台页面。
- 宪法仍然只由人修改；`Agent` 资源与其他 Registry 资源一样经 ChangeSet 推广。

---

## 4. 任务映射

| 任务 | 内容 | 规范 |
|---|---|---|
| M12-01 | `runtime.command`、`instance` 校验、`SandboxSpec.command`、`Sandbox.alive`、`provider.attach` | `instances.md` 3.1、5.1 |
| M12-02 | `Instances`：`pooled` 租借、`reset`、拷入拷出、准入；`Runner` 接线 | `instances.md` 5.2–5.4 |
| M12-03 | 常驻实例：`reconcile`、健康、退避重启、`crashed`、恢复；`factory instance` | `instances.md` 5.5–5.6、5.8 |
| M12-04 | `factoryctl emit` 与 emit job | `instances.md` 5.7 |
| M12-05 | 入站网关 | `ingress.md` |
| M13-01 | `Agent` kind：schema、MRL 抽取与规则、`invoke.resume` | `agents.md` 3.1、4.1 |
| M13-02 | 邮箱与订阅（定时、入站、Bean、收件箱回流）；`factory agent send` | `agents.md` 3.2、5.1、5.2 |
| M13-03 | 轮次：常驻沙箱、续接、预算、超时、恢复；`agent` 角色的会话；`factory agent` 其余命令；`serve` 装配 | `agents.md` 5.3–5.5、5.7 |
| M13-04 | 上下文轮换与 Agent 的 MCP 工具 | `agents.md` 3.4、5.6 |
| M13-05 | 厂长常驻模式 | `agents.md` 5.8 |

---

## 5. 原型验证

设计方在 M11 原型的工作树上实现了 M12、M13 的原型，跑通了 `tests/acceptance/m12/`（29 个）、`tests/acceptance/m13/`（22 个）与全部既有测试。原型不提交；实现者按规范独立实现。原型中发现的问题已直接写进规范正文，例如：`factory.json` 各段的错误码（没有通用的 `CONFIG_INVALID`）；`LocalProvider.attach` 总能接上，所以 Agent 沙箱靠 `agents.sandbox` 摘要决定是否 `attach`；失败的轮次不增加 `turns_in_conv`；轮次运行时就要用到 `agent` 角色的 MCP 会话，所以它在 M13-03 而不是 M13-04；收件箱的 `question` 回答只存 `choice` 或 `text`。

---

## 6. 既有测试

| 测试 | 变化 | 原因 |
|---|---|---|
| `tests/fakes/fake_agent.py` | 增加 `--resume`、对话历史（存在 `/workspace/.home/.fake-agent/`）、`history` 与 `usage` 动作、按 `FACTORY_TURN` 选脚本 | `agents.md` 4.7；旧参数行为不变，既有测试全部通过 |
