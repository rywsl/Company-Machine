# factory 命令行（cli.md）

> 规范性文档，M1。模块：`mao.cli`。任务：**M1-09**。
> 入口：`factory = "mao.cli:main"`。退出码以 README 第 4.10 节为准。

---

## 1. 目的与范围

**做什么（M1）**

- `factory` 命令行：检查 Registry、导入 Bean、提交并等待一个工位、查看 job 与血缘。
- 全局参数 `--root`、`--json`。默认 `--provider docker`（D5）。

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

全局：`--root PATH`（缺省 `$FACTORY_ROOT` 或 `./.factory`）、`--json`、`--provider docker|local`。

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
2. `--provider` 缺省 `docker`。`local` 以外的值 → 退出码 2。
3. `check`：`Snapshot.load` 失败且 code 是 `REGISTRY_INVALID` → 退出码 3。
4. `job run`：`Runner.run` 得到 `SEALED` → 0，打印输出 Bean id。`DEAD` → 1，`--json` 时打印 `error`。
5. 未捕获的 `FactoryError` 中，设施类（`SANDBOX_FAILED`、`IMAGE_NOT_FOUND`、`EGRESS_UNSUPPORTED`）→ 4。其他业务码 → 1。其他异常 → 5，stderr 有类型名，不打印秘密。

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
| `test_usage_exit_2` | 无子命令 → 2 |
| `test_check_invalid_registry` | 坏 Registry → 3 |
| `test_job_run_sealed` | 夹具工位跑通 → 0，stdout 含 Bean id |
| `test_job_run_dead` | 注定失败的工位 → 1 |
| `test_default_provider_is_docker` | 不传 `--provider` 时构造的是 DockerProvider |
| `test_version` | 输出版本字符串，退出码 0 |

测试把 `--provider local` 显式传给会执行的子命令。`test_default_provider_is_docker` 只检查解析结果，不要求本机有 Docker。

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
