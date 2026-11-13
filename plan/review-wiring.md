# 接线审查（2026-11-13）

> 设计方在阶段门 M9 通过之后、编写多语言文档时按文档实际运行产线所做的审查。**受保护，只能由人修改。**
> 纠偏任务是 **M10-01 至 M10-03**，规范 `spec/system/wiring.md`，验收 `tests/acceptance/m10/`（23 个）。
> 本文件的编号（W-xx）被规范与任务卡引用，不要重新编号。

---

## 0. 结论

阶段门 M0–M9 全部通过：2026-11-13 的 `gate M9` 记录 pytest 1448 个、零失败、零跳过。M9 闭环的动态结构、Plan、B 形态网关、生产后端已经在生产路径上运行。

按 `docs/en/getting-started.md` 的演练运行 `note-pipeline@1`，第一次 `SUCCEEDED`；**用同样的输入再运行一次，publish 节点报 `IDEMPOTENCY_CONFLICT`，run `FAILED`**。追查发现同一类问题——能力只被验收直接调用，生产路径没有接上：

1. **Sink 回执**：`Runner` 只做 `connectors.md` 5.2 的占用，之后像 transform 一样收货、检验、封存。`ReceiptStore.publish`、`settle` 在 `src/mao` 里没有调用者，`connectors.verify` 是 `del ctx, snapshot; return True` 的桩。回执永远停在 `claimed`，所以同一物料不能再发布，V5 的回执核验从未发生。
2. **`factoryctl receipt`** 没有实现；夹具工具 `publish_copy.py` 的文档字符串写着“代替尚未安装的 factoryctl receipt”。
3. **命令**：`connectors.md` 3.3 的 `factory receipt ls/show/verify/void`、`factory source run`，以及 `cache.md` 第 3 章的 `factory run STATION@N --cache` 没有实现；`factory run` 注册成了永远返回 2 的占位。
4. **缓存**：`LineRunner` 有 `cache` 参数，但 `line run`、`trigger process`、`serve`、控制台、治理重建都不传，`cache_hit` 在生产路径上永远为假。
5. **网关**：`trigger process`、控制台、治理流水线默认的 `contracts`/`rebuild`、影子运行仍然 `Runner(ctx, snapshot)`，B 形态工位在这些路径上 `FORM_UNSUPPORTED`。

根因两边都有。实现方用桩和占位通过了验收（W-01 至 W-07）。设计方 M2-10 / M2-11 的验收直接调用 `ReceiptStore` 与 `Runner`，没有一条经过 Sink 的执行路径或命令行；`commands.md` 漏列了 `receipt`、`source`、`run`；`governance.md`、`corrections.md` 的默认写法本身就是 `Runner(ctx, snap)`；`closure.md` 5.3 只列举了部分构造点；`connectors.md` 5.3 要求核验失败时 `DEAD`，而 `runner.md` 的状态表没有 `GATING → DEAD`（W-08）。

---

## 1. 审查方法

- 搜索调用者：`ReceiptStore.publish`、`ReceiptStore.settle`、`connectors.verify`、`JobCache(` 在 `src/mao` 中的生产调用者为零；`Runner(`、`LineRunner(` 的构造点逐一检查是否带网关与缓存。
- 在 `registry-m2` 的副本上写了 8 个 Sink 工位（操作工是测试运行时写出的标准库脚本）与 2 条以测试目录为发布目标的产线（`tests/acceptance/m10/wiring_world.py`），夹具经 `Runner.run`、`mao.cli.main` 与守护进程实际运行。
- 新验收对 M9 结束时的实现逐条运行：23 个全部失败，失败原因与下表一致。
- 设计方在一个不提交的原型上实现了 `wiring.md` 5.1 与 5.3：23 个新验收全部通过，M9 阶段门的 1448 个测试也全部通过，说明规范与既有验收没有冲突。

---

## 2. 问题清单

| 编号 | 位置 | 现象 | 后果 | 处置 |
|---|---|---|---|---|
| W-01 | `src/mao/runner.py` Sink 分支；`src/mao/connectors.py` `verify` | 占用后不取回执、不发布、不核验；`verify` 是桩 | 回执停在 `claimed`；同一物料再发布 `IDEMPOTENCY_CONFLICT`；回执核验从未发生 | M10-01，`wiring.md` 5.1 |
| W-02 | `src/mao/runner.py` `_execute` | 只有退出码非 0 判 `SINK_OUTCOME_UNKNOWN`；超时与 `factoryctl fail` 走普通重试 | 发布可能已部分发生却被重跑；重跑时又撞上冲突 | M10-01，`wiring.md` 4.3、5.1 第 2 步 |
| W-03 | `src/mao/ctl/factoryctl.py` | 没有 `receipt` 子命令 | agent 操作工无法按 `connectors.md` 3.2 留回执 | M10-01，`wiring.md` 4.1 |
| W-04 | `src/mao/commands/` | 没有 `receipt`、`source` 命令；Source 产物的来源证明没有 `external` | 回执无法复核或作废；Source 只能在代码里调用 | M10-02，`wiring.md` 5.2 |
| W-05 | `src/mao/commands/job.py` | `factory run` 是返回 2 的占位 | `cache.md` 的单工位缓存不可用 | M10-02，`wiring.md` 5.2 第 6 条 |
| W-06 | `commands/line.py`、`commands/trigger.py`、`daemon.py`、`console.py`、`governance/changesets.py` | `LineRunner` 不带 `JobCache` | 缓存在生产路径上永不命中，重建与重跑重复付费 | M10-03，`wiring.md` 5.3 |
| W-07 | `commands/trigger.py`、`console.py`、`governance/changesets.py`、`governance/shadow.py`、`runner.py` | 直接 `Runner(...)` / `LineRunner(...)` | 这些路径上 B 形态工位不可用；新调用点容易再漏 | M10-03，`wiring.md` 5.3 |
| W-08 | 设计方：`tests/acceptance/m2/test_m2_10_sink.py`、`test_m2_11_source.py`；`commands.md`；`governance.md`、`corrections.md`；`closure.md` 5.3；`runner.md` 状态表 | 验收只测存储层；命令清单漏项；默认写法不带网关；构造点列举不全；缺 `GATING → DEAD` | 桩与占位能通过阶段门 | 本次新增 `wiring.md` 与 `tests/acceptance/m10/`；M2 两个文件不改（结论仍成立） |

---

## 3. 设计方同时修改的受保护文件

| 文件 | 修改 |
|---|---|
| `spec/system/wiring.md` | 新增：W-01 至 W-08 的规定与验收 |
| `plan/review-wiring.md` | 本文件 |
| `tests/acceptance/m10/` | 新增夹具 `wiring_world.py` 与 3 个验收文件（23 个测试） |
| `spec/system/connectors.md`、`cache.md`、`commands.md`、`governance.md`、`corrections.md`、`closure.md`、`runner.md` | 文首指向 `wiring.md` |
| `plan/tasks.yaml`、`plan/gates.yaml`、`plan/milestones.md`、`plan/tasks/M10-0*.md`、`spec/system/README.md` | 任务 M10-01 至 M10-03 与阶段门 M10（继承 M9 的全部检查） |

---

## 4. 给实现方

1. 读 `spec/system/wiring.md` 全文，再读它引用的 `connectors.md` 3–5 章、`cache.md` 3 与 5 章、`factoryctl.md`、`commands.md` 3 章。
2. 按 M10-01 → M10-02 → M10-03 的顺序做。M10-02 的 `receipt verify` 用 M10-01 的真实 `verify`；M10-03 的缓存验收需要 Sink 能重复发布。
3. 以下任何一种都算任务失败，即使验收通过：在实现里出现验收或夹具里的工位名、配方名、路径、文件名作为常量或分支条件；在宿主上检查目标文件代替核验沙箱；让 `factory run` 命中缓存时仍创建 job；修改夹具或替身让检查通过。
4. 验收与规范矛盾时，照 `plan/README.md` 第 5 节写日志并 `task block`。
