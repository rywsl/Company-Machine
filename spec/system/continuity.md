# continuity：持续自治与故障闭环（M17）

> 设计冻结输入；生产实现尚未开始。基线、证据与总体理由见 `plan/design-continuity.md`、`plan/evidence/m17/README.md`。依赖 M16 完成，不修改 M16 的规范、任务或验收。本文在明确列出的 M17 增补处优先于旧条款，其余契约保持有效。

## 1. 目的与范围

统一修复驱动、持久投递、所有权、发布、服务执行、成本与反馈，补齐从宏观目标出发的长期自治周期。控制面只做确定性调度、校验和计量；观察、候选目标发现、价值判断、业务计划与调整由厂内 Agent 执行。

范围是单工厂 SQLite、单主机、多进程；不是分布式一致性。业务 Agent 可在授权范围内扩充 Registry 业务能力，不能改平台 `src/`、宪法、授权或验收标准。发现平台未知错误时，持久登记并隔离受影响工作；可以继续独立工作，不允许伪报成功。

M17的完整部署/接入/使用边界另由operability.md冻结：原生CLI资格、有界网络、产品身份、只读预检/非破坏初始化、受限外部操作文件邮箱及CLI/help/skill。核心验收不替代这些专项；所有已登记问题由closure.yaml映射，最终08依赖09–12。

## 2. 依赖

- 既有 Context/Database/Clock、Mailbox/AgentHost、WakeQueue/LeaseStore、Daemon 与 serve。
- Registry、EpochStore、ChangePipeline、risk.mrl、现有审批/委托/人收件箱。
- Runner/Gate/LineRunner、真实 factoryctl、Gateway、ModelProxy、job/line/agent 预算。
- LocalProvider；§5.3 的命令来源规则明确增补并优先于 sandbox.md §5.6 的“先映射再替换 argv”旧措辞，其余路径/env/cwd 与无隔离语义保持有效。
- M16 AssetStore/领域 contract 授权、证据标准和求值、保留根、检查点；payload 仍在 Bean/CAS。
- 不新增第三方依赖，不在控制面调用 LLM API，不挂 docker.sock，不用 shell=True。

## 3. 接口

### 3.1 可靠驱动增补（M17-01、02）

既有公开签名保留。`AgentMessage` 末尾增加 `enqueue_seq: int = 0`；`Lease` 末尾增加 `generation: int = 0`。新增：

```python
class LeaseStore:
    def acquire_fenced(self, resource: str, *, owner: str,
                       ttl_s: float = LEASE_TTL_S) -> Lease | None
    def renew_fenced(self, token: Lease, *, ttl_s: float = LEASE_TTL_S) -> bool
    def release_fenced(self, token: Lease) -> bool
    def guard(self, resource: str, *, owner: str, generation: int,
              conn: sqlite3.Connection | None = None) -> None
        # 本事务检查有效租约及代数，否则 LEASE_LOST；不续约

class DeliveryStore:  # mao.delivery
    def __init__(self, ctx: Context) -> None
    def drain(self, *, limit: int = 100) -> list[str]
        # 幂等处理到期 outbox；只返回本次成功的 outbox id
```

acquire_fenced 在取得租约的同一事务返回含代数的 token；重取过期租约（即使 owner 相同）也增加代数。renew/release_fenced 必须比较 resource/owner/generation 且拒绝过期 token。旧 acquire/renew/release 兼容旧调用，但 M17 宿主、发布和副作用路径必须使用 fenced 接口，不能先 acquire 再另读代数。guard 的 conn 非空时在调用方事务执行，不允许另开事务掩盖窗口。

outbox 仅是内部可靠外部效果协议，不作为 Agent 可写通用队列。内部事务助手接受当前 conn，用同一事务入队；禁止组件各自先提交再补事件。`drain` 的 kind 限第 4 章，不能让 body 执行任意函数。

### 3.2 服务恢复与预算（M17-04、05）

`factoryctl service openapi|submit|poll|fetch` 沿用 gateway.md 参数和 JSON 形状，真实安装工具必须实现。`openapi_to_tools(document, allow=None)` 保留签名，M17 将 `None` 明确定为**空工具集**。旧纯函数 allow=[] 为兼容旧 M4 验收保留原筛选行为，但生产授权入口禁止传 None/[] 来开放工具。生产工具清单从 Machine.spec.operate.long_task.submit/poll 与 OpenAPI 的 method/path 精确匹配生成显式非空 operationId 列表；缺匹配则 OPENAPI_UNSUPPORTED。fetch 是受限结果表达式而非 OpenAPI 路径，只能经既有 fetch 通道取回授权任务的产物，不额外开放任意 HTTP 操作。这覆盖 gateway.md §5.2 中缺少任务上下文的默认推断。

```python
class ServiceOperations:  # mao.gateway
    def __init__(self, ctx: Context) -> None
    def get(self, job_id: str, key: str = "recipe") -> dict[str, Any]

class BudgetLedger:  # mao.budget
    def __init__(self, ctx: Context) -> None
    def balance(self, scope: str) -> dict[str, Any]
        # scope = job:<id> / agent:<name>:<UTC-date> / run:<id> / objective:<id> / objective:<id>:<UTC-date>
```

`get` 返回 §4.3；不存在用 `SERVICE_OPERATION_NOT_FOUND`。balance 返回 `{scope,cap_usd,spent_usd,reserved_usd,remaining_usd,unknown_cost,as_of_seq}`；金额有限且非负，remaining 可为负以显式展示事后超限。cap 可为 null 表示该层不另设限，不覆盖上层限额。内部 reserve/settle/release 按固定 operation id 去重，同一 SQLite 事务；不向 Agent 暴露任意结算写接口。

### 3.3 运行反馈（M17-06）

```python
class ResultStore:  # mao.results
    def __init__(self, ctx: Context, epochs: EpochStore) -> None
    def get(self, run_id: str) -> dict[str, Any]
    def start(self, agent: str, line: str, inputs: Mapping[str, str | Sequence[str]], *,
              request_key: str, cycle_id: str | None = None,
              task_id: str | None = None, turn_id: str | None = None) -> dict[str, Any]

class ForemanQuery:
    def metrics_query(self, station: str, metric: str, *,
                      since: str | None = None, until: str | None = None) -> dict[str, Any]
```

get 返回 `{id,line,epoch,state,trigger,nodes,outputs,error,budget,receipts,as_of_seq}`；nodes 按 run_nodes.ord，每项 `{node_id,job_id,state,outputs,error,cache_hit}`；receipts 是相关既有回执 id 列表。不存在沿用 RUN_NOT_FOUND；秘密沿用 shared 查询打码。

start 返回原有 `{id,state}`；同一 agent/request_key、相同规范化参数返回同一 run，不同参数抛 RUN_REQUEST_CONFLICT。身份由 Session 提供。cycle_id/task_id 必须同时存在或同时不传。带周期时按 §5.6 校验授权并原子建立 run、关联和预算保留；不带时沿用 Agent.lines 旧权限。旧调用不传 request_key 时仍可运行，但响应附注日志标为 legacy_non_idempotent；控制面不谎称可对未知旧请求去重。M17 自治路径必须显式传 key。

新增 shared MCP `run_get(run_id)`；扩展 worker `run_start(line,inputs,request_key?,cycle_id?,task_id?)`，不加入 foreman。扩展 metrics_query 的 since/until 可选参数。

### 3.4 宏观目标与周期（M17-07）

```python
class ObjectiveStore:  # mao.objectives
    def __init__(self, ctx: Context, epochs: EpochStore) -> None
    def authorize(self, objective: str, grant: Mapping[str, Any], *, by: str) -> dict[str, Any]
    def get(self, objective_id: str) -> dict[str, Any]
    def list(self) -> list[dict[str, Any]]
    def pause(self, objective_id: str, *, reason: str, by: str) -> dict[str, Any]
    def resume(self, objective_id: str, *, by: str) -> dict[str, Any]
    def revoke(self, objective_id: str, *, reason: str, by: str) -> dict[str, Any]
    def collect(self) -> list[str]  # daemon tick 内，返回新建 cycle id
    def commit_plan(self, cycle_id: str, plan: str, *, by: str,
                    turn_id: str | None = None) -> dict[str, Any]
    def dispatch(self, cycle_id: str, task_id: str, *, by: str,
                 turn_id: str | None = None) -> dict[str, Any]
    def evaluate(self, cycle_id: str, evidence: Mapping[str, Mapping[str, str]], *,
                 by: str, turn_id: str | None = None) -> dict[str, Any]
```

授权结果及 get/pause/resume/revoke 返回 `{id,asset_id,grant,state,next_due_at,active_cycle,spent_usd,reserved_usd,reason}`；list 按 id。authorize 初始 state=active、next_due_at=now，asset_id+相同 grant 幂等；不同 grant 拒绝 OBJECTIVE_STATE_INVALID。状态只有 active/paused/revoked/expired；revoked/expired 不可 resume，pause/resume 不延长 expires_at。collect 在许可有效、到期且无活动周期时建一个 discovering 周期，并可靠投 foreman。停机错过多个周期时只补一个，避免补跑风暴。

commit_plan / evaluate 返回 `{id,objective_id,number,state,plan_asset,checks,evidence,next_due_at}`，checks/evidence 初始空。所有资产与 evidence 引用固定版本。by 来自身份边界，不允许工具参数伪造。commit_plan/dispatch/evaluate 必须提供有效 running foreman turn 的 turn_id，租约/周期绑定也必须匹配；缺失则 OBJECTIVE_FORBIDDEN。work计划首次提交时还按5.7预留一次closeout轮费用，不足时不提交该计划。dispatch 返回 `{cycle_id,task_id,message_id,state}`，state=pending/delivered/done，重复调用返回原派工。

```text
factory objective authorize ASSET --grant FILE --by NAME [--json]
factory objective ls|show [ID] [--json]        # ls 不带 ID；show 必填
factory objective pause|revoke ID --reason TEXT --by NAME [--json]
factory objective resume ID --by NAME [--json]
```

成功退出 0、evaluate failed 退出 1、用法错误 2；FactoryError 沿用现有 CLI 映射。authorize/revoke/resume 只有宿主管理 CLI；MCP 没有授权工具。常驻 foreman MCP 新增 `objective_get(objective_id)`、`objective_list()`、`objective_commit_plan(cycle_id,plan)`、`objective_dispatch(cycle_id,task_id)`、`objective_evaluate(cycle_id,evidence)`、`objective_pause(objective_id,reason)`。worker/reviewer 仅有 objective_get/list 只读；worker 的 scope 与派工以 §5.6 为准。工具仅在 continuity.enabled 时加入。

## 4. 数据格式

### 4.1 配置与迁移

`factory.json.continuity={"enabled":true}`，缺省 false。启用要求 assets 段已按 assets.md 启用、foreman.mode=resident、已配置 Agents 和 governance；不满足抛 CONTINUITY_CONFIG_INVALID，不隐式修改旧工厂。§5.1–5.5 的可靠性 bugfix 无 opt-in，§5.6 目标自治才受本开关约束。

追加迁移，冻结顺序 `043_delivery.sql`、`044_execution.sql`、`045_objectives.sql`；实现前确认 M16 实际尾号，只能追加，若冲突先按设计维护更新映射，不能改历史迁移。

迁移所有权固定如下；不得因先完成局部功能而提前创建后序迁移。M17-09必须依赖M17-07，确保数据库不会先升到046再忽略后来加入的045。合并M16后若尾号冲突，进入任何M17实现前仅按最大已部署编号N将这四个逻辑槽整体映射为N+1至N+4，并同步M17文档/任务路径；不改任何已部署SQL。已运行的M17物理迁移不可这样重编号，须另行追加兼容迁移设计。

| 逻辑迁移/责任任务 | 一次创建或增补的结构 |
|---|---|
| 043_delivery / M17-01 | agent_messages.enqueue_seq；agent_turns.owner/lease_generation；lease_generations、delivery_keys、effect_outbox、rollout_owner |
| 044_execution / M17-04 | service_operations、budget_entries、run_requests（含nullable cycle_id/task_id，不要求尚未创建的目标表才能运行） |
| 045_objectives / M17-07 | execution_origins、objective_bindings、objectives、objective_cycles、objective_dispatches；agent_turns.objective_id/cycle_id/task_id |
| 046_operability / M17-09 | operability.md §4.1的全部六张表；M17-10/11只使用，不改这条已交付迁移 |

| 表/增补 | 必需字段与约束 |
|---|---|
| agent_messages | enqueue_seq INTEGER，UNIQUE；迁移旧行按 created_at,id 稳定回填，再由持久单调计数分配，不能复用删除的序号 |
| lease_generations | resource PRIMARY KEY, generation INTEGER NOT NULL；跨 leases 删除仍递增 |
| agent_turns | owner TEXT, lease_generation INTEGER, objective_id, cycle_id, task_id nullable；提交与每次副作用校验，旧终态行可为空 |
| execution_origins | kind, execution_id, objective_id, cycle_id, task_id nullable, parent_kind, parent_id；PRIMARY KEY(kind,execution_id)，由控制面事务创建，不接受模型写入 |
| objective_bindings | kind, registry_key, objective_id, activation_epoch；PRIMARY KEY(kind,registry_key)，目标内新增岗位/产线的来源绑定 |
| delivery_keys | kind, source, target, message_id；PRIMARY KEY(kind,source,target) |
| effect_outbox | id PRIMARY KEY, effect_key UNIQUE, kind, body JSON, state(pending/claimed/done), owner, lease_generation, claim_until, revision INTEGER, attempts INTEGER, next_at, error JSON, created_at, updated_at |
| rollout_owner | slot INTEGER PRIMARY KEY CHECK(slot=1), changeset_id UNIQUE；与 candidate 同事务更新 |
| service_operations | job_id, operation_key, request_digest, provider_digest, state, idempotency_key, task_id nullable, outputs JSON, error JSON, updated_at；PRIMARY KEY(job_id,operation_key) |
| budget_entries | id PRIMARY KEY, operation_id, scope, kind(reserve/settle/release/unknown), amount_usd, at；UNIQUE(operation_id,scope,kind)，对应实际 model_usage 由唯一关联键防重复 |
| run_requests | agent, request_key, args_digest, run_id UNIQUE, cycle_id nullable, task_id nullable；PRIMARY KEY(agent,request_key)，目标任务另 UNIQUE(cycle_id,task_id)（非空时） |
| objectives | id PRIMARY KEY, asset_id, grant JSON, state, next_due_at, active_cycle nullable, reason, updated_at；asset_id UNIQUE，同一版本授权重试幂等，不能隐式换 grant |
| objective_dispatches | cycle_id, task_id, message_id UNIQUE, state；PRIMARY KEY(cycle_id,task_id)，与邮件/outbox 同事务 |
| objective_cycles | id PRIMARY KEY, objective_id, number INTEGER, state, plan_asset nullable, checks JSON, evidence JSON, watermark INTEGER, context JSON, created_at, finished_at nullable；UNIQUE(objective_id,number)，每目标至多一个非终态周期 |

execution_origins.kind 为 agent_message/turn/run/job/service/model_request/changeset；父为相应执行kind或cycle/task。turn父是其实际首条agent_message；objective_cycle邮件父为cycle/id，dispatch邮件父为task/(cycle_id+"/"+task_id)，控制面生成的根邮件也登记origin。task_id仅在任务路径非空。

日期 UTC ISO8601；JSON 用 canonical_json；金额比较使用项目既有数值精度并在同一事务读账本，不用进程缓存决定授权。新表都在现有 factory.db，通过 M16 冷检查点一并保存。资产及引用加入 M16 保留根，未完成周期/未知外部结果/待结算账目不可被 retention 清理。

outbox.kind 仅 `epoch_refs`、`rebuild_run`、`agent_mail`；effect_key 分别为 promotion/cs、rebuild/oldrun/epoch、mail/target/source。payload 含精确对象 ID；只能调固定处理器。重试指数退避（1s 起，60s 上限），不删除未解决错误；一次 drain 最多 limit 条，失败不阻塞其他键。

### 4.2 目标与计划资产

M16 扩展 kind `objective` 的 data：`{intent,observation_scope,contract}`，intent 非空宏观方向，observation_scope 非空来源/限制文本，contract 是已由人授权的 M16 contract 资产 id。它不是预填具体任务列表。

grant 固定字段：`{agents:[name],lines:[完整Line key],total_usd,daily_usd,every_s,expires_at,max_cycle_s,extensions?}`。数组非空去重；agents/lines 必须存在于当前 active，且至少一对 worker 的 Agent.lines 包含允许 line；金额正数，60<=every_s<=86400，max_cycle_s>=60 且有限，expires_at 在将来；负责人固定 foreman。长期授权只允许这些集合的交集，Registry 更新不得超出 grant 的精确集合或下述预授权扩展范围。grant 变化需新 objective 资产版本并单独授权；撤销旧目标后可授权新版本。authorize 还固定 contract standards 的摘要，不允许换标准而继续旧授权。

extensions 缺省为空；可选结构 `{agent_prefixes:[name前缀],line_prefixes:[name前缀],max_risk_tier:T1|T2|T3,policies:[完整Policy key]}`。前缀只匹配 Registry metadata.name，以点分命名空间边界匹配，例如 research. 仅匹配 research.*；禁止空串/*。授权时固定 policies 的内容摘要，后续 policy 同 key 改内容不得沿用。目标内新建/升级岗位与产线需在前缀内、依赖闭包只用这些固定 Policy、每个实际 ChangeSet 风险不高于上限并通过原治理签核；不是靠自然语言允许任意能力。新增 Agent/Line 激活时写 objective_bindings，定时/入站/下游触发不得变成无来源的 legacy 工作；没有计划 task 时只向目标负责人发观察事件，不直接开生产 run。超出扩展范围才需要新的人的授权；范围内的业务扩展可以自主完成。

M16 扩展 kind `objective_plan` 的 data：

```json
{"cycle_id":"oc-1","observations":[{"asset_id":"asset:sha256:...","note":"来源与发现"}],
 "candidates":[{"id":"c1","reason":"为什么值得做","evidence":["asset:sha256:..."]}],
 "selected":["c1"],"disposition":"work",
 "tasks":[{"id":"t1","candidate":"c1","agent":"worker","line":"Line:publish@1","inputs":{"brief":"sha256:..."}}],
 "review_after_s":3600,"reason":"选择理由与预期结果"}
```

objective.requires 必须包含 contract；plan.requires 必须包含 objective 资产、所有 observations/candidates.evidence 引用，并引用所需业务材料。校验 data 中的 id 与顶层 requires/beans/evidence 一致，引用校验、反向边、目标/周期根注册与状态写入同事务，不能只把 id 藏在 data。

disposition 为 work/idle/blocked；observations 非空且引用真实未释放资产（其 Bean 来源由 M16 校验）；work 时 candidates/selected/tasks 非空，任务候选引用合法、task id 唯一。idle/blocked 的 selected/tasks 必须空；reason 非空，说明证据和等待条件。review_after_s>=every_s、<=86400；blocked 另可靠通知 foreman 的异常卡片。Agent 不能伪造外部来源和事实；结构检查只证明引用与协议，不替模型证明观察内容正确。领域判定用固定 contract 和真实执行。

### 4.3 运行与外部任务数据

`service_operations.state` 为 prepared/submitting/submitted/polling/fetching/completed/failed/outcome_unknown；输出持久任务 ID、供应商配置摘要、参数摘要及已取回文件摘要。不保存凭据。`get` 返回 `{job_id,key,state,task_id,outputs,error,request_digest,provider_digest}`。

`run_result` 邮件 source=`run:<id>:terminal`，body=`{run_id,state,epoch,cycle_id,task_id}`，完整结果通过 run_get 查；不灌全厂 Bean。历史终态 run 按 trigger.agent 回填通知键，先记录迁移水位，只补启用 M17 后创建的 run，不因升级重放全厂旧结果。

metrics 返回 `{station,metric,value,sample_count,since,until,run_ids,as_of_seq}`。station 解析为完整 active Station key。窗口 [since,until)，默认 since=null、until=null（在当前数据库快照内不限制时间上下界）；只读同一事务快照。按含该完整 Station key 节点的终态 run 去重；success_rate=SUCCEEDED/全部终态 run；gate_reject_rate=存在 Gate reject 事件的 run/全部终态 run（即使重试成功仍计该事件）；cost_per_run_usd=这些 run 的实际 spent_usd 之和/数量，不把工位单价混成全产线成本。CANCELLED 计分母但非成功。cache run 计一次、费用按实际入账；零样本 value=null，不伪装 0/1。返回 run_ids 支持审计，未来分页须另改契约。

## 5. 行为

### 5.1 投递、领取、排序和所有权

- `WakeQueue.next` 事务内条件领取，只在 rowcount=1 时返回；不同连接竞争最多一个成功，失败返回 None 或继续尝试下一张，不返回未取得的票。
- schedule 的 delivery_keys.source 包含 agent、订阅版本/索引和本次到期时刻；邮件 source 保持旧 schedule:<index> 格式，消息、水位同事务。wake claim 与 mailbox 投递同事务。独立 `agent_send` 每次调用仍是独立消息，不因相同文本被合并。
- 可用邮件按 enqueue_seq 升序；not_before 未到的消息不挡后面的已到消息。同批失败重试保留原序号，后来的撤销不能随机排到原命令之前。旧数据同时间缺真实顺序时明确只能稳定回填，不能声称恢复未知历史。
- agents 租约取得后加载当前 generation。入队/领批/创建 turn/提交 turn 与消息确认均在有效 generation 下校验。失去租约置停止事件，不启动新轮次，终止或隔离在途进程，禁止旧宿主写入成功结果。外部副作用用 §5.3 协议限制，不声称租约可以撤销已发出的远程请求。
- 恢复只处理持有者已失效的 running turn；重置 delivered 为 pending 或按既有重试上限 dropped，并写 interrupted 事件。可恢复来源不能永远停在 taken/delivered。故障退避使用既有时限，不热循环。

### 5.2 宿主与发布

真实 CLI serve 从装配完成的 daemon.epochs 获取治理实例，按已配置模式启 AgentHost；不能依赖外部调用者有没有额外传 epochs。常驻厂长和复核器职责不混用。启动 CLI/沙箱、解析、超时、工具错误、失租都经统一结案；退出0且空/畸形 transcript 不算完成，要求 runtime 声明的结构化 result 有一次可解析成功终态。支持 resume 的岗位还必须有非空 session id；失败保留 pending/retry，不能凭空产出 session。此处增强 agents.md §5.3 旧成功契约。

safe_mode 的 T2+ 向前推进在 verified→shadow、shadow→canary、canary→active 每处检查；安全回滚始终可用。发布槽在同一事务统计 shadow/canary/active 观察期；并发额度与单 candidate 槽均须满足。验证状态 verifying 在进程重启或临时 I/O 错误后可从头重入，不能重复受理同一变更。确定性规范错误才 rejected；暂态故障保留诊断并退避。

推广数据库事务同时更新三个指针、ChangeSet、转换 resolved、发布事件及 refs/rebuild outbox；事务失败全部不变。提交后同步 Git refs；失败时留 pending，治理写入屏障保持，启动先重放。rebuild 失败只写失败状态和去重唤醒，不撤销推广、不重复建 run。

Agent 增改删必须准确声明；验证阶段对漏声明拒绝（REGISTRY_INVALID/声明不符既有码），schema 接受 Agent。实际所有可加载 kind 都对比，未知 kind fail-closed。新风险最低等级：新增/弃用 Agent=T2；退役沿用 T3；物理删除仍由 V3_IMMUTABLE 拒绝；只改 charter/context/model/runtime=T2；扩大 lines、降低定时间隔/增加订阅/扩大 ingress、增加 turn/daily 预算或出网权限=T3；纯缩减权限和预算且其余字段不变=T1；组合取最高。保留更高的既有 Machine/Policy/宪法规则；按现有 T2/T3 signoff 配置判断，不强加每次人审。宿主 diff_facts 从版本差异计算 scope 分类，不信 ChangeSet.diff 自报：只减少 lines 集合、只删除订阅/ingress、提高现有 every、降低 daily/turn 金额/timeout 且其余内容不变时，全部替换为单一 field_changed("scope.reduced")；任何新增 line/订阅/ingress、缩短周期、提高预算/timeout 都额外给 scope.expanded。混合 charter/model/context 等改动保留原字段等级，未知字段 RISK_UNSCORED；policy 不同或无法证明为纯缩减按 T3。已冻结 schema/risk.mrl 的 Agent 项是授权设计输入，M17-03 只实现事实抽取，不临时编辑 L0。

每次装载也验证身份、runtime、lines、策略与已激活快照，不能靠旧缓存接回已删除岗位。

### 5.3 服务真实路径与副作用恢复

LocalProvider 的 `commands` 和默认命令映射补入的 argv 前缀都是宿主命令词元（包括解释器、脚本、固定参数），不得再次按虚拟挂载重写；原调用 argv 的剩余参数才按既有最长前缀规则映射。未命中命令映射时，原 argv 全部沿用旧虚拟路径规则。`open(spec.command)` 和 `exec(argv)` 必须使用相同来源区分；不凭文件是否存在或特判 sys.executable 判断来源。env/cwd 和虚拟参数的映射保持旧契约，LocalProvider 仍仅为无隔离测试提供者。这是对 sandbox.md §5.6 歧义的精确增补，不扩张为生产隔离。

真实客户端的 openapi/submit/poll/fetch 必须在受控服务通道执行，沿用出网与秘密规则。缺命令是协议/设施错误，不能统一包装成服务健康故障。agent-cli 工位先启动 operator，让其用白名单工具调用服务；其 exit/transcript、预算、输出和 Gate 都要成功。脚本工位沿用 recipe。

同一 job/operation_key 在提交前写 prepared 与摘要；原子转 submitting，固定 idempotency_key。取得响应时持久 task_id 后才 poll。若提交无响应，支持供应商幂等协议则用原键对账；不支持就 outcome_unknown，暂停该操作、投负责人异常，人工或现有可审计业务对账机制解决前禁止重新提交。task_id 已知时重试只 poll/fetch，provider/参数摘要改变不得复用。完成文件原子落盘及摘要后才 completed/Gate。中断在每一边界均可恢复；lease 失效后的旧结果不可覆盖新拥有者。

### 5.4 持久成本

ModelProxy 从持久 model_usage/预算账本读累计；重启不清零。用唯一请求关联防止重复结算。既有 job 超限语义是严格 spent>cap 后拒绝后续请求，不能把此次修复改成兼容性破坏；请求前有可靠上界时先保留，否则标记未知成本并按已声明策略限制并发，不能承诺未知供应商费用的绝对零超支。

真实 factoryctl budget 优先读可原子替换的 budget.json，兼容无 --json 的文本输出；不存在时保留旧离线格式并明确未知，不返回虚构余额。真实代理请求→入账→model.metered→文件→CLI 都有对应验收。run/agent/目标费用引用同一执行账目聚合，不相互重复计费。

### 5.5 结果闭环

run 的终态事务入 agent_mail outbox。目标 task 的终态还投一封服务端产生的 objective_task_result 给 foreman，绑定 objective/cycle/task/run 与原始 event seq，使厂长下一轮能安全评价；不靠自由文本取得 cycle 身份。若旧调用路径漏 outbox，恢复按 terminal event seq 补齐；相同逻辑通知只一封，处理邮件可 at-least-once，业务动作必须通过 request_key 幂等。成功、失败、取消都通知原发起岗位，岗位已删除则发 foreman 异常，不能无声丢失。query 返回稳定快照，不把运行 ID 当 job ID。

### 5.6 宏观目标的持续周期

1. 人注册 objective 资产、授权固定领域契约与 grant；控制面验证配置、来源、标准摘要和 scope。宿主 CLI 是管理信任边界，`--by` 只是审计标签，不是密码或独立人审证明；Agent 只有受限 MCP，不能拿任意 shell/宿主配置写权限再宣称受治理。若部署将管理 shell 给外部代理，本系统无法靠字符串区别真人。
2. collect 在到期时建 discovering 周期，记录事件 watermark，原子投 `objective_cycle` 邮件给 foreman。Agent 根据 observation_scope 用现有工具/worker 获得新证据、发现候选并记录 immutable objective_plan；commit_plan 验 scope、版本、观察引用和预算，不替 Agent 排序候选。
3. work 计划提交后 state=executing。foreman 用 objective_dispatch 发送由控制面构造的持久任务信封（复用 Mailbox）；agent_send 仅供协作，不作为可信任务绑定。worker run_start 必须带 cycle/task/request_key。服务端查固定计划、调用身份、inputs 摘要、Agent.lines 与 grant 交集；每个 task 只能绑定一个 run，重试同 key 不重复执行。任何一个越界、撤销、过期、标准 stale 或预算不足都拒绝新动作。同一周期任务用不同 request_key 也只能绑定一个 run，先比较 UNIQUE(cycle_id,task_id) 并核验相同参数；不同参数拒绝。commit_plan 相同资产重试幂等，不同资产在已有计划后 OBJECTIVE_STATE_INVALID；计划状态、版本、派工 outbox 同事务。来源与成本按 §5.7 继承，不能通过省略参数转为 legacy。
4. evaluate 只能常驻 foreman 调用；evidence 按 M16 `case_name->{kind:job|turn,id}`，调用方不传 passed/facts；evidence 内部额外键用 OBJECTIVE_EVIDENCE_INVALID 拒绝，顶层必填参数缺失仍是 MCP -32602。使用 §5.8 明确的目标评价接口，复用 M16 已授权领域标准及表达式构造，不调用不兼容的 open_trial 或增加它的 purpose；要求执行晚于周期 watermark、属于该周期 task 的 run/相关岗位的本周期 turn、终态且上下文匹配。结果检查保留摘要；重复评价返回同一终态；不同证据改写终态拒绝。合法且全部 case 通过才 completed，否则 failed；无效/旧/关联错误的引用抛 OBJECTIVE_EVIDENCE_INVALID 并保持 executing，不能借错误证据提前结束周期；进程0不代替领域判定。
5. idle 计划提交即周期 state=idle；blocked 计划为 blocked；这两种与 work 的 completed/failed 都是周期终态，必须原子写 finished_at、清除 objective.active_cycle 并下一次 next_due_at=finished_at+review_after_s。失败通知负责人，下一周期可在原授权内调整具体工作；不能修改过去结果/标准。max_cycle_s 超时周期 failed，通知负责人并取消尚未派发任务；在途 run 的预算/外部不确定操作继续保留并对账，新周期不能重复该副作用。
6. 总预算/日预算耗尽或标准 stale 将目标 paused，发一次去重异常；UTC 次日可自动解除仅日预算 pause，总额耗尽仍需新授权。人 pause、revoke、到期都停止新周期及新任务；不假称撤销已经发生的外部动作。paused 保留当前周期以便人工 resume；revoked/expired 关闭未派发任务，在途结果继续可查。重启恢复同一周期/计划/任务/run，不复制产线。
7. 没有任何新工作时保持低频观察；不存在仅为了“常驻”无预算热循环的模型调用。控制面不会因为代理完成一个空轮次就宣布目标达成。

### 5.7 可信来源、派工与预算保留

Session 末尾新增内部可选 `turn_id/lease_owner/lease_generation/objective_id/cycle_id/task_id`，由 AgentHost 的启动通道从数据库加载，客户端 tools/call 参数不能设置或替换。MCP 每次改变状态的调用、代理外发前、邮箱派发、run 创建和结果提交都在相应事务检查 token；失租的旧 MCP 句柄不能继续提议变更、派工、请求秘密或付费。远端响应已发出时即使失租仍要以唯一请求 ID 记录费用和不确定结果（不能丢成本），但不能再启动新动作或确认旧 turn 成功。

同一 turn 的消息批次只取按 enqueue_seq 顺序的同一来源上下文连续前缀。周期邮件、dispatch、remind、agent_send、子 run/job/服务/model request 都通过 execution_origins 继承 objective/cycle；用户不能通过自由文本声明来源。周期 worker 的 run_start 省略 cycle/task 时从可信 Session 补齐，与传参不符拒绝；Session 绑定 task，查计划和调用身份。周期内派给别的岗位仍继承授权，超出 agents/扩展范围拒绝，不得投成普通邮件。目标内变更创建的订阅/触发按 objective_bindings 继承；新角色加载也不能丢来源。普通人的独立邮件和原有已配置岗位的既有独立 schedule 保留 legacy，但周期发起者不能制造一条无来源消息逃逸；这些独立工作不借用目标权限/预算，也不算其成果。

发现/规划/worker turn 启动前按其有效 cfg.turn_usd 为目标总额及 UTC 日额预留；生产任务 run_start 按路由后产线声明 budget.cap_usd 预留，run 内 job/model 请求只消耗此保留，不在目标层再次加一份；厂长/worker 的协作轮费用是该 turn 的独立成本。计费链每条真实请求只有一个唯一 operation_id，对账时逐级替换所属保留为实际结算，完成/确定失败释放未用差额；unknown 保留不释放。UTC 日期按发起请求/预留时固定，跨午夜结算仍记原日，不能转到次日逃额度。每天可用额考虑当日保留和未知额。run 最大cap大于可用额时 OBJECTIVE_BUDGET_EXCEEDED，但若未真正耗尽则目标保持 active，厂长可选小任务；总额/当日已spent+reserved达到cap才按 §5.6 暂停。

work计划在派工前为最终评价预留一次有效foreman轮上界 `closeout_usd=foreman有效turn_usd`，独立于run和当前发现轮费用。此保留绑定cycle和预留当日，直到全部task得到终态/blocked未知结果后才发服务端closeout通知并启动一次closeout_only厂长轮；普通task结果邮件仍可靠入队，但不逐条强制触发付费评价。即使总额/日额已用尽而paused，也允许使用已保留的closeout费用查结果和evaluate，禁止新plan/dispatch/付费子任务。该轮成功后结算并释放差额；若其本身失败或失租，不在无余额时无限重试，确定性记录cycle failed(reason=closeout_failed)、保留所有真实结果并通知管理面，不能伪称领域通过。重启可恢复尚未消费的同一保留；UTC日界不改归属。idle无需此保留。revoke/expire后不再启动任何新的付费closeout，确定性终止未完成周期为failed(reason=authorization_stopped)，保留在途结果对账。

目标模式的 turn/run/服务必须有可验证最大费用上界，unknown 价格或供应商不能给出上界时拒绝新付费提交并标为 blocked；不支持从 token max 或 cfg 上限强制保证的费用必须显式报告不支持，不能把 turn_usd 当供应商保证。已发请求响应丢失计 unknown_cost 并保留上界，不能继续重复消费保留。非目标旧 job 的事后超限兼容按 §5.4，不把它误称为严格事前保证。最小预留单位、耗尽比较使用同一金额规范化与事务，补两个并发争用最后额度、跨午夜及失租结算测试。已有 turn 成功预留后，即使总 reserved 达上限，仍可在该预留内完成计划/idle/评价等不增加付费的收尾；只禁止新增付费保留。AgentHost 无 model_usage 时沿用 transcript 实际费用回退，有 model_usage 时以其为准，不能两者重复累计；离线假 CLI 报告费用只用于协议测试。

ResultStore.start 先查 agent/request_key（目标另查cycle/task）：相同参数只读返回已有run及原epoch，不重新授权或分桶，异参冲突。仅对新请求，以 line + ":" + canonical_json(inputs) 调用 EpochRouter.choose(key,line=line)，与既有CLI/talk分桶一致；request_key只用于幂等。路由和授权在同一快照决议校验，提交前重检候选状态/版本，固定所选epoch创建run。已有线按既有分桶；新线只在 candidate 时仅在对应 ChangeSet 已满足签核且处于 canary、绑定同目标、风险/Policy/命名空间符合 extensions 时允许。可使用该已验证 candidate 中同一既有 worker 的 Agent.lines 来取得受限试运行权，runtime 身份仍来自已运行的 active 岗位；不调度未激活的新岗位。shadow 阶段不能当生产启动；回滚/退出 canary 后拒绝新的候选试运行。已取得 request_key 的重试返回原 run/epoch，不重新分桶或产生新 run；已开始的 run 按原快照继续或既有取消策略处理。该路径同时解决自主新线无金丝雀样本的接线缺口。

### 5.8 评价、outbox 与冷恢复的精确边界

新增内部确定性接口（M17-06/07）：

```python
# mao.learning；不更改 LearningStore.open_trial 的 purpose/旧语义
# context 在 commit_plan 固定，包含 contract standards、计划任务的
# active/candidate epoch、实际 Agent/Station/runtime/model/Policy 依赖摘要。
def evaluate_objective_contract(ctx: Context, epochs: EpochStore, *,
        cycle_id: str, contract: str, plan: str, watermark: int,
        context: Mapping[str, Any],
        evidence: Mapping[str, Mapping[str, str]]) -> dict[str, Any]
    # {passed:bool, checks:[{case,passed,reason}], evidence:<规范化映射>}
```

cycle_id 为受测目标上下文，plan 为实际受测 target；scope 必须是 objective 的 factory scope，case subject/kind/断言仍按 M16 contract。watermark 在 collect 建周期时固定；context 在 commit_plan 固定，运行时由真实选定 epoch 记入 execution_origins，不能由调用方给当前结果改 context。job/turn 的创建与 RUNNING 必须在 watermark 后，真实归属当前 cycle，job 是绑定 task 的 run 节点，turn 是该周期受控派工后的岗位。uses_target 在目标契约中明确指“由该 plan 的 task 绑定且实际消费计划声明的全部输入”，并不冒充 M16 knowledge 的注入证明：worker 挂载固定 objective_plan 及其依赖、AgentHost 记录 asset_uses；job 的普通输入与固定计划 inputs 完全对应，缓存命中不能证明新能力。共享 M16 的已授权 standards 校验、out 字段构造与 MRL 求值助手，禁止复制成较弱的 passed 字段判断；不得创建伪 knowledge trial。证据重复用于同subject/kind的多个case仅在断言都成立时允许。相关标准/依赖变化 stale，拒绝评价并暂停目标；单周期评价与终态更新、保留根同事务，多个周期/目标不互相覆盖。

outbox 的领取必须有过期可重领的 owner/generation/claim_until，done 更新也检查 token。agent_mail 与去重键事务提交；rebuild 用 oldrun/epoch 稳定键与新 run 建立唯一绑定（扩展 run_requests 的内部主体 namespace），run 创建和业务键同事务，因此在创建后未记 done 的崩溃仍返回原run。epoch_refs 携带发布 revision，处理时从数据库最新指针重建 refs；旧 revision 不覆盖新决议。失败键退避，不阻塞其他消息；成功结果写 done 前重放仍安全。

活动周期/dispatch/run/unknown service 与关联资产、执行日志、epoch 是 M16 保留根。data 中业务引用必须转为顶层 requires/beans/evidence 及现有反向边，注册和释放同 GC 写屏障；不能仅口头称“保留”。冷检查点照 M16 停机屏障保存；恢复副本清租约、递增 restore_generation，所有目标变 paused(reason=restore_needs_acceptance)，grant 不自动可用，outbox 只允许 refs 重建/结果对账，不发新的付费/生产动作。恢复验真并确认原工厂已停机或外部资源所有权已转移后，人通过已有验收流程及 objective resume 恢复原授权（已过期/撤销不恢复）；移交包不继承授权。已知 task_id 只对账/poll/fetch，unknown 不重发；不允许原工厂和恢复副本同时消费同一外部资源。


### 5.9 服务池输出隔离与旧验收矛盾（R01，M17-04）

M4旧节点test_pooled_reset_between_jobs在未改c538768也失败；其夹具两次复用同一个sandbox和同一个/factory宿主挂载，并把fetch目标字符串不同当作隔离证明。closure.md §5.3固定/factory/out端口布局，instances.md §4.2/§5.2允许同一实例job/out目录复用并要求stage/reset清理。因此路径名称相同本身不构成跨job泄漏；不能为了该断言改变所有服务的产物布局，也不能据此宣布生产已安全。

冻结隔离契约：Runner/Instances真实租借两个不同job，第一次在输出及服务工作区留下随机canary，stage_out后保留其不可变Bean/attempt结果；release清空job/in/out/ctl并执行reset/health，第二次stage_in及服务读取不得看到旧canary/旧ctl/task_id/认证上下文，Gate仅消费第二次attempt的真实新字节。复用固定虚拟路径合法，必须核对内容、归属和实际文件访问，不能仅比较路径或复用同一个手工sandbox代替池生命周期。

实际instance借用复用§3.1的fenced LeaseStore：resource=instance:<id>，owner=job:<job_id>:<attempt>，有效期覆盖既有job时限，recover/release不得仅凭同一实例名操作。mao.instances.Lease末尾可选增加token（mao.durable.Lease或None，旧手工构造保持兼容）；生产acquire必须装配token。stage_in/out、ctl效果、exec及release清理检查当前job与token；旧代不能执行、拷贝、清空或关闭新job资源。在途进程未停止或隔离前不把实例交给新job，外部已发请求仍按§5.3对账。reset非0、超时或health失败必须销毁而不复用；不能吞失败后仍ready，不改变已完成job的真实结果。

本条授权M17-04中的限定设计维护：保留原test_pooled_reset_between_jobs节点、reset前后执行证据及隔离目标，把该函数及仅它使用的必要夹具改为真实Runner/Instances的两个job，路径不等断言改为实际旧输出/上下文不可见与新输出归属校验。再新增独立tests/system/test_m17_04_pool_isolation.py::test_pooled_jobs_isolate_outputs_and_reset_failures，含正常清理、reset失败/超时销毁、旧handle迟到及故意禁用清理的失败对照。不得删用例、skip/xfail、替换被测网关/池、仅更改期待值、降低断言或改其他M4/M16测试；先审阅设计维护差异并只同步该文件哈希，再做实现验收。本轮只冻结此维护方案，不改旧测试或生产代码。

## 6. 错误

| 错误码 | 触发与 detail |
|---|---|
| `LEASE_LOST` | 旧拥有者或过期代数尝试写入；resource,owner,generation |
| `AGENT_RESULT_INVALID` | 缺结构化 result/session；agent,turn,reason |
| `SERVICE_OPERATION_NOT_FOUND` | 未登记操作；job_id,key |
| `SERVICE_OUTCOME_UNKNOWN` | 提交可能成功但无法对账；job_id,key,phase |
| `SERVICE_OPERATION_CONFLICT` | 同操作参数/供应商改变；job_id,key |
| `RUN_REQUEST_CONFLICT` | 同请求键参数改变；agent,request_key |
| `CONTINUITY_CONFIG_INVALID` | 启用前置条件不满足；reason |
| `OBJECTIVE_INVALID` | 目标/计划/授权结构错误；field,reason |
| `OBJECTIVE_NOT_FOUND` | 目标或周期不存在；id |
| `OBJECTIVE_FORBIDDEN` | 身份或任务越界；id,reason |
| `OBJECTIVE_STATE_INVALID` | 无法执行的状态转换；id,state,operation |
| `OBJECTIVE_BUDGET_EXCEEDED` | 目标层无法保留所需预算；id,scope,remaining_usd |
| `OBJECTIVE_EVIDENCE_INVALID` | 来源、窗口或关联不匹配；cycle_id,case,reason |

既有错误继续使用，不为了一个新测试改旧码。detail 不包含秘密、模型原文或敏感 URL。新码在对应实现任务登记 CODES；本次设计不改 src。

## 7. 验收测试

诊断与冻结契约放在 `plan/evidence/m17/`，显式路径运行；不进入 `pytest tests` 默认收集，不削弱 M16 回归。整个目录的源证据受 protected.lock 保护。未来若迁入默认 acceptance 目录，需设计维护同步登记，不得直接移动来规避门槛。每项 accept/gate 都要求 failed/errors/skipped=0；基线红测只证明问题存在。

| 文件 | 冻结测试或场景 | 必须证明 |
|---|---|---|
| `plan/evidence/m17/repro/test_host.py` | `test_wake_claim_is_exclusive_under_concurrency`、`test_taken_wake_survives_mailbox_write_failure`、`test_schedule_post_is_not_duplicated_across_interruption`、`test_batch_order_matches_chronological_prompt`、`test_lost_agents_lease_stops_host` | M17-01：竞争、故障点、顺序与失租，5 项 |
| 同文件 | `test_cli_serve_starts_configured_resident_host`、`test_cli_launch_error_does_not_strand_delivered_mail`、`test_empty_successful_cli_output_is_not_completed_work`、`test_local_agent_diagnostic_fixture_can_complete_mail` | M17-02：真实入口、异常恢复与正向对照，4 项 |
| `plan/evidence/m17/repro/test_governance.py` | `test_safe_mode_stops_existing_t2_shadow`、`test_safe_mode_stops_existing_t2_canary_promotion`、`test_failed_promotion_keeps_database_and_git_pointers_atomic`、`test_rebuild_failure_preserves_promotion_and_wakes_foreman`、`test_existing_shadow_blocks_second_changeset`、`test_undeclared_new_agent_cannot_hide_in_changeset`、`test_verifying_resumes_after_transient_epoch_failure` | M17-03：7 项；Agent 漏报在验证阶段 rejected，见 §5.2 |
| `plan/evidence/m17/repro/test_execution.py` | `test_e01_real_factoryctl_runs_service_job`、`test_e02_openapi_default_does_not_export_unrelated_operations`、`test_e03_agent_operator_runs_for_service_station`、`test_e05_fetch_retry_reuses_known_external_task` | M17-04：4 项，后两项夹具隔离客户端缺失；总验收用真实客户端串联 |
| `plan/evidence/m17/deployment/test_host_paths.py` | `test_mapped_host_script_and_virtual_argument`（exec/start两参数） | M17-04：真实宿主脚本保持可执行，原虚拟参数继续映射；PR2单模块对照不代替整体验收 |
| 同文件（test_execution.py） | `test_e04_model_proxy_restart_keeps_job_budget`、`test_e06_factoryctl_budget_reports_live_balance` | M17-05：2 项，需补完整代理→账本→CLI 单元链 |
| `plan/evidence/m17/repro/test_feedback.py` | `test_terminal_run_returns_to_initiating_agent`（成功/失败两个参数）、`test_metrics_query_returns_evidence_not_only_echoed_arguments` | M17-06：3 项 |
| `plan/evidence/m17/acceptance/test_agent_risk.py` | 5个顶层函数、7个参数化用例 | 冻结L0新增/弃用/退役/字段/缩减/扩大/未知风险输入 |
| `plan/evidence/m17/acceptance/test_live_validator.py` | 25个合成报告正反例 | 只读校验器结构、文件摘要、SQLite引用、防越界及缺证据拒绝；不是真实live |
| `plan/evidence/m17/acceptance/test_objectives.py` | 12个顶层函数、13个用例，见该文件与 cases.yaml 的 M17-07 场景 | 授权、实际信息导出计划、范围、评价、时钟、重启、空档、撤销、预算；零跳过 |
| `tests/system/test_m17_04_pool_isolation.py` | `test_pooled_jobs_isolate_outputs_and_reset_failures` | R01：与旧M4 reset节点共同验收§5.9；真实两个job的内容/上下文隔离、失败销毁与旧handle拒绝，二者至少2用例 |
| `plan/evidence/m17/cases.yaml` | M17-08 全部场景 | 至少四周期、两个不同工作、真实进程入口与故障恢复；全部记录 event/turn/job/run/receipt ID |

旧M4完整测试清单仍由gateway.md第7章维护；本规范只增补reset节点的§5.9语义和限定设计维护边界，两个精确节点均由closure.yaml和M17-04/最终门执行。

额外精确节点和任务依赖见operability.md第7章及closure.yaml；原C10之外还须四格真实witness和index校验，任一缺失不可通过。新能力的可执行验收与完整跨进程 runner 的编写方式见 M17-07、08 任务卡。M17-08 不得仅运行文件存在检查或把 cases.yaml 自评为 passed；实现者须增加 tests/system 下真实可运行的集成测试，并冻结 live 报告字段。离线脚本 Agent 与真实 CLI/Docker 分别报告；真实能力退出条件不能由离线脚本代替。M17-08还必须运行独立的真实live witness集成测试（cases.yaml live_witness_contract）；完整性脚本只是必要条件，不是live成功的充分条件。只读校验入口 `python plan/evidence/m17/verify_live.py --report PATH --factory-root ROOT` 返回JSON `{ok,status,issues,scope}`；缺文件、blocked、引用错误均exit1，通过exit0。校验仅验证报告和同一工厂一致快照引用的完整性，不自动认证模型判断或报告来源真实性。

## 8. 错误示范与正确示范

- ✖ 先 taken，下一事务投邮箱；✔ 同一事务写来源去重键、邮箱和水位，故障可重放。
- ✖ active 已切但状态仍 shadow；✔ 数据库决议一起提交，refs/outbox 重放恢复外部镜像。
- ✖ fetch 出错重新付费提交；✔ 已知 task_id 恢复取回，未知提交结果先对账。
- ✖ 外层脚本每小时下达具体任务并帮它签核；✔ 一次授权，厂内 Agent 从新资料中发现工作、经固定范围执行、评估并安排下一周期。
- ✖ 工厂进程运行或某次 run 成功就宣布自治；✔ 连续多个周期、无人接续、领域事实及故障恢复均有证据。

## 9. 风险与注意事项

- SQLite 事务不能包住 LLM、HTTP、Docker 或 Git 操作；outbox 和业务幂等承担边界，远程未知结果如实保留。
- 租约只限制工厂可控写入，无法撤销已经提交的外部动作；供应商幂等支持要固定版本并验真。
- 周期只是机会，不保证模型会发现有价值的任务。应固定领域评价与观测来源，在真实部署测量质量、成本及空转率。
- 源材料、日志和附件含任何命令都视为数据；不把它们变成控制面或人授权。历史轨迹含秘密，不纳入交付包。
- M16 正在变动：只在其完成后重放诊断、验证保留根和检查点，再启动 M17。不得降低旧阶段门、关闭审计/金丝雀来获得自治表象。
