[English](../en/concepts.md) | [日本語](../ja/concepts.md) | [한국어](../ko/concepts.md) | [Français](../fr/concepts.md) | [Deutsch](../de/concepts.md) | [Español](../es/concepts.md) | [Português (BR)](../pt-BR/concepts.md) | [Русский](../ru/concepts.md) | **简体中文**

# 核心概念

Machine Agent Org 的元模型很小。你能搭建的任何东西，无论是视频流水线、代码评审产线还是每晚的报表，都用下面七个原语表达。它们以 YAML **Spec** 的形式存放在 Git registry 中，结构编译器像类型检查器检查代码一样检查这些 Spec。

> 设计理由见 [`docs/architecture.md`](../architecture.md) 第 3–10 节；形式化规则见 [`spec/mrl/factory-profile.md`](../../spec/mrl/factory-profile.md)。

## 1. 原语

| 原语 | 含义 | 要点 |
|---|---|---|
| **Type** | 物料规格 | `name@major.minor`<br>种类：原子、`bundle{…}`、`list<T>`、`ref<T>`、`any`<br>带 payload 布局、meta schema、validator（含正反样例）、实测 *facts* 和命名 *properties* |
| **Bean** | 一份物料 | 内容寻址（`sha256:…`），不可变；封存时附带血缘与来源证明 |
| **Machine** | 现成产品 | 由 Machine Package 描述：按 digest 固定的镜像、配方、输入输出、probe、conformance 黄金样例 |
| **Operator** | 驱动机器的一方 | agent CLI（Claude Code 无头模式、Codex）、脚本，或通过收件箱的人 |
| **Station** | Recipe × Operator × Policy | 端口带类型<br>角色：`source`、`transform`、`sink`；`converter`、`judge`、`planner` 是特化的 transform |
| **Line** | 由工位组成的带类型 DAG | 外加受限的动态构造；声明了接口的产线可以当作工位使用（`subline`） |
| **Policy** | 权限 | 出网等级、密钥、预算、审批、发布、保留期 |

元层之上还有三个概念：
- **Spec**：上面每一个定义都是一个 `spec.<kind>@N` 类型的 Bean。
- **Invariant**：用 MRL 写的宪法规则。
- **ChangeSet**：Spec 的差异，本身也是 Bean。

既然工厂自己的结构也是物料，同一套检查就能保护维护工作本身。厂长正是这样被约束在秩序之内的。

## 2. 类型、facts 与 properties

类型声明两样东西：*facts* 由 validator 实测得出；*properties* 是基于 facts 的命名 MRL 表达式。

```yaml
# spec/examples/registry/types/video.yaml（节选）
kind: Type
metadata: {name: video.mp4, version: "1.3"}
spec:
  extends: video@1
  payload:
    - {path: video.mp4, media_type: video/mp4, required: true}
  validators:
    - name: ffprobe-facts
      run: [probe-video, /bean/payload/video.mp4]
      emits: [duration_s, width, height, fps, vcodec, audio_streams, mean_volume_db]
      fixtures:
        accept: [fixtures/video/ok-1920x1080.mp4]
        reject: [fixtures/video/truncated.mp4, fixtures/video/png-renamed.mp4]
  properties:
    has_audio: "bean.facts.audio_streams > 0"
    vertical:  "bean.facts.height > bean.facts.width"
    hd:        "(bean.facts.width < bean.facts.height ? bean.facts.width : bean.facts.height) >= 1080"
```

每个 validator 都带正样例和反样例。如果某次变更让反样例也能通过（相当于“放水”），会被自动判为高风险。

## 3. 消费者契约：`guarantees` 与 `requires`

端口用 properties 声明契约。输出端口列出它**保证**的属性，由出厂检验强制执行；输入端口列出它**要求**的属性。

```yaml
# spec/examples/registry/stations/vertical.yaml
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
```

编译期，每条边都必须满足 `requires ⊆ guarantees ∪ 类型自带属性`。这是简单的集合包含判断，所以上游一旦去掉某个保证，编译器会精确指出哪些下游端口受影响。运行期，来料检验在真实的 Bean 上计算同样的属性。

## 4. 转换：让产物自动成为物料

边两端的类型或属性不匹配时，编译器在 registry 中查找 **converter** 工位，并插入唯一一条转换链。有多条候选链时，依次按以下规则选择：无损优先 → 步数最少 → 估计成本最低 → 估计耗时最短 → 名称字典序。

[快速上手](getting-started.md)里就有这样一例：`note.raw@1 → note.text@1` 由 `note-to-text@1` 桥接。找不到转换链时，编译器不会猜测：它报告 `E-NO-CONVERTER`，并生成一条*转换需求*唤醒厂长，由厂长提议新增 converter。

## 5. 产线

```yaml
# spec/examples/registry/lines/explainer-multilang.yaml（节选）
kind: Line
metadata: {name: explainer-multilang, version: 5}
spec:
  interface:
    inputs: {brief: {type: brief.video@1}}
  trigger:
    - {on: bean, type: brief.video@1, label: "line=explainer-multilang"}
  budget: {cap_usd: 12.0}
  steps:
    explainer: {station: explainer@3, in: {brief: $inputs.brief}}
    dub_en:    {station: dub@2, params: {target_language: en},
                in: {video: explainer.video, subtitle: explainer.subtitle}}
    vertical:  {station: vertical@1, in: {video: dub_en.video}}
    to_s3:     {station: publish-s3@2, params: {bucket: media-archive, prefix: "explainers/{{ run.id }}/"},
                in: {artifact: vertical.video}}
```

产线遵守以下规则：
- 每个终端产物都必须被某个 Sink 消费，或写进 `keep`。
- 预算分层：全局 → 产线 → run → 工位 → job。
- 触发方式有四种：Bean 到达、`cron`、`every` 和手动。

### 受限的动态结构

| 构造 | 作用 | 上限 |
|---|---|---|
| `map` | 对 `list<T>` 的每一项运行一个子步骤 | `max_items`（溢出时拒绝或截断）、`max_parallel` |
| `switch` | 按 MRL 条件选择一个分支 | 必须穷尽，并带 `default` |
| `until` | 重复子步骤，直到 MRL 条件成立 | `max_iter`，用尽则 run 失败 |
| `subline` | 在本次 run 的 epoch 内运行另一条产线 | 深度上限 |
| `plan` | **planner** 工位产出 `Plan`（一个子 DAG） | 运行之前先经 V1/V2 编译，并对照 `plan_bounds` 检查 |

## 6. Epoch：变更不打断正在运行的工作

每个通过验证的 registry commit 都可以成为一个 **epoch**。每次 run 在启动时绑定一个 epoch，一直用到结束，所以结构变更从不影响正在运行的工作。回滚就是把“当前 epoch”指针移回去。多个 epoch 可以同时生效，例如当前版本与金丝雀并存。

## 7. 六层验证

| 层 | 检查内容 | 时机 | 失败后果 |
|---|---|---|---|
| **V1** 规格校验 | schema、引用、版本固定、策略 lint | 每次提交变更 | 拒绝变更 |
| **V2** 结构编译 | 边类型与契约、环、必填输入、产物去处、权限、预算、动态上限 | 提交变更时 + 每次运行前 | 拒绝变更或拒绝运行 |
| **V3** 演进校验 | 与上一版本的兼容性、所有下游消费者契约、影响面 | 提交变更时 | 拒绝，或要求配迁移 converter |
| **V4** 契约测试 | 机器 conformance、黄金 brief、converter 往返、Sink 演练 | 提交变更时 + 定时金丝雀 | 拒绝，或产生漂移告警 |
| **V5** 运行时检验 | 来料检验、出厂检验（结构 → validator → accept → judge）、Sink 回执核验 | 每个 job | 退回重试，或进死信 |
| **V6** 持续校验 | 不变量审计、漂移监测、CAS 与血缘完整性、调和 | 周期运行 + 事件触发 | 隔离、熔断、回滚、唤醒厂长 |

## 8. 四类偏移

| 偏移 | 例子 | 由谁发现 |
|---|---|---|
| 规格偏移 | 有人改了类型或端口 | V1–V3 与消费者契约 |
| 行为偏移 | 产品升级、换模型或改 prompt | 版本固定、V4 金丝雀、影子运行 |
| 语义偏移 | 产物仍能通过 schema，但质量在下滑 | 验收表达式、judge 抽样、指标基线 |
| 配置偏移 | 带外修改了容器或密钥绑定 | 调和器与不变量审计 |

## 9. 治理等级

验证流水线根据每个 ChangeSet 的 diff 确定性地算出风险等级，任何人都不能降级，厂长也不能。

| 等级 | 典型变更 | 要求 |
|---|---|---|
| T0 | 重试、隔离 | 自动执行 |
| T1 | 模板调优、新增 converter、类型 minor 升级 | 验证 + 影子运行 |
| T2 | 接入或升级机器、新建产线、类型 major 升级 | 再加金丝雀 + 由另一个 runtime 交叉复核 |
| T3 | 新增 Source/Sink、出网或凭据绑定、修改策略、退役 | 再加人工审批 |
| T4 | 宪法 | 只能由人修改 |

宪法（L0）是最终锚点。机器和 agent 可以改变其他一切，但只能通过经过验证、可复核、可回滚的 ChangeSet。
