# 委托与能力设计（2026-11-13）

> 设计方根据用户对“厂长自主接入新机器”的要求所做的设计与原型验证。**受保护，只能由人修改。**
> 任务是 **M11-01 至 M11-09**，规范 `spec/system/inbox.md`、`spec/system/delegation.md`、`spec/system/capabilities.md`，验收 `tests/acceptance/m11/`（41 个）。
> 本文件的编号（G-xx、D-xx）被规范与任务卡引用，不要重新编号。

---

## 0. 结论

用户希望厂长能像接入 B 站投稿这样的工作一样，自己完成镜像、Playwright 脚本、Machine、Station、Policy 与产线；人可以把审批全部交给厂长，必要时厂长再向人申请；操作工可以向厂长申请；厂长写的监控规则命中后能触发厂长审批；秘密由人填写并在保存前核验；验证码之类交给人处理。

按这个目标逐项演练现有系统，结论是：**做不到，而且有几处不是“缺功能”，而是已有规范从未接上**（第 1 章）。M11 用三份规范补齐，并修掉这些接线问题：

- `inbox.md`：人的统一入口。收件箱、通知钩子、控制台写入、秘密申请与核验、人工协助。
- `delegation.md`：授权模式 `bypass`、审批委托与改道、操作工向厂长的定向申请、ChangeSet 签核方式。
- `capabilities.md`：出网主体令牌、通配域名、按需放行、监控规则、沙箱资源、镜像构建网络。

用户的四个选择（2026-11-13）：

| 问题 | 选择 |
|---|---|
| `bypass` 放开什么 | **只跳过审批**：所有等级的交叉复核与人工审批都交给厂长；V1–V4、影子、金丝雀自动回滚、沙箱隔离、秘密保密不变；宪法仍然只由人修改 |
| 验证码 | **截图加回答，实时画面以后再做**：协议里给 `view.kind = "live"` 留位置 |
| 出网名单 | **通配加按需询问**：`*.example.com`，以及 `on_unlisted: ask` 交给工位的审批人 |
| 通知 | **控制台加通知钩子**：钩子是人配置的 argv，不经过 shell |

---

## 1. 现状差距

| 编号 | 问题 | 后果 | 处理 |
|---|---|---|---|
| G-01 | `runner.md` 5.3 的静态拒绝里，`EGRESS_UNSUPPORTED`、`SECRETS_UNSUPPORTED` 两行是 M1 的临时限制，`egress.md` 3.2 写了出网与秘密的处理，但实现一直保留着拒绝 | 策略要出网或要秘密的工位，job 在 `PENDING` 直接 `DEAD`，从未到过沙箱。M8 的出网验收直接驱动代理，没有经过 `Runner`，所以没发现 | `capabilities.md` 5.0 |
| G-02 | `egress.md` 3.2 要求 `network == "egress"` 时把 `store.inject(plan.secret_names)` 并进沙箱环境，实现没有做 | 即使解除 G-01，工位也拿不到秘密 | `capabilities.md` 5.0 |
| G-03 | `Runner` 的 ctl op `approval` 仍是 `runner.md` 5.6 的 M1 写法：自己读写 `approvals` 表，没有经过 `Approvals`，handler 里有一个 600 秒的缺省期限 | 路由改写、厂长唤醒票、人的收件箱都接不上；`approvals.md` 4.3 的响应格式没有生效；`Runner.decide` 不写 `decided_by` | `delegation.md` 5.9 |
| G-04 | 出网代理只靠 `X-Factory-Job` 头识别 job | 任何能连到代理的沙箱都可以冒用别的 job 的名单 | `capabilities.md` 5.1 |
| G-05 | 代理的事件 id 是每个实例从 1 数起的 `ev-px%06d` | 守护进程重启、或命令行另开一个代理时，第一条事件就违反主键 | `capabilities.md` 5.1 第 8 步 |
| G-06 | 金丝雀路由按哈希分桶 | 新产线只存在于候选 epoch，分到 active 的 run 找不到产线；样本凑不够，ChangeSet 永远停在金丝雀 | `delegation.md` 5.7 |
| G-07 | `images.network` 缺省 `none` | 厂长写的 Dockerfile 里 `apt-get`、`pip install` 全部失败 | `capabilities.md` 5.7 |
| G-08 | 沙箱资源固定：`--pids-limit 256`、`/tmp` 256 MiB、没有 `/dev/shm` 设置、根文件系统只读 | Chromium（Playwright）起不来或很快崩溃 | `capabilities.md` 5.6 |
| G-09 | T2/T3 变更必须交叉复核或人工审批；没有人回答问题的渠道；秘密只能由人手工放进文件；操作工遇到验证码只能失败；操作工不能向厂长申请；没有监控规则 | 厂长无法自主完成接入 | 三份新规范 |
| G-10 | `factory check` 等于 `Snapshot.load`，它容忍 `E-EXPR` | 表达式类型错误不会让 `factory check` 失败（既有行为，M11 不改）；监控规则的类型检查用 kernel 检查器验收 | `capabilities.md` 第 7 章 |

---

## 2. 设计决定

- **D-01 收件箱是人的唯一入口。** 审批、签核、秘密、协助、厂长的问题都是 `human_requests` 里的一行。命令行、控制台、通知钩子都只是它的视图。同一 `(kind, subject)` 只开一条。
- **D-02 `bypass` 只改路由。** `review` 与 `human` 路由改写为 `foreman`，并写 `approval.delegated`；ChangeSet 的签核方式变成 `auto`。V1–V4、影子、金丝雀回滚、宪法、沙箱、秘密都不变。`hold` 监控与人工协助不受 `bypass` 影响：前者总是交给厂长，后者总是交给人。
- **D-03 厂长可以改道给人。** `approval_escalate` 把 `foreman` 路由的审批改成 `human`，开收件箱项。厂长不能决定 `human` 路由的审批，也不能投复核票。
- **D-04 操作工向厂长申请是独立的定向审批。** `factoryctl ask` 不读工位的审批规则，总是 `foreman`，到期不让 job 失败。
- **D-05 签核方式按风险等级配置。** `auto`、`review`、`human`；`human` 方式开 `signoff` 收件箱项。签核只在 `verified` 这一步判断，已进入影子或金丝雀的 ChangeSet 不会因为人改配置而退回。
- **D-06 秘密先核验后保存。** 机器为每个秘密声明核验命令（`needs.secret_checks`）。人填的值先规范化，再在一个只为核验开的沙箱里运行这条命令，退出码 0 才保存。失败时事项保持打开，旧值不变。厂长永远看不到值。
- **D-07 人工协助只交截图与回答。** 截图必须在绑定挂载里，是 PNG，不超过 5 MiB。回答类型有 `text`、`point`、`points`、`offset`、`done`，按截图尺寸检查边界。控制台用 `<input type="image">` 取点击坐标，不需要 JavaScript。
- **D-08 出网主体用令牌认证。** 每个 attempt、每个核验沙箱各有一个令牌，只存 sha256。代理不认识的请求一律 407。Chrome 不接受代理 URL 里的凭据，所以同时给出 `FACTORY_EGRESS_USER` 与 `FACTORY_EGRESS_TOKEN`。
- **D-09 名单外的主机可以按需询问。** `on_unlisted: ask` 时，代理经 job 的 ctl 邮箱提问，job 按工位的 `approval.in_run` 路由（含 `bypass` 改写）。同一 attempt 对同一主机只问一次。
- **D-10 监控规则是 kernel 表达式。** `monitors.<name>.when` 与 `approval.in_run` 用同一个类型环境，在 propose 时就做类型检查。求值出错按命中处理。动作只有 `hold`、`deny`、`wake`，厂长不能写任意代码进控制面。

---

## 3. 不变的边界

- 宪法只由人修改。`bypass` 不给厂长任何改宪法、读秘密、进沙箱的工具。
- 秘密值不进事件、日志、job 行、收件箱的回答、MCP 的返回或控制台页面。核验日志先把候选值换成 `***`，再整体 `redact`。
- 通知钩子只能由人在 `factory.json` 里配置，按 argv 运行，不经过 shell。
- 控制台只多了一个写入口 `POST /inbox/<id>`，要鉴权、Host 校验与 CSRF。

---

## 4. 任务映射

| 任务 | 内容 | 验收 |
|---|---|---|
| M11-01 | 收件箱、通知钩子、控制台写入、`escalate` | `test_m11_01_inbox.py`（4） |
| M11-02 | 授权模式、审批委托与改道、`factoryctl ask`、G-03 | `test_m11_02_approvals.py`（7） |
| M11-03 | 签核、复核票、新产线路由（G-06） | `test_m11_03_signoff.py`（6） |
| M11-04 | G-01、G-02、G-04、G-05，通配域名，按需放行，核验沙箱的秘密与代理 | `test_m11_04_egress.py`（6） |
| M11-05 | 秘密申请、核验、保存 | `test_m11_05_secrets.py`（5） |
| M11-06 | 人工协助 | `test_m11_06_handoff.py`（5） |
| M11-07 | 监控规则 | `test_m11_07_monitors.py`（4） |
| M11-08 | 沙箱资源（G-08）、镜像构建网络（G-07） | `test_m11_08_sandbox.py`（3） |
| M11-09 | `bypass` 下厂长接入一条发布产线的端到端演练 | `test_m11_09_onboarding.py`（1） |

既有验收改了两处：

- `tests/acceptance/m8/test_m8_01_hardening.py::test_egress_denials`：“不带任何身份”的请求，期望由 403 `EGRESS_JOB_INVALID` 改为 407 `EGRESS_AUTH_REQUIRED`。M11-01 至 M11-03 的 `regress` 暂时排除这一个用例。
- `tests/acceptance/m7/test_m7_02_console.py::test_security_headers_and_host`：CSP 加 `img-src 'self'`。M11-01 实现它。

---

## 5. 原型验证

设计方在一个不提交的工作树上实现了三份规范：

- 41 个新验收全部通过。
- 原型过程中发现并写回规范的问题：G-01、G-02、G-03、G-05、G-10；`Snapshot.key` 从不抛错，判断存在要用 `snapshot.doc`；核验秘密时只注入已经保存的其余秘密，人可以按任意顺序填写；`factoryctl handoff` 的等待上限取 `contract.json` 的 `handoff.timeout_s`。
- 既有测试的结果见第 6 章。

---

## 6. 既有测试

原型上完整的 `tests`（按目录分批运行）共 1520 个，全部通过：41 个新验收，加 1479 个既有测试（含第 4 章改过的两处）。`ruff check`、`ruff format --check`、`doccheck` 通过。

原型第一次运行完整 `tests` 时，`tests/test_codebase_policy.py` 拒绝了几处写法，规范已经按它写明：

- 代理等待按需放行的回答时不能用 `time.sleep`、`time.monotonic`，也不能用注入的 `clock.sleep`（`FakeClock` 会推进假时间，让审批提前到期），改用 `threading.Event().wait`；
- 事件 id 用注入的 `IdSource`；
- 秘密值用公开的 `SecretStore.inject` 取，不访问私有成员。
