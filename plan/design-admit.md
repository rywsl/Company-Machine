# 设计说明：跨端口来料约束（M14）

> 背景：一条产线的启动物料里，既有系统里已封存的 Bean，也有向真实世界拉来的物料。两者在下游工位汇合（装配）。
> 讨论中的例子：现实世界装好了一台车，需要给它刷一份定制的软件；这个车型只接收一部分软件。问题是现有的类型系统能不能表达“只接收一部分”。
> 规范见 `spec/system/admit.md`。

## 1. 约束的四种形状

| 需求 | 例子 | 规划时的状态 |
|---|---|---|
| 上界：只收某一族 | 只收平台 A 的固件 | 已有。`fw.platform-a@1 extends fw@1`，端口写父类型，所有子类型都能接；`list`、`ref` 协变（`kernel/types.mrl`） |
| 交集：同时满足几个维度 | 平台 A、ABI ≥ 3、已签名 | 已有，但不能带参数。命名属性 + `requires` 是细化类型；每个常量要单独写一个属性 |
| 依赖：与另一份输入匹配 | `固件.target_model == 车.model` | 没有。来料检验只看单个端口 |
| 并集：两族任选其一 | 两家供应商的固件 | 没有。`extends` 只有一个父类型，已经挂在不同父类型下的两族无法事后归并 |

协变本身不是缺口：Bean 不可变，`list`、`ref` 已经协变。缺的是第三行。

## 2. 决定

**做 `admit`**：工位级、跨端口的 MRL 布尔表达式，环境是全部输入加工位参数，在来料检验时、建 job 之前求值。

- 它解决第三行。也覆盖第二行里“按常量收窄”的需要：`inputs.firmware.facts.target_model == params.model`，一个工位一个参数，不必为每个车型写属性。
- 复用现有机制：表达式位置（抽取 → 快照编译 → 求值）、`accept` 的 `inputs` 环境、`INTAKE_REJECTED` 的问题对象。kernel 只多一条规则，宿主多一个警告码。
- 出厂 `accept` 能做同样的判断，但那是事后检验：工位已经跑完。`admit` 在开工前拒收。

**准入只信 facts**。meta 是生产者自报的，所以有 `W-ADMIT-META`。

## 3. 暂缓的方案与触发条件

| 方案 | 代价 | 什么时候再做 | 在那之前怎么办 |
|---|---|---|---|
| 带参数的命名属性 `targets(model: "X")` | 属性引用从字符串变成“名字 + 参数”：`E-PROPERTY`、`E-CONTRACT` 的覆盖、converter 的 `preserves`、转换解析里的属性集合、一致性用例都要改 | 需要上游的 `guarantees` **静态**覆盖一个带常量的约束，或者需要它穿过 converter 保持下来 | `admit` + 工位参数（运行期检查） |
| 参数化类型 `fw<model>` | `texpr` 文法、`tsub` 加类型参数与方差声明、转换路径搜索、抽取映射 | 同一族的不同参数需要不同的转换路径 | 名义子类型 + `admit` |
| 并集 `fw.a@1 \| fw.b@1` | 边解析出现歧义（走哪条 converter）；端口类型不再是单个类型 id | 一个端口必须接两族 payload 不同的物料 | 两个工位 + `switch` 按 facts 分流 |
| `bundle{...}`、`ref<T>` 的检验 | `gate.md` 的 `GATE_UNSUPPORTED`；宽度/深度子类型 | 装配结果需要按字段做子类型 | 装配结果建成原子类型（例如 `vehicle.flashed@1`），关键信息放进 facts |
| 产线接口级的 admit | `start_run` 要编译、求值一套新位置 | 需要在任何 Source 运行之前就拒掉一对不匹配的接口输入 | 写在第一个消费它们的工位上 |

## 4. 例子

`tests/acceptance/m14/admit_world.py` 就是这个场景：

- 接口里是一台已封存的车（`vehicle.build@1`），Source `pull-fw@1` 按车的 facts 现拉固件（`fw.image@1`，来源证明 `external: true`）。
- `flash@1` 同时绑定 `$inputs.car` 与 `pull.firmware`，两边都 `DONE` 才提交；提交时 `admit` 核对车型、平台与 ECU 配额，不匹配就拒收，节点 `FAILED`，不建 job。
- 产物 `vehicle.flashed@1` 是普通类型的新 Bean，下游可以继续接。
