# M17 治理与厂内自治审计：可复现基线

- 日期：2026-11-16。
- 检出基线：`c538768`，包含 M15 实现与 M16 冻结设计；该基线不包含正在其他工作区实现的 M16 代码。本报告不判定 M16 的实现状态。
- 范围：厂长提议后的验证、发布、恢复、岗位变更和厂长观测。人只给宏观目标时，后续工作应能由具备授权的厂内角色持续推进。宏观目标发现及其具体状态模型尚须设计，不能把当前未规定的接口当既定缺陷。
- 诊断只使用隔离 worktree 的临时 Git 库、SQLite、项目现有 contracts/shadow 测试替身和可控故障注入；无真实付费模型、网络和业务数据。未修改生产源码与既有验收。

## 证据与运行方式

```bash
uv run --offline python tools/harness.py pytest plan/evidence/m17/repro/test_governance.py --basetemp=.factory/m17-repro/governance --tb=short
```

基线结果：**7 failed、0 skipped**。这 7 个测试针对 6 类发现，断言当前规范及不得静默放行未声明岗位的期望；基线红灯是故障证据，不是验收通过。

夹具正向自检（同一项目原有验收，未更改）：

```bash
uv run --offline python tools/harness.py pytest tests/acceptance/m3/test_m3_03_rollout.py -k 't1_shadow_then_active or t2_waits_for_review_then_canary or canary_promotes_on_slo or blocked_runs_rebuilt_after_activation or safe_mode_pauses_t2' --tb=short
```

基线结果：**5 passed、0 skipped**。它证明临时库、epoch、正常 shadow/canary/推广和正常 rebuild 夹具可用；也说明现有检查没有覆盖以下故障边界。

## G01：安全模式不能阻止在途 T2 变更继续发布

- 规范：`spec/system/governance.md` 5.13 明确：安全模式开启时，已经在 `shadow`、`canary` 的 T2+ 也不再推进。
- 代码：`src/mao/governance/rollout.py` 的 `_from_verified` 检查安全模式，而 `_from_shadow` 和 `_from_canary` 不检查。
- 复现：`test_safe_mode_stops_existing_t2_shadow`、`test_safe_mode_stops_existing_t2_canary_promotion` 使用真实 ChangePipeline 和 SQLite：在 T2 shadow 或已有足够成功样本的 T2 canary 写入真实 `audit_findings(state='wake')`，调用 `tick()`。安全模式确实为 `on`；实际分别得到 `canary`、`active`，预期留在 `shadow`、`canary`。
- 影响：工厂发现需要人工或厂长处理的不变量异常时，在途高风险结构仍可继续放量或激活。由此限制的是自主决策的可靠性。
- 修复与验收约束：统一在发布状态机判断向前推进的边界；不能因此阻止应当发生的安全回滚。覆盖安全模式进入前后每个状态、T1 对照、解除后的恢复和并发调用。

## G02：推广不是原子事务，失败后实际 active 与 ChangeSet 状态分裂

- 规范：`spec/system/governance.md` 5.10 第 1–2 步要求在一个数据库事务中切换 previous/active/candidate、ChangeSet 状态和事件，之后才同步 Git refs。
- 代码：`src/mao/governance/rollout.py` `_promote` 先三次调用 `EpochStore.set_pointer`，各自提交并同步 Git ref，最后单独更新 ChangeSet；`src/mao/governance/epochs.py` `set_pointer` 各自开启事务。
- 复现：`test_failed_promotion_keeps_database_and_git_pointers_atomic` 在临时 SQLite 加触发器，仅在写 `changesets.state='active'` 时注入失败。实际 ChangeSet 仍为 `shadow`，数据库与 Git 的 active 已为候选、candidate 已清空、previous 已变化；预期整个数据库状态维持推广前一致，Git refs 亦不提前变化。
- 影响：进程中断或持久化失败后，厂长看到的发布状态与实际路由版本不一致。重启后必须能识别并安全恢复，不能依靠外层代理人工检查改指针。
- 修复与验收约束：数据库状态和事件原子提交，Git 引用属于提交后幂等同步，启动/调和时以权威状态恢复；对事务前、中、提交后及 refs 同步各故障点做注入，验证重试不会误推广或丢失回滚能力。

## G03：解决转换后 rebuild 失败既传播异常又丢失唤醒

- 规范：`spec/system/governance.md` 5.10/5.12：推广后重建被转换阻塞的 run；重建失败只写唤醒票，不影响推广结果。
- 代码：`src/mao/governance/rollout.py` `_rebuild` 先把转换需求标为 `resolved`，再直接调用 `pipeline.rebuild`，没有失败记录或唤醒处理。
- 复现：`test_rebuild_failure_preserves_promotion_and_wakes_foreman` 建立真实失败 run 和转换需求，在重建边界注入一次异常。结果 `advance raised M17 rebuild failed; state=active; conversion=resolved; wakes=0`。
- 影响：能力已经激活，但原任务仍失败；转换需求却已 resolved，厂长没有失败通知，也没有可追踪的后续恢复动作。
- 修复与验收约束：可重试的重建记录必须与原 run 和新 epoch 绑定、幂等，失败不撤销已成功推广；记录足够信息并可靠唤醒厂长，成功后不重复执行或重复发布。队列应能跨重启恢复。

## G04：单个候选位未保护，第二个变更可覆盖第一条 shadow

- 规范：`spec/system/governance.md` 5.9 要求 shadow、canary、观察期均计入并行发布上限；`spec/mrl/governance/risk.mrl` 设置 `concurrent_rollouts = 1`。
- 代码：`src/mao/governance/rollout.py` `_queued` 只统计 active 的观察期，忽略当前 shadow/canary；系统只有一个 `candidate` 指针，`EpochRouter.choose` 取第一个 canary。
- 复现：`test_existing_shadow_blocks_second_changeset` 创建两份从同一 active 派生的独立真实 ChangeSet，先让第一份进入 shadow，再推进第二份。实际第二份也进入 `shadow`，而非保持 `verified` 且报告 `queued`；随后会覆盖 candidate。
- 影响：自主厂长连续提议多项工作时，候选版本、路由与变更归属可以互相干扰；后续验证或推广结果不再对应唯一候选。
- 修复与验收约束：以 ChangeSet 为持有者原子占用和释放发布槽；必须计入已有 shadow/canary/观察期，并处理跨进程竞争、撤回、失败、崩溃和恢复。路由与实际 candidate 必须一致。

## G05：唤醒领取并发竞争（归属常驻 Agent 审计）

- `spec/system/foreman.md` 5.1 要求两个进程同时取票只有一个成功。原治理审计用两个独立 SQLite 连接及同步栅栏已观察到两次返回同一 `wk-0001`：`WakeQueue.next` 在事务外查询，在条件更新后不检查受影响行数。
- 为避免重复维护，本项的最终回归测试和详细运行证据由 M17 常驻 Agent 审计统一保留；本报告只标注它与治理任务领取的交叉影响。

## G06：新增 Agent 未纳入变更声明与风险，能夹带激活并被调度

- 已确认的实际行为：`test_undeclared_new_agent_cannot_hide_in_changeset` 将新 `Agent:extra-worker@1`（含 `schedule` 与允许的 `lines`）与一个 Station 更新放在同一真实 commit；ChangeSet 只声明 Station。候选快照确实含新 Agent，验证结果却是 `verified`、`risk_tier=T1`。
- 根因：`spec/schemas/records.schema.json` 的 ChangeSet kind 枚举、`src/mao/governance/risk.py` `_GOVERNED` 与 `spec/mrl/governance/risk.mrl` 都未纳入 M13 增加的 Agent。声明核对只比较过滤后的变更事实；`propose` 使用整个源 commit 的树。单独声明 Agent 也不能通过现有枚举。
- 进一步的本地真实调度探针：相同夹带 commit 经真实 pipeline 得到 `verified T1 → shadow T1 → active T1`，随后 `AgentHost.collect()` 的输出确认 `active_agent True`，该 Agent 的邮箱出现 `[('schedule', 'pending')]`。这证明它能够进入调度，不仅仅是报告漏显示。
- 自治影响：工厂无法通过正常声明完整表达自建岗位，绕行路径却可把未声明岗位带入生产；岗位的职责、周期、可调用产线和预算未得到与实际变更相符的治理。
- 边界与设计：这是跨里程碑接线/治理缺陷。现有规范没有为 Agent 各字段定风险等级，不能自造既定等级；M17 设计应先冻结新增/升级/停用岗位、权限和风险映射，确保实际结构变更都能被声明、评级并验证，既不静默漏报，也不使在授权范围内新增岗位只能靠外层手改。
- 可重现调度探针（仅创建临时工厂）：

```bash
uv run --offline python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from tests.acceptance.m13.agent_world import HELPER, World
from tests.acceptance.m3.gov_world import changeset_doc, commit_change, upper_v2_limits, upsert
from tests.acceptance.m3.test_m3_03_rollout import T1_ROLLOUT, _pipe
with TemporaryDirectory(prefix="m17-governance-") as folder:
    w = World(Path(folder))
    pipe = _pipe(w)
    agent = HELPER.replace("name: helper", "name: extra-worker").replace(
        "  budget:",
        "  lines: [note-trigger@1]\n  subscribe:\n    schedule:\n      - {every: 1h, note: periodic work}\n  budget:",
    )
    source = commit_change(w.repo, {**upper_v2_limits(), "agents/extra-worker.yaml": agent}, parent=w.base)
    cs = pipe.propose(changeset_doc(w.base, [upsert("Station", "upper")], rollout=T1_ROLLOUT), source, by="human:probe", timeout_s=60)
    for phase in ("verify", "shadow", "promote"):
        cs = pipe.advance(cs.id)
        print(phase, cs.state, cs.risk_tier)
    w.host(pipeline=pipe).collect()
    print("active_agent", "Agent:extra-worker@1" in w.epochs.materialize(w.epochs.current()).keys("Agent"))
    print("new_agent_messages", [(m.kind, m.state) for m in w.messages("extra-worker")])
    w.ctx.close()
PY
```

## G07：验证中断后 ChangeSet 永久停在 verifying

- 规范：`spec/system/governance.md` 5.7 规定写入 `verifying` 后崩溃，重入时从头验证。
- 代码：`src/mao/governance/changesets.py` `advance` 只对 `draft` 执行验证；`src/mao/governance/rollout.py` `advance_open` 没处理 `verifying`。
- 复现：`test_verifying_resumes_after_transient_epoch_failure` 在 candidate epoch 首次加载时注入一次临时 I/O 失败。状态确为 `verifying`；随后故障已消失，再次 `advance` 仍抛 `CHANGESET_STATE_INVALID`。
- 影响：工厂无法自行完成正在接入的变更；`tick()` 后续轮次也无法正常推进这项工作。失败记录不能替代重新入队和恢复。
- 修复与验收约束：验证过程按固定基线可重入，重复执行不重复产生外部副作用或错误提案；覆盖异常前后、进程重启和多个变更并存时的公平推进。

## G08：厂长指标查询无法提供评价结果，返回契约仍需设计

- 实际行为：`src/mao/foreman.py` `ForemanQuery.metrics_query(station, metric)` 对合法指标只返回输入的 station/metric，没有数值、样本数量或时间范围。`spec/system/governance.md` 5.11 声称厂长指标沿用 `run_metrics` 定义，但 `foreman.md` 未固定站点统计边界、时间窗或返回数据结构。
- 定性：当前接口确实不能让厂长按数据评估效果；由于现有规范不完整，不能把某个猜测出来的站点统计算法当已确定 bug。M17 设计应先冻结指标口径、窗口、空样本、权限及可核验证据，再建立跨轮自评和下一轮选择的场景验收。
- 与宏观目标发现关系：周期订阅、已授权产线、邮箱和 M16 计划中的资产/知识可以复用，但是否生成、筛选和执行具体目标仍取决于授权、可查询事实、持久目标状态、反馈与恢复；这些是要设计并证明的整体闭环，不能由一个指标接口单独保证。
