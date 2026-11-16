# operability：M17 通用接入、部署与可操作性

> 规范性设计输入；未实施。对应 M17-09、10、11、12，M17-08 作最终验收。与 continuity.md 共同组成 M17；本文件只在明确列出的增补处优先于旧规范，不修改 M16 契约。证据基线 c538768；HC01–HC14/U01–U06 的覆盖入口为 plan/evidence/m17/closure.yaml。

## 1. 目的与范围

使已发现的 Grok 部署/运行补偿成为通用能力：可靠网络、真实运行时协议、声明与实际产品相符、非破坏部署、可信操作入口，以及帮助/错误/文档/skill 的完整使用流程。日常操作中的外部 Agent 提交人的目标和查看结果；厂内岗位自主发现、规划、派工、生产、评价和继续。

设计覆盖已登记的问题，不把“所有未知 bug 不存在”作为可证明承诺。冻结基线红测、历史静态线索与未来能力验收分别标明；历史因果未确认的网络问题仍要求通过协议正反例关闭，不能虚构已复现原因。没有实际资源时 live 为 blocked，M17 阶段门不得通过。不得以关闭审计、用别的产品冒名、伪造 session 或更改验收实现效果。

## 2. 依赖

- continuity.md 的可靠消息/fencing、受控服务、预算、结果、持续目标与长期授权；M16 资产/固定领域契约/冷检查点。M17-09依赖M17-07，先045再046；迁移所有权以continuity.md §4.1为准。
- cli.md、commands.md、daemon.md、agents.md、runtimes.md、operator.md、gateway.md、egress.md、governance.md、delegation.md、backup.md。
- 仅现有依赖与 Python 标准库。真实 CLI、Docker、产品实例是显式部署资源，不新增 Python 包、控制面模型决策循环或临时模型 SDK。
- 网络时间与 ID 使用项目注入的 Clock/IdSource；普通帮助、能力查询和 doctor 不得初始化 Context 或触发付费。

## 3. 接口

### 3.1 传输与可恢复请求（M17-09）

在 mao.transport 冻结以下公共接口；现有 ModelProxy/EgressProxy 的构造签名兼容，内部按 ctx.root 装载配置，并使用注入 Clock。

```python
@dataclass(frozen=True)
class Deadline:
    connect_s: float
    idle_s: float
    total_s: float

def load_transport(root: Path) -> Mapping[str, object]
    # 返回规范化配置；不写文件，不打开模型连接。
def resolve_service_location(location: str, *, base_url: str,
                             allowed_origins: Sequence[str]) -> str
    # 规范化 URL，失败 SERVICE_LOCATION_DENIED；每次重定向仍需校验。
```

`factory.json.transport` 格式见 §4.1。模型/服务每次真实外发继承已有 operation_id、执行来源和预算保留；没有可信来源不得从新网络路径绕开 continuity §5.7。deadline 取有效 job/turn 剩余时限、配置 total 的最小值。网络错误必须返回 `{code,request_id,phase,outcome,retryable}`；不含认证头、令牌、响应全文或敏感 URL 查询串。

### 3.2 接入资格（M17-10）

```text
factory runtime qualify RUNTIME@V --profile offline|live --out FILE [--manifest FILE] [--allow-paid --budget USD]
factory machine qualify MACHINE@V --profile offline|live --out FILE [--manifest FILE] [--allow-paid --budget USD]
```

输出为 §4.2 receipt；offline 用本地可计数协议上游和明确标记的协议子进程夹具，证明接线与正反例；它不证明已接入真实vendor CLI。live 运行声明的已安装真实 CLI/产品；live 若可能收费，必须同时给 allow-paid 和正数 budget，且有可强制的费用上界，否则拒绝。首次部署可给 --manifest，由qualify在专用临时工厂装配已校验的Registry/config/受控代理和预算，不要求生产serve已启动或生产root已初始化，避免先资格后安装的循环依赖；诊断不自动执行qualify。qualify 在专用隔离目录运行，不加入生产调度；不会为通过而改 runtime、机器声明或引用其他产品。CLI 的支持取决于实际版本和测试，不按名字特判。profile offline 的结果不能替代 live。

新增 mao.qualification 公共函数 `qualify_runtime(ctx, runtime_key, *, profile, budget_usd=None, allow_paid=False) -> dict`、`qualify_machine(ctx, machine_key, *, profile, budget_usd=None, allow_paid=False) -> dict`，与 CLI 完全复用；报告失败仍保存已发生费用和脱敏诊断。控制面只启动既有 CLI，不新增自行调用模型/选择工具的智能循环。

### 3.3 声明式部署和只读预检（M17-11）

```text
factory doctor --profile resident|local-test [--manifest FILE] [--json]
factory init --manifest FILE --plan [--json]
factory init --manifest FILE --apply --plan-digest SHA256 [--json]
```

init 必选 plan/apply 之一；没有 force/reset/自动清理 Registry 选项。manifest/root/config 摘要变化必须重新 plan。mao.deployment 的公共函数：

```python
def plan_deployment(root: Path, manifest: Mapping[str, object]) -> dict
def apply_deployment(root: Path, manifest: Mapping[str, object], *, plan_digest: str) -> dict
def diagnose(root: Path, *, profile: str, manifest: Mapping[str, object] | None = None) -> dict
```

doctor/plan 不创建 root/DB/租约、不启动 serve/容器、不请求模型、不自动安装依赖；已有 DB 只读且不改变 WAL。报告包含检查事实、ready/degraded/blocked、能力限制、错误码和具体下一步，见 §4.3。apply 仅可信宿主管理面可执行，并在无有效 serve/agents owner 时进行。服务仍在运行则 DEPLOY_BUSY；不得杀进程代替资源移交。

### 3.4 管理面与外部操作者（M17-11、12）

沿用宿主管理 CLI 作为可信管理边界。新增受限外部 CLI 模式，显式全局参数 `--endpoint DIR --credential-file FILE`，两者必须同时提供；endpoint 是共享文件邮箱目录，不是 URL，不开放新控制端口。该模式禁止本地 root/registry/local-commands 的回退，不直接打开工厂 DB、Git 或 SecretStore。外部 Agent 只挂载该凭据的邮箱和只读凭据文件，工厂数据/管理凭据通过不同 OS 用户或 Docker 沙箱隔离；同 OS 管理权限不视为可抵御主体。factoryctl 的既有 /factory/ctl 协议不变（D7）。

```text
factory access issue --subject NAME --expires-at UTC --out FILE [--json]
factory access revoke CREDENTIAL_ID [--json]
factory operation show REQUEST_ID [--json]
```

只有本地可信管理面有 access issue/revoke。issue 固定 scope=`operate`、root ID、subject、expiry 和随机 credential id，返回 credential_id/endpoint（不是秘密值）；秘密只写指定新文件，文件已存在则拒绝，POSIX 0600，Windows 等价用户 ACL。使用注入的安全 TokenSource，在 mao.base 的实际实现生成至少256位秘密；Clock/普通 IdSource 仍控制时间和公开 ID。秘密不输出/不进入 argv、日志或审计。serve 的 operations 邮箱处理器在同一 daemon tick 内按 owner fence 执行；无有效 owner 时客户端报告 pending/owner_unavailable，不自行打开 DB 处理请求。

请求先写临时文件再原子 rename 为 `requests/<request_id>.json`，格式 `{schema_version:"mao-operation-1",credential_id,request_id,command:[...],request_key:null|string,signature}`。signature=HMAC-SHA256(credential_secret, canonical_json(除signature字段的整个对象))；服务端在该credential专属邮箱中核验签名、scope/root/expiry/revoke，不接受actor/role等未知字段。响应同样原子写入 `responses/<request_id>.json`，含 `{schema_version,credential_id,request_id,state,result,error,signature}` 并签名，客户端不得把未签名/错ID响应视作成功。凭据文件含 id/root_id/subject/secret/endpoint/expiry，服务端 secret 受管理目录权限保护，不放在共享邮箱；DB存摘要和受保护存储引用。请求重复和目录内路径/symlink都必须校验，不能借交换目录读取管理文件。

server 使用固定 handler 映射，不执行任意 shell、不把输入转成本地 argv。允许操作：guide/capabilities/doctor、status/events、agent list/show/turns、talk send/ls/show、objective ls/show、line ls/show/nodes、job show、changeset ls/show、inbox ls/show、audit ls、receipt ls/show、operation show（只能自己的）。query 不需要 request_key；talk send 必须有客户端生成且稳定的 key，同 credential+key+相同载荷只产生一条消息，异参拒绝 OPERATION_CONFLICT。客户端等待30秒仍无响应，输出 `{request_id,state:"pending",recovery}`（不是目标已完成），退出0，并给 operation show 下一步；使用相同key重投不生成新消息。业务失败按原错误退出，不以 pending 吞掉明确拒绝。

`--by` 在受限模式如出现必须等于 `external:<subject>`，否则 OPERATION_FORBIDDEN；缺省由 server 补同一值。它只作审计标签，不提供 human 权限。talk 提议确认、objectives authorize/resume/revoke、签核、秘密回答、active/rollback、直接 line run/retry、Agent 调度修改、init/apply、任意文件/Git/SQL 均不在 operate 范围。普通操作不需要外层逐个派工：这些动作来自已有长期授权下的厂内 Agent。受限 talk.send 仅请求/建议或转达人类宏观方向，不能生成授权。真正用户确认在可信管理面完成并有独立记录。

旧本地 CLI 及其 by 参数为兼容保留，但 doctor resident 和 live witness 必须确认外部 Agent 不持有该管理 shell、工厂原始写权限或凭据目录；无法隔离则安全/零代签能力 blocked，不宣称仅靠命令标签防止管理员操作。operations.scope 不能配置成任意命令/administrator；需要管理动作就交回可信管理面。

### 3.5 可发现帮助与恢复（M17-12）

```text
factory guide [overview|setup|goals|recovery|results|permissions] [--json]
factory capabilities [--json]
```

guide/capabilities 在未初始化目录也可用，无副作用。所有顶层命令/subcommand 的 help 来自命令注册元数据：用途、输入、前置、角色/权限、是否变更或付费、结构化输出、下一查询命令和至少一个实际可运行示例；帮助直接显示主要内容，不能仅给外链。未知命令退出2，并指向 guide 和最相近合法命令，不能发明未注册命令。新元数据不得改变旧调用参数和退出码。

capabilities 返回 `{schema_version:"mao-capabilities-1",version,commands:[{path,summary,roles,mutates,may_charge,prerequisites,next,examples}],profiles:[...],guides:[...],skill:{name,path,version,sha256}}`。commands 必须来自实际注册树；未实现的命令/能力不得列 supported。guide 返回 `{schema_version:"mao-guide-1",topic,steps:[{purpose,commands,expected,next,on_error}],limitations}`，命令使用结构化 argv 模板，参数槽明确，不拼 shell。metadata 与输出例子必须由实际 CLI 行为测试核验，不以文案正则命中算验收。

保留旧成功 JSON 形状。在 `--json` 失败响应中增补 `recovery={category,state,retryable,actions:[{command,reason,requires}],correlation_ids}`；若旧错误已有 code/detail，原字段不删除。category 只为 usage/configuration/dependency/permission/pending/outcome_unknown/business/internal。等待岗位、缺资源、需人授权、已在执行、外部结果未知与终态失败不能混为“运行失败请重试”。动作仅引用已注册/有权限入口；unknown 不能给盲目 submit/retry 建议，缺配置不能教删除 DB/覆盖 Registry/关闭审计。非 JSON 错误显示同一恢复事实和简短下一步。

### 3.6 令牌来源与初装装配

`mao.base.TokenSource` 协议为 `token_hex(nbytes: int = 32) -> str`；实际 `SystemTokens` 用标准库安全随机源，仅在base内生成，测试注入确定性TokenSource。Context新增末尾可选 `tokens: TokenSource | None = None`，默认SystemTokens；不改变已有clock/ids调用。operation私钥仅存在root/private/operation-credentials/<id>.key（目录0700/文件0600），id须合法且路径不可逃逸；数据库secret_ref只记录受保护位置。共享邮箱不保存私钥副本。

部署后派生的 `factory.json.deployment={profile,root_id,manifest_digest,plan_digest,version:1}` 由apply写入，不能以用户任意设置的real=true代替资格。旧未启用M17持续模式的工厂保留旧启动语义；normal serve启动M17持续生产时必须resident profile且资格/治理/隔离预检通过。显式local-test的协议单元工厂允许离线资格和fake组件，不能用于通过live门。首次qualification可从manifest构造临时工厂和预算/模型代理，init不依赖已在运行的生产serve；因此不存在资格与启动互等的闭环阻塞。

## 4. 数据格式

### 4.1 配置与迁移

```json
{"transport":{"model":{"connect_s":10,"idle_s":90,"total_s":300},
 "egress":{"connect_s":10,"idle_s":300,"total_s":900,"max_tunnels":64},
 "tls_inspection":null},
 "operations":{"mailbox":"operations","rollout_minimums":{"T1":{"shadow_runs":1,"observe_s":1},"T2":{"shadow_runs":1,"observe_s":1},"T3":{"shadow_runs":1,"observe_s":1}}}}
```

transport 缺省采用上述有限上界；所有时长必须是正数且有限，不接受 bool；connect/idle<=total；max_tunnels 正整数。job/turn 更小的时限始终优先。允许为本地测试配更短时限，不在代码写测试域名/模型特例。未知字段拒绝 TRANSPORT_CONFIG_INVALID。

tls_inspection 缺省 null；operations.mailbox 为 root 下相对目录，禁止 ..、绝对路径或符号链接逃逸，凭据各有独立子目录；该配置只启用受限文件邮箱，不能改变本地可信管理权限。

tls_inspection 可配置 `{ca_file,hosts:{canonical_host:{cert_file,key_file}}}`。均为可信宿主绝对路径，不传入工位；只把公开 CA 注入获授权客户端。证书由部署者预置，不新增自动签发工具/依赖；leaf 必须对应 CONNECT authority，握手校验有效链和域名，不能用一个任意证书冒充所有主机。缺证书/链/支持能力时受限 HTTPS 显式 blocked，不能降级通配方法。

新增迁移 `046_operability.sql`（M16 合并后仅若尾号冲突按设计维护整体平移，禁止修改历史迁移）：

| 表 | 固定字段/约束 |
|---|---|
| transport_attempts | request_id PRIMARY KEY, operation_id, principal_kind/id, origin_id, phase, outcome, bytes_sent, deadline_at, error_code nullable, usage_ref nullable, created_at, updated_at；operation_id 沿用原账本逻辑操作键，通过 execution_origins 的 (principal_kind,principal_id) 关联服务/模型来源，attempt 不新建付费身份，不向非唯一的 budget_entries.operation_id 建立无效外键 |
| qualifications | id PRIMARY KEY, kind(runtime/machine), subject_key, subject_digest, profile, platform_digest, environment_digest, status(passed/failed/blocked), receipt_digest, created_at；live 不由 offline 推导 |
| deployment_operations | plan_digest PRIMARY KEY, manifest_digest, initial_state_digest, owner, lease_generation, stage(planned/staged/applying/committed/failed), journal JSON, created_at, updated_at |
| operation_credentials | id PRIMARY KEY, token_digest UNIQUE, root_id, subject, scope CHECK(scope='operate'), secret_ref, expires_at, revoked_at nullable, created_at |
| operation_requests | credential_id, request_id, request_key nullable, args_digest, state(pending/completed/failed), result JSON, error JSON nullable, message_id nullable, created_at, updated_at；PRIMARY KEY(credential_id,request_id)；同request_id异参拒绝 |
| operation_request_keys | credential_id, request_key, args_digest, message_id UNIQUE, result JSON, created_at；PRIMARY KEY(credential_id,request_key)；talk业务去重键、邮箱、事件、request终态同事务 |

不重复建立 service_operations/budget_entries；transport 费用归属原账本。部署 journal 如 DB 尚未存在则先写 root 的带摘要/fsync sidecar，迁移后写入上述表并保留可恢复关联；应用路径不能依赖一张尚不存在的表才能初始化。operation_requests持久记录每次请求及状态，operation_request_keys只登记talk.send业务效果；不同request_id重放相同key时复用原message/result，并记录本次响应，使operation show始终按本凭据的request_id可查。文件响应在事务后可重建，崩溃不重复投邮件。未完成部署、未知外部请求、未失效的去重键与凭据撤销记录加入备份/保留边界；冷恢复撤销 operate 凭据，重新 issue，不能带回旧授权。

### 4.2 qualification receipt

`{schema_version:"mao-qualification-1",id,kind,subject_key,subject_digest,platform_digest,profile,status,environment,checks:[{id,passed,evidence_refs,reason}],evidence_index,costs,blocked_reasons}`。environment 固定 CLI 名/版本/可执行或镜像摘要、配置/adapter 摘要、实际模型和脱敏上游标识、产品源码 commit/image digest。每个 evidence_ref 必须能核对进程、工具调用、产物摘要或真实失败；布尔标记 real=true 不能单独证明。

runtime 检查 ID 固定 `launch,contract,tools,limits,result,resume,fencing`；machine 为 `identity,health,invoke,outputs,recovery,network`。不支持 resume 的一次性 script 明确 not_applicable，但 resident agent-cli 必须全通过，不能以 N/A 逃验收；代数/权限检查仍执行。声明/CLI/model/image/platform 摘要改变即资格失效；旧 receipt 不得认证新文件。生产 resident 启用的 agent-cli 与机器必须有匹配 live 资格，local-test 可用 offline，但状态明确为无隔离协议测试。

### 4.3 deployment manifest 与诊断

manifest 为 `{schema_version:"mao-deployment-1",profile,root_id,registry:{source,commit},config,runtimes:[key],machines:[key],qualification_receipts:[path]}`。只允许 pinned Registry commit 和固定产品/runtime 版本；没有 secret 值、默认 human actor、视频专有模型或代签配置。profile resident/local-test；resident 要求 Docker 可用、真实 qualified CLI、可达受控网络、预算来源、M16/长期目标配置、启用的审计/canary和有效治理/风险策略、外部 operate 权限隔离。local-test 明确无隔离/无真实模型证明，不能转换成 resident passed。

plan 返回 `{schema_version:"mao-deploy-plan-1",plan_digest,manifest_digest,initial_state_digest,steps,conflicts,requirements}`；steps 为固定 action 枚举 validate/stage_registry/write_new_config/migrate/verify/commit，不含任意 shell。apply 返回 `{plan_digest,state,changed,report}`，相同计划在完成后重放 changed=false。变更既有业务配置需新的显式迁移计划，不能用 init 覆盖。diagnose 返回 `{schema_version:"mao-doctor-1",profile,status,checks:[{id,status,observed,required,code,recovery}],limitations}`；status ready/degraded/blocked，check status passed/failed/blocked/unsupported，不能只打印总体布尔值。

### 4.4 跨领域 live index

补充witness在 `.factory/m17-repro/operability-live/index.json` 写：

```json
{"schema_version":"mao-operability-live-1","status":"passed","witness_run_id":"witness-id",
 "platform_source_digest":"sha256:...","unplanned_business_interventions":0,"unplanned_actions":[],
 "cells":[{"domain_id":"domain-a","runtime_ref":"runtime-a@1","entry_mode":"help_only","report":"a-a/report.json",
 "report_sha256":"64 hex","factory_root":"a-a/factory"}],
 "product_receipts":[{"machine_key":"Machine:...@1","path":"products/product.json","sha256":"64 hex"}],
 "artifacts":[{"evidence_id":"audit-1","kind":"deployment","path":"audit/deployment.json","sha256":"64 hex"}]}
```

cells必须完整2领域×2runtime共四项、四个不同工厂根；每格是cases.yaml已有mao-m17-live-1报告和一致性snapshot，额外标明domain_id/witness_run_id/entry_mode，与index对应项相同。entry_mode只允许help_only或skill；每种模式两格，且每个领域、每个runtime都分别覆盖两种模式。source digest须四格相同，domain固定契约摘要在两个runtime间相同、两个领域间不同，且至少一个领域为非媒体业务（由witness检查输入/输出和领域标准而非仅改名字）。各格原有C01–C10仍完整校验，不降低原四周期/idle/新工作/失败调整门槛。

required_live_products来自冻结closure.yaml的PR1机器库存；全部需当前witness真实调用并给live资格receipt，不接受unsupported替代通过。receipt带本次witness_run_id，evidence_index项格式`{evidence_id,kind,path,sha256}`，checks的evidence_refs引用该索引。所有路径是index目录下不含..的相对文件，sha256为64hex；文件不可为空、符号链接或越界。artifacts至少含deployment/operator_permissions/transport/runtime_qualification/help_only/skill_usage六种实际证据。不能只写布尔成功标记；实际真实性由独立live测试观察和断言，校验器只证明引用/摘要完整。

## 5. 行为

### 5.1 可控时限与未知结果

模型 connect 包括建立连接/写头前等待，idle 是成功 I/O 间无进展的最长间隔，total 从外发尝试开始直到完整响应/取消；进展不能无限刷新 total。控制面代理保持既有 JSON/SSE 计量；完整 SSE 按原协议核算，不把不完整流计0或当成功。传输可缓冲，但不得将 buffered response 的成功误报为实时流能力。升级默认 idle 不是把 5 秒或 90 秒当通用 SLA。

连接确认未写出请求字节失败可标 not_sent/retryable=true，但不自动重发有副作用请求。任何已发送/无法确定发送状态的超时、断开、部分响应或取消均 outcome_unknown/retryable=false；保持原费用上界，投持久异常，由既有 provider 幂等/对账解除。HTTP 的明确业务响应按协议分类，不能全部包装 MODEL_UPSTREAM_UNKNOWN。客户端断开仍结算已观察费用，发出后失租仍记录未知结果，旧 owner 不得新发。

model/egress/service 共用 deadline 计算与错误分类，仍分别走原控制通道：模型代理不绕通用出网，服务实际 HTTP 仍在沙箱内。错误包括请求 ID/phase 与可查询持久状态；不得自动重提付费调用“修复”未知结果。

### 5.2 CONNECT 与 TLS 方法约束

CONNECT 校验身份、authority、主机/端口和策略后才连接上游。HTTP 头后的同包字节可能已在 handler buffer，必须恰好转发一次；仅等 socket 会丢，阻塞 peek 等“可选字节”会挂起。使用具有单一读取者、deadline 和取消的有界缓冲，支持分片与半关闭，连接/线程/缓冲可在 total/idle/close 后释放；并发超过 max_tunnels 明确拒绝，不无限占用线程。

没有 CA 且策略限制内层方法时继续 EGRESS_METHOD_OPAQUE，不透传；有合法 tls_inspection 时，终结客户端 TLS，仅协商 HTTP/1.1，校验 CONNECT authority/SNI/内层 Host 一致；对每个 HTTP 请求的真实方法、目标、重定向重复授权，再以验证上游证书的 TLS 发往固定授权目标。禁止的方法和 Host/SNI 绕行不得抵达上游。不支持的升级/HTTP2/跨 authority 请求显式拒绝，不静默退回 opaque。方法为 * 可使用普通字节隧道，主机与身份检查仍强制。上游凭据只在既有受控通道注入，不进入沙箱或日志。

### 5.3 原生运行时与工具语义

命令/模板/transcript adapter 只做确定性转换；模型和工具推理由已存在的真实 CLI 承担，不把 PR1 的自行模型工具循环复制进控制面。所有岗位/领域/model 参数来自声明和有效授权，不按 foreman、某视频机器或特定模型名走内核分支。

runtime qualification 在同一隔离 session 内执行两轮：第一轮给随机挑战事实且不写入后续输入/资产/共享文件，第二轮只问该事实。应从实际 CLI 会话取回，错 session/删除会话数据必须失败或明确不支持；仅返回稳定非空 ID 不足。生产仍可通过 M16 资产重建业务上下文，但不能拿资产重建冒充 CLI 私有历史恢复。

任务书给出随机契约要求，实际受控工具/产物须体现；故意丢弃 factory_contract 的 adapter 必须不合格。工具循环测试含多个工具、operationId/前缀与 call ID 配对、错误、重复回包、最大轮数/超时、最终回复至多一次；不以工具名文字恰好为 talk_reply 推断成功。每个副作用检查 scope/fence；超限停止、事件与费用完整，空 result/exit0 不通过。两个不同原生 CLI/adapter 均走此协议，不只为单一 CLI 写补丁。

### 5.4 真实产品和地址恢复

产品 qualification 固定声明 source commit/image/实际工具版本，记录真实执行链和 self-check。若声明 OpenMontage 却仅运行自写图像 HTTP 脚本，identity 检查失败；若选择 HTTP 图像服务，必须如实声明为该服务的独立 Machine，经治理接入。不能仅比较机器名或自报版本；真实工件/调用日志/产品来源必须相符。

保持 Machine form workspace/service/tool 三类与 existing recipe/long_task 字段；不增加视频专有全局类型。异步服务沿用 done/fetch MRL 表达式和 service_operations；同步服务用 script/tool 或明确定义的普通受控调用，不强造 task_id。供应商返回值到 done/fetch 的转换是显式版本化 adapter/声明，不能根据 URL 或模型名猜协议。

fetch 表达式可返回相对 HTTP 地址：以该响应的有效 service base_url 解析；gateway 的普通服务只允许原容器内 service origin，公网资源必须经已有获授权的外部 tool/provider 通道，不能因新增相对 URL 支持放开 gateway.md §5.3 的网络边界。绝对/相对地址和每次 redirect 均检查 scheme、canonical origin、端口、userinfo、控制字符、授权与目标网络；外部工具使用已批准 origins。拒绝 file://、公网绕行或任意宿主文件。沙箱本地绝对产物路径仍按旧 workspace/数据目录规则处理，不误当 URL。task 已知完成后 URL/下载/完整性失败只重取原 task，不能 submit；配置/adapter 摘要变更不得复用旧任务身份。

### 5.5 部署、升级与治理

plan/doctor 先给具体能力差距：依赖、镜像/CLI/产品资格、模型配置与价格、治理、岗位装配、权限/可达性、旧 schema。不得把缺 Docker 变成关闭 audit/canary。apply 在自有 staging 准备 Registry、新配置和迁移 journal；记录每个幂等步骤和前后摘要，同一 root 的部署锁检查 generation。提交前重读 initial_state_digest，变化则 DEPLOY_CONFLICT，不覆盖并发人的改动。

从空 root 安装；已有相同实例重跑应 no-op；已有未知/不同业务 Registry、未提交更改、秘密、资产/CAS/运行历史全部保留，冲突显式报告。不得 rmtree/unlink 原 Registry 后复制示例。失败恢复按同 plan journal 对账，未完成 intent 不重新初始化业务；M16 checkpoints/数据库事务用于保护已有状态，不能宣称跨 Git/文件/DB 物理原子。serve 配置/资格完整才启动；init 不代签、不会自动确认 talk 卡或自动发付费生产任务。

rollout 参数合法性沿用治理规范；M17 自治 resident 新增 `operations.rollout_minimums`，配置为 `{T1:{shadow_runs,observe_s},T2:{...},T3:{...}}`，整数非负，resident 缺省 T1={1,1},T2={1,1},T3={1,1}，用户可在可信管理面显式调整并纳入部署摘要。最小值取现行 Policy/治理策略与此处的更严格者，永不降低原要求；违背最低值的自主提议在验证时 rejected。明确允许的零样本/零观察记录 not_performed 和原因，不能填写 actual_compared/observed=true。旧未启用 resident 的兼容工作流不被新的缺省门槛改写。影子/金丝雀样本仍由 continuity §5.7 受控候选路由产生，外部 Agent 不代喂已完成业务。

### 5.6 帮助、skill 与行为验收

发布仓库 `skills/mao-factory/SKILL.md`（按版本生成/维护，同包 references），从 capabilities/顶层帮助可发现，并给出可安装位置；安装是显式操作，不修改用户其他 skill。frontmatter 明确面向工厂运营，正文区分 factory/factoryctl/mrl/harness、resident/local-test、受限外部/可信管理/厂内岗位、已实现/不支持；不包含场景视频模型、代人标签或执行附件指令。

skill 使用新 doctor/init/guide 和旧目标/结果查询接口；与安装版本匹配。先预检、需要时可信管理员初始化和授权，外部操作者只转达目标/看结果/转交真正人类事项。不得把“给外部 Agent 更多源码/编排步骤”当使用补救。帮助缺口用实际错误/状态驱动文档和 CLI 修正，不能仅靠 skill 告诉模型忽视坏提示。旧中文及英文命令参考、getting-started/runtime 补齐 talk/objective/身份/恢复，其他语言至少标明版本并指向相同事实源，不能继续显示相互矛盾的接口。

陌生 Agent 的验收输入仅公开 CLI/help/发布 skill、宏观目标与受限凭据；不能读平台源码、看预写业务动作或获得修复提示。两种不同领域 × 两种 runtime/adapter 共四格，按下表交叉分配使用入口；这是四格，不是复用四个工厂各跑两遍。先完成help_only两格，再完成skill两格，每格都有新的外部操作者会话、CLI私有会话、凭据和工厂根，不能共享历史/业务文件。各格至少四周期、idle、新机会和失败调整。

| 领域 / runtime | A | B |
|---|---|---|
| 领域A | help_only | skill |
| 领域B（至少一个领域非媒体） | skill | help_only |

A/B是witness开始前固定的实际领域和真实native runtime标签。help_only外部操作者只能读取CLI/help/guide，不能加载skill正文；skill格另给本次发布的版本化skill。每个领域与每个runtime都分别出现两种入口，避免入口和某一领域或runtime绑定。report及index.cells均记录entry_mode；仅改标签或复制已有工厂不算独立演练。资料源含事实，不含候选/派工/代码步骤；领域标准可预先固定。业务由厂内 Agent 完成，外部操作者的正常观察与合法人类事项转交单列，不计为业务接续；代编排/业务修改/代签/手动重试均使该格失败。

普通日常使用不主动制造故障或强制等待验收窗口；上述是独立资格/阶段验收场景，不能强加所有用户操作。真实能力与离线协议各自报告。

## 6. 错误

新增错误在对应任务登记 CODES，本次不改源码。沿用旧错误码时保留原语义和退出码；以下为新增固定码：

| 码 | 触发/分类 | 退出码 |
|---|---|---|
| TRANSPORT_CONFIG_INVALID | 时限/inspection 配置不合法；configuration | 2 |
| TRANSPORT_TIMEOUT | 时限到，phase 和 outcome 必填；dependency/outcome_unknown | 4 |
| TRANSPORT_CANCELLED | 取消/失租后保留请求结果与费用；outcome_unknown | 1 |
| SERVICE_LOCATION_DENIED | 地址/跳转越界；permission | 1 |
| RUNTIME_UNQUALIFIED | 运行时挑战、版本或真实资格未通过；dependency | 4 |
| MACHINE_IDENTITY_MISMATCH | 声明与实际产品不一致；dependency | 4 |
| DEPLOY_MANIFEST_INVALID | manifest 不合法；configuration | 2 |
| DEPLOY_CONFLICT | state/manifest/Registry 与 plan 不符；configuration | 1 |
| DEPLOY_BUSY | 有效运行 owner 未移交；dependency | 4 |
| DEPLOY_INCOMPLETE | journal 未完成，需原计划恢复；configuration | 4 |
| OPERATION_FORBIDDEN | 缺失/错 scope/root/身份或禁止命令；permission | 1 |
| OPERATION_CREDENTIAL_EXPIRED | 已过期/撤销；permission | 1 |
| OPERATION_CONFLICT | 同请求键异参；usage | 1 |
| ROLLOUT_EVIDENCE_REQUIRED | 自主提议低于生效最低样本/观察；business | 1 |

结构化错误不包含 secret 文件路径以外的值、令牌、TLS 私钥或完整认证响应。操作邮箱错误响应使用同一 code/detail/recovery；CLI映射回上表/旧 FactoryError，不把所有失败当内部异常5。未认证请求不给出敏感对象状态或凭据存在性。

## 7. 验收测试

完整精确节点与问题对应见 `plan/evidence/m17/closure.yaml`；下列文件为 M17 新增实现测试，缺文件/缺节点本身使 accept/gate 失败，不允许 skip/importorskip/xfail。测试源只能在各自 task paths 中新增/开发，需求与断言来自本节和 frozen contract，不能改冻结 manifest 绕过。

| 文件 | 测试 | 必须证明 |
|---|---|---|
| `tests/system/test_m17_09_transport.py` | `test_model_deadlines_and_partial_response`、`test_total_deadline_not_extended_by_progress`、`test_timeout_unknown_keeps_cost_and_prevents_resubmit`、`test_connect_buffered_fragmented_and_empty_payload`、`test_connect_tls_method_and_authority_enforcement`、`test_tunnel_cancel_and_capacity_release` | 本机可计数上游；慢首包/分块/总时限；未知成本；同包/分片/空包；CA/无CA与有限方法；关闭/限额；不触发付费 |
| `tests/system/test_m17_10_qualification.py` | `test_real_cli_session_contract_and_negative_adapters`、`test_tool_ids_duplicates_errors_and_turn_limit`、`test_two_runtime_adapters_share_protocol`、`test_machine_identity_cannot_be_replaced_by_stub`、`test_service_relative_absolute_redirect_and_integrity`、`test_sync_and_async_nonmedia_operations`、`test_qualification_invalidates_on_version_change` | 两种真实进程适配、私有会话与错误对照、声明产品调用、通用服务地址/未知恢复、receipt摘要；live另验真实资源 |
| `tests/system/test_m17_11_deployment.py` | `test_doctor_and_plan_are_read_only`、`test_init_repeat_preserves_registry_assets_secrets_and_runs`、`test_apply_crash_and_state_conflict_recover`、`test_missing_docker_or_qualification_blocks_resident`、`test_external_cli_cannot_impersonate_human_or_open_database`、`test_operator_credential_expiry_revoke_and_dedup`、`test_rollout_minimums_and_truthful_zero_evidence` | 非破坏幂等部署、故障边界、配置/资源/资格预检、真实管理/operate隔离、零样本语义 |
| `tests/system/test_m17_12_cli_usage.py` | `test_public_help_discovers_complete_lifecycle`、`test_capabilities_examples_match_installed_parser`、`test_error_recovery_handles_pending_unknown_and_permission`、`test_skill_packaging_version_and_truthful_capabilities`、`test_help_only_operator_hands_goal_to_factory`、`test_skill_operator_hands_goal_to_factory` | 真子进程help/错误/状态/文档skill链；离线操作者探索不能代厂内业务，不是预置业务脚本证明智能 |
| `tests/system/test_m17_08_operability_live.py` | `test_live_cross_domain_onboarding_and_recovery` | 真实四格领域/运行时矩阵、实际产品、历史补偿闭合、介入账本；见 operability-cases.yaml |

`plan/evidence/m17/acceptance/test_plan_coverage.py`的8个顶层测试、49个参数化用例验证设计/校验工具：漏项与错引用、依赖/门绕过、专项场景漏组、隔离目录内真实harness对缺节点的失败、四格合成索引和缺证据拒绝。它们不依赖生产功能尚未实现，实施后仍有效；合成报告仅证明完整性工具，不能证明模型行为或真实来源。最终`m17-contracts`要求原45用例加本组49用例，至少94用例零失败/错误/跳过。

固定设计覆盖验证 `plan/evidence/m17/verify_plan.py` 校验所有已登记问题都有规范/任务/精确测试/验收门；它不能证明行为通过。任务 `run_contracts.py --task M17-09|10|11|12` 与最终 `--all` 真正调用 harness pytest 对精确节点执行断言。M17-08还需运行独立 live witness 和报告完整性校验，不得由报告存在性代替。

## 8. 错误示范与正确示范

- 错：统一把超时调90秒并遇错重复submit；对：有界connect/idle/total、可信operation、未知结果保留和对账。
- 错：部署脚本删Registry或关闭审计让视频通过；对：manifest plan/apply、状态摘要、幂等journal、治理和保留证据。
- 错：--by human:名字就算人授权、外层代做业务；对：管理边界/operate凭据隔离、长期范围内厂内自主执行。
- 错：help/skill有几个关键词就认为会用；对：独立外部Agent仅用公开入口完成交接/观察，四格业务由厂内Agent完成。

## 9. 风险与注意事项

TLS inspection 会改变客户端信任，只对明确配置/授权的域名和预置证书启用；不支持的协议明确blocked，不削弱方法约束。无法可靠判断发送边界时选择unknown；不承诺通用exactly-once或精确未知费用。权限边界无法抵御同权管理员改源码/DB，部署应隔离外部Agent与管理资源。原生CLI版本可能不支持正确resume/contract，应不合格而非改验收。真实产品/四格模型资源不足可先完成离线实现，但M17最终门保持失败/blocked，不能给全覆盖已修复结论。
