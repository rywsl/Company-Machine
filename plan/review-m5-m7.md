# M5–M7 交付审查（2026-11-12）

> 设计方对实现方交付的 M5–M7 做的审查。**受保护，只能由人修改。**
> 纠偏任务是 **M8-01**，规范 `spec/system/hardening.md`，验收 `tests/acceptance/m8/test_m8_01_hardening.py`（8 个，对当前实现全部失败）。
> 本文件的编号（R-xx）被规范与任务卡引用，不要重新编号。

---

## 0. 结论

M0–M7 的阶段门全部记录为通过；lint、format、lock、doccheck、静态规则（82 个）也都通过。审查发现，**系统的两个核心能力在生产路径上不存在**：

1. **出网代理是空壳。** `EgressProxy` 用标准库的 `BaseHTTPRequestHandler` 作处理器，对任何方法都回 501，不转发也不按白名单拒绝。`egress.md` 5.4 的行为（识别 job、域名 × 方法、CONNECT、事件）一条都没有实现。它只让“代理在线”的检查通过。web-read、publish、providers 级别的工位在真实环境里全部不能出网。
2. **无人值守的自治循环不存在。** `ForemanSupervisor.serve` 没有实现；`factory foreman supervise` 不带 `--once` 时启动后立即关闭、退出码 0；`factory serve` 在 `supervise = true` 时不启动值守线程。只有测试使用的 `--once` 路径能跑。

另有 7 处实现或设计问题（第 2 节）。根因分两类：

- **实现方**：规范写了、验收没覆盖的部分没有实现，或用占位代替（R-01、R-03、R-05、R-06、R-07）；在规范之外自行加了 Docker Desktop 转发容器且写死未固定的镜像（R-04）。
- **设计方**：M2 的出网验收只检查“计划构造正确、端口已绑定”，`test_connect_without_ca_rejected_when_methods_limited` 名为“拒绝”却没有发送任何 CONNECT（R-01 因此漏过）；`runtimes.md` 把代理改为守护进程所有时，没有规定代理如何得到别的进程里 job 的出网计划（R-02）；没有规定 `serve` 一轮失败后的行为与 SIGTERM（R-08）；值守会话的网络规则与 `runtimes.md` 5.6 不一致（R-09）。

流程上：M5–M7 的全部实现（`src/mao/supervisor.py`、`daemon.py`、`durable.py`、`mcp.py`、`status.py`、`console.py`、`retention.py`、`backup.py`、`images.py`、`commands/`、迁移 024–034 等）**都没有提交**，只存在于工作区。

---

## 1. 审查方法

- 运行全部静态检查与 `lock --verify`；核对 `plan/state.json` 中 M5–M7 阶段门的证据。
- 在 `src/mao` 中搜索占位、写死的值、`0.0.0.0`、未固定的镜像、未被调用的公开函数。
- 逐条对照 `runtimes.md` 5.4–5.6、`supervisor.md`、`daemon.md` 5.5–5.7、`retention.md` 5.2、`egress.md` 5.4 与实现。
- 为每个确认的问题写一个对当前实现失败的验收（`hardening.md` 第 7 章），确认失败原因正是该问题。

---

## 2. 问题清单

| 编号 | 位置 | 现象 | 后果 | 依据 | 处置（M8-01） |
|---|---|---|---|---|---|
| R-01 | `src/mao/egress.py` `EgressProxy.start` | `ThreadingHTTPServer((host, port), BaseHTTPRequestHandler)`：没有任何 `do_*` 方法 | 所有出网请求得到 501；白名单、方法检查、CONNECT、`egress.allowed/denied` 事件都不存在 | `egress.md` 5.4 | 按 `hardening.md` 5.1 实现 |
| R-02 | `src/mao/egress.py` `start_owned_proxies` | `EgressProxy(lambda _job: EgressPlan("none", ...), ...)` | 守护进程拥有的代理拒绝一切；即使 R-01 修好也不能出网 | 设计缺口：`runtimes.md` 5.4 没规定跨进程取计划 | 新表 `egress_plans`、`register_job_egress`、`plans_from_db`（`hardening.md` 5.2） |
| R-03 | `src/mao/commands/job.py` `cmd_exec`；`commands/foreman.py` `cmd_supervise` | `discover_proxies` 定义了但没有任何调用者 | 每个 `job exec` 子进程自己起一对代理（端口随机、拒绝一切），Docker Desktop 上还各起一个转发容器且不删除 | `runtimes.md` 5.4 第 3 条 | `open_context` 设置 `ctx.proxies = discover_proxies(ctx)`（`hardening.md` 5.3） |
| R-04 | `src/mao/egress.py` `ensure_desktop_forwarder` | 规范之外的转发容器；镜像写死为 `node:22-bookworm-slim`（无 digest）；容器名按 pid；`docker network connect` 的失败被忽略；第一处 `subprocess.run` 没有捕获异常 | 违反“全部版本固定”；子进程泄漏容器；失败时留下半配置的容器 | 设计缺口（Docker Desktop 的 internal 网络到不了 `host-gateway`）+ 实现越界 | 追认转发容器，按 `hardening.md` 5.4 收紧：镜像来自 `serve.forwarder_image`（必须带 digest）、只由代理拥有者启动、关闭时删除、`alias` 写进 `serve.json` |
| R-05 | `src/mao/supervisor.py`；`commands/foreman.py:237`；`commands/serve.py` | 没有 `ForemanSupervisor.serve`；`supervise` 非 `--once` 时 `start(); shutdown()`；`serve` 非 `--once` 时不托管值守 | 生产环境没有厂长：唤醒票堆积，自治闭环只在测试的 `--once` 里成立 | `supervisor.md` 第 3 章；`daemon.md` 5.7 | `serve(stop, idle_s)`、`run_serve`、信号处理（`hardening.md` 5.5） |
| R-06 | `src/mao/foreman.py`、`src/mao/daemon.py` `_pump` | 没有 `ConversionStore.reopen`，信号泵没有“提议失败后重开”这一步 | 厂长的 ChangeSet 被拒绝、撤回或回滚后，转换需求永远停在 `in_progress`，不会再唤醒厂长 | `supervisor.md` 3.x 与信号泵增补 | 按原文实现（`hardening.md` 5.6） |
| R-07 | `src/mao/retention.py` `_sweep_blobs` | 死 digest 在事务外计算，进事务后不复查就删除 | 计算与删除之间有新 Bean 引用同一 digest 时，删掉它的字节（数据丢失） | `retention.md` 5.2 第 1–2 步“在一个写事务里” | 在事务内查询再删除（`hardening.md` 5.7） |
| R-08 | `src/mao/daemon.py` `Daemon.serve`；`commands/serve.py` | `tick()` 抛出任何非 `LEASE_LOST` 的 `FactoryError` 都让 `serve` 退出；没有 SIGTERM 处理 | 一次 docker 超时就让守护进程停止；SIGTERM 直接杀进程，租约要等过期、job 子进程成为孤儿 | 设计缺口 | 记 `serve.tick_failed` 后继续；SIGINT/SIGTERM 置位 `stop`（`hardening.md` 5.5 第 3 条、5.8） |
| R-09 | `src/mao/supervisor.py` 会话的 `SandboxSpec` | `network` 只看 `config.network` | 配置为 `none` 时真实 CLI 连不到模型代理（D-31 在值守上重现） | 设计缺口：`supervisor.md` 与 `runtimes.md` 5.6 不一致 | 引用 `${MODEL_PROXY}` 的 runtime 一律 `egress`（`hardening.md` 5.9） |

---

## 3. 设计方同时修改的受保护文件

| 文件 | 修改 |
|---|---|
| `spec/system/hardening.md` | 新增：R-01 至 R-09 的规定与验收 |
| `tests/acceptance/m8/test_m8_01_hardening.py` | 新增 8 个验收：真实上游收发、真实值守与守护进程循环 |
| `plan/tasks.yaml`、`plan/gates.yaml`、`plan/milestones.md`、`plan/tasks/M8-01.md` | 任务 M8-01 与阶段门 M8（继承 M7 的全部检查） |
| `spec/system/README.md` | 文档地图与迁移编号表（`035`） |
| `tests/acceptance/m6/live_world.py` | 2026-11-12 补：`serve.forwarder_image` 取 `tools/live_versions.json` 的 `base`。此前 Docker Desktop 上的 M6 实况测试按 `hardening.md` 5.4 必然失败，实现方正确地阻塞了 M8-01（`plan/journal/2026-11-12-M8-01-forwarder-image.md`） |

M2 的弱验收（`test_m2_07_egress.py` 中只检查计划与端口的几条）不改名、不删除；行为由 M8 的验收覆盖。

---

## 4. 给实现方

1. 先提交 M5–M7 的现有实现（按任务分提交，提交信息写明任务号），再开始 M8-01。
2. 读 `spec/system/hardening.md` 全文。第 5 章每一条都要实现，包括没有验收的 5.4、5.7、5.9 与 5.2 第 1 条；为它们在 `tests/system/` 写单元测试。
3. 验收测试与规范矛盾时，照 `plan/README.md` 第 5 节写日志并 `task block`。M7 期间的两次阻塞都处理得正确。
4. 以下做法任何一种都算任务失败，即使验收通过：用占位实现让检查通过；写死镜像、端口或计划；在规范之外新增组件而不写日志说明。
