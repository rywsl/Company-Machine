# 验收指标体系

> 本文件规定：
> - 每一级验收看哪些指标；
> - 每个指标怎么量（具体命令）；
> - 门槛是多少；
> - 证据记在哪里。
>
> 门槛的权威来源是 `plan/tasks.yaml`（任务级）和 `plan/gates.yaml`（提交前检查与阶段门）。本文件解释它们，两者不一致时以 yaml 为准，并把不一致当作缺陷报告。

## 1. 三级验收

| 级别 | 什么时候 | 命令 | 通过条件 |
|---|---|---|---|
| **L1 任务** | 每个任务完成时 | `harness accept <ID>` | ① 受保护文件与一致性用例未被改动；② 任务的全部验收项通过；③ 越界改动记入证据 |
| **L2 阶段门** | 里程碑的全部任务完成后 | `harness gate <M>` | ① 本里程碑全部任务为 `done`；② 前置阶段门已通过；③ 受保护文件未被改动；④ 阶段门检查项全部通过 |
| **L3 持续** | 每次提交前；人审查时 | `harness check`（日常可用 `--fast`） | `gates.yaml` 的 `check` 列表全部通过 |

- L1 只证明“这个任务做完了”。
- L2 证明“这个里程碑作为整体成立”：它会重新运行全部测试，包括前面任务的测试，所以后面的任务不能破坏前面的成果。
- L3 保证任何时刻工作区都处于可交接的状态。

## 2. 指标维度

每个维度都写明：定义、度量命令、门槛、证据位置。

### 2.1 正确性：行为符合规范

| 指标 | 度量 | 门槛 |
|---|---|---|
| 验收测试 | `harness pytest tests/acceptance/<m>/<文件>` | `failed == 0 && errors == 0 && skipped == 0 && passed >= N`。N 写在任务的验收项里，等于该文件的测试数 |
| MRL 一致性 | `mrl conformance --json` | `failures` 为空，且 `total` 不低于上一个阶段门记录的值（M0：702） |
| 场景 | `harness scenario run --all` | 全部通过 |
| 示例 Registry | `mrl registry check spec/examples/registry --strict` | 退出码 0：零 error、零 warning |

为什么 `skipped == 0`：验收测试在模块不存在时整体跳过。只看“没有失败”，一个空实现也能通过。

### 2.2 完整性：规范的每一部分都有实现，也都有验证

| 指标 | 度量 | 门槛 |
|---|---|---|
| 诊断码覆盖 | `mrl coverage --json` | `unexercised_codes` 为空：每个诊断码至少被一个用例期待 |
| 规则覆盖 | 同上 | `unfired_rules` 为空：内核的每条诊断规则至少触发过一次 |
| 文档一致性 | `harness doccheck` | 通过。检查内容：MRL 错误码与标准库函数都写进了 `mrl-1.0.md`；诊断码与输入关系都写进了 `factory-profile.md`；`spec/system` 文档按模板组织；登记的控制面错误码都写进了规范；任务卡存在、引用的规范存在、每个里程碑都有阶段门 |
| 验收清单落地 | 阶段门检查项 | 规范第 7 章列出的每个测试文件都存在，并被阶段门运行 |

### 2.3 不回退：已经成立的东西不会悄悄失效

| 指标 | 度量 | 门槛 |
|---|---|---|
| 受保护文件 | `harness lock --verify` | 与 `plan/protected.lock` 一致 |
| 一致性用例只增不改 | 同上（逐个用例比对规范 JSON 的哈希） | 已登记的用例没有被修改或删除 |
| 测试总数 | `harness pytest tests` 的 `total` | 不低于阶段门写明的下限 |
| 全量测试 | `harness pytest tests` | `failed == 0 && errors == 0` |

### 2.4 质量：代码可读、可维护、守规矩

| 指标 | 度量 | 门槛 |
|---|---|---|
| lint | `ruff check src tests tools` | 零问题 |
| 格式 | `ruff format --check src tests tools` | 零差异 |
| 静态规则 | `tests/test_codebase_policy.py` | 通过。规则包括：不用 `shell=True`、`eval`；只在 `base.py` 里接触真实时间与随机数；不新增依赖；不吞异常；显式指定编码；factoryctl 兼容 Python 3.8 |
| 单元测试 | `harness pytest tests/system` | `failed == 0 && errors == 0` |
| 越界改动 | `state.json` 中任务的 `outside_paths` | 为空，或由人在阶段门审查后接受 |

### 2.5 确定性：同样的输入，逐字节相同的输出

| 指标 | 度量 | 门槛 |
|---|---|---|
| Registry 检查的确定性 | `tests/test_factory.py` 中的确定性测试：文件顺序打乱后，检查输出不变 | 通过 |
| 转换解析与库顺序无关 | `tests/test_factory.py` | 通过 |
| 控制面输出 | 验收测试使用 `FakeClock`、`CounterIds` 和金样文件（Brief、检验报告） | 通过 |
| 重复运行 | 阶段门会再次运行全部测试 | 与任务验收时结果一致 |

### 2.6 安全：安全基线不可降级

| 指标 | 度量 | 门槛 |
|---|---|---|
| 容器参数 | `DockerProvider.build_run_argv` 的单元测试（`test_m1_04_sandbox.py`） | 必含 `--cap-drop ALL`、`--read-only`、非 root 用户、`--security-opt no-new-privileges`；不得出现 docker.sock、`--privileged`、host 网络 |
| 只读输入、无网络 | Docker 端到端测试（`test_m1_10_e2e.py`，`docker` 标记） | 通过，且 `MAO_REQUIRE_DOCKER=1` 下零跳过 |
| 静态规则 | `tests/test_codebase_policy.py` | 通过 |
| 宪法 | `harness lock --verify` + 示例 Registry 自带宪法副本的一致性测试 | 通过 |

### 2.7 端到端：从 M1 起

| 指标 | 度量 | 门槛 |
|---|---|---|
| Local 端到端 | `test_m1_10_e2e.py` 中不带 `docker` 标记的测试 | 全部通过 |
| Docker 端到端 | 同一文件中带 `docker` 标记的测试，设置 `MAO_REQUIRE_DOCKER=1` | 全部通过，零跳过 |

## 3. 各里程碑的门槛

| 里程碑 | 阶段门要点（完整列表见 `gates.yaml`） |
|---|---|
| **M0** MRL 与结构编译 | 一致性 `total >= 702` 且零失败；覆盖率 29/29 诊断码、79/79 规则；示例 Registry `--strict` 通过；4 个场景通过；全量 pytest 零失败且 `total >= 726` |
| **M1** 单工位跑通 | M0 的全部检查；`tests/acceptance/m1` 在 `MAO_REQUIRE_DOCKER=1` 下 `failed == 0 && errors == 0 && skipped == 0`，且 `passed` 不低于各文件测试数之和；单元测试零失败 |
| **M2–M4** | 每个里程碑：前一阶段门的全部检查，加上本里程碑 `tests/acceptance/mN` 零失败、零跳过。具体数目在该里程碑开始前写入 `gates.yaml` |

阶段门一旦通过，它的检查项会被下一个里程碑原样继承：`requires_gates` 加上重复列出。门槛只升不降。

## 4. 证据

- **记录**：`harness accept` / `harness gate` 把每个检查项的命令、退出码、耗时、stdout 的 SHA-256 和末尾 12 行写进 `plan/state.json`，同时记下当时的 commit 和工作区是否有未提交改动（`dirty`）。
- **复核**：人复核时，在干净的检出上重新运行 `harness gate <M>`。证据只是线索，**能重现才算数**。
- **审查越界改动**：阶段门会列出每个任务的 `outside_paths`。

## 5. 防作弊设计

实现者可能（不一定有意）用各种方式让指标“看起来”通过。逐一对应的防线如下：

| 手段 | 防线 |
|---|---|
| 修改验收测试或夹具 | 受保护文件 + PreToolUse hook；`accept`/`gate` 先跑 `lock --verify` |
| 测试因为导入失败被整体跳过 | 验收要求 `skipped == 0` |
| 没有 Docker，Docker 测试被跳过 | 阶段门设置 `MAO_REQUIRE_DOCKER=1`，Docker 不可用时直接失败 |
| 修改已有的一致性用例来迁就实现 | 用例逐个登记哈希，只增不改 |
| 收集阶段出错导致测试变少 | `passed >= N`、`total >= N` 的下限 |
| 特判测试输入 | 验收测试使用多组输入，并有反例；人在阶段门抽查 |
| 绕过 MRL、自己解析表达式 | 静态规则禁止 `eval`；验收测试覆盖 MRL 的错误语义（如缺字段时失败关闭） |
| 改规范来迁就实现 | 规范文件受保护 |

## 6. 如何新增或修改指标（人或计划者）

1. 先在本文件写明：定义、度量、门槛、证据。
2. 再改 `gates.yaml` 或 `tasks.yaml`。
3. 需要新的度量命令时，命令的 stdout 必须是 JSON，好让 `assert` 引用。
4. 运行一次 `harness gate <M>`，确认新指标确实会在违规时失败：
   - 临时制造一次违规，确认检查项失败；
   - 再恢复。
5. `lock --update`，审查，提交。
