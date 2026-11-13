[English](../en/development.md) | **简体中文** | [日本語](../ja/development.md)

# 开发流程

Machine Agent Org 采用明确的**设计方 / 实现方**分工，并由确定性的 harness（`tools/harness.py`）强制执行，任何一方都不能悄悄改动验收标准。

## 1. 角色

| 角色 | 负责 | 不能做 |
|---|---|---|
| **设计方** | 规范（`spec/`）、计划与任务卡（`plan/`）、验收测试（`tests/acceptance/`）、审查 | 实现生产代码 |
| **实现方** | `src/`、系统测试（`tests/system/`）、日志 | 修改规范、验收测试或验收标准；发明接口；提前做后续里程碑 |
| **人** | 宪法、ADR、**重新登记受保护文件**（`harness lock --update`） | — |

受保护文件按 glob 登记在 `plan/protected.lock` 中，并用 SHA-256 固定：
- PreToolUse hook（`.claude/settings.json`）拒绝 agent 编辑这些文件；
- `harness lock --verify` 能发现任何改动。

实现方遇到矛盾时，停下来写 `plan/journal/`，并执行 `harness task block`。设计方修正受保护文件后，由人重新登记 lock。

## 2. harness 工作循环

```bash
uv run python tools/harness.py doctor            # 环境
uv run python tools/harness.py status            # 里程碑、任务、阶段门
uv run python tools/harness.py task next         # 第一个依赖已完成的任务
uv run python tools/harness.py task show M9-01   # 任务卡与必读章节
uv run python tools/harness.py task start M9-01
# …… 实现 ……
uv run python tools/harness.py accept M9-01      # 运行任务的验收项并记录证据
uv run python tools/harness.py gate M9           # 阶段门：本里程碑的全部检查（含继承的检查）
```

其他子命令：
- `doccheck`：规范第 7 章的测试表必须与测试函数一致。
- `check`：运行单项检查。
- `scenario`：运行 `plan/scenarios/` 中的脚本化场景。
- `pytest`：运行 pytest 并输出 JSON 汇总。请始终用它代替直接运行 `pytest`，因为它会设置 `tests` 包的导入路径。
- `lock --verify`：对照 lock 检查受保护文件。

证据（命令、退出码、输出摘要、git commit）写入 `plan/state.json`。

## 3. 里程碑

```mermaid
flowchart LR
    M0["M0<br/>结构编译"] --> M1["M1<br/>单工位跑通"] --> M2["M2<br/>产线与外部世界"] --> M3["M3<br/>治理与厂长"] --> M4["M4<br/>动态结构与生产化"]
    M4 --> M5["M5<br/>自治闭环"] --> M6["M6<br/>真实接入"] --> M7["M7<br/>运维"] --> M8["M8<br/>交付审查纠偏"] --> M9["M9<br/>闭环审查纠偏"]
```

每个里程碑结束时，系统都处于可用、可交接的状态；每个阶段门都继承前一个阶段门的全部检查。

M5、M8、M9 是**纠偏**里程碑，各自来自一次设计方审查（`plan/drift.md`、`plan/review-m5-m7.md`、`plan/review-closure.md`）。审查发现，有些能力能通过自己的测试，却没有接入生产路径。由此得出的教训已经成为编写验收测试的规则：**驱动生产入口，替身必须检查行为。** 例如：假 S3 重新计算 SigV4，假 `psql` 记录每一次调用，假 `factoryctl` 转发真实的方法与路径。

## 4. 测试

| 层 | 位置 | 内容 |
|---|---|---|
| 验收 | `tests/acceptance/m0…m9/` | 每个任务一个文件，在规范第 7 章列出；受保护 |
| 系统 | `tests/system/` | 实现方负责的单元与集成测试 |
| 一致性 | `spec/mrl/conformance/`、`tests/test_conformance.py` | MRL 表达式、规则与 factory 用例；已有用例只增不改 |
| 规则 | `tests/test_codebase_policy.py`、`tests/test_hooks.py` | 针对 `src/mao` 的静态规则（禁止给导入对象打补丁、禁止读当前目录、禁止跨模块访问私有成员……） |

测试替身在 `tests/fakes/`：
- `fake_agent.py`：脚本化的 Claude Code 仿制品，行为来自任务 Bean 的 `task.json`；
- `fake_llm.py`：现场测试层用的模型上游；
- `tools/`：夹具工具。

夹具 registry 是 `tests/fixtures/registry-m1` 和 `registry-m2`。Docker 测试层需要先运行 `uv run python tools/build_test_images.py`。没有 Docker 时这些测试会跳过；设置 `MAO_REQUIRE_DOCKER=1` 可改为失败。阶段门要求零跳过。

## 5. 代码规则

以下规则来自 `spec/system/README.md`（决定 D1–D14）和 `plan/README.md` 第 6 节：

- Python ≥ 3.12。运行时依赖只允许标准库、`pyyaml`、`jsonschema` 和本仓库的 `mrl`；新增依赖需要 ADR。Docker 通过子进程调用命令行，SQLite 直接用 `sqlite3`。
- 禁止 `shell=True`。禁止调用 LLM API：智能只来自操作工 CLI。
- **MRL 是唯一的表达式与模板语言。** Spec 中的模板用 `mrl.expr` 编译；渲染 Spec 内容时禁止使用 `eval`、Jinja、`str.format`。
- 库代码的路径来自 `Context.root` 或调用方参数，禁止读当前目录。
- 运行期 SQL 必须能在 SQLite 与 PostgreSQL 之间移植。SQLite 特有写法只允许出现在 `db.py`、`backends.py`、`backup.py`。
- `uv run ruff check src tests tools` 和 `uv run ruff format --check` 必须通过；行宽 110。

## 6. 延伸阅读

| 主题 | 文档 |
|---|---|
| 工作流规则、日志格式、禁止事项 | [`plan/README.md`](../../plan/README.md) |
| 里程碑与退出条件 | [`plan/milestones.md`](../../plan/milestones.md)、[`plan/gates.yaml`](../../plan/gates.yaml) |
| 跨组件决定与约定 | [`spec/system/README.md`](../../spec/system/README.md) |
| MRL 语言 | [`spec/mrl/mrl-1.0.md`](../../spec/mrl/mrl-1.0.md)、[`spec/mrl/factory-profile.md`](../../spec/mrl/factory-profile.md) |
| 实现方须知 | [`AGENTS.md`](../../AGENTS.md) |
