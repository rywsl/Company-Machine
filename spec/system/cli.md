# factory 命令行（cli.md）

> 规范性文档，M1。模块：`mao.cli`。任务：**M1-09**。
> 入口：`factory = "mao.cli:main"`。退出码以 README 第 4.10 节为准。

---

## 1. 目的与范围

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

文件：`tests/acceptance/m1/test_m1_09_cli.py`。

| 测试 | 验证 |
|---|---|
| `test_usage_exit_2` | 无子命令、未知 provider、`--local-commands` 配 docker → 2 |
| `test_check_invalid_registry` | 坏 Registry → 3；夹具 Registry → 0 |
| `test_job_run_sealed` | `bean import` 打印 id；`job run upper@1 --input note=ID` → 0，stdout 是 `note\t<id>`；`--json job show` 的 `state` 是 `SEALED` |
| `test_job_run_dead` | 输入含 `#fail` → 1；`--json` 输出的 `error.code` 是 `OPERATOR_FAILED` |
| `test_bean_lineage_json` | `--json bean lineage OUT --direction up` 的 `beans` 含输入 id |
| `test_default_provider_is_docker` | 不传 `--provider` 时解析结果是 `docker` |
| `test_version` | 输出 `mao.__version__`，退出码 0 |

测试把 `--provider local --local-commands FILE` 显式传给会执行的子命令，FILE 由 `tests/fakes/fakelib.py` 的 `local_commands()` 写出。`test_default_provider_is_docker` 只检查解析结果，不要求本机有 Docker。

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
