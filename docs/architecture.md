# 智能机器工厂（machine_agent_org）架构设计

> 状态：草案 v0.1　|　日期：2026-11-09　|　范围：架构设计（不含实现）

## 阅读指引

本文回答四个问题：

| 问题 | 主要章节 |
|---|---|
| 怎么做到**通用**：任何开源产品、任何 CLI agent 都能接进来？ | 3 元模型、5 适配层 |
| 怎么保证**秩序**：产物一定能作为下一站的物料？ | 6 运行层、7 验证体系、12 秩序保障总览 |
| 产物和下游需要的物料**不兼容**时怎么办？ | 9 转换体系 |
| 外部机器人（厂长）怎么**维护**整个系统，防止契约偏移和结构被破坏？ | 8 契约治理、11 治理与 Foreman |
| 结构怎么做到既能**动态变化**又保持**稳定**？ | 10 动态结构与稳定性 |

初读建议顺序：0 → 1 → 3 → 4 → 13（示例），之后按需阅读其他章节。附录 A 是各类 Spec 的字段参考，附录 B 是术语表。

## 目录

0. 背景、目标与非目标
1. 总体思路与分层
2. 设计原则
3. 元模型
4. 概念映射
5. 适配层
6. 运行层
7. 验证体系
8. 契约治理：防止契约偏移
9. 转换体系：让产物自动成为物料
10. 动态结构与稳定性
11. 治理与 Foreman（厂长）
12. 秩序保障总览
13. 示例
14. 技术选型与对照
15. 路线
16. 开放问题
- 附录 A：Spec 字段参考
- 附录 B：术语表

---

## 0. 背景、目标与非目标

### 0.1 背景

目标是把“用 agent 驱动开源产品产出东西”组织成一座工厂：

- **智能机器**：一个开源项目或产品，可能是以下几类之一：
  - **智能体原生**的仓库，例如 OpenMontage。它没有代码编排器，靠编程 agent 阅读仓库里的 `CLAUDE.md`、技能文件和流水线清单来驱动，并且自带质检、预算控制和人工审批点。
  - **服务型**产品，例如 MoneyPrinterTurbo。它提供 WebUI / REST API，任务异步执行。
  - **普通工具**，例如 ffmpeg、rclone、pandoc。
- **机器人（操作工）**：已有的 CLI agent，例如 Claude Code（无头模式）、Codex。它通过标准接口操作智能机器，把**物料**加工成**产物**。
- **物料流转**：上一个工位的产物成为下一个工位的物料，就像工厂里的物料流转。
- **外部机器人（厂长）**：维护整个系统，保证产物始终能作为物料使用。
- **消费者**：不再产出物料的智能机器，负责把最终产物消费掉，例如推送到 GitHub、存到 S3、发布到外部网站。

原始草图（简化）：

```
外部机器人 ──控制──▶ 编排
                     bean 物料 ──▶ 沙箱（已有产品：机器人）──▶ 沙箱（已有产品：智能机器）──┃──▶ bean 产物
```

草图中的竖线表示：**最终的消费终端可以没有产物**。

### 0.2 目标

| 编号 | 目标 | 衡量方式 |
|---|---|---|
| G1 | 通用接入：任意开源产品、任意 CLI agent 都能接入 | 新增产品或 agent 只需编写声明式描述，不改内核代码 |
| G2 | 物料可流转：产物一定能被下游当作物料 | 每条边都经过编译期的类型与契约检查，以及运行期检验 |
| G3 | 秩序：agent 的不确定性被限制在工位内部 | 工位之间的所有事情都由确定性机制完成 |
| G4 | 可维护且完整自治：厂长能维护整个系统，而且不会把系统改坏 | 运行与维护不需要人工审批；所有结构变更都经过验证流水线；宪法只能由人修改 |
| G5 | 动态而稳定：结构可以随时演进，运行不受干扰 | 运行绑定结构快照；任何变更都能一键回滚 |
| G6 | 可追溯：每个产物都能追溯到输入、配置、模型和生产过程 | 内容寻址 + 来源证明 + 血缘 |
| G7 | 自动适配：不兼容的产物与物料之间由转换自动衔接 | 写过的转换命中即自动触发；缺失时由厂长编写，之后持续优化 |

### 0.3 非目标

- **不自研 agent**：系统里没有任何 LLM 调用循环，所有智能都来自已有的 CLI agent（Claude Code、Codex 等）。
- **不替代产品内部的编排**：工位内部怎么做，由产品和 agent 决定；工厂只管工位之间。
- **不做通用工作流引擎**：只服务于“agent + 产品 → 带类型的产物”这一类流程。
- **不保证 LLM 产出是确定的**：保证的是过程可追溯、结果可验收、失败可控。
- **首版不做多租户和多主机集群**：先用单机 Docker 作为执行环境，接口为后续扩展预留。

### 0.4 已确认的关键决策

| 编号 | 决策 | 在本设计中的体现 |
|---|---|---|
| D1 | 最终的消费终端可以没有产物（草图中的竖线） | Sink 默认不产出物料，每次执行只留下回执记录（见 3.6、5.4） |
| D2 | 1 个工位 = 1 台机器的 1 个 Recipe | 多台机器协作时，用多个工位或嵌套产线组合（见 3.4） |
| D3 | 完整自治：运行与维护不需要人工审批；宪法只能由人修改 | 高风险变更改为多个 runtime 交叉复核，另设安全模式兜底（见 11.3、11.4） |
| D4 | 不兼容时由厂长编写转换；写过的转换命中后直接触发；厂长可以优化这些转换 | 转换体系（见第 9 章） |

---

## 1. 总体思路与分层

一句话概括：**内核稳定，结构即数据，适配器可插拔，治理回路是确定性的，智能被限定在边界内。**

```
┌─ 治理层 Governance ─────────────────────────────────────────────────────
│  验证流水线(V1–V6) · 风险分级 · 发布控制(影子/金丝雀/回滚) · 不变量审计
│  漂移监测 · 调和器 · 熔断/背压控制器 · 审批服务
│  ‖ Foreman（agent，负责需要判断的维护工作）  ‖ 人（只修改宪法 L0）
├─ 结构层 Structure ──────────────────────────────────────────────────────
│  Registry（Git）：Type / Machine / OperatorRuntime / Station / Line / Policy / 转换库 …
│  每个合入的 commit 就是一个 epoch，附带一张转换解析表；每次运行绑定一个 epoch
├─ 运行层 Run ────────────────────────────────────────────────────────────
│  调度器 · Job 状态机 · 工位沙箱 · 质检门 · 自动转换 · 配额与预算 · Warehouse（Bean + 血缘）
├─ 适配层 Adapter ────────────────────────────────────────────────────────
│  SandboxProvider · Station Gateway / factoryctl · 收货器 · Runtime 渲染器
│  出网代理 · 模型代理 · Source/Sink 连接机制 · 存储后端
└─ 内核 Kernel ───────────────────────────────────────────────────────────
   元模型 · 编译器 · 宪法不变量引擎 · 事件日志（极少变化）
```

说明：

- 具体的 Machine Package、OperatorRuntime 等**实例**都是 Spec，存放在结构层的 Registry 里。适配层指的是**解释和执行**这些 Spec 的通用机制。
- 越往下越稳定，越往上越动态：

| 层 | 内容 | 典型变化频率 | 谁能改 |
|---|---|---|---|
| 内核 | 元模型、编译器、不变量引擎 | 很少（随系统版本发布） | 系统开发者 |
| 宪法（L0） | 不变量、风险分级规则、审批矩阵、厂长权限 | 很少 | 只有人 |
| 适配层 | 沙箱、网关、代理、收货器等机制 | 少（随系统版本发布） | 系统开发者 |
| 结构层（L1） | Type、Machine、Station、Line、Policy 等 Spec | 以天或小时计 | 厂长，通过 ChangeSet |
| 运行层（L2） | run、job、Bean | 以秒或分钟计 | 调度器、操作工；厂长可以执行运行操作 |
| 治理层 | 验证、审计、控制回路的执行 | 持续进行 | 确定性组件 + 厂长 |

**物料流转全景**：

```
外部世界              工厂内部：只流转已封存（sealed）的 Bean                                         外部世界
webhook/上传 ─▶ [Source] ─[检验]─▶ (Bean) ─▶ [Station] ─[检验]─▶ (Bean) ─[自动转换]─▶ … ─▶ [Sink] ─▶ S3 / GitHub / 网站
                                                                                            ┃
                                                                          消费终端：没有产物，只留下回执记录

Foreman ──Control-Plane MCP / factory CLI──▶ 治理层 ──ChangeSet──▶ Registry ──epoch──▶ 运行层
```

---

## 2. 设计原则

| 编号 | 原则 | 含义 | 为什么 |
|---|---|---|---|
| P1 | 站间确定性，站内智能化 | 物料流转、检查、存储、调度都由确定性代码完成；agent 只在工位沙箱里驱动产品 | LLM 的不确定性必须有边界，秩序只能建立在确定性之上 |
| P2 | 不造 agent | 智能全部来自已有的 CLI；网关、代理、验证器、调和器都是确定性组件 | agent 产品迭代很快，系统应该借用它们，而不是重造 |
| P3 | 结构即数据 | 所有结构都是声明式的 Spec，存放在 Git 中，本身也是带类型的 Bean | 结构可以用同一套机制校验、版本化、回滚；厂长修改结构就是修改数据 |
| P4 | 契约优先 | 编译期检查类型和消费者契约；运行期做来料检验和出厂检验 | “产物能作为物料”必须在接线时就能证明，而不是等到运行时碰运气 |
| P5 | 只增不改 | Bean 和 Spec 版本都不可变；演进方式是新增版本 → 迁移引用 → 退役旧版本 | 不可变才能做到可追溯、可缓存、可回滚 |
| P6 | 快照隔离 | 每次运行都绑定一个结构版本（epoch） | 结构变更不会影响正在进行的运行 |
| P7 | 副作用由凭据把关 | 发布类凭据只给 Sink，摄入类凭据只给 Source | 没有凭据就无法产生对外副作用，这比靠约定可靠 |
| P8 | 能不用 LLM 就不用 | 操作工可以是 agent 或脚本 | 确定性的步骤交给脚本，更便宜也更稳定 |
| P9 | 一切都要验证，包括验证器和厂长 | 验证器自带正反样例；厂长的变更同样要经过验证流水线 | 防止“放水”，也防止维护者把系统改坏 |
| P10 | 完整自治，宪法归人 | 运行与维护不需要人工审批；人只修改宪法 | 自治靠确定性的约束兜底，而不是靠人盯着 |
| P11 | 转换写一次，处处复用 | 不兼容时由厂长编写转换；之后命中即自动触发，并持续优化 | 适配成本只付一次，而且越用越便宜 |

---

## 3. 元模型

元模型是通用性的根基：系统只认识 7 个原语和 3 个元层概念，其余一切都是它们的组合。

### 3.1 原语一览

| 原语 | 工厂类比 | 一句话定义 |
|---|---|---|
| **Type** | 物料规格 | 描述什么样的东西算作这种物料：schema、validator、命名属性 |
| **Bean** | 物料 / 产物 | 某个 Type 的一个实例，按内容寻址，不可变 |
| **Machine** | 智能机器 | 一个已有产品，由 Machine Package 描述，对外提供若干 Recipe |
| **Operator** | 操作工 | 驱动机器的执行者：agent CLI 或脚本 |
| **Station** | 工位 | Recipe × Operator × Policy，端口带类型 |
| **Line** | 产线 | 由工位组成的带类型 DAG，可以嵌套 |
| **Policy** | 厂规细则 | 权限、出网、凭据、预算、审批、发布、保留期 |

### 3.2 Type（物料规格）

- **命名**：格式为 `<领域>.<名字>@<major>.<minor>`，例如 `video.mp4@1.3`。引用时写成 `name@major`，在 epoch 内解析为该 major 下最新的 active minor 版本。minor 版本之间保证双向兼容，见 8.3。
- **种类**：
  - 原子类型：`video.mp4@1`、`doc.markdown@1`、`subtitle.srt@1` 等；
  - `bundle{a: T, b: U}`：具名字段的组合；
  - `list<T>`：同类元素的集合，用作 `map` 展开的输入或输出；
  - `ref<T>`：指向超大或外部对象的引用，只保存 digest 和定位信息；
  - `any`：顶类型，常用于通用的 Sink。
- **子类型规则**：
  - `T <: any`；
  - A 声明了 `extends: B` 且两者 major 相同时，`A <: B`，该关系可以传递；子类型会继承父类型的命名属性；
  - 当 `A <: B` 时，`list<A> <: list<B>`，`ref<A> <: ref<B>`。因为 Bean 不可变，这种协变是安全的；
  - `bundle` 支持宽度子类型和深度子类型：字段更多、或字段类型更具体的，是子类型；
  - 不同 major 的类型是不同的类型，只能通过 converter 转换（转换会被自动触发，见第 9 章）。
- **组成部分**：
  - `payload`：文件规则，包括路径、媒体类型、是否必需；
  - `meta_schema`：生产者**自报**元数据的 JSON Schema；
  - `validators`：在沙箱里运行的确定性程序，从 payload 中**实测**出 `facts`（例如用 ffprobe 测出时长和分辨率）。每个 validator 都自带正样例和反样例；
  - `properties`：**命名属性**。每个属性是一段 CEL 表达式，原则上只引用 `facts`，因为实测值比自报值可信；
  - `compat`：兼容规则，默认是 `full-within-major`。

### 3.3 Bean（物料）

- **结构**：`bean.json`（清单）+ `payload/`（文件）。
- **标识**：`bean_id = sha256(规范化后的 {type, meta, payload 中各文件的 digest})`。
- **facts**：由 validator 派生得到，按 (bean_id, validator 版本) 存储。封存时的 facts 会随来源证明一起固定下来。
- **来源证明（provenance）**：
  - 作为附件挂在 Bean 上，不参与 `bean_id` 的计算（参考 in-toto / SLSA）。
  - 内容包括：
    - 产出它的工位、job 和 epoch；
    - 机器版本与镜像 digest；
    - operator 的 runtime 和模型；
    - 输入 Bean 和参数；
    - 检验报告和成本；
    - transcript、决策日志、工作区快照的引用。
  - 同样的内容由不同 job 产出时，得到的是同一个 Bean，挂着多份来源证明。
- **生命周期**：
  1. `candidate`：还在工位的输出目录里。
  2. `sealed`：通过出厂检验，已入仓。
  3. `quarantined`：被隔离（可能发生）。
  4. `expired`：payload 按保留策略回收；清单和来源证明作为墓碑保留，保证血缘闭合。
- **标签**：可变的指针，类似 git ref，例如 `run/r-0042/vertical.video`。标签只用于查找，不影响内容。

### 3.4 Machine 与 Recipe

- 一个 Machine 对应一个已有产品，由 **Machine Package** 描述（见 5.1）。
- 一个产品可以提供多个 **Recipe**（配方）。每个 Recipe 声明输入输出类型、任务模板或调用方式、收货规则。例如 OpenMontage 的 12 条流水线可以对应 12 个 Recipe。
- 规则：**1 个工位 = 1 台机器的 1 个 Recipe**。需要多台机器协作时，用多个工位或嵌套产线组合实现。
- 纯 agent 的工作（例如做计划、编写接入包）使用通用的 `workspace` 机器：一个装好常用工具的基础镜像，提供一个自由形式的 Recipe `freeform`。

### 3.5 Operator（操作工）

| 种类 | 例子 | 适用场景 |
|---|---|---|
| `agent-cli` | Claude Code、Codex | 需要理解、判断，或操作复杂产品 |
| `script` | 控制面直接执行 Recipe 中声明的命令或 API 调用 | 参数确定、无需判断：转码、上传、结构化的 API 调用 |

完整自治的设计里没有人工工位，也没有人工审批（见 0.4 的 D3）。

### 3.6 Station（工位）

- 定义 = Recipe × Operator × Policy，再加上带类型的端口：
  - 输入端口：`type` + `requires`（依赖的命名属性）；
  - 输出端口：`type` + `guarantees`（保证的命名属性）+ `accept`（验收表达式，可以引用输入）。
- **角色**：
  - `source`：没有物料输入，从外部摄入物料；
  - `transform`：把物料加工成物料。以下三种是它的特化形式：
    - `converter`（类型转换）：单输入单输出，可以被编译器自动插入，见第 9 章；
    - `judge`（评审）；
    - `planner`（产出计划）。
  - `sink`：消费物料，对外产生作用。
    - **默认没有产物**（即草图中的竖线），也可以按需声明产物端口，例如发布后得到的链接。
    - 每次执行都会留下一条**回执记录**，用于幂等和审计。回执是执行记录，不是产物，不能被下游引用。

### 3.7 Line（产线）

- 产线是带类型的 DAG：每个 step 引用一个工位，输入写成 `$inputs.x` 或 `step.port`。
- 支持几种受限的动态构造：`map`、`switch`、`subline`、`plan`、`until`（见 10.2）。
- **分形组合**：声明了 `interface`（输入和输出）的 Line，本身就可以当作一个工位，被其他 Line 嵌套使用。

### 3.8 Policy（策略）

Policy 把“允许做什么”从工位定义中分离出来，方便集中治理。它涵盖：出网档案、凭据绑定（只保存引用）、预算、审批委托、发布策略、保留期、资源配额。

### 3.9 元层概念与自举

| 概念 | 说明 |
|---|---|
| **Spec** | 上面的每一个定义（Type、Machine、Station 等）都是一个 Spec，使用统一的信封：`apiVersion / kind / metadata / spec`。每个 Spec 本身也是一个 `spec.<kind>@N` 类型的 Bean |
| **Invariant** | 宪法规则，即系统在任何时候都必须满足的条件（见 12.3） |
| **ChangeSet** | 一组 Spec 的差异，本身也是带类型的 Bean（`spec.changeset@1`），是修改结构的唯一途径 |

统一信封示例（`mao` 即 machine agent org）：

```yaml
apiVersion: mao/v1
kind: Station            # Type | Machine | OperatorRuntime | Station | Line | Policy | Plan | ChangeSet | ConversionRequest | GoldenSet | Baseline
metadata:
  name: explainer
  version: 3             # Spec 版本：Type 用 "major.minor"，其余 kind 用递增整数；发布后不可变
  lifecycle: active      # active | deprecated | retired
  labels: {domain: video}
spec: {}                 # 各 kind 的具体内容，见附录 A
```

**自举**：既然结构本身也是 Bean，维护工作就可以建成产线。例如，“接入新产品”产线产出 `spec.machine-package@1` 类型的 Bean，用验证流水线作为它的出厂检验，再由一个 Sink 把它作为 ChangeSet 提交（见 11.10）。这样，厂长的工作也受同一套秩序约束。

---

## 4. 概念映射

| 草图中的元素 | 本设计中的概念 | 备注 |
|---|---|---|
| bean 物料 / 产物 | Bean + Type | 能不能作为物料，只看类型和契约 |
| 沙箱（已有产品：智能机器） | Machine | 由 Machine Package 描述 |
| 沙箱（已有产品：机器人） | Operator | 由 OperatorRuntime 描述 |
| 两个沙箱合在一起 | Station | 另外加上端口、质检门、策略 |
| 竖线 | 产物可选的边界 | 最终的消费终端可以没有产物：Sink 默认不产出物料，只留下回执记录 |
| 编排 | 运行层 + 结构层 | 确定性的调度和结构 |
| 外部机器人 | Foreman（厂长） | 只能通过 Control-Plane MCP / `factory` CLI 维护系统 |
| 无产物的智能机器 | Sink | 默认没有产物 |
| （草图中没有画出） | Source | 没有物料输入，从外部摄入物料；与 Sink 对称 |
| （草图中没有画出） | 出厂检验（Gate） | 每个工位的候选产物都要通过检验，才会被封存成 Bean |
| （草图中没有画出） | 转换（converter） | 上游产物与下游需要的物料不兼容时自动插入，见第 9 章 |

重画后的草图：

```
Foreman ──Control-Plane MCP──▶ 治理层 / 结构层 / 运行层

        ┌─────────────── Station ───────────────┐
Bean ──▶ Operator ──操作──▶ Machine ──▶ 候选产物 ─[出厂检验]─▶ Bean ─[自动转换（如需要）]─▶ 下一个 Station … ─▶ Sink ┃ 没有产物
```

---

## 5. 适配层

### 5.1 Machine Package：工厂与产品之间的标准接口

每个产品在 Registry 里对应一个目录：

```
machines/openmontage/
├── machine.yaml          # 主描述：启动、喂料、操作、收货、自检、需求
├── Dockerfile            # 或 compose.yaml；上游不提供时由厂长编写
├── adapter/              # 可选：收货脚本、OpenAPI→MCP 适配配置、reset 钩子、配置模板
├── FACTORY_NOTES.md      # 写给 agent 的工厂补充说明：无人值守规则、路径约定、已知问题
└── conformance/          # 黄金样例和冒烟测试
```

Machine Package 要回答六个问题：

| 问题 | 对应字段 | 说明 |
|---|---|---|
| ① 怎么启动 | `source`、`runtime` | 固定上游 commit 和镜像 digest；声明资源需求；实例策略为 `per_job`（默认）或 `pooled`（预热复用，job 之间执行 `reset`）；共享缓存卷 |
| ② 怎么喂料 | `recipes.*.inputs`、`task_template` / `call` / `run` | 输入 Bean 放到哪里；怎样把物料渲染成产品能理解的任务（自然语言、API 参数或命令行） |
| ③ 怎么操作 | `form`、`operate` | 接入形态（见下文）；给 agent 的操作说明，包括产品自带的 `CLAUDE.md`/`AGENTS.md` 和 `FACTORY_NOTES.md` |
| ④ 怎么收货 | `recipes.*.outputs.*.harvest` | 收货规则：哪个路径下的文件作为哪种类型的 Bean。agent 也可以用 `factoryctl submit` 显式提交，显式提交优先 |
| ⑤ 怎么自检 | `probe`、`conformance` | `probe` 动态报告当前可用的能力（例如配置了哪些 key）；`conformance` 是黄金样例测试，优先复用产品自带的测试 |
| ⑥ 需要什么 | `needs` | secrets（只写名字）、出网档案、预算旋钮映射、速率限制 |

**三种接入形态**：

| 形态 | 例子 | 控制接口 | Operator 放在哪里 |
|---|---|---|---|
| **A `workspace`：智能体原生工作区** | OpenMontage | 仓库 + agent 指令文件 + 技能 + CLI/Python 工具 | 与产品放在**同一个沙箱**里，cwd 指向工作区。agent 需要使用产品的 shell、文件和运行时，所以拆不开 |
| **B `service`：服务型** | MoneyPrinterTurbo、ComfyUI、带 MCP 的 Blender | REST / OpenAPI / MCP，异步任务 | 放在**单独的沙箱**里，经 Station Gateway 访问产品。Gateway 把 OpenAPI 转成 MCP 工具，并统一成“提交 → 轮询 → 取回”的长任务协议。参数是结构化的时候，脚本可以直接调用，不需要 agent |
| **C `tool`：工具型** | ffmpeg、rclone、pandoc、git | CLI | 一般由 `script` 操作工直接执行 |

> 草图里的“两个沙箱”在 B 形态下是物理隔离的；在 A 形态下是同一个沙箱里的两个进程，但逻辑上仍是两个独立的产品。

**示例 A：OpenMontage 类产品**（字段含义见附录 A.3；具体命令和路径以产品文档为准）

```yaml
apiVersion: mao/v1
kind: Machine
metadata: {name: openmontage, version: 7}
spec:
  form: workspace
  source:
    repo: https://github.com/calesthio/OpenMontage
    commit: 3f2a9c1                # 固定上游 commit
  runtime:
    image: registry.local/machines/openmontage@sha256:5d1e…   # 上游没有提供 Docker，由厂长编写 Dockerfile 构建
    workdir: /workspace            # 每个 job 独占一份仓库工作区
    instance: per_job
    resources: {cpus: 4, memory: 8Gi, gpu: none}
    caches:
      - {name: om-corpus, mount: /cache/corpus, mode: rw}      # 素材检索索引，跨 job 共享
  operate:
    guide: [CLAUDE.md, AGENT_GUIDE.md]   # 产品自带的 agent 契约
    factory_notes: FACTORY_NOTES.md
  needs:
    secrets: [FAL_KEY, PEXELS_API_KEY, ELEVENLABS_API_KEY]     # 都是可选的，按工位策略注入
    egress:
      providers: [fal.run, api.pexels.com, api.elevenlabs.io]
      web_read: true                     # 调研阶段需要读网页
    budget_knobs:                        # 把工位预算写进产品自己的预算机制（键名以产品文档为准）
      - {file: .env, set: {"<预算模式键>": cap, "<总预算键>": "{{ budget.cap_usd }}"}}
  recipes:
    animated-explainer:
      inputs:
        brief: {type: brief.video@1, place: /factory/in/brief}
      outputs:
        video:    {type: video.mp4@1,    harvest: "projects/*/renders/final.mp4"}
        subtitle: {type: subtitle.srt@1, harvest: "projects/*/renders/*.srt", optional: true}
        script:   {type: doc.script@1,   harvest: "projects/*/script*.json",  optional: true}
      task_template: |
        使用 animated-explainer 流水线制作视频。
        主题：{{ inputs.brief.meta.topic }}；受众：{{ inputs.brief.meta.audience }}。
        时长约 {{ inputs.brief.meta.duration_s }} 秒，画幅 {{ inputs.brief.meta.aspect }}，语言 {{ inputs.brief.meta.language }}。
        风格：{{ inputs.brief.meta.style | default("clean professional") }}。
    localization-dub:
      inputs:
        video:    {type: video.mp4@1}
        subtitle: {type: subtitle.srt@1}
      params:
        target_language: {type: string}
      outputs:
        video:    {type: video.mp4@1,    harvest: "projects/*/renders/*{{ params.target_language }}*.mp4"}
        subtitle: {type: subtitle.srt@1, harvest: "projects/*/renders/*{{ params.target_language }}*.srt"}
      task_template: |
        使用 localization-dub 流水线，把 /factory/in/video 中的视频翻译并配音为 {{ params.target_language }}，
        字幕参考 /factory/in/subtitle。
  probe:
    run: ["python", "-c", "from tools.tool_registry import registry; import json; registry.discover(); print(json.dumps(registry.support_envelope()))"]
  conformance:
    - {name: zero-key-demo, run: ["make", "demo"], expect: {harvest: {video: video.mp4@1}}}
    - {name: contracts,     run: ["make", "test-contracts"]}
```

**示例 B：MoneyPrinterTurbo 类产品**（节选；接口路径与字段以产品实际为准）

```yaml
apiVersion: mao/v1
kind: Machine
metadata: {name: moneyprinter, version: 2}
spec:
  form: service
  source: {repo: https://github.com/harry0703/MoneyPrinterTurbo, commit: b81c0d2}
  runtime:
    compose: compose.yaml               # 采用上游 compose，镜像全部固定 digest
    service: {name: api, port: 8080}
    instance: pooled                    # 预热实例，job 之间执行 reset
    reset: ["sh", "-c", "rm -rf /app/storage/tasks/*"]
  operate:
    openapi: http://api:8080/openapi.json     # Gateway 据此生成 MCP 工具
    long_task:                                # 统一的长任务协议
      submit: "POST /api/v1/videos"
      poll:   "GET /api/v1/tasks/{task_id}"
      done:   "<任务完成条件，按产品返回的状态字段编写>"
      fetch:  "<产物 URL 列表所在的字段>"
  needs:
    secrets: [LLM_API_KEY, PEXELS_API_KEY]
    egress: {providers: [api.pexels.com, api.deepseek.com]}
    config_render: {template: adapter/config.toml.tmpl, to: /app/config.toml}   # 把凭据和参数渲染进产品配置
  recipes:
    quick-short:
      inputs:  {brief: {type: brief.video@1}}
      outputs: {video: {type: video.mp4@1}}
      call:                             # 结构化参数：script 操作工可以直接调用
        video_subject:  "{{ inputs.brief.meta.topic }}"
        video_aspect:   "{{ inputs.brief.meta.aspect }}"
        video_language: "{{ inputs.brief.meta.language }}"
  conformance:
    - {name: smoke, recipe: quick-short, call: {video_subject: "conformance test"}, expect: {outputs: {video: video.mp4@1}}}
```

**示例 C：ffmpeg**

```yaml
apiVersion: mao/v1
kind: Machine
metadata: {name: ffmpeg, version: 3}
spec:
  form: tool
  runtime:
    image: registry.local/machines/ffmpeg@sha256:a03f…
    instance: per_job
    resources: {cpus: 2, memory: 2Gi}
  needs: {egress: none}
  recipes:
    reframe-vertical:
      inputs:  {video: {type: video.mp4@1}}
      outputs: {video: {type: video.mp4@1, harvest: "/factory/out/video/video.mp4"}}
      run: ["ffmpeg", "-i", "/factory/in/video/video.mp4",
            "-vf", "crop=ih*9/16:ih,scale=1080:1920", "-c:v", "libx264", "-c:a", "copy",
            "/factory/out/video/video.mp4"]
    subs-convert:                    # 由厂长编写的转换配方（见 9.5）
      inputs:  {in:  {type: subtitle.vtt@1}}
      outputs: {out: {type: subtitle.srt@1, harvest: "/factory/out/out/subtitle.srt"}}
      run: ["ffmpeg", "-i", "/factory/in/in/subtitle.vtt", "/factory/out/out/subtitle.srt"]
  conformance:
    - {name: reframe, recipe: reframe-vertical, inputs: {video: fixtures/1920x1080.mp4}, expect: {props: [vertical]}}
    - {name: subs,    recipe: subs-convert,     inputs: {in: fixtures/sample.vtt}}
```

### 5.2 OperatorRuntime：不绑定具体 agent

OperatorRuntime 用声明式模板描述“如何以无头方式启动某个 CLI”。换 agent 只需要换一个 runtime，工位和产品都不用改。

```yaml
apiVersion: mao/v1
kind: OperatorRuntime
metadata: {name: claude-code, version: 4}
spec:
  kind: agent-cli
  inject:                                   # 以只读卷注入产品容器；不行时再构建派生镜像
    volume: operator-claude-code@sha256:9b7c…
    mount: /opt/operator
  invoke:                                   # 参数仅作示例，以 CLI 当前文档为准
    argv: [/opt/operator/bin/claude, -p, "{{ brief.task }}",
           --append-system-prompt, "{{ brief.factory_contract }}",
           --output-format, stream-json, --verbose,
           --max-turns, "{{ limits.max_turns }}",
           --model, "{{ operator.model }}",
           --permission-mode, bypassPermissions]     # 沙箱本身就是安全边界
    mcp: {format: claude-json, flag: --mcp-config}   # 工位提供 MCP 版 factoryctl 或 Gateway 时使用
  env:
    ANTHROPIC_BASE_URL: http://model-proxy:8080      # 模型代理负责注入密钥和计量；容器内没有模型密钥
  transcript: {format: stream-json, from: stdout}
  models_allowed: [claude-opus-5-5, claude-sonnet-5-5]
---
apiVersion: mao/v1
kind: OperatorRuntime
metadata: {name: codex, version: 2}
spec:
  kind: agent-cli
  inject: {volume: operator-codex@sha256:41e2…, mount: /opt/operator}
  invoke:
    argv: [/opt/operator/bin/codex, exec, --json, "{{ brief.factory_contract }}\n\n{{ brief.task }}"]
    mcp: {format: codex-toml, path: ~/.codex/config.toml}
  env:
    OPENAI_BASE_URL: http://model-proxy:8080/v1
  transcript: {format: jsonl, from: stdout}
---
apiVersion: mao/v1
kind: OperatorRuntime
metadata: {name: script, version: 1}
spec: {kind: script}                        # 控制面直接执行 Recipe 的 run / call
```

能力边界：

- **A 形态**：agent 需要 Bash 和文件工具，因此能力边界由沙箱本身划定，包括文件系统、网络、凭据和资源。
- **B 形态**：可以通过工位的 `operator.tools` 把工具白名单收窄到 Gateway 的 MCP 工具（例如 Claude Code 的 `--allowedTools`），agent 就只能经由 Gateway 操作产品。

### 5.3 SandboxProvider

接口（与实现语言无关）：

| 方法 | 作用 |
|---|---|
| `prepare(job)` | 创建 job 专属的网络和卷，按 digest 拉取镜像 |
| `mount_inputs(job, beans)` | 把输入 Bean 解包到 `/factory/in/<port>/`（只读） |
| `start(job, spec)` | 启动产品容器、operator 进程、Gateway sidecar |
| `exec(job, argv)` | 在沙箱内执行命令（script 操作工、validator、probe） |
| `collect(job)` | 取回候选产物、transcript、日志 |
| `snapshot(job)` | 保存工作区快照，用于 resume 和排查 |
| `destroy(job)` | 回收容器和网络，按策略保留工作区 |

MVP 用 Docker Engine API 实现。以后可以换成 gVisor、Firecracker 或云沙箱，只要实现同一套接口即可。

### 5.4 Source 与 Sink 的连接机制

Source 和 Sink 不是特殊组件，而是**角色特殊的工位**。它们同样由 Machine（例如 webhook 接收器、rclone、git、平台上传 CLI）+ Recipe + Operator 组成，区别只在策略：

- 只有 Source 能绑定 `ingress`，即接收外部推送或拉取外部数据源。它产出的 Bean 同样要经过出厂检验，外部输入一律视为不可信。
- 只有 Sink 能绑定 `publish` 出网和发布凭据。它默认没有产物，每次执行只留下一条回执记录。回执记录包括目标、定位信息（URL、对象键、commit）、幂等键、核验结果和时间；它存放在 Warehouse 的记录库中，向下追溯血缘时可以看到“这个 Bean 被谁消费、发布到了哪里”，但它不是 Bean，不能被下游引用。

```yaml
apiVersion: mao/v1
kind: Station
metadata: {name: intake-brief, version: 1}
spec:
  role: source
  machine: webhook-intake@1
  recipe: receive
  operator: {runtime: script@1}
  outputs: {brief: {type: brief.video@1}}
  policy: source-webhook@1        # 只有 Source 能绑定 ingress
```

### 5.5 存储后端

Warehouse 提供以下接口：

- `put_blob / get_blob`：CAS 读写
- `seal(manifest, provenance)`：封存 Bean
- `attach(bean, attestation)`：附加证明
- `label(ref, bean)`：设置标签
- `lineage(bean, direction, depth)`：查询血缘
- `gc(retention)`：按保留策略回收

MVP 用本地文件系统 CAS + SQLite。生产环境换成 S3/MinIO + Postgres，也可以把 Bean 存成 OCI artifact（ORAS）。

---

## 6. 运行层

### 6.1 Job 与 Station Brief

调度器为每个工位实例创建一个 job。如果操作工是 agent，系统会把以下内容渲染成 **Station Brief**（任务书）：

1. **工厂契约**：无人值守规则、输入输出约定、禁止事项。通过 `--append-system-prompt` 等方式注入，优先级高于产品文档中的交互约定；
2. **任务**：用 Recipe 的任务模板填入输入物料得到；
3. **输出契约**：端口、类型、guarantees、验收标准；
4. **预算与审批策略**；
5. **上次被退回的反馈**（仅重试时有）。

渲染示例：

```text
# 工厂契约
你正在“智能机器工厂”的工位 explainer@3 中无人值守地运行（run r-0042 / job j-7Q2M）。
- 不会有人回答你的提问。产品文档中所有“等待用户确认”的环节，改为执行
    factoryctl approval request --kind <creative|cost> --summary "<要点>" [--cost-usd N]
  然后按返回结果继续（approved，或 rejected + 原因）。
- 输入物料在 /factory/in/<端口>/（只读）。用 `factoryctl inputs` 查看清单和元数据。
- 必须交付：
    video    → video.mp4@1     保证 has_audio、not_silent、hd；时长 54–66 秒
    subtitle → subtitle.srt@1
    script   → doc.script@1
  交付前可以用 `factoryctl check <端口> <路径>` 预检；完成后逐个执行 `factoryctl submit <端口> <路径>`。
- 预算上限 5.00 USD（已写入产品的预算配置）。用 `factoryctl budget` 查看剩余额度。
- 禁止：对外发布或上传；修改 /workspace 和 /factory/out 以外的文件；尝试访问未授权的网络。
- 无法完成时执行 `factoryctl fail "<原因>"`，不要提交不合格的产物。

# 上次退回的反馈（第 2 次尝试）
出厂检验未通过：video 时长 71.2 秒，超出验收范围 54–66 秒。

# 任务
使用 animated-explainer 流水线制作视频。
主题：为什么天空是蓝色的；受众：初中生。
时长约 60 秒，画幅 16:9，语言 zh-CN。
风格：clean professional。
```

### 6.2 factoryctl：工位内的标准接口

`factoryctl` 是系统提供的确定性 CLI，后端是 Station Gateway，同时也有等价的 MCP 版本。任何能执行 shell 命令的 agent 都能使用它。

> 注意区分：`factory` 是工位外（控制面）的 CLI，供厂长和人使用；`factoryctl` 是工位内的 CLI，供操作工使用。两者的权限完全不同。

| 命令 | 作用 | 说明 |
|---|---|---|
| `factoryctl inputs [--json]` | 列出输入端口、Bean id、类型、meta/facts 摘要、文件路径 | 只读 |
| `factoryctl contract` | 打印输出契约：端口、类型、guarantees、accept | 让 agent 明确交付标准 |
| `factoryctl check <port> <path>` | 对候选产物预跑出厂检验（结构、validator、契约） | 不计入正式判定，用来减少无效提交 |
| `factoryctl submit <port> <path> [--meta k=v …]` | 提交候选产物 | 可以多次提交，以最后一次为准 |
| `factoryctl approval request --kind … --summary … [--cost-usd N]` | 申请审批，阻塞直到有结果或超时 | 期间 job 处于 `AWAITING_APPROVAL` |
| `factoryctl budget` | 查看预算和已花费金额（来自代理计量） | |
| `factoryctl log <message>` | 写入结构化工作日志 | 会进入来源证明 |
| `factoryctl fail <reason>` | 主动声明失败 | job 进入 `FAILED` |

### 6.3 无人值守适配

很多产品（例如 OpenMontage）在创意决策点或超额支出时会停下来等用户确认。工厂里没有用户在场，这些决策点改由工位的**审批策略**处理：

```yaml
approval:
  propose: {auto_if: "proposal.cost_estimate_usd <= 3.0", else: foreman}   # 两阶段工位的方案审批
  in_run:  {auto_if: "request.cost_usd <= 0.5",           else: foreman}   # 运行中的审批请求
  timeout: 2h                                                               # 超时视为拒绝
```

审批人有三种，都不需要人：

- `auto`：按规则自动通过；
- `foreman`：由厂长在委托范围内决定；
- `review`：交给多个不同 runtime 交叉复核，用于超出厂长委托范围的决定。

所有审批记录都会写入来源证明。

### 6.4 沙箱拓扑与加固

**A 形态：一个沙箱，两个产品**

```
┌─ Job 沙箱（Docker，job 专属的 internal 网络）
│  /factory/in    只读：输入 Bean 解包后的内容
│  /factory/out   候选产物
│  /workspace     产品工作区（job 独占）；/cache/* 为共享缓存
│
│  Operator 进程：claude -p …   （cwd = /workspace）
│    ├─ 阅读产品自带的 CLAUDE.md / AGENT_GUIDE.md，以及 FACTORY_NOTES.md
│    ├─ 运行产品的 CLI / Python 工具
│    └─ factoryctl inputs / check / submit / approval / fail ──▶ Station Gateway（sidecar）
│
├─ 出网：经出网代理，按“域名 × 方法”放行并计量
└─ 模型请求：经模型代理（注入密钥、统计 token）
```

**B 形态：两个沙箱，通过网关连接**

```
[Operator 沙箱]  claude -p …   只能访问 Gateway 和模型代理；工具白名单收窄到 Gateway 的 MCP 工具
      │ MCP
      ▼
[Station Gateway]  station.* 工具（等价于 factoryctl） + machine.* 工具（由 OpenAPI 生成）；白名单 · 限流 · 审计
      │ HTTP（长任务协议：submit → poll → fetch）
      ▼
[Machine 沙箱]  产品服务（REST / WebUI）；出网 = providers 白名单
```

**C 形态**：没有 operator 进程，控制面通过 `SandboxProvider.exec` 直接执行 Recipe 中的命令。

**加固基线**（适用于所有形态）：

- 以非 root 用户运行；`cap-drop ALL`；根文件系统只读（工作区和输出目录除外）；限制 CPU、内存、进程数和磁盘配额；
- **永不挂载 docker.sock**，也不暴露控制面接口；
- 输入以只读方式挂载；每个 job 独占工作区，job 之间不共享状态；失败时保留工作区，用于 resume 和排查；
- 只按工位策略注入它需要的那几个密钥；模型密钥只存在于模型代理中。

**出网分级**：

| 级别 | 允许的访问 | 典型使用者 |
|---|---|---|
| `none` | 没有外网，只能访问 job 内部网络 | 工具型机器、converter、validator |
| `providers` | 白名单内的 API 域名（任意方法），经代理计量 | 调用生成模型、素材库的产品 |
| `web-read` | 任意域名的 GET/HEAD 请求，经代理记录 | 需要联网调研的工位 |
| `publish` | 对白名单发布目标使用写方法，并持有发布凭据 | **只有 Sink** |
| `ingress` | 接收外部推送，或拉取外部数据源 | **只有 Source** |

operator 访问模型 API 不占用上述任何级别，统一经过模型代理。

> 需要说明：`web-read` 在技术上并不能杜绝所有副作用（例如带参数的 GET 请求）。真正把关的是**凭据**：非 Sink 工位不持有任何发布凭据，因此无法以组织的身份对外发布。

### 6.5 Job 状态机

```
PENDING → PROVISIONING → RUNNING ⇄ AWAITING_APPROVAL
                            ↓
                         GATING → SEALED（终态）
                            ↓
                 REJECTED / FAILED → 未超过重试上限：回到 PENDING（带反馈；fresh 或 resume）
                                   → 超过重试上限：DEAD（死信队列 → 厂长）
```

| 状态 | 含义 | 何时离开、去往哪里 |
|---|---|---|
| `PENDING` | 等待调度：输入已全部 sealed，等配额或背压放行 | 被调度 → `PROVISIONING` |
| `PROVISIONING` | 创建网络、卷、容器，注入输入，做健康检查（含 probe） | 就绪 → `RUNNING`；失败 → `FAILED` |
| `RUNNING` | operator 或脚本正在工作 | 申请审批 → `AWAITING_APPROVAL`；退出且已提交 → `GATING`；超时、`fail` 或异常 → `FAILED` |
| `AWAITING_APPROVAL` | 等待审批（两阶段工位的方案审批，或运行中提出的审批请求） | 得到结果 → `RUNNING`；超时 → `FAILED` |
| `GATING` | 出厂检验 | 通过 → `SEALED`；不通过 → `REJECTED` |
| `SEALED` | 产物已封存入仓，并写入来源证明和血缘 | 终态 |
| `REJECTED` | 检验未通过，附带结构化反馈 | 未超过重试上限 → `PENDING`（反馈写入下一次的 Brief）；超过 → `DEAD` |
| `FAILED` | 运行失败 | 未超过重试上限 → `PENDING`（`fresh` 从头开始，或 `resume` 复用工作区续跑）；超过 → `DEAD` |
| `DEAD` | 死信 | 由厂长处理：带覆盖参数重试、修改 Spec 后重跑，或者放弃 |

**run 的状态**：

- 正常情况下：`RUNNING → SUCCEEDED | FAILED`。
- 如果编译或运行中遇到缺失的转换，run 进入 `BLOCKED`。转换生效后，系统以新 epoch 自动重建这个 run；已经完成的工位会命中缓存，不会重复执行，也不会重复花钱（见 9.5）。

### 6.6 两阶段工位

高成本工位和 Sink 都可以声明 `phases: [propose, execute]`：

1. **propose**：生产工位给出方案和成本预估；Sink 执行 dry-run，例如列出将要上传的对象、将要创建的提交。
2. **审批**：按 6.3 的策略进行。
3. **execute**：真正生产，或真正发布。

这样，“花钱”和“对外发布”这两类不可逆的操作都被放在审批之后。

### 6.7 出厂检验（Gate）

```
候选产物
  ─▶ ① 结构检查：文件齐全、媒体类型嗅探、大小上限、meta 符合 meta_schema
  ─▶ ② 类型 validator：在沙箱内运行（如 ffprobe），实测出 facts
  ─▶ ③ 契约与验收：guarantees（命名属性） + accept（可引用输入的表达式） + 自报的 meta 与实测的 facts 一致
  ─▶ ④ 可选的 judge：只读的 operator 按 rubric 打分（有抽样率和阈值），输出结构化结论
  ─▶ 通过：封存为 Bean，写入来源证明　｜　不通过：REJECTED + 结构化反馈
```

- 产品内部的质检（例如 OpenMontage 的渲染后自检）是**厂内自检**，工厂的这道门是**出厂检验**，两者不互相替代：前者保证产品按自己的标准把活做完，后者保证产物符合工厂的契约。
- judge 也只是一次 OperatorRuntime 调用（例如以只读方式运行 Claude Code），不是系统自研的 agent。
- 检验报告本身也是 Bean（`report.gate@1`），挂在来源证明上。

### 6.8 缓存与幂等

- **缓存键**：`job_key = H(工位 Spec 版本, 机器版本与镜像 digest, recipe, runtime + 模型, 输入 bean_id 列表, 参数, variant)`。命中且已有 sealed 产物时直接复用，避免为同一个需求重复付费。需要“再做一版”时，改 `variant` 即可。
- **Sink 幂等**：幂等键默认是 `sink + 输入 bean_id + 目标`。如果同一个键已经有回执记录，就不再重复发布。
- **转换缓存**：转换 job 同样适用缓存键，同一个 Bean 用同一版本的转换处理过，就直接复用结果（见 9.4）。

### 6.9 调度、配额与资源

- 按资源类型（CPU / GPU / 内存）排队；资源池按产线和优先级划分（隔舱）。
- 共享 API key 的速率和预算由控制面统一限流（令牌桶），避免各个工位分别撞上限额。
- 预算分层：全局 → 产线 → run → 工位 → job。超出预算时，拒绝调度、申请审批或终止运行。

### 6.10 Warehouse 与血缘

每个 Bean 的来源证明都记录了它的输入，因此任意一个 Bean 都可以向上追溯（它从哪里来），也可以向下追溯（它被谁使用、最后发布到了哪里）：

```
$ factory bean lineage sha256:77e0… --up
video.mp4@1.3  sha256:77e0…   ← explainer@3 / job j-7Q2M / epoch 9c1e7a2 / claude-code@4 (claude-opus-5-5)
└─ brief.video@1.2  sha256:41ab…   ← intake-brief@1 / job j-6P0A
```

### 6.11 可观测性

- **transcript**：agent 的完整输出流（stream-json / jsonl）作为附件存档；
- **审计日志**：Gateway 记录每一次工具调用，出网代理记录每一次外部请求；
- **指标**：各工位的成功率、退回率、耗时、成本、facts 分布，供漂移监测和控制器使用；
- **事件**：状态变化、审批、告警都写入事件日志，可以用来唤醒厂长。

---

## 7. 验证体系

### 7.1 总览

验证分为六层，覆盖“写下结构 → 修改结构 → 测试结构 → 运行 → 长期运行”的全过程。除可选、抽样、只读的 judge 外，每一层都是确定性的。

| 层 | 验证内容 | 时机 | 执行者 | 失败后果 |
|---|---|---|---|---|
| **V1 规格校验** | Spec 的 schema；引用存在且未退役；版本与 digest 已固定；策略 lint | 每次 propose | 变更流水线 | 拒绝变更 |
| **V2 结构编译** | 边类型兼容（含自动转换解析）、消费者契约、无环、必填输入齐全、终端物料有去处、权限、可行性、预算、动态构造上限 | 每次 propose；每次运行前；运行期生成的 plan | 编译器 | 拒绝变更 / 拒绝运行 / 退回 plan / 生成转换需求 |
| **V3 演进校验** | 与上一版本的兼容性、所有下游的消费者契约、影响面分析、弃用与引用规则 | 每次 propose | 变更流水线 | 拒绝，或要求配上迁移 converter |
| **V4 契约测试** | 机器 conformance、工位黄金样例、converter 的 equivalence 与差分测试、Sink dry-run、验证器正反样例 | propose 时；定时 canary | 变更流水线 / 定时任务（在沙箱内运行） | 拒绝变更 / 漂移告警 |
| **V5 运行时检验** | 来料检验、出厂检验、回执核验 | 每个 job | 运行层 | 退回 / 重试 / 死信 |
| **V6 持续校验** | 不变量审计、漂移监测、CAS 与血缘完整性、声明态与实际态对账 | 周期性运行；事件触发 | 审计器、监测器、调和器 | 隔离 / 熔断 / 回滚 / 唤醒厂长 |

验证结果本身也是 Bean（`report.verification@1`），挂在对应的 ChangeSet 或 job 上，可以审计和追溯。

### 7.2 V1 规格校验

- 每个 Spec 都符合其 kind 的 JSON Schema；
- 所有引用（如 `machine: openmontage@7`、`type: video.mp4@1`）在目标 epoch 中存在，且不是 `retired`；
- 镜像和 operator 卷必须以 digest 引用，上游源码必须以 commit 引用；
- Spec 中不能出现密钥的值，只能引用密钥名；
- 策略 lint：例如，非 Sink 工位不能引用 `publish` 档案；模型必须在 runtime 的 `models_allowed` 范围内。

### 7.3 V2 结构编译

编译器把 Line（以及运行期生成的 plan）当作程序来检查：

| 错误码 | 含义 |
|---|---|
| `E-SCHEMA` | Spec 不符合 schema |
| `E-REF` | 引用不存在，或已退役 |
| `E-UNPINNED` | 版本、镜像或源码没有固定 |
| `E-TYPE-MISMATCH` | 边两端的类型不兼容，而且这条边禁止自动转换（`via: none`） |
| `E-NO-CONVERTER` | 边两端的类型不兼容，转换库中也没有满足条件的路径；自动生成转换需求，唤醒厂长（见 9.5） |
| `E-CONTRACT` | 下游的 `requires` 没有被覆盖。覆盖来源是上游的 `guarantees` 和类型属性；如果经过转换，还包括转换后传播下来的属性 |
| `E-UNBOUND` | 必填输入没有连接 |
| `E-CYCLE` | 存在环 |
| `E-ORPHAN` | 终端物料没有去处：既不被 Sink 消费，也没有标记 `keep` |
| `E-PERMISSION` | 非 Sink 申请 publish 或发布凭据，或非 Source 申请 ingress |
| `E-INFEASIBLE` | probe 显示所需能力不可用，例如缺少某个 provider 的 key |
| `E-BUDGET` | 各工位预算上限之和超过了产线预算 |
| `E-BOUNDS` | 动态构造超出上限（展开数、深度、步骤数） |
| `E-INVARIANT` | 违反宪法不变量 |

类型不兼容时，编译器按转换库自动解析，并插入转换步骤（见第 9 章），检查报告中会列出这些自动插入的步骤。如果找不到可用的转换，就生成转换需求。下面是对一个草稿的检查输出：

```text
$ factory check lines/explainer-multilang.yaml
ℹ 自动转换        steps.dub_en.in.subtitle
    explainer.subtitle : subtitle.vtt@1  →  需要 subtitle.srt@1
    命中转换库：插入 [subs-convert@1]（无损）
✖ E-NO-CONVERTER  steps.to_archive.in.doc
    explainer.script : doc.script@1  →  需要 doc.pdf@1
    转换库中没有可用路径 → 已生成转换需求 cr-0031，已唤醒厂长
✖ E-CONTRACT      steps.to_shorts.in.video
    publish-shorts@1 要求 {vertical, hd, has_audio, not_silent}
    vertical@2 保证      {vertical, hd, h264}
    缺失：has_audio、not_silent
✔ 无环 · 去处完整 · 版本已固定 · 权限 · 预算 8.0 / 12.0 USD
```

### 7.4 V3 演进校验

对 ChangeSet 中每个被修改的 Spec，与它在基线 epoch 中的上一个版本比较：

1. **Type 变更**：按 8.3 的规则判断是否兼容。不兼容的变更必须作为新的 major，并配上迁移 converter。
2. **工位端口变更**：
   - 输出端口的类型变了 → 检查所有下游边，包括所有 active Line 中的边，以及所有 planner 的 `allowed_stations`；
   - 删除了某个 guarantee → 找出所有依赖它的下游 `requires`；
   - 新增了 requires，或收窄了输入类型 → 检查所有上游是否满足；
   - 受影响的边按转换库重新解析。如果解析结果变了（例如改用了另一条转换链），列入影响面分析。
3. **重新编译**：所有受影响的 Line（包括通过 `subline` 间接引用的）重新跑一遍 V2。
4. **影响面分析**：列出受影响的 Line；正在运行的 run（因为绑定的是旧 epoch，不受影响，但也会列出）；缓存失效范围；对外发布行为可能改变的 Sink。
5. **引用与弃用**：被引用的版本不能退役。引用计数同时统计当前 epoch 和所有仍被运行绑定的 epoch。

### 7.5 V4 契约测试

| 测试 | 对象 | 内容 |
|---|---|---|
| conformance | Machine | 优先复用产品自带的测试，例如零 key 演示、契约测试；检查收货规则能取到正确类型的产物 |
| 黄金样例 | Station | 用 `GoldenSet` 中的输入运行工位；产物必须通过出厂检验，且指标落在 `Baseline` 的容差内 |
| equivalence 与差分测试 | Converter | 检查转换前后 equivalence 是否成立，例如字幕条数不变。新版本还要和旧版本在样例与历史输入上做差分对比（见 9.6） |
| dry-run | Sink | 对测试目标（测试桶、测试仓库）执行一次 dry-run 和一次真实提交 |
| 正反样例 | Validator | 必须接受全部正样例，拒绝全部反样例 |

成本控制：

- 每个 GoldenSet 都声明测试预算；
- 优先使用零 key、低成本的 Recipe；
- 昂贵的测试只在 T2 及以上风险等级的变更中运行。

**定时 canary**：每个 active 工位都会定期在当前 epoch 上跑一个黄金样例。即使我们自己的版本全部固定，外部 provider（模型 API、TTS、素材库）也可能悄悄变化，canary 就是用来发现这类版本固定也挡不住的漂移。

### 7.6 V5 运行时检验

- **来料检验**：job 开始前，确认每个输入 Bean 都已 sealed、类型兼容，并且满足本工位的 `requires`（用 CEL 实际计算）。Source 摄入的外部物料同样要过检验。
- **出厂检验**：见 6.7。
- **回执核验**：Sink 提交后，核实结果确实存在，例如 S3 对象存在且 hash 与 Bean 一致、Git 提交可见、发布页面可以访问。核验结果写入回执记录。

### 7.7 V6 持续校验

- **不变量审计**：周期性检查 12.3 中的全部不变量，例如抽样重算 CAS hash、扫描血缘是否闭合、扫描悬空引用。
- **漂移监测**：见 8.7。
- **调和**：比较声明态（epoch）与实际态（镜像、实例、网络策略、凭据绑定、部署指针），发现不一致时自动恢复（见 11.9）。

### 7.8 验证器本身也要被验证

验证器写错，就等于质检门失效。因此：

- 每个 validator 都带**正样例**（必须接受）和**反样例**（必须拒绝）。例如 `video.mp4@1` 的反样例包括截断的文件、零长度的文件、改了扩展名的 PNG。
- 反样例必须始终被拒绝，而且**反样例集合只增不减**；删除反样例属于宪法级操作，只有人能做（不变量 I8）。因此，任何让已知坏样例通过的修改都会被直接拒绝。
- 修改 validator、guarantees、accept、judge 阈值或转换的 equivalence 时，流水线会检查标准是否变宽松。删除 guarantee、放宽验收或放宽 equivalence 都判定为**放水**，风险等级至少为 T3。
- 黄金样例集和指标基线同样是版本化的 Spec。修改它们至少是 T2，并且需要交叉复核，以防有人通过改基线来掩盖漂移。放宽基线容差属于 T3，单次放宽的幅度受宪法限制。

---

## 8. 契约治理：防止契约偏移

### 8.1 四类偏移

| 偏移类型 | 例子 | 检测 / 预防 | 处置 |
|---|---|---|---|
| **规格偏移** | 有人修改了 Type 或端口定义 | V1–V3：兼容规则 + 消费者契约 | 拒绝变更，或走 expand → migrate → contract 流程 |
| **行为偏移** | 产品升级、换模型、改提示词后，产出变了 | 全部版本固定；升级走 V4 + 影子运行对比；定时 canary | 告警厂长；回滚 |
| **语义偏移** | 产物仍能通过 schema，但质量或含义在漂移 | 验收标准 + judge 抽样 + 指标基线 | 熔断；唤醒厂长调优 |
| **配置 / 结构偏移** | 有人在流水线之外修改了容器或凭据绑定；出现悬空引用、孤儿 Bean | 调和器 + 不变量审计 | 自动恢复到声明态，或隔离 |

### 8.2 基于属性的消费者契约

类型只能说明“这是一个 mp4”，但下游往往依赖更具体的性质，比如“有声音”“是竖屏”。这些依赖必须**显式写出来**，否则上游一改，下游就会悄悄坏掉。

Type 定义**命名属性**，每个属性是一段只引用实测 facts 的 CEL 表达式：

```yaml
# types/video.mp4.yaml（节选）
properties:
  has_audio:  "bean.facts.audio_streams > 0"
  not_silent: "bean.facts.mean_volume_db > -50.0"
  vertical:   "bean.facts.height > bean.facts.width"
  landscape:  "bean.facts.width > bean.facts.height"
  hd:         "(bean.facts.width < bean.facts.height ? bean.facts.width : bean.facts.height) >= 1080"
  h264:       "bean.facts.vcodec == 'h264'"
```

上游的输出端口声明 `guarantees`，由出厂检验强制执行：

```yaml
# stations/vertical.yaml（节选）
outputs:
  video:
    type: video.mp4@1
    guarantees: [vertical, hd, h264, has_audio, not_silent]
```

下游的输入端口声明 `requires`：

```yaml
# stations/publish-shorts.yaml（节选）
inputs:
  video:
    type: video.mp4@1
    requires: [vertical, hd, has_audio, not_silent]
```

三个阶段各自这样检查：

- **编译期**：检查 `requires ⊆ guarantees ∪ 类型恒真属性`。这是简单的集合包含判断，结果确定，而且可以解释。
- **运行期**：来料检验时用 CEL 实际计算 `requires`，出厂检验时实际计算 `guarantees`。
- **演进期**：上游一旦删掉某个 guarantee，V3 能精确列出所有依赖它的下游。

在同一个 major 内，属性定义不可修改，因为改了含义就等于另一个属性。要改语义，就新增一个属性名。

### 8.3 版本与兼容规则

Type 的版本号采用 `major.minor`：

| 变更 | 能否在同一个 major 内只升 minor |
|---|---|
| 新增可选 meta 字段（有默认值） | ✔ |
| 新增命名属性 | ✔ |
| 新增可选的 payload 文件 | ✔ |
| 新增更严格的 validator | ✔（只影响生产者，已封存的 Bean 不受影响），但会标记提醒 |
| 删除或重命名字段；修改字段类型 | ✘ → 必须升 major |
| 收窄或放宽枚举 | ✘ → 必须升 major |
| 删除命名属性；修改属性定义 | ✘ → 必须升 major（或者新增一个属性名） |
| 修改必需的 payload | ✘ → 必须升 major |

也就是说，minor 版本之间必须**双向兼容**，其他任何改动都必须升 major。不同 major 是不同的类型，可以同时存在。

### 8.4 Expand → Migrate → Contract

以 `video.mp4@1` 升级到 `video.mp4@2` 为例：

1. **Expand**：新增 `video.mp4@2` 和迁移 converter `video-v1-to-v2`（最好是双向的），旧版本保持不变。
2. **Migrate**：逐个把生产者和消费者切换到 `@2`。迁移 converter 已经在转换库中，过渡期内编译器会在 `@1` 与 `@2` 的交界处自动插入它（见第 9 章），所以生产者和消费者可以各自按自己的节奏迁移。
3. **Contract**：当 `@1` 的引用计数归零，即当前 epoch 和所有仍被运行绑定的 epoch 都不再引用它时，把它标记为 `deprecated`，最终退役（`retired`）。

每一步都是一个独立的 ChangeSet，可以单独验证、发布和回滚。

### 8.5 弃用生命周期与引用计数

- 生命周期为 `active → deprecated → retired`：
  - `deprecated` 仍然可以使用，但编译器会给出警告，审计器会把它列入弃用积压；
  - `retired` 不能再被新的结构引用。
- 只要还被引用，就不能退役（不变量 I5）。
- Bean 按保留策略回收 payload，但清单和来源证明会作为墓碑保留，保证血缘闭合（不变量 I2）。

### 8.6 黄金样例集与指标基线

```yaml
apiVersion: mao/v1
kind: GoldenSet
metadata: {name: explainer-goldens, version: 4}
spec:
  station: explainer
  cases:
    - {name: sky-blue-60s,   inputs: {brief: goldens/sky-blue.json}}
    - {name: black-hole-45s, inputs: {brief: goldens/black-hole.json}}
  budget_usd: 6.0
---
apiVersion: mao/v1
kind: Baseline
metadata: {name: explainer-baseline, version: 7}
spec:
  station: explainer@3
  window: 30d
  metrics:
    gate_reject_rate: {mean: 0.12, max: 0.25}
    judge_score:      {p50: 0.81, min_p50: 0.72}
    duration_error:   {p90: 0.06, max_p90: 0.10}
    cost_usd:         {p50: 2.4,  max_p50: 3.5}
```

### 8.7 行为与语义漂移监测

- 对每个工位，按滑动窗口统计以下指标：退回率、judge 分数、facts 分布（时长、响度、分辨率）、成本、耗时、重试次数。
- 与 Baseline 比较：比率型指标用阈值判断；分布型指标用分布距离判断，例如 PSI 超过 0.2 就告警。
- 告警写入事件日志。情况轻微时只通知厂长；情况严重时，质量控制器直接熔断该工位（见 10.6）。

---

## 9. 转换体系：让产物自动成为物料

上游的产物和下游需要的物料，类型经常对不上。例如：字幕是 VTT 而下游要 SRT；视频是 WebM 而发布平台要 MP4；脚本是 JSON 而归档要 Markdown。转换体系负责把它们自动接上，规则是：

- **不兼容时，由厂长编写转换**；
- **写过的转换，之后命中就直接触发**：不唤醒厂长，不需要 ChangeSet，也不用修改产线；
- **厂长持续优化这些转换**。

### 9.1 原则

| 原则 | 含义 |
|---|---|
| 转换是一等公民 | 转换是 `role: converter` 的工位，收录在**转换库**中，和其他 Spec 一样版本化、验证、发布 |
| 隐式插入 | 产线只写业务步骤；遇到类型不兼容的边，编译器按转换库自动补上转换步骤，类似编程语言里的隐式类型转换 |
| 解析确定 | 同一个 epoch 内，同样的不兼容总是解析到同样的转换链；run 绑定 epoch，所以结果可以复现 |
| 两级命中 | 规则级：这对类型之间已经有转换，直接触发；实例级：这个 Bean 已经转换过，直接复用结果 |
| 缺失即工单 | 找不到可用的转换时，自动生成转换需求，唤醒厂长编写 |
| 先能用，再变好 | 第一版转换可以由 agent 驱动，比较灵活；厂长观察一段时间后，再把它固化为脚本，更便宜也更稳定 |

### 9.2 转换（converter）的定义

converter 是一种特殊的工位：只有一个输入 `in` 和一个输出 `out`，没有对外副作用，并且必须声明转换前后要保持的关系。

```yaml
apiVersion: mao/v1
kind: Station
metadata: {name: subs-convert, version: 1}
spec:
  role: converter
  converts:
    from: subtitle.vtt@1
    to: subtitle.srt@1
    lossless: true
    preserves: []                     # 输入满足、输出也一定满足的属性
  machine: ffmpeg@3
  recipe: subs-convert
  operator: {runtime: script@1}
  inputs:  {in:  {type: subtitle.vtt@1}}
  outputs: {out: {type: subtitle.srt@1}}
  equivalence:                        # 转换前后必须成立的关系，由出厂检验强制执行
    - "out.facts.cue_count == in.facts.cue_count"
    - "out.facts.last_cue_end_s == in.facts.last_cue_end_s"
  cost: {usd_est: 0, p50_s: 2}        # 用于路径排序
  fixtures:
    accept: [fixtures/basic.vtt, fixtures/multiline.vtt, fixtures/styled.vtt]
    reject: [fixtures/broken-timestamps.vtt]       # 对这类输入必须报错，而不是产出错误的结果
  policy: tool-default@1
```

- `preserves` 列出“输入满足、输出也一定满足”的属性，属性名必须在两个类型上都有定义：可以各自定义同名属性，也可以从共同的父类型继承。例如视频转码 `video.webm@1 → video.mp4@1` 可以声明 `preserves: [has_audio, vertical, landscape]`。
- 约束：
  - 只能有一个输入和一个输出；
  - 出网只能是 `none` 或 `providers`，不能持有发布凭据；
  - 必须有 equivalence 和正样例；
  - 优先由脚本驱动；需要理解内容结构时（例如某些文档转换），也可以由 agent 驱动。

### 9.3 转换库与解析

**转换库**是当前 epoch 中所有 active converter 构成的有向图：节点是 Type，边是 converter。

**解析规则**是确定性的。对一条边，设上游输出类型为 U、上游保证的属性为 G；下游输入类型为 D、下游要求的属性为 R：

1. 如果 `U <: D` 且 `R ⊆ G`，直接连接，不需要转换。
2. 否则，在转换图上找一条路径，满足两个条件：
   - 路径终点的类型是 D 的子类型；
   - 属性沿路径传播后满足 R。每经过一个转换，属性集合变为 `该转换的 guarantees ∪ (进入时的属性 ∩ 该转换的 preserves)`。
3. 有多条路径时，按以下顺序选出唯一的一条：无损优先 → 步数最少 → 估计成本最低 → 估计耗时最短 → 名称字典序。
4. 找不到路径时报 `E-NO-CONVERTER`，并生成转换需求（见 9.5）。

**转换解析表**：

- 每个 epoch 构建时，编译器为所有 active Line 中的边预先计算解析结果，写入该 epoch 的转换解析表，格式为 `(U, G, D, R) → [converter@版本, …]`。
- 运行期遇到新的组合（例如 plan 中的边，或运行期才确定的子类型），按同样的规则解析，再把结果追加到表中。解析是纯函数，追加不会改变已有的结果。
- run 只按解析表插入转换（不变量 I14），所以同一个 epoch 内的行为可以复现。

**隐式步骤**：

- 编译器把转换步骤插入编译后的执行计划，产线的源文件保持不变。
- 隐式步骤有固定的命名，例如 `dub_en.subtitle~subs-convert`。
- 隐式步骤在血缘中可见：来源证明中标注 `implicit: true`，并记录对应的解析表条目。

**显式控制**：产线可以在某条边上覆盖默认行为。

```yaml
    to_archive:
      station: publish-archive@1
      in:
        doc: {from: explainer.script, via: [script-to-md@2]}   # 显式指定转换链
    to_legal:
      station: publish-legal@1
      in:
        doc: {from: explainer.script, via: none}                # 禁止自动转换，要求类型严格匹配
```

### 9.4 两级命中

| 级别 | 命中条件 | 效果 |
|---|---|---|
| 规则级 | 解析表中已经有 `(U, G, D, R)` 对应的转换链，也就是“写过的转换” | 直接插入并执行，不唤醒厂长，不需要 ChangeSet，也不用修改产线 |
| 实例级 | 转换 job 的缓存键 `H(converter 版本, 输入 bean_id, 参数)` 已经有 sealed 结果 | 直接复用已经转换好的 Bean，不再执行 |

举例：某个产品输出 VTT 字幕，三条产线的下游都需要 SRT。厂长只在第一次遇到时编写 `subs-convert@1`。之后这三条产线，以及以后新建的、遇到同样不兼容的任何产线，都会自动使用它。同一份 VTT 字幕即使被多条产线使用，也只会转换一次。

### 9.5 缺失转换：由厂长编写

```
编译 / 运行前检查发现某条边找不到转换路径
  → E-NO-CONVERTER：生成转换需求（相同的需求自动合并）
  → 唤醒厂长
  → 厂长编写 converter：优先写成脚本，必要时由 agent 驱动，必要时接入新的工具机器；
     同时编写 equivalence 和正反样例
  → 提交 ChangeSet（新增转换，且不改变任何现有边的解析结果 → T1）
  → V1–V4（包括 equivalence 测试）→ 影子运行 → 生效（新的 epoch）
  → 被阻塞的 run 以新 epoch 自动重建；已经完成的工位命中缓存，不重复执行，也不重复花钱
```

转换需求本身也是带类型的 Bean（`request.conversion@1`）：

```yaml
apiVersion: mao/v1
kind: ConversionRequest
metadata: {name: cr-0012}
spec:
  from: {type: subtitle.vtt@1, guarantees: []}
  to:   {type: subtitle.srt@1, requires: []}
  found_at:
    - {changeset: cs-0142, edge: "explainer@4.subtitle → dub@3.subtitle"}   # 在哪里发现（产线、run 或 ChangeSet 草稿）
  samples: [sha256:5a1c…, sha256:e07b…]      # 实际遇到的输入 Bean，用作编写和测试的样例
  blocked_runs: []
  status: open                                # open → in_progress → resolved（关联到对应的 ChangeSet）
```

**转换写在哪里**：

- 如果现有的工具机器已经够用，就在它的 Machine Package 里增加一个转换配方，例如在 ffmpeg 里增加 `subs-convert`。
- 否则写成脚本，放在 Registry 的 `converters/<name>/` 目录，由通用的转换工具箱机器 `convkit` 执行。
  - `convkit` 是一个装好常用转换库的工具型镜像。
  - 脚本以只读方式挂载进去，作为 epoch 的一部分参与内容寻址，所以修改脚本不需要重新构建镜像。

### 9.6 厂长优化转换

**触发信号**（来自转换统计和漂移监测）：

- 命中很频繁、但由 agent 驱动的转换：成本高、结果不稳定，适合固化；
- 高频的多跳转换链（例如 A → B → C）：适合合并成直连；
- 耗时或成本偏高、出现 equivalence 违例、下游退回率与某个转换相关；
- 长期没有命中的转换：适合退役。

**优化方式**：

| 方式 | 说明 |
|---|---|
| 固化 | 把 agent 驱动的转换改写为确定性脚本，成本和不确定性都会大幅下降 |
| 调优 | 调整参数，在质量与速度之间取得平衡 |
| 合并 | 用直连转换替换高频的多跳链，例如用 `webm → mp4` 替换 `webm → mkv → mp4` |
| 修复 | 针对失败的样例修正，并把这些样例加入正样例或反样例 |
| 退役 | 解析表中已不再引用的转换，走 deprecated → retired |

**差分测试**：转换会被全局自动触发，影响面是所有解析到它的边。所以优化后的新版本必须先和旧版本做比较：

1. 在正样例，以及从 Warehouse 抽样的历史真实输入（旧版本处理过的 Bean）上，同时运行新旧两个版本；
2. 新版本的输出必须通过目标类型的检验，并满足 equivalence 和 guarantees；
3. 与旧版本的输出比较：关键 facts 一致或在容差之内；不允许出现“旧版本通过、新版本失败”的样例；
4. 之后照常走影子运行、金丝雀和观察期。

**风险分级**：

| 变更 | 等级 |
|---|---|
| 新增转换，且不改变任何现有边的解析结果 | T1 |
| 替换转换版本；或者新转换改变了现有边的解析结果（例如出现了更优的路径） | T2：差分测试 + 金丝雀 + 交叉复核 |
| 放宽 equivalence；让有损转换进入原本无损的路径 | T3 |

转换升级后会产生新的缓存键，新版本不会复用旧版本的转换结果；旧结果作为历史记录保留。

### 9.7 转换的边界

- 转换只做**同一内容在不同类型之间的表达变换**，包括格式、编码、结构和版本迁移，并且必须有 equivalence 约束。
- **改变内容含义的加工不是转换**，例如翻译、剪辑、配音、摘要。它们必须作为普通工位，显式写进产线。
- 转换没有对外副作用：不能是 Sink，也不能持有发布凭据。
- 产线可以对某条边禁止自动转换（`via: none`）。
- 类型大版本之间的迁移转换（例如 `video.mp4@1 → @2`）也是转换，同样会被自动触发，所以 expand → migrate → contract 的过渡期几乎没有额外成本（见 8.4）。

---

## 10. 动态结构与稳定性

### 10.1 epoch 与快照隔离

- Registry 中由变更流水线合入的每个 commit 就是一个 **epoch**。
- 控制面维护三个指针：
  - `active`：新 run 默认绑定的 epoch；
  - `candidate`：影子运行和金丝雀使用的 epoch；
  - `previous`：用于快速回滚。
- 每个 run 在创建时绑定一个 epoch。run 内的所有引用（包括运行期生成的 plan）都在这个 epoch 内解析。
- 多个 epoch 可以同时存在：旧 run 在旧 epoch 上跑完，金丝雀 run 在候选 epoch 上跑。
- 回滚就是把 `active` 指针移回 `previous`。
- 只要还有 run 绑定某个 epoch，或者它仍在回滚窗口内，这个 epoch 的可部署产物（镜像、operator 卷）就不会被回收（不变量 I12）。

### 10.2 受限的动态构造

运行期的动态行为只能通过以下几种构造表达。它们都带类型，并且都有上限：

```yaml
steps:
  # map：对列表展开，输出自动聚合为 list<…>
  dubs:
    map: {over: [en, ja], as: lang, max_parallel: 2}     # 也可以对一个 list<T> 类型的 Bean 展开
    station: dub@2
    params: {target_language: "{{ lang }}"}
    in: {video: explainer.video, subtitle: explainer.subtitle}

  # switch：按 CEL 条件分支，必须穷尽所有情况并提供 default
  route:
    switch:
      on: explainer.video
      cases:
        - {when: "bean.facts.duration_s <= 60.0", goto: to_shorts}
      default: trim

  # subline：嵌套调用另一条声明了 interface 的产线
  subs:
    subline: subtitle-pipeline@2
    in: {video: explainer.video}

  # until：有最大次数的迭代，运行时展开为串行的 job
  refine:
    until: {cond: "out.gate.judge_score >= 0.85", max_iter: 3}
    station: explainer@3
    in: {brief: $inputs.brief}
```

编译器会检查每种构造：

- `map`：展开数量不超过上限。
- `switch`：分支穷尽，且各分支目标的类型兼容。
- `subline`：接口类型匹配。
- `until`：设置了最大迭代次数。

如果 `map` 的输入是运行期才知道长度的列表，上限就在运行期检查，超出时按 `overflow` 的设置处理（`reject` 或 `truncate`）。

### 10.3 plan：运行期生成的结构

有些结构只有看到物料后才能决定，例如“这份 brief 需要配几种语言、要不要切竖版”。这时由 **planner 工位**（agent）产出一个 `spec.plan@1` 类型的 Bean，描述一个子 DAG：

```
planner 工位（agent）──产出──▶ spec.plan@1（候选）
      ▲                              │
      │ 编译错误作为反馈              ▼
      └──── REJECTED ◀──── 出厂检验 = V1 + V2 + plan_bounds 策略检查
                                     │ 通过
                                     ▼
              封存 plan Bean → 调度器把子 DAG 挂到当前 run 上（同一个 epoch）
```

约束如下：

- plan 只能使用 `plan_bounds.allowed_stations` 中列出、且在当前 epoch 中存在的工位，**不能定义新的 Spec**；
- 步骤数、展开数、嵌套深度和预算都有上限，预算不能超过 run 的剩余预算；
- plan 只能使用允许的 Sink；
- plan 中的边同样按转换库自动解析。缺少转换时，plan 被退回，planner 可以换一种方案；同时生成转换需求；
- plan Bean 会进入血缘，可以追溯哪些 job 是由哪个 plan 产生的。

这样，结构可以由 agent 在运行期动态生成，但它与人写的 Line 经过**完全相同**的编译和策略检查。

### 10.4 结构变更的分阶段发布

| 阶段 | 做什么 | 通过条件 | 不通过时 |
|---|---|---|---|
| verified | V1–V4 全部通过 | — | 退回厂长 |
| shadow（影子运行） | 用候选 epoch 重放最近 N 个真实输入（或黄金样例）。**Sink 被替换为空 Sink**：只执行 dry-run，记录本来会发布什么。然后与 active epoch 的结果对比 | 类型与检验结果没有退化；指标在基线容差内；judge 对比不比现状差 | 退回厂长 |
| canary（金丝雀） | 让一定比例的新 run 绑定候选 epoch，真实发布 | 满足 SLO，且样本数达到 `min_runs` | 自动回滚 |
| active | 切换 `active` 指针，保留 `previous` | 观察期内满足 SLO | 自动回滚 |

影子运行也要花钱，所以 ChangeSet 必须声明影子运行的预算。T1 级变更可以只用黄金样例做影子运行。

### 10.5 稳定性控制

| 机制 | 作用对象 | 触发条件 | 动作 |
|---|---|---|---|
| 熔断器 | 机器、provider、工位 | 失败率或退回率超过阈值 | 打开时暂停调度该对象；半开时放少量 job 试探 |
| 背压（在制品上限） | 工位的输入队列 | 队列长度达到上限 | 上游工位暂停调度；产线入口限流 |
| 隔舱 | 资源池（GPU、provider 配额） | — | 按产线或优先级划分，互不挤占 |
| 分层预算 | 全局 → 产线 → run → 工位 → job | 剩余预算不足 | 拒绝调度、申请审批或终止 |
| 有毒物料隔离 | Bean | 同一个 Bean 导致下游连续失败 k 次 | 隔离（quarantine），移出所有队列，通知厂长 |
| 变更治理 | ChangeSet | 并发发布数、变更速率、封版窗口、单次变更规模超限 | 排队、拒绝，或要求拆分 |
| 速率限制 | 共享的 API key | 接近 provider 的限额 | 用令牌桶排队 |

### 10.6 自稳控制回路

确定性的控制器围绕设定值工作：能机械调节的自己处理，处理不了的升级给厂长。

| 控制器 | 观测信号 | 设定值 | 自动动作 | 何时升级给厂长 |
|---|---|---|---|---|
| 队列控制器 | 队列长度、等待时间 | 在制品上限 | 背压、调整并发 | 积压持续超过阈值 |
| 质量控制器 | 退回率、judge 分数分布 | 基线 ± 容差 | 熔断工位 | 熔断器打开 |
| 成本控制器 | 每个 run / job 的花费 | 预算 | 拒绝、终止 | 连续超支 |
| 发布控制器 | 金丝雀与观察期的 SLO | SLO | 推广或回滚 | 发生回滚 |
| 调和器 | 声明态与实际态 | 两者一致 | 恢复声明态 | 无法自动恢复 |
| 审计器 | 不变量 | 全部满足 | 隔离、冻结相关对象 | 任意一条被违反 |

---

## 11. 治理与 Foreman（厂长）

### 11.1 三层权限

| 层 | 内容 | 谁能改 | 怎么改 |
|---|---|---|---|
| **L0 宪法** | 不变量（12.3）、风险分级规则、审批矩阵、出网分级定义、厂长权限 | 只有人 | Registry 中受保护的 `constitution/` 目录，需要人的签名提交 |
| **L1 结构** | Type、Machine、OperatorRuntime、Station（包括 converter）、Line、Policy、GoldenSet、Baseline | 厂长（人也可以提交，但没有任何特权） | 只能通过 ChangeSet 流水线 |
| **L2 运行** | 启动或取消 run、重试 job、隔离 Bean、委托范围内的审批 | 厂长、自动化组件 | 通过控制面 API，全程审计 |

风险分级规则本身属于 L0，所以厂长无法改变“自己的变更如何被评级”。

### 11.2 ChangeSet

ChangeSet 是修改结构的唯一途径：

```yaml
apiVersion: mao/v1
kind: ChangeSet
metadata:
  name: cs-0142
  proposer: "foreman (claude-code@4 / claude-opus-5-5)"
  trigger: "upgrade-machine/r-0057"
spec:
  base_epoch: 9c1e7a2
  intent: "…"
  changes: [ … ]          # 每一项：op（upsert / deprecate / retire）+ kind + name + from/to 版本 + diff
  rollout:
    shadow: {runs: 3, budget_usd: 15}
    canary: {percent: 20, min_runs: 5}
    observe: 24h
    slo: {success_rate: ">= 0.9", gate_reject_rate: "<= 0.2", cost_per_run_usd: "<= 10"}
```

生命周期为 `draft → verifying → verified → shadow → canary → active`，任何阶段都可能变成 `rejected` 或 `rolled-back`。每次状态变化都附带验证报告或指标快照，全程可审计。完整示例见 13.2。

### 11.3 风险分级

风险等级由流水线**根据 diff 确定性地计算**，取 ChangeSet 中所有变更的最高等级。厂长不能自己声明等级，也不能降低等级。

| 等级 | 包含的变更 | 要求 |
|---|---|---|
| **T0** | 运行操作：重试、取消、隔离 | 自动执行 |
| **T1** | 调优任务模板或 FACTORY_NOTES；新增转换（不改变任何现有边的解析结果）；Type 升 minor；在策略范围内调整工位限额 | V1–V4 + 影子运行，通过后自动推广 |
| **T2** | 接入或升级机器（镜像或 commit 变化）；更换模型或 runtime；新建产线；Type 升 major；替换转换版本，或改变现有边的转换解析；修改 GoldenSet / Baseline | 在 T1 的基础上加金丝雀，并由**另一个 runtime 做交叉复核**（例如由 Codex 复核 Claude Code 提出的 ChangeSet） |
| **T3** | 新增或修改 Source / Sink；新的出网或凭据绑定；修改 Policy；退役任何 Spec；任何“放水”（见 7.8） | 在 T2 的基础上，要求**至少两个不同 runtime** 的交叉复核一致通过，并延长金丝雀和观察期 |
| **T4** | 修改宪法 | 只能由人完成；系统内的任何 agent 都无法发起 |

另外，单个 ChangeSet 涉及的 Spec 数量有上限。超过上限时，会自动提升等级或要求拆分。

### 11.4 完整自治与安全模式

本设计采用**完整自治**：运行和维护的任何环节都不需要人工审批，人只做一件事，就是修改宪法。自治之所以安全，靠的是下面这些确定性的兜底，而不是靠人盯着：

1. 每次变更都会强制检查宪法不变量，而厂长无法修改宪法；
2. 风险等级由流水线计算，高风险变更需要多个不同 runtime 的交叉复核一致通过；
3. 影子运行、金丝雀、观察期和自动回滚；
4. **安全模式**：
   - 触发条件：任意一条不变量被违反；观察窗口内自动回滚的次数超过阈值；成本或失败率出现异常。满足任意一条，系统就自动进入安全模式。
   - 进入安全模式后：`active` 固定在最近一个健康的 epoch；暂停 T2 及以上的结构变更；厂长只能做 T0/T1 操作和诊断。
   - 退出：触发条件消除，并经过一段冷却期后自动退出；反复进入时，冷却期按指数延长。
   - 上述各项阈值都写在宪法里。
5. 人会收到通知，但通知不会阻塞任何流程；需要时，人通过修改宪法来调整规则。

**L0 宪法始终不对厂长开放**，这是结构不被破坏的最终锚点。

### 11.5 厂长的形态与工具

厂长是一个 Claude Code 或 Codex 实例：

- 工作目录是 Registry Git 仓库的一个工作副本。厂长只能在分支上修改，不能直接合入主干。
- 通过 `factory` 控制面的 MCP 工具或 CLI 观察和操作系统。
- 由事件唤醒，以无头方式处理问题。人可以随时查看状态、审计记录和通知，但系统的运行不依赖人。

Registry 仓库结构：

```
registry/
├── constitution/        # L0：不变量、风险分级规则、审批矩阵、厂长权限（受保护，需要人的签名）
├── types/               # Type Spec、validator、正反样例
├── machines/<name>/     # Machine Package
├── runtimes/            # OperatorRuntime
├── stations/<name>/     # station.yaml、rubric.md
├── converters/<name>/   # 厂长编写的转换：station.yaml、转换脚本、equivalence、正反样例
├── lines/               # Line
├── policies/            # 出网档案、凭据绑定（只含密钥名）、预算、审批委托
├── goldens/             # GoldenSet 与样例输入
├── baselines/           # Baseline
└── AGENTS.md            # 厂规：给厂长的工作守则（属于 L1，修改走 ChangeSet）
```

`AGENTS.md`（另有一个 `CLAUDE.md` 指向它）写的是“怎么把事情做好”；宪法规定的是“绝对不能做什么”。即使厂规写错了，宪法和验证流水线仍然有效。

控制面 MCP 工具：

| 类别 | 工具 | 说明 |
|---|---|---|
| 观察 | `registry.get / list / diff`、`epoch.list / show`、`bean.get / lineage`、`job.get / transcript`、`dlq.list`、`drift.list`、`invariant.report`、`metrics.query`、`approval.list` | 只读，不返回密钥 |
| 变更 | `changeset.propose`（返回验证报告和风险等级）、`changeset.status`、`changeset.withdraw` | 不能直接合入 |
| 运行 | `run.start / cancel`、`job.retry`（可以带覆盖参数）、`bean.quarantine / release`、`approval.decide`（仅限委托范围内） | 属于 L2 |
| 转换 | `conversion.resolve(from, to, requires)`、`conversion.requests`、`conversion.stats`、`conversion.diff_test(new@v, old@v)` | 查询解析结果和转换需求，查看命中与成本统计，运行差分测试 |
| 辅助 | `machine.probe(name@v)`、`goldens.run(station@v)` | 在沙箱内执行，计入预算 |

### 11.6 维护循环与信号

```
观察信号 → 诊断（查看 transcript、血缘、差异） → 提出 ChangeSet → 验证（V1–V4） → 发布（影子 / 金丝雀） → 监控
   ▲                                                                                                │
   └────────────────────────────────────────────────────────────────────────────────────────────────┘
```

| 信号 | 来源 | 厂长的典型响应 |
|---|---|---|
| 定时 canary 失败 / 漂移告警 | V4、漂移监测 | 定位原因（产品、provider 还是模板），然后修收货规则、模板或 provider 选择 |
| 退回率升高 | 质量控制器 | 调优 Recipe 模板，或拆分工位；必要时提交修改验收标准的 ChangeSet（放宽属于 T3） |
| 死信 | 状态机 | 诊断后带覆盖参数重试，或修改 Spec 后重跑 |
| 转换需求（`E-NO-CONVERTER`） | 编译器 | 编写 converter（见 9.5） |
| 转换统计：agent 驱动的转换命中频繁、多跳链高频、成本或耗时偏高、equivalence 违例 | 转换统计、漂移监测 | 优化转换：固化、调优、合并、修复（见 9.6） |
| 熔断器打开 | 控制器 | 判断是 provider 故障还是产品问题，然后切换 provider 选择或回滚 |
| 上游发布新版本 | 订阅 GitHub release 的 Source 工位 | 触发 `upgrade-machine` 产线 |
| 弃用积压 | 审计器 | 迁移引用，然后退役（包括长期没有命中的转换） |
| 待审批 | 审批服务 | 在委托范围内做决定；超出范围的交给交叉复核 |
| 不变量被违反 | 审计器 | 系统自动进入安全模式（见 11.4）；厂长调查并提出修复 |

### 11.7 职责清单

**接入新产品**：

1. 阅读 README、AGENT_GUIDE 和部署文档，判断接入形态（A / B / C）。
2. 编写 Dockerfile（上游常常不提供），或采用上游的 compose；固定 commit 和 digest。
3. 编写 `machine.yaml`：
   - 每个 Recipe 的输入输出类型、任务模板或调用方式、收货规则；
   - 所需的 secrets、出网和预算旋钮。
4. 编写 `FACTORY_NOTES.md`：无人值守规则、路径约定、已知问题。
5. 编写 conformance（优先复用产品自带的测试）和 probe。
6. 必要时注册新的 Type（可以直接复用产品自带的 JSON Schema）和 converter。
7. 调用 `changeset.propose`，依次经过 V1–V4、T2 交叉复核、影子运行、金丝雀，最后推广。

**升级产品**：

1. 上游 release 触发 `upgrade-machine` 产线。
2. 在沙箱中构建候选版本，运行 conformance 和黄金样例，与 Baseline 对比。
3. 差异超出容差时，厂长阅读 release notes 和差异，调整收货规则、模板或 converter。
4. 提交 ChangeSet（T2），依次走影子运行、金丝雀和推广。

**其他职责**：

- 编写和优化转换（见第 9 章）；
- 处置漂移；
- 推进 expand → migrate → contract 和退役；
- 调优 Recipe；
- 处理升级到厂长这里的审批。

### 11.8 厂长的边界

| 不能做的事 | 由什么保证 |
|---|---|
| 修改宪法 | `constitution/` 受保护，只接受人的签名提交 |
| 修改已封存的 Bean | CAS 只写一次；厂长没有仓库写权限 |
| 读取密钥 | 控制面工具不返回密钥；密钥只存在于密钥服务和代理中 |
| 绕过验证 | Registry 主干只接受流水线合入 |
| 进入沙箱 | 控制面不提供 exec，厂长只能查看 transcript 和日志 |
| 自定风险等级 | 等级由流水线根据 diff 计算 |
| 删除反样例 | 反样例集合只增不减，删除属于宪法级操作 |
| 一次改太多 | ChangeSet 有规模上限 |

### 11.9 调和器与厂长的分工

| | 调和器（确定性） | 厂长（agent） |
|---|---|---|
| 处理什么 | 机械性的偏移：实际状态偏离了声明 | 需要判断的问题 |
| 例子 | 镜像被手工替换 → 按 digest 重新部署；凭据绑定被改动 → 恢复；预热实例版本不对 → 重建 | 编写适配器、修收货规则、做类型迁移、调提示词、决定接入方案 |
| 方式 | 直接恢复到 epoch 中声明的状态 | 提交 ChangeSet，走验证和发布流程 |

### 11.10 自举：维护产线

维护工作本身也可以建成产线，受同一套秩序约束：

```yaml
apiVersion: mao/v1
kind: Line
metadata: {name: onboard-machine, version: 2}
spec:
  interface:
    inputs: {request: {type: request.onboard@1}}          # {repo, commit, intent}
  steps:
    package:
      station: machine-packager@1     # workspace 机器 + claude-code：编写 Dockerfile、machine.yaml、conformance
      in: {request: $inputs.request}
      # 输出 spec.machine-package@1（目录树形式的 Bean）；出厂检验 = V1 + 镜像构建 + V4 conformance
    propose:
      station: registry-sink@1        # Sink：把接入包作为 ChangeSet 提交到变更流水线（只能提交，不能合入）
      in: {package: package.package}
```

`registry-sink` 是一个 Sink，因为它对 run 之外的世界产生了作用（向 Registry 提交 ChangeSet）。它只持有“提交 ChangeSet”的凭据，没有合入权限。

### 11.11 如何防止厂长犯错

厂长是 LLM，会犯错。系统不依赖厂长“不犯错”，而是保证它犯的错**能被拦下，或者可以回滚**：

1. 所有结构变更都要经过 V1–V4；
2. 风险等级由流水线计算，厂长不能自己降级；
3. T2 及以上的变更需要另一个 runtime 交叉复核；T3 需要至少两个不同 runtime 一致通过；
4. 放水检测：反样例只增不减，并且必须被拒绝；验证器、guarantees、accept、equivalence、基线的任何放宽都会被识别出来；
5. 影子运行和金丝雀能拦住“验证通过、但实际效果变差”的变更，违反 SLO 时自动回滚；转换的新版本还要先过差分测试；
6. 每次变更都会强制检查宪法不变量，而厂长无法修改宪法；
7. ChangeSet 的规模和提交速率都有上限，并且全程审计；
8. 出现异常时，系统自动进入安全模式（见 11.4），冻结高风险变更。

---

## 12. 秩序保障总览

### 12.1 风险与对应机制

| 风险 | 事前预防 | 事中 / 事后检测 | 处置 |
|---|---|---|---|
| 产物不能作为下游的物料 | Type 契约、V2 编译、消费者契约、转换库 | V5 来料检验与出厂检验 | 自动转换；缺少转换时由厂长编写；退回重试；进入死信 |
| 转换引入错误 | equivalence 约束、差分测试、正反样例 | 转换统计、下游退回率 | 回滚转换版本；厂长修复 |
| 规格偏移 | V3 兼容规则、消费者契约 | — | 拒绝变更；走 expand → migrate → contract |
| 行为 / 语义偏移 | 版本固定、影子运行 | 定时 canary、漂移监测 | 熔断、回滚、厂长调优 |
| 结构被破坏 | 宪法不变量准入、引用计数、ChangeSet 流水线 | 不变量审计 | 拒绝变更、回滚 epoch |
| 厂长犯错 | 风险分级不可自降、交叉复核、规模上限、放水检测 | 影子运行与金丝雀的 SLO | 自动回滚、安全模式 |
| agent 越权 | 沙箱、出网分级、凭据最小化、只读输入 | Gateway 与代理的审计 | 终止 job、隔离 |
| 对外副作用失控 | 发布凭据只给 Sink、两阶段执行、审批 | 回执核验 | 按幂等键拒绝重复、熔断该 Sink |
| 成本失控 | 分层预算、两阶段报价、缓存 | 代理计量 | 触及硬上限即终止、熔断 |
| 失败扩散 | 背压、隔舱 | 熔断器 | 隔离有毒物料、进入死信 |
| 配置漂移 | 只按 epoch 部署 | 调和器 | 自动恢复 |
| 不可追溯 | 内容寻址、版本固定 | 血缘、transcript | 在同一 epoch 下用同样的输入重放 |
| 外部物料中的恶意内容 | Source 检验；把物料当作数据处理 | — | 用沙箱限制影响范围；凭据隔离 |

### 12.2 权限矩阵

| 角色 | 能做 | 不能做 |
|---|---|---|
| Operator（agent） | 读本工位的输入；操作本工位的机器；提交候选产物；申请审批 | 访问其他工位和仓库；持有发布凭据；修改 Spec；封存 Bean |
| Operator（脚本） | 执行 Recipe 中声明的命令或 API 调用 | 执行声明之外的任何操作 |
| Machine | 读 `/factory/in`；写工作区和 `/factory/out`；按出网档案访问外网 | 使用 publish 出网（Sink 除外）；访问控制面 |
| Gate / Validator / Judge | 只读访问候选产物；实测 facts；给出结论 | 修改产物 |
| Source | 持有摄入凭据；产出外部物料（同样要过检验） | 持有发布凭据 |
| Sink | 持有发布凭据和 publish 出网；留下回执记录；默认没有产物 | 把回执当作物料交给下游 |
| Converter | 把一种类型的物料转换成另一种类型（单输入单输出，满足 equivalence） | 产生对外副作用；持有发布凭据；改变内容含义 |
| Foreman | 查看所有非机密数据；提交 ChangeSet；执行 L2 运行操作；在委托范围内审批 | 修改宪法、修改已封存的 Bean、读取密钥、绕过验证、进入沙箱、自定风险等级 |
| 调和器 / 控制器 | 恢复声明态；熔断、背压、回滚 | 修改 Spec |
| 人 | 修改宪法；查看审计记录和通知 | 不参与运行和维护中的审批（完整自治）；所有操作同样留痕 |

### 12.3 不变量清单（宪法）

每条不变量都在以下三类位置中的一处或多处强制执行：变更准入时、运行时（由构造方式直接保证）、持续审计。

| 编号 | 不变量 | 准入时（变更） | 运行时（由构造保证） | 持续审计 |
|---|---|---|---|---|
| I1 | 已封存的 Bean 不可变，`bean_id` 与内容一致 | — | CAS 只写一次；只读挂载 | 抽样重算 hash |
| I2 | 血缘闭合：每个 Bean 都有完整的来源证明，它的输入 Bean 都存在（包括墓碑） | — | 封存时写入来源证明 | 血缘扫描 |
| I3 | 只有已封存（sealed）的 Bean 能被引用，即出厂检验不可绕过 | — | 调度器只解析 sealed 引用 | 扫描 job 输入 |
| I4 | 当前 epoch 中所有 active Line 都能编译通过 | V2 编译所有受影响的 Line | 运行前再编译一次 | 每个 epoch 全量编译 |
| I5 | 仍被引用的 Spec 版本不能退役或删除 | V3 引用计数 | — | 悬空引用扫描 |
| I6 | 只有 Sink 能绑定发布凭据和 publish 出网；只有 Source 能绑定 ingress | 策略准入 | 网络和凭据按工位注入 | 运行态对账 |
| I7 | 每个 Sink 都定义了幂等键和回执核验 | V1 / V2 | 提交前查询幂等记录 | 抽查回执 |
| I8 | 每个 validator（以及 converter）都能通过自己的正反样例；反样例集合只增不减 | V4；删除反样例需要宪法级权限 | — | 定期重跑样例 |
| I9 | 实际状态等于 epoch 中声明的状态 | — | 只按 epoch 部署 | 调和器对账 |
| I10 | 每个 run 的实际花费不超过授权预算 | V2 预算检查 | 代理计量 + 硬上限 | 账单对账 |
| I11 | 每个生效的结构变更都有 ChangeSet、验证报告和所需的交叉复核记录 | 变更流水线 | 主干只接受流水线合入 | Git 历史审计 |
| I12 | 仍被 run 绑定的 epoch 不会被回收；每个 run 只能看到自己的 epoch | — | run 绑定 epoch | epoch 回收检查 |
| I13 | 宪法只能由人修改 | 受保护路径 + 签名校验 | — | 宪法目录的历史审计 |
| I14 | 转换解析是确定的：同一个 epoch 内，同样的不兼容总是解析到同样的转换链 | epoch 构建时生成转换解析表 | run 只按解析表插入转换 | 重放解析并比对 |

---

## 13. 示例

### 13.1 生产示例：科普短视频的多语言发布

**物料类型**（节选，完整字段见附录 A.2）：

```yaml
apiVersion: mao/v1
kind: Type
metadata: {name: brief.video, version: "1.2"}
spec:
  extends: brief@1
  payload:
    - {path: brief.json, media_type: application/json, required: true}
    - {path: "refs/*", media_type: "video/*", required: false}       # 可选的参考视频
  meta_schema:
    type: object
    required: [topic, duration_s, aspect, language]
    properties:
      topic:      {type: string, maxLength: 500}
      audience:   {type: string}
      duration_s: {type: number, minimum: 5, maximum: 600}
      aspect:     {enum: ["16:9", "9:16", "1:1"]}
      language:   {type: string}
      style:      {type: string}
  properties:
    has_reference: "bean.payload.exists(f, f.path.startsWith('refs/'))"
---
apiVersion: mao/v1
kind: Type
metadata: {name: video.mp4, version: "1.3"}
spec:
  extends: video@1
  payload:
    - {path: video.mp4, media_type: video/mp4, required: true}
  meta_schema:
    type: object
    properties: {title: {type: string}, language: {type: string}}
  validators:
    - name: ffprobe-facts
      image: registry.local/validators/ffprobe@sha256:c4d2…
      run: [probe-video, /bean/payload/video.mp4]
      emits: [duration_s, width, height, fps, vcodec, audio_streams, mean_volume_db]
      fixtures:
        accept: [fixtures/ok-1920x1080.mp4, fixtures/ok-1080x1920.mp4]
        reject: [fixtures/truncated.mp4, fixtures/zero-length.mp4, fixtures/png-renamed.mp4]
  properties:                       # 见 8.2
    has_audio:  "bean.facts.audio_streams > 0"
    not_silent: "bean.facts.mean_volume_db > -50.0"
    vertical:   "bean.facts.height > bean.facts.width"
    landscape:  "bean.facts.width > bean.facts.height"
    hd:         "(bean.facts.width < bean.facts.height ? bean.facts.width : bean.facts.height) >= 1080"
    h264:       "bean.facts.vcodec == 'h264'"
```

**工位**：

```yaml
apiVersion: mao/v1
kind: Station
metadata: {name: explainer, version: 3}
spec:
  role: transform
  machine: openmontage@7
  recipe: animated-explainer
  operator: {runtime: claude-code@4, model: claude-opus-5-5, max_turns: 200}
  inputs:
    brief: {type: brief.video@1}
  outputs:
    video:
      type: video.mp4@1
      guarantees: [has_audio, not_silent, hd]
      accept:
        - "out.facts.duration_s >= in.brief.meta.duration_s * 0.9"
        - "out.facts.duration_s <= in.brief.meta.duration_s * 1.1"
    subtitle: {type: subtitle.srt@1, optional: false}    # 比 Recipe 更严格：本工位要求必须有字幕
    script:   {type: doc.script@1,   optional: false}
  phases: [propose, execute]
  approval:
    propose: {auto_if: "proposal.cost_estimate_usd <= 3.0", else: foreman}
    in_run:  {auto_if: "request.cost_usd <= 0.5", else: foreman}
  gate:
    judge: {runtime: claude-code@4, model: claude-sonnet-5-5, rubric: rubric.md, sample_rate: 0.2, threshold: 0.7}
  limits: {timeout: 90m, budget_usd: 5.0, retries: {rejected: 2, failed: 1, mode: resume}}
  policy: video-default@2
---
apiVersion: mao/v1
kind: Station
metadata: {name: dub, version: 2}
spec:
  role: transform
  machine: openmontage@7
  recipe: localization-dub
  operator: {runtime: claude-code@4, model: claude-sonnet-5-5, max_turns: 120}
  params: {target_language: {type: string}}
  inputs:
    video:    {type: video.mp4@1, requires: [has_audio]}
    subtitle: {type: subtitle.srt@1}
  outputs:
    video:    {type: video.mp4@1, guarantees: [has_audio, not_silent, hd]}
    subtitle: {type: subtitle.srt@1}
  limits: {timeout: 60m, budget_usd: 3.0, retries: {rejected: 2, failed: 1, mode: resume}}
  policy: video-default@2
---
apiVersion: mao/v1
kind: Station
metadata: {name: vertical, version: 1}
spec:
  role: transform
  machine: ffmpeg@3
  recipe: reframe-vertical
  operator: {runtime: script@1}
  inputs:  {video: {type: video.mp4@1, requires: [hd, has_audio]}}
  outputs: {video: {type: video.mp4@1, guarantees: [vertical, hd, h264, has_audio, not_silent]}}
  limits:  {timeout: 15m, budget_usd: 0}
  policy: tool-default@1
---
apiVersion: mao/v1
kind: Station
metadata: {name: publish-s3, version: 2}
spec:
  role: sink
  machine: rclone@1
  recipe: copy
  operator: {runtime: script@1}
  params: {bucket: {type: string}, prefix: {type: string}}
  inputs:  {artifact: {type: any}}
  outputs: {}                               # 消费终端：没有产物，只留下回执记录
  idempotency_key: "{{ inputs.artifact.id }}:s3://{{ params.bucket }}/{{ params.prefix }}"
  verify_receipt: s3-head-and-hash          # 核验对象存在，且 hash 与 Bean 一致
  policy: sink-s3@1                         # 只有这里绑定 S3 写凭据和对应的 publish 出网
---
apiVersion: mao/v1
kind: Station
metadata: {name: publish-github, version: 1}
spec:
  role: sink
  machine: git@1
  recipe: commit-push
  operator: {runtime: script@1}
  params: {repo: {type: string}, branch: {type: string}}
  inputs:
    script:   {type: doc.script@1}
    subtitle: {type: subtitle.srt@1}
  outputs: {}                               # 消费终端：没有产物，只留下回执记录
  idempotency_key: "{{ inputs.script.id }}:{{ inputs.subtitle.id }}:{{ params.repo }}"
  verify_receipt: git-commit-visible
  policy: sink-github@1
---
apiVersion: mao/v1
kind: Station
metadata: {name: publish-shorts, version: 1}
spec:
  role: sink
  machine: yt-upload@1
  recipe: upload-short
  operator: {runtime: script@1}
  inputs:
    video: {type: video.mp4@1, requires: [vertical, hd, has_audio, not_silent]}
    meta:  {type: doc.script@1}
  outputs: {}                               # 消费终端：没有产物，只留下回执记录
  phases: [propose, execute]                # 先 dry-run，厂长审批后再发布
  approval: {propose: foreman}
  idempotency_key: "{{ inputs.video.id }}:shorts"
  verify_receipt: url-reachable
  policy: sink-shorts@1
```

**产线**：

```yaml
apiVersion: mao/v1
kind: Line
metadata: {name: explainer-multilang, version: 5}
spec:
  interface:
    inputs: {brief: {type: brief.video@1}}
  trigger:
    - {on: bean, type: brief.video@1, label: "line=explainer-multilang"}   # 物料到达即触发
  budget: {cap_usd: 12.0}
  steps:
    explainer:
      station: explainer@3
      in: {brief: $inputs.brief}
    dub_en:
      station: dub@2
      params: {target_language: en}
      in: {video: explainer.video, subtitle: explainer.subtitle}
    vertical:
      station: vertical@1
      in: {video: dub_en.video}
    to_s3:
      station: publish-s3@2
      params: {bucket: media-archive, prefix: "explainers/{{ run.id }}/"}
      in: {artifact: vertical.video}
    to_github:
      station: publish-github@1
      params: {repo: org/explainer-scripts, branch: main}
      in: {script: explainer.script, subtitle: dub_en.subtitle}
    to_shorts:
      station: publish-shorts@1
      in: {video: vertical.video, meta: explainer.script}
```

编译结果：每条边的类型都兼容；每个 `requires` 都被上游的 `guarantees` 覆盖；所有物料都有去处；预算为 5.0 + 3.0 + 0 = 8.0 USD，不超过 12.0 USD。

**一次运行的时间线**：

| 时刻 | 事件 |
|---|---|
| t0 | webhook 送达；Source 工位 `intake-brief@1` 产出 `brief.video@1`，通过检验后封存；触发 `explainer-multilang@5`，run `r-0042` 绑定 epoch `9c1e7a2` |
| t1 | 运行前编译通过；`explainer` job 进入 propose 阶段 |
| t2 | agent 给出方案，预估 3.2 USD，超过自动通过的阈值 3.0 → 进入 `AWAITING_APPROVAL` → 厂长在委托范围内批准 |
| t3 | 进入 execute 阶段；agent 在一个创意决策点调用 `factoryctl approval`（0.3 USD，自动通过） |
| t4 | 提交 video / subtitle / script；出厂检验发现时长 71.2 秒，超出 54–66 秒的范围 → `REJECTED`，反馈写入下一次的 Brief |
| t5 | 第 2 次尝试（resume，复用工作区里已经生成的素材）：时长 61.4 秒，judge 打分 0.82 → `SEALED` |
| t6 | `dub_en`、`vertical` 依次 `SEALED` |
| t7 | `to_s3`、`to_github` 直接提交，回执核验通过；`to_shorts` 先 dry-run，厂长审批后发布，回执记录中的 URL 可以访问 |
| t8 | run 完成。血缘：brief → explainer → dub_en → vertical，最后被 3 个消费终端消费。消费终端没有产物，只留下 3 条回执记录 |

**旁路：服务型产品 + 脚本操作工**

```yaml
apiVersion: mao/v1
kind: Station
metadata: {name: quick-short, version: 1}
spec:
  role: transform
  machine: moneyprinter@2
  recipe: quick-short
  operator: {runtime: script@1}      # 参数都能从 brief 中结构化地取得，不需要 agent
  inputs:  {brief: {type: brief.video@1}}
  outputs: {video: {type: video.mp4@1, guarantees: [has_audio]}}
  limits:  {timeout: 30m, budget_usd: 0.5}
  policy: video-default@2
```

### 13.2 演进示例：上游产品升级

假设上游 OpenMontage 发布了新版本 `a71d0e4`，带来两处（假设的）变化：animated-explainer 流水线的字幕默认改为 VTT 格式；所有流水线的渲染输出目录从 `renders/` 改为 `output/`。

1. **发现**：订阅上游 release 的 Source 工位产出 `event.release@1`，触发 `upgrade-machine` 产线。产线在沙箱中构建候选的 `openmontage@8`，运行 conformance 和黄金样例。结果，收货规则找不到 `renders/final.mp4` 和 `*.srt`，conformance 失败，任务转给厂长。由于所有版本都是固定的，线上运行的 `openmontage@7` 完全不受影响。
2. **诊断**：厂长阅读 release notes 和 transcript，确认问题出在路径和字幕格式的变化。
3. **修复草稿**：厂长在 Registry 的分支上修改 `openmontage`（收货路径、字幕类型）和 `explainer`（字幕输出改为 `subtitle.vtt@1`），然后检查草稿。`explainer@4.subtitle`（VTT）要连到 `dub@3.subtitle`（需要 SRT），但转换库里还没有 VTT → SRT 的转换：

```text
✖ E-NO-CONVERTER  explainer@4.subtitle : subtitle.vtt@1 → dub@3.subtitle : subtitle.srt@1
    转换库中没有可用路径 → 已生成转换需求 cr-0012
```

4. **编写转换**：厂长处理 cr-0012：
   - 在 ffmpeg 机器中增加 `subs-convert` 配方；
   - 编写转换工位 `subs-convert@1`（定义见 9.2），连同 equivalence 和正反样例；
   - 提交 ChangeSet `cs-0141`。这是新增转换，而且不改变任何现有边的解析结果，所以属于 T1：验证 → 影子运行 → 生效，得到 epoch `d3a0f55`。
5. **提交升级**：厂长把草稿整理成 ChangeSet `cs-0142`，基线是 `d3a0f55`。**产线里不需要显式写转换步骤**，编译器会在 explainer 与 dub 之间自动插入 `subs-convert@1`：

```yaml
apiVersion: mao/v1
kind: ChangeSet
metadata:
  name: cs-0142
  proposer: "foreman (claude-code@4 / claude-opus-5-5)"
  trigger: "upgrade-machine/r-0057"
spec:
  base_epoch: d3a0f55
  intent: "升级 OpenMontage 到 a71d0e4：修复收货路径；explainer 的字幕改为 VTT（由转换库自动转成 SRT）"
  changes:
    - op: upsert
      kind: Machine
      name: openmontage
      from: 7
      to: 8
      diff:
        source.commit: "3f2a9c1 → a71d0e4"
        recipes.*.outputs.*.harvest: "projects/*/renders/… → projects/*/output/…"
        recipes.animated-explainer.outputs.subtitle: "subtitle.srt@1 (*.srt) → subtitle.vtt@1 (*.vtt)"
    - op: upsert
      kind: Station
      name: explainer
      from: 3
      to: 4
      diff: {machine: "openmontage@7 → @8", outputs.subtitle.type: "subtitle.srt@1 → subtitle.vtt@1"}
    - op: upsert
      kind: Station
      name: dub
      from: 2
      to: 3
      diff: {machine: "openmontage@7 → @8"}
    - op: upsert
      kind: Line
      name: explainer-multilang
      from: 5
      to: 6
      diff:
        steps.explainer: "explainer@3 → @4"
        steps.dub_en: "dub@2 → @3"
        # 不需要新增转换步骤：explainer.subtitle → dub_en.subtitle 由转换库自动衔接
  rollout:
    shadow: {runs: 3, budget_usd: 15}
    canary: {percent: 20, min_runs: 5}
    observe: 24h
```

6. **验证报告**：

```text
cs-0142 验证报告（候选 epoch e4b8d10）
V1 规格校验   ✔ 4 个 Spec 符合 schema；引用、digest、commit 均已固定
V2 结构编译   ✔ explainer-multilang@6：类型、契约、无环、去处、权限、预算 8.0 / 12.0
              ℹ 自动转换：explainer.subtitle (subtitle.vtt@1) → dub_en.subtitle (subtitle.srt@1)，命中 subs-convert@1
V3 演进校验   ✔ explainer@4.outputs.subtitle 类型变化 → 影响 1 条产线，已由转换库自动衔接
              ✔ 没有 guarantee 被删除；没有验证器被放宽
              ℹ explainer@3、dub@2 仍被 video-planner@1 引用 → 保持 active，后续迁移
V4 契约测试   ✔ conformance：zero-key-demo、contracts；黄金样例 3/3；subs-convert equivalence 12/12
风险等级     T2（机器升级）→ 需要：影子运行 3 次 + 金丝雀 20% + 交叉复核（codex@2）
```

7. **发布**：codex@2 交叉复核通过 → 影子运行 3 次（Sink 替换为空 Sink），结果不比现状差 → 金丝雀 20% → 观察 24 小时 → `active` 指向 `e4b8d10`。全程没有人工参与。
8. **不受影响的部分**：发布期间仍在运行的 run 绑定的是旧 epoch，会按旧结构跑完。如果金丝雀阶段违反 SLO，`active` 会自动退回 `d3a0f55`。
9. **收尾**：之后再用一个 ChangeSet 把 `video-planner` 迁移到 `explainer@4`，然后把 `explainer@3`、`dub@2` 标记为 deprecated，最终退役（expand → migrate → contract）。
10. **复用**：两周后，新接入的播客产品 `podcast-studio` 也输出 VTT 字幕，而它下游的发布工位需要 SRT。编译时直接命中 `subs-convert@1` 并自动插入，厂长不参与，也不需要修改任何产线。同一份字幕即使被多条产线使用，也只转换一次（实例级命中）。

**另一种漂移：版本固定也挡不住的外部变化**

假设某个 TTS provider 悄悄更新了模型，导致部分配音的音量过低。处理过程如下：

1. 定时 canary 和漂移监测发现 `not_silent` 的失败率上升，响度分布偏离基线。
2. 质量控制器熔断 `dub` 工位，并唤醒厂长。
3. 厂长提交一个 T1 的 ChangeSet，调整 `dub` 的任务模板，指定改用另一个 TTS provider。
4. 变更经过验证和影子运行后推广，熔断随之恢复。

### 13.3 动态示例：在运行期生成结构

**planner 工位**：

```yaml
apiVersion: mao/v1
kind: Station
metadata: {name: video-planner, version: 1}
spec:
  role: planner
  machine: workspace@2
  recipe: freeform
  operator: {runtime: claude-code@4, model: claude-sonnet-5-5, max_turns: 30}
  inputs:  {brief: {type: brief.video@1}}
  outputs: {plan: {type: spec.plan@1}}
  plan_bounds:
    allowed_stations: [explainer@3, dub@2, vertical@1, publish-s3@2]   # 转换由转换库自动插入，不需要列出
    allowed_sinks: [publish-s3@2]
    max_steps: 12
    max_fanout: 5
    max_depth: 2
    budget_usd: "{{ run.budget.remaining_usd }}"
```

planner 为一份 brief 产出的 plan：

```yaml
apiVersion: mao/v1
kind: Plan
metadata: {name: plan-7f3a, generated_by: video-planner@1, run: r-0063}
spec:
  steps:
    make:
      station: explainer@3
      in: {brief: $inputs.brief}
    dubs:
      map: {over: [en, ja, es], as: lang, max_parallel: 2}
      station: dub@2
      params: {target_language: "{{ lang }}"}
      in: {video: make.video, subtitle: make.subtitle}
    verticals:
      map: {over: dubs.video}              # dubs.video 的类型是 list<video.mp4@1>
      station: vertical@1
      in: {video: $item}
    archive:
      map: {over: verticals.video}
      station: publish-s3@2
      params: {bucket: media-archive, prefix: "plans/{{ run.id }}/"}
      in: {artifact: $item}
```

出厂检验（V1 + V2 + plan_bounds）的结果：

```text
✖ E-BUDGET  预估 5.0 + 3 × 3.0 = 14.0 USD，超过 run 的剩余预算 12.0 USD
```

这条错误作为反馈退回给 planner。planner 改为只配 2 种语言（预估 11.0 USD），重新检验通过后，plan 被封存，子 DAG 挂到 run `r-0063` 上执行。

**clip-factory + map**：某条产线使用 Clip Factory 类的 Recipe，把一段长视频剪成按排名排序的短片，输出 `list<video.mp4@1>`，然后：

```yaml
  publish_clips:
    map: {over: clips.videos, max_fanout: 20, overflow: truncate}   # 超过 20 条时只取排名前 20 的
    station: publish-s3@2
    params: {bucket: clips, prefix: "{{ run.id }}/"}
    in: {artifact: $item}
```

### 13.4 厂长犯错时会发生什么

| 厂长的尝试 | 结果 |
|---|---|
| 为了降低 `vertical` 的退回率，提出 `vertical@2`，删掉 `has_audio`、`not_silent` 两个 guarantee | V3 拒绝，报 `E-CONTRACT`：`publish-shorts@1` 依赖这两个属性（报告见 7.3） |
| 为了让更多视频通过，修改 ffprobe validator，让它容忍截断的文件 | V4 发现反样例 `truncated.mp4` 被接受，违反不变量 I8（反样例必须被拒绝）→ 直接拒绝；删除这个反样例属于宪法级操作，厂长无权执行 |
| 图方便，让 `explainer` 直接上传成品，给它绑定 S3 写凭据 | 违反不变量 I6，报 `E-INVARIANT`，直接拒绝；宪法不对厂长开放 |
| 改了一个模板，验证全部通过，但实际让成片质量下降 | 影子运行中 judge 的对比结果比现状差 → 退回；即使侥幸通过影子运行，金丝雀阶段一旦违反 SLO 也会自动回滚 |
| 把自己的 ChangeSet 标为 T1 | 风险等级由流水线计算，厂长的标注被忽略 |
| 优化 `subs-convert` 时引入错误，导致部分字幕条目丢失 | 差分测试发现 equivalence（`cue_count` 一致）不成立的样例 → 拒绝；即使漏过，下游退回率上升也会触发熔断，转换版本被回滚 |

### 13.5 转换优化示例：把 agent 转换固化为脚本

1. **第一次遇到**：
   - 某条归档产线需要把 `doc.script@1`（JSON 格式的分镜脚本）转换为 `doc.markdown@1`。转换库里没有对应的转换，于是生成转换需求 `cr-0031`。
   - 由于脚本结构多变，厂长先写了一个 agent 驱动的转换 `script-to-md@1`：在 `workspace` 机器上，由 Claude Code 按模板把 JSON 整理成 Markdown。
   - equivalence 要求 Markdown 中的章节数等于 JSON 中的场景数：`out.facts.section_count == in.facts.scene_count`。
   - 这是 T1 变更，验证后生效。
2. **持续命中**：之后 6 周内，3 条产线累计命中 `script-to-md@1` 412 次，另有 97 次实例级命中，直接复用了已有结果。转换统计显示：平均每次 0.04 USD、耗时 40 秒；有 2 次 equivalence 违例，被出厂检验退回后重试成功。
3. **固化**：厂长从 412 个历史输入中归纳出 JSON 的结构规律，把转换改写成在 `convkit` 上运行的确定性脚本 `script-to-md@2`。
4. **差分测试**：在全部正样例和抽样的 200 个历史输入上，同时运行 @1 和 @2：
   - @2 的输出全部通过检验和 equivalence；
   - 关键 facts（场景数、标题、时长标注）与 @1 的输出全部一致；
   - 没有“@1 通过、@2 失败”的样例。
5. **发布**：替换转换版本属于 T2：codex@2 交叉复核 → 影子运行 → 金丝雀 → 生效。解析表中所有指向 `script-to-md@1` 的条目都改为指向 @2。
6. **效果**：每次转换的成本从 0.04 USD 降到接近 0，耗时从 40 秒降到 1 秒，结果完全确定。`script-to-md@1` 在引用归零后退役。

**合并多跳链**：

- 转换统计发现，`video.webm@1 → video.mkv@1 → video.mp4@1` 这条两跳链每天被命中上百次。
- 厂长编写了直连转换 `webm-to-mp4@1`。新路径步数更少，会改变现有边的解析结果，所以属于 T2。
- 差分测试确认它与原链路的输出在关键 facts（时长、分辨率、音轨）上一致，之后发布生效。
- 原来的两个转换只要还有其他路径在用，就继续保留。

---

## 14. 技术选型与对照

### 14.1 推荐技术

| 组件 | MVP | 生产 |
|---|---|---|
| 沙箱 | Docker Engine API + job 专属 internal 网络 | 同 MVP；或通过 SandboxProvider 换成 gVisor、Firecracker、云沙箱 |
| 出网代理 | 支持“域名 × 方法”规则的正向代理 | 同 MVP，另加集中式策略管理 |
| 模型代理 | 轻量反向代理：注入密钥、按 token 计量 | LLM 网关类产品 |
| Warehouse | 本地文件系统 CAS + SQLite | S3 / MinIO + Postgres；可选 OCI artifact（ORAS） |
| 调度与状态机 | 自研，基于 SQLite 事务 | Temporal 等持久化执行引擎 |
| Registry | Git 仓库 + 服务端钩子 | Git + 受保护分支 + 签名提交 |
| 策略与不变量 | CEL | CEL 或 OPA/Rego |
| 事件 | SQLite outbox | NATS / Kafka |
| 控制面接口 | `factory` CLI + MCP server | 同 MVP，另加 Web 控制台 |
| 可观测性 | 结构化日志 + transcript 存档 | OpenTelemetry + Prometheus |

控制面用什么语言实现，本文档不做决定。

### 14.2 借鉴与差异

| 借鉴对象 | 借鉴了什么 | 本设计的不同之处 |
|---|---|---|
| Confluent Schema Registry | Schema 兼容模式 | 增加了属性级的消费者契约 |
| Pact | 消费者驱动契约 | 编译期做集合包含判断，运行期用 CEL 计算 |
| Kubernetes 控制器 / Argo CD | 声明式调和、漂移检测 | 调和对象还包括工位沙箱和凭据绑定 |
| Kubernetes Admission / OPA / CEL | 准入控制、策略即代码 | 把宪法不变量作为准入规则 |
| Argo Rollouts / Flagger | 影子运行、金丝雀、自动回滚 | 发布的单位是结构 epoch，而不是服务版本 |
| Dagster | 资产与血缘 | 产物来自“agent + 外部产品”，并且要过出厂检验 |
| Nix / Bazel | 内容寻址与缓存 | 缓存键包含模型和提示词版本 |
| SLSA / in-toto | 来源证明 | 证明中包含 transcript、成本和审批记录 |
| Flow-Based Programming | 带类型端口的黑盒组件 | 黑盒是“agent + 产品” |
| 编程语言的隐式类型转换（如 Rust 的 From / Into） | 按类型自动插入转换 | 解析结果按 epoch 固定，带 equivalence 约束与差分测试 |

### 14.3 与 OpenMontage 这类产品的关系

OpenMontage 的思路是“agent 即编排器”：在产品内部，由 agent 依据清单和技能完成整条视频流水线，并自带质检和预算控制。本系统位于它的**上一层**：把这类产品当作一台机器、一个工位，在工位与工位之间加上确定性的秩序，包括类型契约、出厂检验、快照隔离和治理。两者是互补的：产品负责把工位内的事情做好，工厂负责让工位之间不乱。

---

## 15. 路线

| 里程碑 | 内容 | 完成标准 |
|---|---|---|
| M0 规范与编译器 | 元模型、各 kind 的 JSON Schema、V1/V2 编译器、静态不变量检查、`factory check` | 文中的示例 Spec 全部编译通过；类型不匹配时能给出 converter 建议 |
| M1 单工位 | Docker SandboxProvider、Station Gateway 与 `factoryctl`、claude-code 与 script runtime、V5 出厂检验、本地 Warehouse | `explainer`（A 形态）和 `vertical`（C 形态）能端到端产出带来源证明的 sealed Bean |
| M2 产线与转换 | 调度器、Line、Source / Sink、缓存、死信、审批、V3/V4；转换库、自动转换与两级命中 | `explainer-multilang` 跑通，能发布到 MinIO 和测试仓库；重跑时命中缓存；Sink 幂等生效；类型不兼容的边能自动插入已有的转换 |
| M3 治理与自治 | epoch、ChangeSet 流水线、影子运行与金丝雀、调和器、漂移监测、安全模式、厂长（控制面 MCP + AGENTS.md）；转换需求与转换优化（差分测试） | 复现 13.2：缺少转换 → 厂长编写 → 自动衔接 → 推广，且运行中的 run 不受影响；复现 13.4 中的各项拦截；复现 13.5 的转换固化 |
| M4 动态与生产化 | map / switch / plan、B 形态 Gateway、Postgres / S3 | 复现 13.3：plan 编译失败后的反馈回路能生效 |
| M5 自治闭环 | M1–M4 纠偏、完整命令行、持久化执行、守护进程与信号泵、MCP 操作面、厂长值守（现成 CLI 会话与交叉复核） | 无人值守复现 13.2：信号 → 唤醒 → 厂长会话提议 → 另一 runtime 复核 → 推广 |
| M6 真实接入 | 固定版本的 Claude Code / Codex 在 Docker 里运行；模型代理与出网代理的归属与网络；厂长构建镜像 | 真实 CLI 跑通工位 job、厂长会话与复核会话，秘密不进沙箱，预算实时生效 |
| M7 运维与生产化 | 状态与指标、只读 Web 控制台、保留期与回收、备份与恢复 | 长期运行时状态可见、存储增长受控、能从备份恢复出可用的工厂 |

---

## 16. 开放问题

1. **长期运行的服务能否作为产物**：Bean 是静态快照，“部署一个网站”更适合建模成 Sink（回执里放 URL），服务本身不进仓库。是否需要“服务型产物”？
2. **超大 Bean**：数据集、长视频用 `ref<T>`（外部存储 + digest）表示。分块上传、回收和成本怎么处理？
3. **一个工位使用多台机器**：目前通过多个工位或嵌套产线组合实现。是否存在必须在同一个沙箱里协作的场景，例如两个产品共享 GPU 显存？
4. **非 LLM 调用的成本核算**：目前依赖产品自报和代理计量来估算。要精确核算，需要对接各 provider 的账单 API。
5. **外部物料中的提示词注入**：外部物料可能包含针对 agent 的恶意指令。目前靠沙箱和凭据隔离限制影响范围。是否需要在 Source 检验中加入注入检测？
6. **许可证与版权**：AGPL 等许可证的产品以服务方式运行时有哪些义务；素材库和生成模型的使用条款如何遵守。可以考虑给 Type 增加 `license_ok` 之类的属性，让 Sink 通过 `requires` 强制检查。
7. **安全模式的阈值**：阈值太敏感，会频繁冻结变更；太迟钝，又会放过问题。阈值写在宪法里，需要在实践中校准。
8. **多租户与多主机**：资源隔离、密钥隔离、跨主机调度。
9. **人工工位**：完整自治的设计不包含人工工位。如果将来确实需要（例如法律审核），可以作为新的 OperatorRuntime 类型加入，但必须由宪法显式允许。
10. **转换的语义边界**：哪些加工算“表达变换”（可以自动转换），哪些算“内容加工”（必须显式写进产线）？需要在实践中积累判例，并写入厂规。

---

## 附录 A：Spec 字段参考

> 各 kind 的完整示例见正文对应章节，这里只给出字段说明。

### A.1 通用信封

| 字段 | 必填 | 说明 |
|---|---|---|
| `apiVersion` | ✔ | 例如 `mao/v1` |
| `kind` | ✔ | `Type` / `Machine` / `OperatorRuntime` / `Station` / `Line` / `Policy` / `Plan` / `ChangeSet` / `ConversionRequest` / `GoldenSet` / `Baseline` |
| `metadata.name` | ✔ | 在同一 kind 内唯一 |
| `metadata.version` | ✔ | Type 用 `"major.minor"`，其余用递增整数；发布后不可变 |
| `metadata.lifecycle` | | `active`（默认）/ `deprecated` / `retired` |
| `metadata.labels` | | 任意标签 |
| `spec` | ✔ | 各 kind 的具体内容 |

### A.2 Type

| 字段 | 说明 |
|---|---|
| `extends` | 父类型（同一 major），用于子类型判断 |
| `kind` | `atomic`（默认）/ `bundle` / `list` / `ref` |
| `fields` / `item` / `target` | 分别是 `bundle` 的字段、`list` 的元素类型、`ref` 的目标类型 |
| `payload[]` | `path`（可以用通配符）、`media_type`、`required` |
| `meta_schema` | 自报元数据的 JSON Schema |
| `validators[]` | `name`、`image`（digest）、`run`、`emits`（产出哪些 facts）、`fixtures.accept / reject` |
| `properties` | 命名属性 → CEL 表达式（只引用 `bean.facts` 和 `bean.payload`） |
| `compat` | 默认 `full-within-major` |
| `retention` | 该类型 Bean 的默认保留期 |

### A.3 Machine

| 字段 | 说明 |
|---|---|
| `form` | `workspace`（A）/ `service`（B）/ `tool`（C） |
| `source` | `repo`、`commit` |
| `runtime` | `image`（digest）或 `compose`；`workdir`；`service`；`instance`（`per_job` / `pooled`）；`reset`；`resources`；`caches[]` |
| `operate` | A 形态：`guide[]`、`factory_notes`；B 形态：`openapi`、`long_task`（`submit` / `poll` / `done` / `fetch`） |
| `needs` | `secrets[]`（只写名字）、`egress`（档案名或明细）、`budget_knobs[]`、`config_render`、`rate_limits` |
| `recipes.<name>` | `inputs`（`type`、`place`）、`params`、`outputs`（`type`、`harvest`、`optional`）、`task_template` / `call` / `run` |
| `probe` | 能力探测命令及其输出格式 |
| `conformance[]` | `name`；`run`、`call` 或 `recipe + inputs` 三者之一；`expect` |

### A.4 OperatorRuntime

| 字段 | 说明 |
|---|---|
| `kind` | `agent-cli` / `script` |
| `inject` | `volume`（digest）和 `mount`；或者 `derive_image: true` |
| `invoke.argv` | 启动命令模板。可用变量：`brief.task`、`brief.factory_contract`、`limits.*`、`operator.model` |
| `invoke.mcp` | MCP 配置的渲染格式和存放位置 |
| `env` | 环境变量模板（例如指向模型代理） |
| `transcript` | 输出格式和来源 |
| `models_allowed[]` | 允许使用的模型 |

### A.5 Station

| 字段 | 说明 |
|---|---|
| `role` | `source` / `transform` / `converter` / `judge` / `planner` / `sink` |
| `machine`、`recipe` | 使用的机器（`name@version`）和配方 |
| `operator` | `runtime`、`model`、`max_turns`、`tools`（B 形态的工具白名单） |
| `params` | 参数及其类型 |
| `inputs.<port>` | `type`、`requires[]` |
| `outputs.<port>` | `type`、`guarantees[]`、`accept[]`、`optional`；sink 默认没有输出端口 |
| `phases` | `[execute]`（默认）或 `[propose, execute]` |
| `approval` | `propose` / `in_run` 的规则，以及 `timeout` |
| `gate.judge` | `runtime`、`model`、`rubric`、`sample_rate`、`threshold` |
| `limits` | `timeout`、`budget_usd`、`retries`（`rejected`、`failed`、`mode`） |
| `converts`、`equivalence`、`cost`、`fixtures` | 仅 converter 使用：`converts` 包括 `from`、`to`、`lossless`、`preserves`；`equivalence` 是转换前后必须成立的关系；`cost` 用于路径排序；`fixtures` 是正反样例 |
| `plan_bounds` | 仅 planner 使用：`allowed_stations`、`allowed_sinks`、`max_steps`、`max_fanout`、`max_depth`、`budget_usd` |
| `idempotency_key`、`verify_receipt` | 仅 sink 使用 |
| `policy` | 引用的 Policy |

### A.6 Line

| 字段 | 说明 |
|---|---|
| `interface` | `inputs`、`outputs`；声明后可以作为 `subline` 被嵌套 |
| `trigger[]` | `manual` / `cron` / `bean`（类型 + 标签） |
| `budget` | `cap_usd` |
| `steps.<id>` | `station` 或 `subline`；`in`（`$inputs.x` / `step.port` / `$item`，或者 `{from, via}`，用 `via` 显式指定或禁止自动转换）；`params`；`map` / `switch` / `until` |
| `keep[]` | 需要显式保留、不交给 Sink 的终端物料 |
| `slo` | 产线级 SLO，供发布控制器使用 |

### A.7 Policy

| 字段 | 说明 |
|---|---|
| `applies_to` | 可以绑定的工位角色（例如 publish 只能绑定 `sink`） |
| `egress` | `none` / `providers` / `web-read` / `publish` / `ingress`，以及明细（域名、方法） |
| `secrets[]` | 允许注入的密钥名 |
| `resources` | 资源池、配额 |
| `budget` | 默认预算和审批阈值 |
| `retention` | 产物、工作区、transcript 的保留期 |

```yaml
apiVersion: mao/v1
kind: Policy
metadata: {name: sink-s3, version: 1}
spec:
  applies_to: [sink]
  egress:
    publish: {hosts: [media-archive.s3.us-east-1.amazonaws.com], methods: [HEAD, PUT, POST]}
  secrets: [S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY]
  retention: {workspace: 7d}
---
apiVersion: mao/v1
kind: Policy
metadata: {name: video-default, version: 2}
spec:
  applies_to: [transform, planner, judge]
  egress: {providers: from-machine, web_read: allow}    # 出网白名单沿用 Machine Package 的声明
  secrets: from-machine
  resources: {pool: cpu-standard}
  retention: {workspace: 3d, transcript: 90d}
```

### A.8 Plan

结构与 Line 的 `steps` 相同，额外字段为 `metadata.generated_by`（产出它的 planner 工位）和 `metadata.run`（所属的 run）。Plan 不能定义新的 Spec。

### A.9 ChangeSet

| 字段 | 说明 |
|---|---|
| `metadata.proposer`、`metadata.trigger` | 提出者和触发来源 |
| `base_epoch` | 基线 epoch |
| `intent` | 变更意图（自然语言，供复核者阅读） |
| `changes[]` | `op`（`upsert` / `deprecate` / `retire`）、`kind`、`name`、`from`、`to`、`diff` |
| `rollout` | `shadow`（次数、预算）、`canary`（比例、最少样本数）、`observe`、`slo` |
| 由流水线写入的字段 | `risk_tier`、验证报告的引用、复核与审批记录、状态 |

### A.10 GoldenSet 与 Baseline

- GoldenSet：`station`、`cases[]`（`name`、`inputs`）、`budget_usd`。
- Baseline：`station@version`、`window`、`metrics.<name>`（统计值和容差）。

### A.11 Bean 清单与来源证明

```jsonc
// bean.json —— bean_id = sha256(规范化后的 {type, meta, payload 各文件 digest})
{
  "type": "video.mp4@1.3",
  "payload": [{"path": "video.mp4", "digest": "sha256:b3a9…", "size": 48211934, "media_type": "video/mp4"}],
  "meta": {"title": "为什么天空是蓝色的", "language": "zh-CN"}
}
```

```jsonc
// provenance —— 作为附件挂在 Bean 上，不参与 bean_id 计算
{
  "bean": "sha256:77e0…",
  "produced_by": {"station": "explainer@3", "job": "j-7Q2M", "run": "r-0042", "epoch": "9c1e7a2"},
  "machine": {"name": "openmontage@7", "image": "sha256:5d1e…", "upstream_commit": "3f2a9c1"},
  "operator": {"runtime": "claude-code@4", "model": "claude-opus-5-5", "turns": 87},
  "inputs": ["sha256:41ab…"],
  "params": {},
  "facts": {"duration_s": 61.4, "width": 1920, "height": 1080, "fps": 30, "vcodec": "h264", "audio_streams": 1, "mean_volume_db": -18.2},
  "gate": {"report": "sha256:…", "judge_score": 0.82, "attempt": 2},
  "approvals": [{"kind": "propose", "decided_by": "foreman", "cost_estimate_usd": 3.2}],
  "cost": {"llm_usd": 0.94, "providers_usd_est": 1.21},
  "attachments": {"transcript": "sha256:…", "decision_log": "sha256:…", "workspace_snapshot": "sha256:…"},
  "timestamps": {"started": "2026-11-09T08:12:03Z", "sealed": "2026-11-09T08:57:41Z"}
}
```

### A.12 Registry 目录结构

见 11.5。

### A.13 ConversionRequest 与转换解析表

ConversionRequest：

| 字段 | 说明 |
|---|---|
| `from` | `type`、`guarantees` |
| `to` | `type`、`requires` |
| `found_at[]` | 发现位置：产线与边、run，或 ChangeSet 草稿 |
| `samples[]` | 实际遇到的输入 Bean |
| `blocked_runs[]` | 因此被阻塞的 run |
| `status` | `open` / `in_progress` / `resolved`（关联到对应的 ChangeSet） |

转换解析表（每个 epoch 一张，由编译器生成，只追加不修改）：

| 字段 | 说明 |
|---|---|
| `key` | `(U, G, D, R)`：上游类型与保证的属性、下游类型与要求的属性 |
| `chain` | 转换链 `[converter@版本, …]`，或 `direct`（不需要转换） |
| `reason` | 排序依据：是否无损、步数、估计成本、估计耗时 |
| `origin` | `precomputed`（epoch 构建时预先计算）或 `runtime`（运行期追加） |

---

## 附录 B：术语表

| 术语 | 英文 | 说明 |
|---|---|---|
| 物料 / 产物 | Bean | 某个 Type 的实例，按内容寻址，不可变 |
| 物料规格 | Type | 描述物料的 schema、validator 和命名属性 |
| 实测值 | facts | validator 从 payload 中实测出的值 |
| 自报值 | meta | 生产者声明的元数据 |
| 命名属性 | property | 基于 facts 的 CEL 表达式，用于契约 |
| 智能机器 | Machine | 已有的产品，由 Machine Package 描述 |
| 配方 | Recipe | 机器提供的一种带类型的能力 |
| 操作工 | Operator | agent CLI、脚本或人 |
| 工位 | Station | Recipe × Operator × Policy |
| 产线 | Line | 由工位组成的带类型 DAG |
| 计划 | Plan | planner 在运行期产出的子 DAG |
| 消费者 | Sink | 只产出回执的工位 |
| 摄入者 | Source | 从外部摄入物料的工位 |
| 回执记录 | receipt | Sink 执行后留下的执行记录（目标、定位信息、幂等键、核验结果）；不是产物，不能被下游引用 |
| 出厂检验 | Gate | 候选产物封存前的检验 |
| 来料检验 | — | job 开始前对输入的检验 |
| 来源证明 | provenance | 产物的生产记录 |
| 仓库 | Warehouse | Bean 的存储和血缘 |
| 注册表 | Registry | 存放所有 Spec 的 Git 仓库 |
| 结构版本 | epoch | Registry 中一个已合入的 commit |
| 变更集 | ChangeSet | 修改结构的唯一途径 |
| 宪法 / 不变量 | Invariant | 系统必须始终满足的规则，只有人能修改 |
| 厂长 | Foreman | 维护系统的外部 agent |
| 调和器 | Reconciler | 把实际状态恢复成声明状态的确定性组件 |
| 任务书 | Station Brief | 交给 agent 操作工的任务说明 |
| 死信队列 | DLQ | 存放重试耗尽的 job |
| 影子运行 | shadow | 用候选结构处理真实输入，但不对外发布 |
| 金丝雀 | canary | 让一部分真实运行使用候选结构 |
| 转换 | converter | 单输入单输出、带 equivalence 约束的类型转换工位，可以被自动插入 |
| 转换库 | converter library | 当前 epoch 中所有 active 转换构成的有向图 |
| 转换解析表 | resolution table | 每个 epoch 中“不兼容 → 转换链”的确定映射 |
| 转换需求 | ConversionRequest | 缺少转换时自动生成的工单，由厂长处理 |
| 固化 | crystallize | 把 agent 驱动的转换改写为确定性脚本 |
| 完整自治 | full autonomy | 运行与维护不需要人工审批；人只修改宪法 |
| 安全模式 | safe mode | 出现异常时自动冻结高风险变更，并固定在最近一个健康的 epoch |
