# factory 命令行（cli.md）

> 规范性文档。活动入口是 Bun `backend/src/cli.ts`，历史 M1 CLI 契约保留在本文末尾用于迁移审计。

## 0. 活动 Bun CLI

活动命令由 `bun run factory` 或 `bun run backend/src/cli.ts` 提供。控制面只保存和分发业务系统、项目、物料、厂长消息与运行时状态。

| 命令 | 作用 |
|---|---|
| `factory serve` | 启动 Bun Web API 和 Web App。 |
| `factory status [--root PATH] [--json]` | 输出业务系统、厂长队列、运行、治理指针、费用和告警。存在错误级告警时退出码为 1。 |
| `factory metrics [--root PATH]` | 以 Prometheus 文本输出当前工厂状态、告警和已记录的历史费用。 |
| `factory runtime status` | 读取当前 Podman 或 Kiri 健康状态。 |
| `factory runtime ensure-ready` | 请求当前运行时就绪。不可用时退出码 4。 |
| `factory project intake ...` | 通过仓库、文件夹或文件引用创建项目。 |
| `factory project register --repo URL [--name NAME] [--system ID]` | 供当前厂长轮登记无凭据仓库，输出 `projectId` 和 `systemId`，并由当前轮继续搭建。指定 `--system` 时添加到该业务系统，省略时新建。 |
| `factory project secret request PROJECT --name KEY --purpose PURPOSE` | 为指定项目申请一个实际所需的密钥。用途使用一行文字。重复申请复用该项目的现有请求，命令输出请求 ID 与状态。 |
| `factory project intake --file PATH`、`factory project fetch PROJECT SHA256 --output PATH` | 导入文件字节并通过稳定摘要取回。`--file` 可重复。 |
| `factory business ...` | 读取或写入业务系统、DAG、运行、产物和转换。 |
| `factory business graph ID [--file PATH]` | 读取已有 DAG。传入图文件时写入更新。 |
| `factory registry check PATH [--json]` | 通过 Bun MRL 只读检查 Registry 的结构、schema 与引用。 |
| `factory bean import --type TYPE --file PATH=HOST` | 检验并封存外部物料。可以重复传入 `--file`。 |
| `factory bean lineage BEAN --direction up\|down` | 查询已封存物料的上下游工位和物料。 |
| `factory job run STATION [--input PORT=BEAN] [--param NAME=JSON] [--cache]` | 执行工位并封存通过 Gate 的产物。`--cache` 可复用已有结果。列表端口允许重复传入 `--input`。 |
| `factory job show JOB`、`factory job exec JOB` | 查询或继续执行已有工位。 |
| `factory events`、`factory job events JOB`、`factory line events RUN` | 从工厂、作业或运行读取已记录的事件证据。支持 `--since ISO`、`--kind PREFIX`、`--limit N` 和 `--json`。 [事件规范](observability.md) |
| `factory cache ls`、`explain JOB`、`invalidate` | 列出缓存记录，查看 JobKey，或使用指定范围和原因失效已有记录。 |
| `factory approval ls`、`show`、`approve`、`deny` | 查询邮箱审批并提交决定，决定者默认取 `FACTORY_HUMAN_BY`。 |
| `factory source run`、`factory receipt ls`、`show`、`verify`、`void` | 执行摄入，查询、核验与作废发布回执。 |
| `factory schedule enqueue`、`tick`、`dlq ls`、`quarantine`、`release` | 持久化待执行工位，推进调度并管理死信及物料隔离。`tick` 可用 `--config PATH` 指定文件。 |
| `factory backup create DEST`、`verify DEST`、`restore DEST --root NEW_ROOT` | 将 SQLite 与 CAS 备份到独立目录，校验和恢复到显式指定的空目录。 |
| `factory business export ID --package DEST`、`verify PACKAGE`、`import PACKAGE --root NEW_ROOT` | 导出指定业务及其 CAS 文件，校验摘要和引用，再安装到新的工厂目录。 |
| `factory line plan LINE`、`factory line run LINE --in PORT=BEAN` | 编译产线计划并将执行进度、Job 与物料输出写入 SQLite。 |
| `factory line ls`、`show`、`nodes`、`exec`、`cancel`、`retry`、`rebuild` | 查看与推进已保存的运行，或按当前 Registry 重建终态产线。 |
| `factory foreman ...` | 查看、领取和完成厂长消息。 |
| `factory talk ...` | 写入人的消息或厂长回复。 |

Bean 与 Job 命令支持 `--root`、`--registry`、`--json`、`--provider podman|kiri|local`。工厂目录和 Registry 分别读取 `FACTORY_ROOT` 与 `FACTORY_REGISTRY`，缺省为 `.factory` 与 `registry`。容器引擎缺省采用 `MAO_CONTAINER_RUNTIME` 的设置，未配置时使用 Podman。`--local-commands FILE` 与显式的 `--provider local` 一起使用，文件内容为命令名到 argv 数组的 JSON 映射。

Registry 检查独立于工厂存储执行。检查通过时退出码为 `0`，目录缺失、路径指向文件或发现 Registry 错误时退出码为 `1`。JSON 模式在 stdout 输出 `root`、`ok`、`stats` 和 `diagnostics`。参数不符合用法时在 stderr 输出命令用法并退出 `2`。

活动 CLI 的退出码为 `0` 成功、`1` 业务或运行失败、`2` 用法错误、`3` 工位命令遇到无效 Registry、`4` 运行时设施不可用。活动测试位于 `backend/tests/cli.test.ts`，MRL CLI 测试位于 `backend/mrl/cli.test.ts`。

---

## 1. 目的与范围

以下章节保留历史 Python M1 CLI 契约。活动 Bun 命令见第 0 章。

**做什么（M1）**

- `factory` 命令行：检查 Registry、导入 Bean、提交并等待一个工位、查看 job 与血缘。
- 全局参数 `--root`、`--registry`、`--json`、`--provider`、`--local-commands`（第 3 章）。默认 `--provider docker`（D5）。

**不做什么**

- 不做 Web UI。
- 不做产线运行（M2）。
- 不在 CLI 里调用模型。

---

## 2. 依赖

`Snapshot`、`Context`、`Runner`、`Gate.admit_external`、`DockerProvider`、`LocalProvider`。`LocalProvider` 只有显式 `--provider local` 才可用。

---

## 3. 接口

```python
def main(argv: list[str] | None = None) -> int
def build_parser() -> argparse.ArgumentParser
```

子命令：

| 命令 | 作用 |
|---|---|
| `factory check ROOT` | 加载快照。有 error 则退出码 3 |
| `factory bean import --type TYPE --file PATH=HOST ...` | `admit_external`。通过则打印 Bean id |
| `factory job run STATION --input PORT=BEAN ...` | `submit` 然后 `run`。`SEALED` 退出码 0，`DEAD` 退出码 1 |
| `factory job show JOB` | 打印 job JSON |
| `factory bean lineage BEAN --direction up\|down` | 打印血缘 |
| `factory --version` | 打印 `mao.__version__` |

全局选项写在子命令**之前**：

| 选项 | 含义 |
|---|---|
| `--root PATH` | `FACTORY_ROOT`。缺省 `$FACTORY_ROOT`，再缺省 `./.factory`（README 3.1） |
| `--registry PATH` | Registry 目录。缺省 `$FACTORY_REGISTRY`，再缺省 `./registry`。`job run`、`bean import` 用它加载快照；`check ROOT` 用位置参数，不读这个选项 |
| `--json` | stdout 只输出一个规范 JSON 值 |
| `--provider docker\|local` | 缺省 `docker` |
| `--local-commands FILE` | 只在 `--provider local` 时允许：JSON 对象 `{"命令名": ["argv", ...]}`，原样作为 `LocalProvider(commands=...)`。与 `docker` 同时出现 → 退出码 2。测试用它把 `upper-text` 等映射到夹具脚本 |

输出（非 `--json`）：

- `bean import`：一行，Bean id。
- `job run`：`SEALED` 时每个输出 Bean 一行 `<端口>\t<Bean id>`（制表符分隔），端口升序；`DEAD` 时 stdout 为空，stderr 一行 `<错误码>: <说明>`。
- `--json` 时：`bean import` 输出 `{"id": ...}`；`job run`、`job show` 输出 `mao.jobs.dump_job(job)`；`bean lineage` 输出 `Warehouse.lineage` 的返回值。

---

## 4. 数据格式

`--json` 时 stdout 只有一个 JSON 值，用规范 JSON。非 json 时人读的行写到 stdout，错误说明写到 stderr。退出码表：

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 业务失败（job DEAD、检验不通过、完整性问题） |
| 2 | 用法错误 |
| 3 | Registry 不合法 |
| 4 | 环境或设施错误（没有 Docker、镜像不存在） |
| 5 | 内部错误（未捕获异常） |

---

## 5. 行为

1. 解析 argv。未知子命令或缺少必填参数 → 退出码 2，stderr 有用法。
2. `--provider` 缺省 `docker`。`docker`、`local` 以外的值 → 退出码 2。`--local-commands` 只能和 `local` 一起用，文件不是 `{str: [str, ...]}` → 退出码 2。
3. `check`：`Snapshot.load` 失败且 code 是 `REGISTRY_INVALID` → 退出码 3。
4. `job run`：`Runner.run` 得到 `SEALED` → 0，打印输出 Bean id。`DEAD` → 1，`--json` 时打印 `error`。
5. 未捕获的 `FactoryError`：`REGISTRY_INVALID` → 3；设施类（`DOCKER_UNAVAILABLE`、`DOCKER_FAILED`、`IMAGE_NOT_FOUND`，与 `sandbox.md` 第 6 章同名）→ 4；其他业务码 → 1。其他异常 → 5，stderr 有类型名，不打印秘密。`EGRESS_UNSUPPORTED` 在 M1 是 Runner 的静态拒绝，job 因此 `DEAD`，走第 4 条的退出码 1。

---

## 6. 错误

CLI 不发明新的业务错误码。它把下层的 `FactoryError.code` 放进 JSON 的 `code` 字段。用法错误没有业务码，只有退出码 2。

| 错误码 | 触发条件 | detail |
|---|---|---|
| `CLI_USAGE` | 仅出现在 `--json` 的用法错误体里，进程仍退出 2 | `argv` |

`CLI_USAGE` 必须登记在 `mao.errors.CODES`，并在本文件出现。

---

## 7. 验收测试

文件：`removed/python/tests/acceptance/m1/test_m1_09_cli.py`。

| 测试 | 验证 |
|---|---|
| `test_usage_exit_2` | 无子命令、未知 provider、`--local-commands` 配 docker → 2 |
| `test_check_invalid_registry` | 坏 Registry → 3；夹具 Registry → 0 |
| `test_job_run_sealed` | `bean import` 打印 id；`job run upper@1 --input note=ID` → 0，stdout 是 `note\t<id>`；`--json job show` 的 `state` 是 `SEALED` |
| `test_job_run_dead` | 输入含 `#fail` → 1；`--json` 输出的 `error.code` 是 `OPERATOR_FAILED` |
| `test_bean_lineage_json` | `--json bean lineage OUT --direction up` 的 `beans` 含输入 id |
| `test_default_provider_is_docker` | 不传 `--provider` 时解析结果是 `docker` |
| `test_version` | 输出 `mao.__version__`，退出码 0 |

测试把 `--provider local --local-commands FILE` 显式传给会执行的子命令，FILE 由 `tests/fakes/fakelib.ts` 的 `local_commands()` 写出。`test_default_provider_is_docker` 只检查解析结果，不要求本机有 Docker。

---

## 8. 错误示范与正确示范

**8.1 把用法错误当成业务失败**

```python
# ✖ 调用方无法区分“命令写错”和“job 死了”
return 1
# ✔ 用法错误是 2
return 2
```

**8.2 默认 local**

```python
# ✖ D5：默认必须是 docker
provider = LocalProvider(root)
# ✔ 只有显式 --provider local 才用 LocalProvider
```

**8.3 JSON 模式里混进日志**

```python
# ✖ --json 时 stdout 必须是一个 JSON 值
print("running...")
print(canonical_json(payload))
# ✔ 人读的进度不要写；错误走 stderr
```

---

## 9. 风险与注意事项

- **S1**：不要在 CLI 里解析 transcript。用 runner 已经算好的 `cost_usd`。
- **R13**：本文件的验收测试不要求 Docker。Docker 端到端在 M1-10。
- 退出码是契约。改一个数字就要改本文件和 README 第 4.10 节，而 README 受保护。
