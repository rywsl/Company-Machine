# 计划与工作流（plan/）

> 本目录写给**执行任务的模型**和**审查它的人**。
> - 设计依据：`docs/architecture.md`（为什么）、`spec/`（是什么、怎么做）。
> - 本目录管“按什么顺序做、做到什么程度算完成、怎么证明”。

## 0. 你的角色

你是**实现者**，不是设计者。

- **你要做的**：按任务卡实现规范里已经定下的东西，并用 harness 证明它完成了。
- **你不做的**：发明接口、改变规范、调整验收标准。
- **规范有歧义或行不通时**：停下来记录，不要猜（见第 5 节）。

判断一件事能不能做，只看三样东西：
1. 任务卡 `plan/tasks/<ID>.md`；
2. 任务卡引用的规范章节；
3. 本文件的禁止事项（第 6 节）。

三者冲突时，以禁止事项为准；其次是规范；任务卡排最后。

## 1. 文件一览

| 文件 | 内容 | 谁能改 |
|---|---|---|
| `plan/README.md` | 本文件：工作流 | 人 |
| `plan/milestones.md` | 里程碑：目标、范围、退出条件 | 人 |
| `plan/acceptance.md` | 验收指标体系：每一级验收看什么、怎么量 | 人 |
| `plan/risks.md` | 风险登记册，附错误示范与正确示范 | 人 |
| `plan/tasks.yaml` | 任务清单：依赖、允许修改的路径、验收项 | 人 |
| `plan/gates.yaml` | 提交前检查（`check`）与各里程碑的阶段门（`gates`） | 人 |
| `plan/tasks/<ID>.md` | 任务卡 | 人 |
| `plan/scenarios/*.yaml` | 可执行的场景（架构文档第 13 节的推演） | 人 |
| `plan/protected.lock` | 受保护文件与一致性用例的基准哈希 | 人（`lock --update`） |
| `plan/state.json` | 任务与阶段门的状态和证据 | harness 自动写入 |
| `plan/journal/` | 工作日志、受阻记录、问题报告 | 实现者 |

## 2. 每次开始工作

在仓库根目录依次运行：

```bash
uv run python tools/harness.py doctor        # 环境：Python、uv、git、docker、受保护文件
uv run python tools/harness.py status        # 进度
uv run python tools/harness.py task next     # 下一个可以开始的任务
uv run python tools/harness.py task show <ID>
```

1. **`doctor` 报错**：先解决环境问题。解决不了就写日志，停止工作（第 5 节）。
2. **读完整张任务卡和它的“必读”章节，再动手。** 不要只读标题就开始写代码。
3. **开始任务**：运行 `uv run python tools/harness.py task start <ID>`。
   - harness 会记下当时工作区的基准，验收时据此判断你改了哪些文件。
   - 有依赖没完成时，harness 会拒绝开始。

## 3. 做任务

1. **先运行验收测试，看清失败的样子。**
   ```bash
   uv run python tools/harness.py pytest tests/acceptance/m1/test_m1_03_warehouse.py
   ```
   在模块实现之前，整个文件会被跳过（`importorskip`）。模块存在之后，失败的测试就是你的待办清单。
2. **按规范实现。** 规范第 3 章“接口”给出签名，第 5 章“行为”给出算法，第 6 章“错误”给出错误码。
   - 签名、错误码、文件格式必须与规范逐字一致。
   - 新的错误码登记在 `src/mao/errors.py` 的 `CODES` 表里。
3. **写自己的单元测试**，放在 `tests/system/`，文件名以 `test_` 开头。验收测试是最低要求，不是全部：你写的每个非平凡函数都应当有单元测试。
4. **经常运行快速检查**：
   ```bash
   uv run python tools/harness.py check --fast
   ```
   它会运行 lint、格式、受保护文件、文档一致性、MRL 一致性、示例 Registry 和场景。全量 pytest 不在其中。
5. **格式化**：`uv run ruff format src tests` 和 `uv run ruff check --fix src tests`。

## 4. 验收

```bash
uv run python tools/harness.py accept <ID>
```

harness 按顺序做三件事：

1. 校验受保护文件和一致性用例。有任何改动，直接拒绝验收。
2. 列出你改动的文件中，不在任务 `paths` 范围内的那些。它们会记入证据，由人在阶段门审查。
3. 逐条运行任务的验收项。全部通过，任务才标记为 `done`。证据（命令、退出码、输出摘要）写入 `plan/state.json`。

验收没通过时：

- 读失败输出。验收测试的文档字符串写明了它验证的规范章节，回去对照规范。
- 修改**实现**，不是修改测试。
- 同一个验收项连续 **5 次**因为同一个原因失败，就停下来，按第 5 节处理。不要无限重试。

里程碑的全部任务完成后，运行 `uv run python tools/harness.py gate <M>`。

## 5. 卡住时怎么办

出现以下情况，停止当前任务：

- 规范有歧义，或者有两种都说得通的理解；
- 规范和验收测试互相矛盾，或者你确信某个受保护文件写错了；
- 环境问题解决不了，例如没有 Docker、镜像构建失败；
- 同一个验收项连续 5 次失败。

处理步骤：

1. 在 `plan/journal/` 新建文件 `YYYY-MM-DD-<ID>-<简短主题>.md`，按下面的模板写：

   ```markdown
   # <ID> 受阻：<一句话>

   - 时间：2026-11-10
   - 任务：<ID> <标题>
   - 现象：<命令、完整的错误输出（截取关键 30 行）>
   - 规范依据：<文件与章节>，原文：“……”
   - 我的理解：<A 理解 / B 理解，各自会导致什么>
   - 已经尝试：<做过什么、结果如何>
   - 建议：<你认为应该怎么改，改哪个文件>
   ```

2. 运行 `uv run python tools/harness.py task block <ID> --reason "<一句话>"`。
3. 去做另一个**不依赖它**的任务（`task next` 会跳过受阻任务的下游）。没有这样的任务，就结束本次工作。

受阻不是失败。猜错之后带着错误继续往下做，代价远比停下来大。

## 6. 禁止事项

违反任何一条，都视为任务失败，即使验收通过。

1. **禁止修改受保护文件。** 清单在 `plan/protected.lock` 的 `patterns` 中，主要有：
   - 验收测试、替身、夹具：`tests/acceptance/**`、`tests/fakes/**`、`tests/fixtures/**`、`tests/conftest.py`；
   - 规范：`spec/**/*.md`、`spec/schemas/*.json`、宪法、示例 Registry；
   - 计划与 harness：`plan/*.yaml`、`plan/*.md`、`plan/tasks/*.md`、`plan/scenarios/*.yaml`、`tools/harness.py`、`tools/hooks/*.py`；
   - 其他：`pyproject.toml`、`AGENTS.md`、`CLAUDE.md`、`.claude/settings.json`、`docs/adr/*.md`。

   PreToolUse hook 会拦截对它们的编辑；绕过 hook（例如用脚本写文件）同样禁止，`lock --verify` 会发现。
2. **禁止运行 `tools/harness.py lock --update`。** 只有人可以执行。
3. **禁止修改或删除已有的 MRL 一致性用例**（`spec/mrl/conformance/**`）。可以新增用例。
4. **禁止为了让测试通过而削弱它们**，包括以下任何一种：
   - 在实现里特判测试输入；
   - 给测试加 `skip` / `xfail`；
   - 把被测对象本身 mock 掉；
   - 吞掉异常；
   - 放宽断言。
5. **禁止新增第三方依赖**（README D2），禁止修改 `pyproject.toml`。
6. **禁止改变规范里的签名、错误码、文件格式、退出码。** 觉得规范该改，走第 5 节。
7. **禁止做任务卡范围以外的事**，包括“顺手”重构、提前实现后续里程碑的功能。想法写进日志。
8. **禁止破坏性的 git 操作**，例如 `git reset --hard`、`git checkout .`、`git clean -fd`、`git push --force`。要恢复某个文件，只恢复那一个：`git checkout -- <file>`。
9. **提交与推送只在人明确要求时进行。**
10. **禁止在控制面里调用任何 LLM API**，禁止挂载 docker.sock，禁止 `shell=True`（README D10、D11、3.7）。

## 7. 文件格式参考

### 7.1 `tasks.yaml`

```yaml
tasks:
  - id: M1-03                      # 唯一；格式 M<里程碑>-<两位序号>
    milestone: M1
    title: Warehouse：CAS、Bean、封存、血缘
    card: plan/tasks/M1-03.md      # 任务卡；标题行必须包含 id
    deps: [M1-01]                  # 依赖的任务全部 done 才能 start / accept
    spec: [spec/system/warehouse.md]
    paths:                         # 允许修改的文件（glob，pathlib 语义：** 跨目录）
      - src/mao/warehouse.py
      - tests/system/**
    accept:                        # 验收项：全部通过才算完成
      - id: acceptance
        run: uv run python tools/harness.py pytest tests/acceptance/m1/test_m1_03_warehouse.py
        assert: out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 20
```

验收项（`gates.yaml` 中的检查项格式相同）的字段：

| 字段 | 含义 | 默认 |
|---|---|---|
| `id` | 名字 | `check-N` |
| `run` | 命令，按 shell 规则切分，但**不经过** shell | 必填 |
| `expect_exit` | 期望的退出码 | 0 |
| `assert` | MRL 布尔表达式。可用的名字有三个：`out`（stdout 解析成的 JSON，不是 JSON 时为 `null`）、`stdout`（字符串）、`exit`（整数） | 无 |
| `timeout` | 秒 | 900 |
| `slow` | `check --fast` 时跳过 | false |
| `env` | 额外的环境变量，例如 `{MAO_REQUIRE_DOCKER: "1"}` | 无 |

### 7.2 `harness pytest` 的输出

stdout 只有一行 JSON，pytest 自己的输出都在 stderr：

```json
{"exit": 0, "total": 24, "passed": 24, "failed": 0, "errors": 0, "skipped": 0, "failures": [], "skips": []}
```

### 7.3 `state.json`

由 harness 写入，不要手工编辑。它记录每个任务的：
- `status`：`todo` / `in_progress` / `blocked` / `done`；
- 开始时的基准；
- 验收证据：每个检查项的命令、退出码、耗时、输出摘要和末尾 12 行；
- 越界改动的文件。

阶段门的结果也记在这里。

### 7.4 场景（`plan/scenarios/*.yaml`）

```yaml
scenario: evolution-13-2
milestone: M0
kind: registry          # 目前实现的种类：registry（对 Registry 打补丁，再检查）
base: example           # example | empty | 相对路径
steps:
  - name: ...
    reset: false        # true：这一步从 base 重新开始；否则在上一步的结果上累积
    patch: [...]        # 补丁格式同 MRL factory 一致性用例（factory-profile 第 8 章）
    expect: {...}       # clean / ok / codes / diags / absent / resolutions
```

## 8. 退出码

| 退出码 | harness | `mrl` | `factory` |
|---|---|---|---|
| 0 | 通过 | 成功 | 成功 |
| 1 | 未通过 | 检查或测试失败 | 业务失败 |
| 2 | 用法错误 | 用法错误 | 用法错误 |
| 3 | — | 静态错误 | Registry 不合法 |
| 4 | — | 运行期错误 | 环境或设施错误 |
| 5 | — | 内部错误 | 内部错误 |

## 9. 人的职责

- 审查阶段门报告里的越界改动，以及受阻任务的日志，再决定改规范、改测试还是改任务。
- 修改受保护文件后运行 `lock --update`，并把新的 `plan/protected.lock` 和改动一起审查、提交。
- 批准 ADR（`docs/adr/`）。
- 决定何时提交、何时推送。
