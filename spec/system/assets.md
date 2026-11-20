# 工厂资产、正式知识与接管（M16）

> 规范性文档，遵循 `spec/system/README.md`。设计依据：`plan/design-assets.md`。
> 模块：`mao.assets`（目录与依赖）、`mao.learning`（契约与证据）、`mao.packages`（检查点与移交包）。
> M16 不改变 D1–D14；不新增 MRL kind、模型服务、调度器或影子执行器。

## 1. 目的与范围

把工厂的编排、知识、技能、工具、材料、显式工作状态和证据组织为可追溯、可交接的数据。软件只管理身份、引用、权限、一致性和证据；视频质量、收益、研发指标等由业务自己的契约决定。

M16 交付三个闭环：

1. 资产目录 → 完整冷检查点 → 从明确披露的资产重建移交包 → 买家绑定自己的资源。
2. 原始证据 → 候选 → 有固定契约与真实执行证据的正式知识 → 失效或被新版本替代。
3. 固化交接 → 全新私有工作区 → 同一个岗位接着做真实工作 → 依据预定契约验收。

启用方式：`factory.json` 出现 `assets` 对象，且有非空 `environment` 对象。该对象只描述非秘密的环境版本、工具链或资源指纹；禁止凭据。没有 `assets` 段时不增加 MCP 工具、不注入上下文、不记录 `asset_uses`、不改变既有轮换协议。显式调用本组件的写操作必须已启用；包校验不需要当前工厂。环境配置变化不会自动开测试。

不交付交易市场、支付、在线迁移、通用隐私识别器、周期性全厂复跑、自动提炼模型、第二套任务状态机。原有结构 ChangeSet 的 V1–V4 与灰度机制保持有效；M16 的业务数据验证不借道影子产线。压缩只形成新数据，不能顺带删除原件、改正式指针或改验收标准。

## 2. 依赖

| 依赖 | 复用方式 |
|---|---|
| `Warehouse`、`BeanDraft`、血缘、封存报告 | 业务文件仍是原有 sealed Bean；目录不复制文件、不另建 CAS |
| `Database.tx`、`Clock`、`IdSource` | 原子指针、事件、并发检查；唯一新增迁移为 `042_assets.sql` |
| `Snapshot`、`EpochStore`、`cache.spec_digest` | 固定结构版本，读取并保留验证依赖 |
| `Runner`、`LineRunner`、`AgentHost`、`McpServer` | 记录既有真实执行；不增加生产副本 |
| `mrl.expr.compile_expr`、`mrl.types.DYN/BOOL` | 领域验收只用 MRL，复用 harness 的动态对象求值方式 |
| `Gate.admit_external`、既有 validator/judge | 移交数据重新准入；语义质量用现有质检机制表达 |
| `SecretStore`、`Inbox`、`LeaseStore`、retention | 拒绝已知秘密、报告交接失败、阻止 GC 破坏证据 |
| `backup`、SQLite online backup、Git argv | 冷检查点复用备份文件格式与校验，另加业务状态清单 |

控制面不调用模型。需要语义提炼、脱敏或主观判断时，厂长自行安排现有 Agent、工位或已批准的 judge；本组件只检查它们的可验证结果，不强制增加一次模型评审。

## 3. 接口

所有返回值均为可规范 JSON 序列化的 `dict` / `list`；内容顺序按第 4 节。库函数不打印。

### 3.1 `mao.assets`

```python
class AssetStore:
    def __init__(self, ctx: Context) -> None
    def put(self, document: Mapping[str, Any], *, by: str) -> dict[str, Any]
    def get(self, asset_id: str) -> dict[str, Any]
    def list(self, *, kind: str | None = None,
             subject: str | None = None, released: bool = False) -> list[dict[str, Any]]
    def closure(self, roots: Sequence[str]) -> dict[str, Any]
    def release(self, asset_id: str, *, reason: str, by: str) -> dict[str, Any]

def capture_handoff(ctx: Context, agent: str, document: Mapping[str, Any], *,
                    expected: str | None, by: str) -> dict[str, Any]
    # 验证并保存 handoff 资产，CAS 更新 handoff:<agent> 指针（5.5）
```

### 3.2 `mao.learning`

```python
class LearningStore:
    def __init__(self, ctx: Context, epochs: EpochStore) -> None
    def authorize(self, asset_id: str, *, by: str) -> dict[str, Any]
        # 仅人的操作面；允许 contract 或 directive，不能由 MCP 提供 by
    def open_trial(self, target: str, contract: str, *,
                   purpose: str, by: str) -> dict[str, Any]
    def evaluate(self, trial_id: str, evidence: Mapping[str, Mapping[str, str]], *,
                 by: str) -> dict[str, Any]
    def get_trial(self, trial_id: str) -> dict[str, Any]
    def cancel_trial(self, trial_id: str, *, reason: str, by: str) -> dict[str, Any]
    def promote(self, target: str, *, trial_id: str | None,
                expected: str | None, by: str) -> dict[str, Any]
    def invalidate(self, target: str, *, reason: str, by: str) -> dict[str, Any]
    def heads(self, subject: str) -> list[dict[str, Any]]
    def prepare_takeover(self, trial_id: str, *, by: str) -> dict[str, Any]
    def finish_takeover(self, trial_id: str, *, by: str) -> dict[str, Any]
    def abort_takeover(self, trial_id: str, *, reason: str, by: str) -> dict[str, Any]

# 生产路径的内部接线；不作为任意外部写证据的工具：
def record_use(ctx: Context, snapshot: Snapshot, kind: str, execution_id: str, *,
               subject: str, fresh: bool, handoff: str | None,
               assets: Sequence[str]) -> None
```

返回格式：AssetStore 的 put/get/release/capture_handoff 返回 4.1 的资产记录，list 按 id 排序。heads/promote 返回 `{slot,asset_id,generation,proof,stale}`（heads 是列表）。authorize 返回 `{asset_id,kind,by,at}`。trial 的 open/get/evaluate/cancel 返回 4.4 的结果，open 时 evidence/checks 为空，另含 context 与 watermark；state 是 persisted 状态，stale 是当前派生判断。cancel 只将 open 改为 cancelled，不能改写过去的 passed/failed。invalidate 返回 `{target,cleared:[slot]}`。三个 takeover 操作返回 `{agent,trial_id,phase}`，abort 返回恢复后的 stable 且岗位仍 paused。

`purpose` 只允许 `knowledge`、`takeover`、`release`。`evaluate` 的 evidence 为 `case_name -> {"kind": "job" | "turn", "id": 执行 id}`。它读取库中的真实状态，不接受调用方给出的 `passed`、facts 或自评文本。

### 3.3 `mao.packages`

```python
CHECKPOINT_FORMAT = "mao-checkpoint/1"
PACKAGE_FORMAT = "mao-factory-package/1"

def create_checkpoint(ctx: Context, dest: Path, *, factory_asset: str,
                      by: str = "human", timeout_s: float = 120.0) -> dict[str, Any]
def restore_checkpoint(src: Path, root: Path, *, registry_dest: Path | None = None,
                       timeout_s: float = 120.0) -> dict[str, Any]
def export_package(checkpoint: Path, dest: Path, *, release: Mapping[str, Any],
                   by: str = "human", timeout_s: float = 120.0) -> dict[str, Any]
def verify_package(src: Path, *, timeout_s: float = 120.0) -> dict[str, Any]
    # 同时支持上述两种格式；{"ok": bool, "problems": [{"kind", "path"}],
    #                         "acceptance": "not_run"}
def import_package(src: Path, root: Path, *, bindings: Mapping[str, Any],
                   provider: SandboxProvider | None = None,
                   by: str = "human", timeout_s: float = 120.0) -> dict[str, Any]
    # Python 兼容执行面 provider 缺省为 DockerProvider；Bun 业务入口使用 Podman。测试显式传 LocalProvider
```

验证与导出不打开源工厂、不开 Agent；导出仅从已完成的 checkpoint 读取。现有 `factory backup` 签名和格式不变，不能把旧备份直接声称为完整检查点或已脱敏包。

### 3.4 CLI 与 MCP

```text
factory asset put FILE --by NAME [--json]
factory asset ls [--kind KIND] [--subject SUBJECT] [--json]
factory asset show ID [--json]
factory asset closure ID ... [--json]
factory asset release ID --reason TEXT --by NAME [--json]
factory knowledge authorize ID --by NAME [--json]
factory knowledge trial TARGET --contract ID --purpose knowledge|takeover|release --by NAME [--json]
factory knowledge evaluate TRIAL --evidence FILE --by NAME [--json]
factory knowledge show TRIAL [--json]
factory knowledge cancel TRIAL --reason TEXT --by NAME [--json]
factory knowledge promote TARGET --trial TRIAL --expect ID|none --by NAME [--json]
factory knowledge promote TARGET --directive --expect ID|none --by NAME [--json]
factory knowledge invalidate TARGET --reason TEXT --by NAME [--json]
factory knowledge heads SUBJECT [--json]
factory agent takeover prepare|finish TRIAL --by NAME [--json]
factory agent takeover abort TRIAL --reason TEXT --by NAME [--json]
factory package checkpoint DEST --factory ID --by NAME [--timeout S] [--json]
factory package restore SRC ROOT [--registry-dest PATH] [--timeout S] [--json]
factory package export CHECKPOINT DEST --release FILE --by NAME [--timeout S] [--json]
factory package verify SRC [--timeout S] [--json]
factory package import SRC ROOT --bindings FILE --by NAME [--timeout S] [--json]
```

`--expect none` 对应 `None`，不是字符串。JSON 文件用 UTF-8 读取；命令行不接受秘密值。成功退出 0；`evaluate` 的终态 fail、`verify` 的 `ok=false` 退出 1；用法错误退出 2；其余错误沿用既有 FactoryError 呈现规则。

MCP 新工具（仅 assets 已启用时加入 tools/list；未启用时保持旧工具表）：

| 角色 | 工具与参数（未标可选的为必填） |
|---|---|
| shared | `asset_get(asset_id)`、`asset_list(kind?, subject?)`、`asset_closure(roots)`、`knowledge_heads(subject)`、`knowledge_trial_get(trial_id)` |
| agent、foreman | `asset_put(document)`；by 固定为 Session 身份 |
| foreman | `knowledge_trial_open(target, contract, purpose)`、`knowledge_evaluate(trial_id, evidence)`、`knowledge_trial_cancel(trial_id, reason)`、`knowledge_promote(target, trial_id, expected)`、`knowledge_invalidate(target, reason)`、`agent_takeover_prepare(trial_id)`、`agent_takeover_finish(trial_id)`、`agent_takeover_abort(trial_id, reason)` |

另将现有 `agent_send(to,text)`、`remind(after,note)` 仅提供给已启用且 `foreman.mode=resident` 的厂长；分派时身份固定为内置岗位 foreman（不是空的 Session.agent；保持 Session.agent=None，只在分派时映射），直接复用既有 agent_tool/Mailbox 的目标校验、秘密打码与时限。提醒发给 foreman，消息来源 agent:foreman。会话式厂长和 reviewer 不取得这两个工具；不加入 foreman 的 run_start，不改 worker 的产线允许列表。这使厂长能把试验安排给已有操作员，而不引入新的执行权或执行器。

agent 的 `asset_put` 只能写 `scope.subject = agent:<Session.agent>` 的资产。foreman 可组织跨岗位和全厂资产。reviewer 只有 shared。不暴露 authorize、release、任意磁盘读写、包导出/导入、修改环境和验收契约的授权。厂长可以产生新的 contract 候选，不能授权它；授权是初始化领域标准的人的动作，同一已授权契约可验证任意多次候选，不需要逐条人工批准。

## 4. 数据格式

### 4.1 资产记录

输入 document 的字段：

| 字段 | 约束 |
|---|---|
| `key` | 非空逻辑名，`^[A-Za-z0-9][A-Za-z0-9._/-]*$`；不是宿主路径 |
| `kind` | 同样的名称语法；内置 `knowledge`、`skill`、`tool`、`workflow`、`handoff`、`contract`、`factory`、`registry`、`evidence`；允许业务扩展 kind |
| `summary` | 非空 UTF-8 文本；只是检索与交接摘要 |
| `scope` | 对象，必有非空 `subject`；`agent:<name>`、`station:<name>@<major>`、`factory`；可附 `conditions` 文本 |
| `beans` | 已封存且未过期的 Bean id 列表；文件仍在 Warehouse；缺省 `[]` |
| `requires` | 已存在且未释放的资产 id 列表；缺省 `[]` |
| `evidence` | `[{kind, id}]`，kind 为 `job/turn/run/receipt/trial`；对象必须存在且已结束；缺省 `[]` |
| `data` | kind 对应的业务对象；缺省 `{}`；不是控制面可执行代码 |

其他顶层键拒绝。数组去重排序（evidence 按 kind/id），其余按规范 JSON。规范化 document 不超过 256 KiB；大文件放 Bean。`id = "asset:" + digest_bytes(canonical_bytes(document))`，包括算法前缀，例如 `asset:sha256:...`。返回 `{id, document, by, created_at, released}`；相同内容 put 幂等，不覆盖首次登记人和时间。

目录记录是不可变的内容地址索引，业务载荷仍是 Bean。`summary`、`data` 本身也是受版本控制的业务数据，不依赖聊天记录。kind 不授予执行权：tool 的代码/镜像和 workflow 的编排若改变结构，仍要通过 Registry ChangeSet。

`closure` 返回 `{assets: [id], beans: [id], blobs: [digest], evidence: [{kind,id}], epochs: [commit]}`，均去重排序。展开 `requires`、`beans`、显式 evidence 及它们的上游来源：job 输入/输出/封存报告，run 的节点，turn 的已记录上下文、handoff 与成功工具引用，receipt 消费的 Bean，trial 的目标/契约/证据。沿 Bean 来源向上，不抓取所有下游消费者。引用到不存在的执行记录、缺失 blob、失去保留的 epoch 都失败，不能静默截断。

### 4.2 知识、契约与验证上下文

knowledge/skill/workflow/tool 若需正式知识指针，data 必有：

```json
{"claim": {"kind": "fact", "statement": "适用范围内的明确结论",
           "limits": "不支持推论的边界", "revalidate": ["工具链版本变化"]}}
```

claim.kind 为 `fact/hypothesis/method/directive`。正式的 hypothesis 仍显示 hypothesis，不变成事实。directive 来自人的明确授权，不能由一次实验推导出来。

contract 的 data：

```json
{
  "registry": ["Agent:scout@1"],
  "files": [],
  "cases": [
    {"name": "normal", "kind": "turn", "subject": "agent:scout",
     "tags": ["normal"], "uses_target": true,
     "assert": "out.state == \"ok\" && size(out.actions) > 0"}
  ]
}
```

case 名唯一；cases 非空；kind 为 job/turn；subject 使用同上语法；tags 是非空字符串列表；uses_target 为 bool，至少一项为 true。case 的非空断言须固定场景输入和领域结果；标签本身没有质量证明作用。所有 `assert` 用 `compile_expr(expr, {"out": T.DYN}, expect=T.BOOL)` 编译。静态/求值错误均不能算通过。job case 的 subject 是工位，turn case 的 subject 是岗位；case 可引用辅助质检工位。领域语义通过该工位既有 validators/accept/judge 的 facts 判定；`out.state == ok` 只能证明进程成功，不能独自证明岗位能力。

授权时检查每个 Spec/文件存在、表达式合法。授权记录的 standards 固定 contract.data.registry/files 明确列出的验收依赖，以及 job case 输出类型的 validator/accept 声明与 foreman case 的宪法/治理规则摘要；它们改变后，必须由人授权新的 contract 版本（data 可附 revision 文本以形成新 id）。相同 id 的授权不能悄悄改 standards。候选自己的 runtime/model/tool 属于被测实现，可在同一固定标准下更换。自动加入每个 case 对应的 Agent/Station、runtime、machine、policy、端口类型与 validator 声明的引用闭包；厂长 contract 额外包括 foreman 配置、宪法及治理规则。`registry` 和 `files` 补充业务依赖，不替代自动闭包。文件是安全的 Registry 相对路径。脚本、样例、工具源文件纳入文件摘要；镜像必须使用原有固定 digest。未知外部可变服务只能按 environment 中声明的版本限定结论，不能声称证明了未测环境。

context 是 `{registry: {key: spec_digest}, files: {path: digest}, environment: <配置摘要>, assets: [版本 id], runtime: {subject: {runtime, model, image, policy}}}` 的规范 JSON。takeover context 另有 `workspace_generation=trial id`，其他情况下为当前岗位 generation（无则 null）；此字段也参与摘要。assets 为相关正式知识，试验目标按同 key 替换旧版本，及它们的依赖闭包；不夹带别的岗位知识。context_digest 是其摘要。绑定的是有效依赖，而非无关 Registry 提交的变化；报告另记原始 epoch。任何相关依赖、知识、模型、环境变了，旧结果显示 stale，不能晋升当前版本；不会因此自动复跑。

### 4.3 单一迁移 `042_assets.sql`

以下为必需列；JSON 文本用规范 JSON，布尔用 0/1。可增加索引，不增加另一套物料或任务库。

```sql
CREATE TABLE asset_records (
  id TEXT PRIMARY KEY, document TEXT NOT NULL, by_actor TEXT NOT NULL,
  created_at TEXT NOT NULL, released_at TEXT, release_reason TEXT
);
CREATE TABLE asset_edges (
  asset_id TEXT NOT NULL REFERENCES asset_records(id), kind TEXT NOT NULL,
  ref_id TEXT NOT NULL, PRIMARY KEY(asset_id, kind, ref_id)
);
CREATE INDEX asset_edges_ref ON asset_edges(kind, ref_id);
CREATE TABLE asset_heads (
  slot TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES asset_records(id),
  generation INTEGER NOT NULL, proof TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE asset_authorizations (
  asset_id TEXT PRIMARY KEY REFERENCES asset_records(id),
  kind TEXT NOT NULL, standards TEXT NOT NULL, by_actor TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE asset_trials (
  id TEXT PRIMARY KEY, target TEXT NOT NULL REFERENCES asset_records(id),
  contract TEXT NOT NULL REFERENCES asset_records(id),
  purpose TEXT NOT NULL, subject TEXT NOT NULL, context TEXT NOT NULL,
  context_digest TEXT NOT NULL, watermark INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('open','passed','failed','cancelled')),
  result TEXT, by_actor TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT
);
CREATE UNIQUE INDEX asset_trials_open ON asset_trials(subject) WHERE state = 'open';
CREATE TABLE asset_uses (
  kind TEXT NOT NULL, execution_id TEXT NOT NULL, subject TEXT NOT NULL,
  epoch TEXT NOT NULL, context TEXT NOT NULL, assets TEXT NOT NULL,
  fresh INTEGER NOT NULL, handoff TEXT, trial_id TEXT, actions TEXT NOT NULL,
  PRIMARY KEY(kind, execution_id)
);
CREATE TABLE agent_workspaces (
  agent TEXT PRIMARY KEY, current_path TEXT NOT NULL, previous_path TEXT,
  trial_id TEXT, phase TEXT NOT NULL CHECK(phase IN ('stable','pending','testing','failed')),
  updated_at TEXT NOT NULL
);
```

head slot：`knowledge:<subject>:<key>`、`handoff:<agent>`、`capability:<subject>`。CAS 比较 asset_id（缺省 None），更新 generation + 1。历史变更写 events；不覆盖旧资产/结果。`proof` 为 trial id 或授权资产 id。试验 id 用 `ctx.ids.new("atry")`。

`record_use` 在真正执行前、同一个登记事务中写入；之后只允许宿主在结束时一次补完 actions。不能经 MCP 插入/改写记录。

### 4.4 执行证据视图

`evaluate` 为每个 case 构造 out：

- 共同字段：`kind,id,subject,state,cost_usd,epoch,context_digest,assets,fresh,handoff`。
- job：`inputs/outputs` 为 `port -> [{id,type,meta,facts}]`；`attempt`；`receipts` 为脱敏回执的 `{id,status,verified}` 列表；`actions=[]`。
- turn：`agent,conversation,messages`（仅 id 列表）、`message_digests`（对应消息 body 的规范 JSON 摘要）、`workspace_generation`、`actions`。可用摘要固定场景输入，避免仅靠标签凑齐覆盖。
- action：仅 `{tool, refs: [{kind,id}]}`。MCP 成功返回且产生/查询到可核验对象后记录，不写任意 arguments、回复正文、凭据、人工 answer。普通只读查询不算业务动作。支持记录既有 `run_start`、`talk_reply`、`talk_propose_run`、`changeset_propose`、`ask_human/escalate`、`agent_send`、`remind` 和 M16 写操作；refs 指向各自持久对象。没有持久对象的写工具可记 `refs=[]`，不能据此声称完成外部业务。

结果 `{id,target,contract,purpose,subject,state,context_digest,evidence,checks,stale}`。checks 为 `[{name,ok,code}]`，不是模型评分。终态重复 evaluate 同一 evidence 幂等；用另一组 evidence 改写终态拒绝，应开新 trial。context 变化导致 stale 的查询不改过去结论。

### 4.5 交接数据与工作区

启用后轮换提示写 `notebook/HANDOFF.next.json`，不覆盖上个可信版本。文件是 4.1 的完整 document，kind=handoff、key=`handoff.<agent>`、scope.subject=`agent:<name>`；capture_handoff 接受同一 document。其 data：

```json
{
  "agent": "scout", "tasks": [], "decisions": [], "uncertainties": [],
  "knowledge": [], "workspace": [], "external": []
}
```

- tasks 为 `[{kind: "job"|"run", id, state, next}]`，state 必须等于检查点时库中状态；`next` 非空。
- decisions/uncertainties 为字符串列表；允许明确的“尚不知道”。
- knowledge 为资产 id 列表，须包含该岗位当前的正式知识；复制进 requires。
- workspace 为 `[{path, bean, payload_path}]`，其中 Bean 自动并入该 handoff 的 beans；tasks 与 external 引用也建立受保留的边，但 tasks 可为尚未结束的实时进度，不能冒充终态 evidence。其映射含义：将业务文件从 sealed Bean 重建到安全的相对路径。必须包含继续工作所需的 notebook、脚本、研究状态；大数据可由资产引用。禁止 .git、.codex、.claude、.home、.runtime、凭据、原生会话目录以及与它们重叠的路径。
- external 为 `[{resource, receipt, next}]`；receipt 是存在的回执 id；不能用“回滚配置”表示撤销交易或物理动作。

handoff 的 summary 是人的可读交接。`HANDOFF.md`/上下文摘要是可重建的读视图；正式知识和业务进度以固定资产、任务表和回执为准。初次启用没有交接资产时可运行现有岗位，但不能声称已经通过替换验收。

普通 agent workspace 仍用原路径。显式 takeover 使用 `agents/<name>/takeovers/<trial-id>/workspace`；`agent_workspaces` 指定当前挂载路径，旧目录保留且不挂入新沙箱。其他容器挂载、读取 HANDOFF 的位置、codex home/MCP 配置和厂长 Registry 克隆均须使用同一已解析工作区。不得只改提示词而仍把旧 CLI 私有历史挂进去。

### 4.6 完整冷检查点

factory 资产的 data：

```json
{
  "workspaces": {"scout": {"business": ["notebook"], "ephemeral": ["scratch"]}},
  "resources": [{"key": "publisher", "kind": "account", "required": true,
                 "fingerprint": "service-version", "state": null,
                 "transfer": "bind", "license": "buyer-provided"}]
}
```

factory.requires 指明完整业务闭包。资源必须覆盖 Registry 中的镜像（含 runtime.inject.volume 的镜像）、秘密名称、外部服务与持久卷；容器内可由镜像重建的 service 不另算外部服务。kind=secret 时增加 name 字段，只有秘密名称不含值，transfer 必须 bind。每个有持久状态的岗位必须列出，含 foreman；business/ephemeral 都是安全路径前缀、互不覆盖。控制面能重建且干净的 Registry 克隆、原生 CLI 私有目录、收件/ctl 临时目录自动排除并列入 omitted；其他未分类文件拒绝。已登记机器/常驻实例的外部服务、镜像、卷等逐项纳入 resources，缺失声明拒绝。state 是可为空的资产 id；非空时必须属于 requires 闭包。`transfer` 为 `include/bind/unavailable`；include 的状态必须可重建，bind 明确要买家补什么，unavailable 的 required 资源不能声称完整可运行。

```text
DEST/manifest.json                 mao-checkpoint/1，最后发布
DEST/backup/                      可由 verify_backup 验证的 mao-backup/1
DEST/business/<agent>/<path>       声明的业务文件，不含 CLI 私有状态
DEST/evidence/agents/<agent>/...    现存已打码的 Agent 轮次日志
DEST/evidence/instances/<id>/...    现存已打码的实例日志
DEST/inventory.json               资产闭包、资源、历史缺口与省略项
```

manifest 必有 `{format, created_at, factory_asset, files, inventory_digest}`。files 为全部普通文件（除自身）的 `{path,digest,size}` 升序列表。inventory 必有 `{closure, resources, workspaces, omitted, history_gaps}`；history_gaps 列出历史上已按 retention 删除的原件/日志，不能把保留期内快照称作从未丢过任何历史。活跃资产闭包中的缺失一律失败。检查点保存全部现存库记录、CAS、Registry 全历史及现存已打码日志；不保存凭据、CLI 私有聊天或重建即可得到的目录。秘密需重新绑定。

### 4.7 可移交包

release 输入：

```json
{
  "root": "asset:sha256:...",
  "registry": "asset:sha256:...",
  "contracts": ["asset:sha256:..."],
  "review": {"closure_digest": "sha256:...", "by": "ada", "note": "已核对授权和隐私"},
  "bindings": [{"key": "publisher", "kind": "account", "required": true}],
  "omitted": [{"item": "seller-history", "reason": "private", "replacement": null}]
}
```

root 是经过脱敏重建的 factory 资产；registry 是 `kind=registry` 资产，其唯一 Bean 的 payload 是完整、干净、已检查的 Registry 文件树（相对路径，没有 .git）。contracts 必须在 root 闭包内。root、registry、contracts 和需要的所有派生数据必须在 checkpoint 中，导出不临时读卖家磁盘。review.closure_digest 绑定规范 JSON `{closure, registry, contracts, bindings, omitted}` 的摘要；不匹配就不能沿用审核。此审核是披露确认，不宣称算法识别了所有个人信息。

```text
DEST/manifest.json                 mao-factory-package/1，最后发布
DEST/registry/<path>               当前审核树，不含任何 .git 或旧提交
DEST/catalog.json                 所选不可变 document，不含卖家授权/heads/trials/actor 列
DEST/beans.json                    所选 Bean 身份、meta、facts、payload 清单
DEST/blobs/sha256/<2>/<62>          仅闭包需要的字节
DEST/bindings.json                 只含资源要求和非秘密的配置蓝图
```

manifest 必有 `{format, created_at, root, registry, contracts, review, omitted, files}`。catalog 为按 id 升序的 `[{id,document}]`，beans 为按 id 升序的 `[{id,type,meta,facts,payload,sources,proofs}]`。sources 是上游 Bean id 列表，proofs 为 `[{report: digest, historical_only: true}]`，记录被披露的原始准入/封存报告；它们不参与 Bean 内容 id、不进入买家活跃执行表。reports/sources 的所有引用也必须闭合。文件闭集与路径规则同检查点。kind=evidence 的业务资产可以保存脱敏历史，但裸 job/turn/run/receipt/trial 引用不能留在公开闭包里：改为新的 evidence 资产并明确 `data.historical_only=true`，旧引用重建为新资产。旧的评测结论只当历史资料，不能伪装为对新脱敏内容的验证。公开 handoff 的 data.tasks/external 必须为空；卖家进行中事务如需展示，转成独立 historical_only 数据，不绑定为买家进行中的任务。

禁止 factory.db、factory.json 原件、registry.bundle、Git 历史、秘密库、原生 CLI 目录、原始日志、任意未列出的字节。卖家的工单、审批、定时器、邮箱、发布回执和交易订单不恢复成买家的待执行事项。所有必需依赖必须在闭包内或有明确 bind 声明；required + unavailable 拒绝导出。

bindings 的输入文件格式为 `{resources: {key: {kind, fingerprint, ref?, secret_class?}}, factory_config: {...}}`。kind=secret 的 ref 必须指向买家明确提供的本地文件（不能是包内路径），secret_class 使用既有 secret_set 分类；导入预检可读性，在新根复用 mao.secrets 的 normalize_value/store_secret，按公开 name 安装买家值。既有 secret 名/挂载引用可通过买家配置绑定，但不能把名字声明当成已经有可用的秘密。factory_config 是买家自己的配置，且 governance.registry 由安装位置覆盖；秘密只接受买家文件引用或已有挂载，不接受 JSON 内联值、包中值；引用路径不写入公开清单或事件。resource 的 kind 与要求一致，必需项齐全；可选项的缺失明确返回。不得在清单、异常或事件里抄出该输入文件内容。

## 5. 行为

### 5.1 目录、原件与 GC

1. put 在事务内校验规范化内容、已知秘密、每条引用以及所有 Bean/blob 的存在与封存；构建反向边后插入。引用校验与插入不得和 expire/GC 错开事务。
2. 所有未 release 的记录是保留根；正式 head、open 试验、当前交接与正在接管的工作区也是根。retention 展开同一 closure，保护 Bean、封存报告、所需 epoch、job/turn 日志和旧接管工作区；不依赖可随意移除的 `pin` label。
3. `Warehouse.expire`（含人工调用）在同一事务中拒绝受保护 Bean。GC 的选择和过期仍须重检；若其间被登记为受保护资产就跳过并计入 kept，不让本次 GC 整体失败，防止“先算出可删，再被登记为证据”的竞态。日志/工作区清理遵循相同保护集合。
4. release 仅显式人的操作面；reason 非空。head/open trial/其他未释放资产仍引用时拒绝。只解除该根，不直接删字节；后续 GC 决定是否回收。历史 document、授权、报告、事件不可删除。
5. 每次写操作发 `asset.created/released`、`knowledge.*`、`asset.trial.*` 或 `agent.takeover.*` 事件；仅写 id、subject、state、reason 的打码值，不广播全文。没有订阅的资产事件不额外唤醒厂长。

### 5.2 授权与试验

1. authorize 只接受有效 contract 或 directive，人的 by 使用与 talk 人名相同的规则。保存与资产 hash 和上述 standards 绑定的授权；同内容、同标准幂等。标准已变则拒绝沿用授权。移交时授权不继承。
2. open_trial 检查授权及 standards、target.scope 与契约 scope 完全一致、依赖与对应 kind 的数据；knowledge 目标需 claim，takeover 目标需 handoff，release 目标需 factory；同 subject 已有 open trial 拒绝。记录有效 context 和 `MAX(events.seq)` watermark，不启动 job、不发新工作消息、不清空会话。
3. 普通知识试验只要求契约自己声明的覆盖。takeover 必须覆盖 `normal/exception/recovery/uncertainty`；foreman 还必须覆盖 `planning/budget/change/handoff`。这些是用例标签，不是内核硬编码的业务阈值。release 的覆盖由已授权的全厂契约决定。
4. 编排在数据中：厂长自己选下一个真实任务、候选适用范围、样例与预算。可以使用同一个真实任务的产出满足多个检查；同一执行只允许满足 subject/kind 相同且断言均适用的 case。禁止为了标签完整而把一个成功轮次当作处理过四类不同场景；授权者应把场景输入/事实断言固定在各 case 中。
5. 对于 station，方法、技能或工具的实际材料必须在 target.beans 中，且全数经普通端口成为该 job 的直接输入；目录 summary 不会偷偷变成工位指令。为空就不能用 job 当使用目标的证据。显式 Bean 输入自然进入已有缓存键，不修改 `KeyMaterial` 和 `render_brief` 签名。
6. 对于 agent，正常工作轮开始时冻结该岗位正式知识；若有 open knowledge trial，以 target 替换同 key 的版本；takeover 的目标只在新工作区进入 testing 后作为 handoff 使用，pending 之前的旧岗位不能成为接管证据。只读 `/factory/in/asset-context.json` 列 `{context_digest, assets:[完整目录记录], handoff}`；相关 Bean 按资产 id/materialize 到 `/factory/in/assets/`。提示只加路径与摘要，不拼原始聊天和整个证据史。每轮固定版本，轮中晋升只影响后轮。
7. Runner 的实际新 job 与 AgentHost 的 work turn 在执行前记录 asset_uses。job 只登记确实通过直接输入使用的目标；agent 登记本轮挂载版本。无 open trial 的既有普通执行无需计算全套契约文件摘要。缓存命中没有新执行，不为它补造 use。未启用时没有额外扫描或记录。

### 5.3 验收与正式知识

1. evidence 必须精确覆盖所有 case；对象存在、已结束；subject/kind、固定 context、target 使用关系一致。创建与真正运行事件都必须在 watermark 后。job 必须有新的 RUNNING 事件；run node 的旧 job/cache_hit、旧日志、候选作者写的 `passed` 字段一律不是新能力证据。
2. 每个 case 构造 4.4 的 out，执行已授权的 MRL 断言；结果存为不可改写报告。查不到字段、未完成、错误引用为 `ASSET_EVIDENCE_INVALID`，trial 保持 open；合法但断言 false/求值出错是 failed，不修改正式 head。
3. takeover 中 uses_target 的 primary turns 必须 handoff=target、workspace_generation=trial id，且至少第一个 primary turn 为 fresh=true，并实际使用新工作区。后续正常续接同一新私有会话允许，不能因此要求每个场景重复冷启动。新模型/工具/环境只能用自己的 context；复用没有变化的昂贵上游 Bean 允许，primary 的实际能力执行不得用旧产物替代。
4. promote 需要 passed 的 knowledge trial、同 target、当前 context 一致、scope 与授权契约相同；或 target 为已由人 authorize 的 directive 且 trial_id=None。hypothesis 的分类不变。CAS expected 不匹配则整笔回滚。一个事务更新 head 和事件，不改任何工位契约、旧证据或其他 agent 私有文件。
5. invalidate 只让指向该 target 的 head 失效，保留所有版本与证据。回退是再次 promote 仍适用且有有效证明的旧版本；不自动抹掉新版本已产生的任务和外部后果。
6. 对已失效或 context 已变的证明，heads/get_trial 返回 stale=true；原报告的过去结论保留。新工作不把 stale head 当正式知识注入，厂长可以开新试验；不因此自动全厂复测。

### 5.4 每轮的业务记忆隔离

正式知识、handoff、消息队列、任务/回执、CLI 私有会话各自有明确归属。只将固定正式版本、当前试验候选和最新交接引用交给本轮；工作便笺可以继续留在 notebook，不能因为被摘要提及就自动成为正式知识。厂长可读目录、提出新版本、失效错误知识；不能用覆盖操作员私有文件的方式完成晋升。

上下文清单的构造复用本轮已有查询；不可变闭包/文件摘要可按内容 hash 缓存。不得每轮重新扫描全部 CAS、运行完整包校验、调用提炼模型或写全厂快照。整理频率由厂长按上下文与预算判断，没有新的压缩定时器。

### 5.5 普通轮换：先保交接，再清会话

仅在启用 M16 时替代 agents.md 5.6 的无条件轮换：

1. 运行现有一次 handoff CLI（沿用原预算/超时），提示输出 HANDOFF.next.json。解析并校验 4.5、已知秘密、业务引用/状态。HANDOFF.next 的存在但没有成功 handoff turn 也不算成功。
2. 成功后 capture_handoff，在同一个事务中保存新 head、清 session、递增 conversation、清 rotate_pending，记录原有 `agent.rotated` 成功事件。清理 next 文件可在提交后做；已提交资产不可被工作区后续编辑改变。
3. CLI 失败、空/损坏 JSON、状态/引用不符或漏了正式知识：保留 session、conversation、旧 handoff/head，把岗位 paused，保留 rotate_pending，开一条去重的 Inbox question（origin=`asset-handoff:<agent>`），发 `agent.handoff.blocked`；不写成功 rotated 事件，不在后台无限重试。
4. 宕机恢复不能仅凭 next 文件认定交接已通过。已提交 head 可重建读视图；未提交文件保留供厂长检查。resume 后再次正常轮换仍需合法 handoff。

### 5.6 岗位替换：同一条生产通道

1. open takeover trial 的 target 必须是该岗位有效 handoff 资产。prepare 要求没有另一接管；持有该岗位原有互斥租约。若目标当前有 work turn，记 pending，等该轮成功结束后再安装；失败轮不安装，保留旧工作区并取消 pending。不得中途把运行中容器改挂载。
2. 安装前再次核对 handoff 中任务状态和回执。若 pending 期间发生了相关业务进展，拒绝陈旧交接并通知厂长重新固化。用空的新工作区重建 4.5 声明文件、新建原生 CLI home、重建厂长 Registry 克隆。拒绝路径穿越/重叠、链接与私有目录。保存旧路径，设置 testing；清 session、递增 conversation；新任务沿原有邮箱、预算、权限运行。旧 worker/沙箱先关闭；generation 参与实例绑定，不能复用仍挂着旧工作区的沙箱。没有任务复制。
3. prepare 本身不生成合成工作、不发布、不增大权限、不改变模型/Registry；新 runtime/model 要先经原有配置治理。恢复被暂停的岗位时明确 resume；厂长自身的 pending 安装在成功轮结束后继续原本 active 状态。
4. evaluate passed 后 finish_takeover 原子写 capability head(proof=trial)，工作区 phase=stable；保留旧目录和交接依赖。failed 或 interrupted 的接管停止该岗位并 phase=failed；不自动把同一批消息派给旧 worker，不撤销已完成的外部动作。
5. abort 仅在没有运行轮次时恢复旧路径、清 session，并保持 paused；保留新目录、实际完成的消息/任务/回执。厂长据真实进度重新固化交接再 resume，不能把恢复配置当成业务撤销。不能 finish 尚未通过的 trial。
6. 日常同配置上下文轮换无需开 takeover trial；已有的有效能力证据仍适用。只有替换、相关依赖变更、导出/买家接手等明确事件才需要新证明，而且不自动全量执行。

### 5.7 冷检查点的一致性

1. 只支持现有 SQLite + 本地 CAS。backend-active 表示 Postgres/S3 时 `BACKUP_UNSUPPORTED`；不假称已经覆盖生产后端。格式把存储与业务分开，将来可增加提供同一一致性语义的适配器。
2. 目标须不存在或为空，已有 `.part` 不覆盖。先取现有 gc 租约，再进入 DB 写事务作为写入屏障；在屏障内确认无有效 serve/worker/agent 租约、无执行中的 job/turn、无未 stopped 的常驻/池实例，无处于 testing/pending 的接管。待执行任务可以保留。
3. 冷检查点不自动停止服务。停止并固化外部服务/卷的流程由资源所属方完成。进程、GPU 状态、浏览器 cookie、物理设备现场不能靠复制 SQLite 获得；状态已导成资产或明确重新绑定才可声明可恢复。
4. 保持写屏障期间，通过**另开的只读 SQLite 连接**做 online backup，再复制 CAS、固定 Registry bundle、已打码日志与声明 business 文件。禁止在持有写事务的同一个连接上调用 backup（会自锁）。可提取 backup 的公共内部实现复用，但不能直接在屏障内重入 create_backup 的写租约/事件流程。
5. 屏障使已检查为空闲的执行不能跨入 RUNNING。Registry refs/配置/业务文件在复制前后核对摘要，不同即 `ASSET_CHECKPOINT_BUSY`，不发布半成品。外部程序绕过控制面改文件不在信任模型内，但可观察到的变化必须失败。canonical Registry 有未提交改动、或将被排除的厂长克隆存在脏文件/仅在克隆中的未提交提案 commit 时，必须 ASSET_STATE_UNDECLARED；先将工作固化为资产或正常 ChangeSet，不得按“可重建”静默丢失。长复制不启用会和写屏障死锁的续租线程；GC 的写入同样受屏障约束。
6. inventory、旧 backup 的 manifest 和外层 manifest 校验成功后，原子发布目录；事件在写屏障释放后登记。任何错误清理自己创建的 `.part`，不覆盖旧完整包。hash 流式计算，每个 blob 只复制一次。
7. restore 先 verify，再按旧 backup 恢复库/CAS/Registry，加业务目录和上述 evidence 日志。清所有租约/沙箱实例归属；Agent 全部 paused、session=NULL，从固定 handoff 重新开私有会话。已完成节点/消息、审批决策、receipt/idempotency key 原样保留；不自动启动 serve、不恢复 native session、不重放已发布操作。capability 记录保留为历史，移除 capability head，并在 assets.environment 加由 IdSource 生成的 restore_generation，防止原环境的证明直接冒充新环境验收。知识版本保留但原证明变 stale，重新验证后可晋升同一内容版本。恢复报告在旧 backup 的字段之外返回 resources_missing、roles_paused 和 needs_acceptance=true。既有 Sink 恢复继续遵循回执核验，不能仅按 job 未终结就再次 execute。

### 5.8 导出、校验与买家接手

活动 Bun 业务包沿用 `mao.business-system/1` 业务文档。`factory business export ID --package DEST` 将该文档、文档中的 `cas://sha256:…` 引用对应的文件和 SHA-256 清单写入新目录，格式为 `mao.business-package/1`。文件名由内容摘要确定。导出扫描所选项目已配置的密钥值，发现匹配便终止发布。`factory business verify PACKAGE` 检查文件集合、文档引用、大小及摘要，校验报告显示 `acceptance: "not_run"`。`factory business import PACKAGE --root NEW_ROOT` 使用同级临时目录安装文件和业务定义，成功后发布到目标目录。买方获得新的业务和工位标识，以及待配置的密钥申请。导入的运行摘要可用于查看历史，执行队列从空状态开始。本入口提供业务定义与文件的跨工厂移交，M16 的披露审核、授权与契约验收使用以下规范。

Bun 导出从所选工厂的 CAS 读取引用文件。CAS 目录、摘要目录及摘要分桶都必须是实际目录。遇到通过符号链接或目录联接点提供的路径时，导出失败并清理本次临时目录。

1. 从已 verify 的 checkpoint 读取 release，计算闭包，核对 review 摘要、资源与许可。默认拒绝未选择的字节；未经审核的原始历史不因存在 backup 就被导出。脱敏修改必须成为新 Bean/新 document，不修改内容地址或沿用不再匹配的证明。
2. 校验 registry Bean 树（含脚本、样例、规范文件）通过既有 Registry 检查；禁止 .git、链接、设备文件、绝对路径、`..`、反斜线/盘符混淆、同路径/大小写折叠冲突。检查点和包的所有文件清单均遵守这些路径规则。
3. 已知 SecretStore 值的扫描在源检查点生成时完成；发现匹配就拒绝而非悄悄改字节。导出对公开文本/JSON/路径再做现有密钥模式检查，对二进制仅流式检查已知值与已批准 hash。检测结果不打印原文。对未知 PII/版权/二进制内嵌身份，必须由业务脱敏与披露审核负责，不输出“自动保证完全脱敏”的结论。
4. 严格按 4.7 生成新目录，无 DB、git bundle、旧 CLI 数据。verify 校验完整文件闭集、字节数/hash、Bean/资产内容 id、闭包无悬空、资源声明、Registry、公开格式禁止项、审核摘要。错误按 kind/path 排序。verify 不运行模型/业务/validator；acceptance 永远 not_run，不能用它代替能力验收。
5. import 先静态 verify 和检查买家 bindings，目标非空拒绝。缺必需绑定先失败，不向清单写凭据。创建自己拥有的临时根，安装已检查的 Registry 成为一个新的 Git 初始提交（没有卖家 refs），使用买家 factory_config 并先安装其文件引用所提供的秘密，随后启用既有打码过滤。唯一 Bean 按既有 Gate.admit_external 重新准入；实际 facts 与移交描述不一致则失败，应产生新的派生资产，不能伪造封存。准入按已批准类型的 validator，不重跑生产机器或全厂模型任务。大数据/昂贵 validator 的选择成本属于明确的导入动作，不按轮次重复。
6. 仅导入所选目录与物料、脱敏的历史 evidence 数据。创建空的执行/审批/邮箱/定时状态；不导入卖家 head、authorize、trial、缓存键或原生 session。角色初始 paused，知识初始 candidate；报告 `{root, state: "needs_acceptance", contracts, bindings_missing: [], roles_paused}`。全部完成才发布目标根；失败删除自己的临时根。
7. 买家授权自己的契约后，在新根用正常岗位运行代表性任务，形成 buyer context 的 takeover/release 证明；未变且已准入的昂贵上游材料可以复用。脱敏改变的知识/样例必须以新版本验证，卖家旧证明只有 historical_only。包的可交易范围包含整厂编排与完整业务闭包，但不许把本来缺失的资源、未验证质量或无法交付的数据包装为已经具备。

## 6. 错误

实现时按任务使用范围登记 `mao.errors.CODES`；设计阶段不改实现。

| 错误码 | 触发条件 | detail |
|---|---|---|
| `ASSET_NOT_ENABLED` | 无 assets 配置 | `field` |
| `ASSET_INVALID` | 文档/配置/claim/交接结构或类型不合法 | `field` |
| `ASSET_NOT_FOUND` | 目录版本不存在 | `id` |
| `ASSET_REFERENCE_INVALID` | 引用未封存/已过期/缺字节、坏来源或悬空 | `kind,id` |
| `ASSET_SECRET` | 文档、公开载荷或检查点含已知秘密/禁止凭据 | `path`（逻辑位置，无内容） |
| `ASSET_IN_USE` | release/expire 会破坏活跃闭包 | `id,roots` |
| `ASSET_HEAD_CONFLICT` | expected 与当前 head 不符 | `slot,expected,actual` |
| `ASSET_CONTRACT_INVALID` | 契约无效、表达式不合法、覆盖不够 | `field` |
| `ASSET_NOT_AUTHORIZED` | 契约/directive 未由人授权 | `id` |
| `ASSET_TRIAL_CONFLICT` | 同岗位已有试验或终态被改写 | `subject` |
| `ASSET_EVIDENCE_INVALID` | 证据不是指定目标/context 的新真实执行 | `case,reason` |
| `ASSET_PROOF_REQUIRED` | 晋升/接管缺 passed 或用途/目标不符 | `id` |
| `ASSET_CONTEXT_STALE` | 有效依赖变化 | `id` |
| `ASSET_HANDOFF_INVALID` | 交接失败或与当前业务状态不符 | `agent,field` |
| `ASSET_TAKEOVER_BUSY` | 接管与正在跑的轮次/另一接管冲突 | `agent` |
| `ASSET_CHECKPOINT_BUSY` | 非冷状态、文件/refs 改变或检查点并发 | `resource` |
| `ASSET_STATE_UNDECLARED` | 持久文件/外部状态未分类 | `path` |
| `PACKAGE_RELEASE_INVALID` | 公开闭包/审核/脱敏引用不合法 | `field` |
| `PACKAGE_BINDING_REQUIRED` | 缺必需资源或绑定 kind 不符 | `key` |
| `PACKAGE_TARGET_NOT_EMPTY` | 包/导入/恢复目标或 .part 已有内容 | `path` |
| `PACKAGE_CORRUPT` | 文件闭集、路径、hash、引用/准入校验失败 | `problems` |

`verify_package` 把不完整/损坏转为 problems，不抛出当作成功。沿用 `BACKUP_UNSUPPORTED`、`GC_BUSY`、`DB_BUSY`、Registry 和 Gate 的既有码；库的错误 detail 一律经过秘密过滤。

## 7. 验收测试

夹具 `tests/acceptance/m16/asset_world.py` 复用真实 Warehouse/Gate/Runner/AgentHost 和 M13/M15 的离线假 CLI，不 mock 被测组件。未实现时 importorskip，正式任务验收必须零跳过。测试是最低边界，单元测试还须覆盖并发与异常清理。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m16/test_m16_01_catalog.py` | `test_content_address_and_idempotency` | 相同内容幂等，不覆盖登记人；修改摘要产生新版本。 |
| | `test_rejects_unsealed_missing_and_secret_references` | 未封存/不存在引用和含已知秘密的记录都不能登记。 |
| | `test_dependency_closure_shares_payload_and_keeps_proofs` | 多资产共享一个 Bean，闭包包含真实准入报告且不复制载荷。 |
| | `test_gc_and_manual_expire_preserve_registered_evidence` | GC 与人工 expire 不能删活跃依赖；解除根之后才允许回收。 |
| | `test_release_cannot_break_another_asset` | 仍被其他未释放资产引用时拒绝 release，原件继续存在。 |
| | `test_broken_payload_is_reported_without_truncated_closure` | 活动证据字节丢失必须报错，不能返回看似完整的闭包。 |
| | `test_unknown_business_kind_is_data_not_execution` | 允许业务扩展 kind，登记工具/流程不自动改 Registry 或执行。 |
| | `test_disabled_factory_has_no_implicit_asset_work` | 旧工厂运行不产生 use，显式资产写入提示尚未启用。 |
| | `test_registration_and_expire_serialize_across_connections` | 未提交登记与另一连接的过期操作串行，提交后证据仍完整。 |
| `tests/acceptance/m16/test_m16_02_learning.py` | `test_contract_requires_exact_human_authorization` | 厂长不能靠候选中的自评或未授权契约晋升。 |
| | `test_normal_work_is_evidence_without_extra_execution` | 新真实 job 的既有 Gate facts 直接验收，不额外生产或调用模型。 |
| | `test_old_or_wrong_input_job_cannot_be_new_evidence` | 旧 job/cache 产出和没有直接使用候选材料的新 job 都拒绝。 |
| | `test_failed_assertion_preserves_old_head` | 实际产物不达既定标准时失败，不覆盖原正式版本。 |
| | `test_promotion_is_cas_and_terminal_proof_is_immutable` | 过时 expected 不能覆盖并发晋升；不能换证据改写终态。 |
| | `test_environment_change_stales_proof_without_rerun` | 环境变化使证明过期；查询状态不自动安排重测。 |
| | `test_agent_uses_version_pinned_context` | 下一次正常工作实际挂载候选，只有本岗位资产且不展开历史。 |
| | `test_directive_needs_authority_not_experiment` | 用户偏好需授权确切版本；保留 claim 分类与可追溯指针。 |
| | `test_case_coverage_and_mrl_errors_do_not_pass` | 无效 MRL 和不完整接管覆盖不得被授权/用于替换。 |
| `tests/acceptance/m16/test_m16_03_handoff.py` | `test_failed_handoff_preserves_session_and_last_good_version` | 交接 CLI 失败不清旧 session/交接，暂停且只开一个通知。 |
| | `test_successful_handoff_is_immutable_before_rotation` | 合法结构化交接先固定，下一轮新 session，工作区编辑不改资产。 |
| | `test_malformed_or_secret_handoff_never_rotates` | CLI 成功不等于交接成功，损坏或秘密材料不能促成轮换。 |
| | `test_takeover_starts_clean_and_consumes_one_real_queue` | 新工作区看不到旧私有历史；代表任务只消费一次并形成有效能力证明。 |
| | `test_takeover_failure_does_not_redeliver_completed_messages` | 失败暂停且保留新工作成果；abort 不把已做事项重新派给旧 worker。 |
| | `test_stale_business_progress_blocks_handoff_capture` | 交接记录写错任务状态时拒绝，不用旧记忆覆盖真实执行进度。 |
| | `test_workspace_escape_is_rejected` | 交接不能借路径穿越或 CLI 私有目录访问旧数据。 |
| | `test_ordinary_rotation_does_not_create_takeover_or_duplicate_work` | 普通上下文轮换只增加已有 handoff 轮，不建立验证副本或新试验。 |
| `tests/acceptance/m16/test_m16_04_checkpoint.py` | `test_checkpoint_contains_factory_state_and_business_files` | 检查点含现存业务、目录、队列和 Registry，排除原生 CLI 凭据。 |
| | `test_live_serve_blocks_checkpoint_without_stopping_it` | 活跃服务拒绝冷检查点，不擅自停止生产或发布不完整目录。 |
| | `test_unclassified_workspace_state_blocks_complete_claim` | 业务文件没有分类时不能声称完整保存。 |
| | `test_corrupt_checkpoint_fails_before_restore` | 摘要不符和额外文件都失败，恢复前不改目标。 |
| | `test_backend_limit_and_existing_destination_are_explicit` | 不支持的后端与已有目标显式拒绝，不能覆盖或静默降级。 |
| | `test_restore_keeps_idempotency_and_does_not_publish` | 已核验回执、幂等键原样恢复，恢复动作不重新创建 job。 |
| `tests/acceptance/m16/test_m16_05_package.py` | `test_export_rebuilds_closed_package_without_database_or_git_history` | 只导出审核闭包：不带未选资产、Git 旧史或运行数据库。 |
| | `test_changed_release_cannot_reuse_disclosure_review` | 审核绑定确切闭包，改内容/披露范围后必须重新审核。 |
| | `test_required_unavailable_resource_cannot_be_sold_as_runnable` | 不能交付的必需资源不能伪装为完整可运行能力包。 |
| | `test_known_secret_in_binary_blocks_checkpoint` | 已知秘密不能因藏在二进制而进入完整包，也不静默改写。 |
| | `test_verify_rejects_extra_bytes_and_unsafe_paths` | 额外字节和穿越路径不能通过静态校验或影响包外文件。 |
| | `test_missing_buyer_binding_fails_before_install` | 缺少必需绑定先失败，不建立半成品买家工厂。 |
| | `test_import_has_fresh_execution_state_and_untrusted_knowledge` | 导入真实准入数据，但不继承授权、正式指针、旧订单或会话。 |
| | `test_source_execution_reference_requires_sanitized_evidence_asset` | 裸执行引用不能带入公开图；需要重建为明确的历史资料。 |
| `tests/acceptance/m16/test_m16_06_surface.py` | `test_cli_catalog_roundtrip_and_authorize` | CLI 读写使用同一目录；人的 authorize 绑定确切契约。 |
| | `test_mcp_roles_keep_authorization_and_disk_outside_agents` | 厂长可组织和验收；普通 Agent 只贡献自己的候选，不能改标准/磁盘。 |
| | `test_agent_can_contribute_own_asset_without_actor_spoofing` | MCP 的作者由 Session 决定，Agent 不能登记别的岗位资产。 |
| | `test_foreman_normal_work_to_knowledge_loop` | 厂长用 MCP 选候选、收正常轮次证据、晋升，不增加生产轮次。 |
| | `test_package_verify_cli_reports_no_business_acceptance` | CLI 的静态校验只报告字节/引用完整性，不宣布业务验收通过。 |
| | `test_asset_operations_leave_governance_and_jobs_unchanged` | 资产组织不绕过治理：不修改 Registry/宪法，不生成 job 或启动发布。 |
| | `test_repeated_status_reads_do_not_schedule_validation` | 读目录、heads、试验状态不消耗模型、派消息或制造验证任务。 |
| | `test_foreman_can_prepare_its_own_successor_after_current_turn` | 厂长自身的接管请求延迟到本轮成功后安装，不中途换挂载。 |
| | `test_resident_foreman_orchestrates_existing_workers_and_reminders` | 常驻厂长复用邮箱委派和提醒，worker 仍只运行已授权产线。 |

M16-01 至 M16-06 的门槛依次为 9, 9, 8, 6, 8, 9；总计 49，均要求零失败、零错误、零跳过。

## 8. 错误示范与正确示范

- ✖ 把厂长总结覆盖到操作员的私有笔记，再当成正式知识。✔ 新建不可变候选，用固定契约与真实证据晋升；下一轮才读取新指针。
- ✖ 把一次 cache hit 或 `turn.state=ok` 当作换模型后的能力通过。✔ 对确切上下文执行新的 primary 任务，检查业务事实、异常和恢复场景。
- ✖ 在后台复制所有生产任务去评测候选。✔ 同一个岗位接着消费原队列；正常工作兼作证据，失败暂停并保留外部回执。
- ✖ 对旧备份压缩打包，附一句“已脱敏”。✔ 用审核过的派生资产重建公开闭包；Git 历史、DB、秘密和旧私有会话没有默认通行权。
- ✖ handoff 失败仍清 session；恢复后重跑未终态的发布。✔ 先固化交接再轮换；恢复核对 receipt/idempotency，不把配置回退当业务撤销。

## 9. 风险与注意事项

- 这是能力证据框架，不是通用真理判定器。输入/上下文绑定证明材料确实被交付，不能证明模型在因果上使用了它；必须继续检查实际业务结果。一次窄样例通过不能被展示成跨环境通用能力。弱契约仍会批准弱能力；管理上将标准制定/授权与候选生产/实施分开，不能让被考核者临时改指标。
- 试验目标、合同、事实和环境必须一起保留。保留根防止隐式退化，但会增加存储；释放是显式管理动作，压缩不兼任删除。
- 冷检查点占用写屏障且需要停机窗口，这是为一致性付出的 I/O 与可用性成本；不是持续模型算力成本。大规模远程后端适配另立里程碑。
- 新工作区隔离私有历史不等于净化所有业务数据；原始网页/资料仍是不可信输入，不能升级成权限或控制指令。
- 不把量化的回测等同实盘、把一次研发样机成功等同量产；契约应固定时间/市场/设备条件、反例与失效触发。领域统计显著性由领域契约处理。
- 可重用材料、可恢复进度、可复现结果、可转让权利、可在买家环境运行是不同主张，包必须分别有证据或明确缺口。禁止一个静态校验绿灯覆盖这五种含义。
