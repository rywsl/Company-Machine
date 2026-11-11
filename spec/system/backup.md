# backup：备份、校验与恢复（M7-04）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M7-04**。
> 工厂的状态分散在三处：状态库（`factory.db`）、CAS、Registry Git 仓库（含 `refs/factory/*` 与 `refs/changesets/*`）。只拷其中一处恢复不出能用的工厂：库里的 Bean 指向 CAS 的字节，epoch 指针指向 Git 的提交。本文件定义一次一致的备份、可独立校验的清单，以及恢复到新根目录的步骤。秘密值永远不进备份。

---

## 1. 目的与范围

**做什么（M7-04）**

- `mao.backup.create_backup`：状态库在线备份 + CAS + 配置 + Registry 的 `git bundle` + 清单。
- `mao.backup.verify_backup`：只看备份目录本身就能完成的校验。
- `mao.backup.restore_backup`：恢复到一个空的新根目录，并把 Registry 恢复成可用的仓库。
- 命令行：`factory backup create|verify|restore`。

**不做什么**

| 不做 | 原因 |
|---|---|
| 备份秘密值（`secrets/values/`）、`console.token` | 秘密只存在于本机。恢复后由人重新放入，清单只记名字 |
| Postgres 与 S3 后端的备份 | 用数据库和对象存储自己的备份工具；本命令在这种配置下报 `BACKUP_UNSUPPORTED` |
| 增量备份、定时备份、上传到远端 | 由人用 cron 与自己的存储完成。备份目录是普通文件，可以直接同步 |
| 备份 job 工作区、`tmp/`、`serve.json`、epoch 检出目录 | 工作区是临时的；检出目录可从 Registry 重建（5.3 第 7 步） |

---

## 2. 依赖

| 依赖 | 用途 |
|---|---|
| `Database`（`mao.db`） | 本任务增加 `backup_to(path)`：SQLite 在线备份 API |
| `Warehouse`、`BlobStore`（`warehouse.md`、`backends.md`） | CAS 文件、`verify` |
| `LeaseStore`（`durable.md`） | 备份期间持有 `gc` 租约，回收不能同时删字节 |
| `EpochStore`（`governance.md`） | 指针、`retain`、`materialize` |
| `git`（argv 列表调用，超时取 `governance.git_timeout_s`） | `bundle create`、`bundle verify`、`fetch` |

---

## 3. 接口

```python
# mao.db（增补）
class Database:
    def backup_to(self, path: Path) -> None
        # sqlite3.Connection.backup 到新文件；目标已存在 → ValueError。非 SQLite 后端 → BACKUP_UNSUPPORTED

# mao.backup
FORMAT = "mao-backup/1"

def create_backup(ctx: Context, dest: Path, *, with_logs: bool = False,
                  by: str = "human") -> dict[str, Any]          # 返回清单（4.2）
def verify_backup(dest: Path, *, timeout_s: float = 120.0) -> dict[str, Any]
    # {"ok": bool, "problems": [{"kind": ..., "path": ...}]}，problems 按 (kind, path) 升序
def restore_backup(dest: Path, root: Path, *, registry_dest: Path | None = None,
                   timeout_s: float = 120.0) -> dict[str, Any]
    # {"root", "registry", "epochs": [...], "secrets_missing": [...]}
```

```
factory backup create DEST [--with-logs] [--json]
factory backup verify DEST [--json]
factory backup restore DEST ROOT [--registry-dest PATH] [--json]
```

- `create`：成功退出码 0。`verify`：`ok` 为真退出码 0，否则 1。`restore`：成功 0。
- `verify` 与 `restore` 不打开当前工厂，不调用 `open_context`，不需要 `--root`。`restore` 的 `ROOT` 是新的工厂根目录。
- `--json` 时三个命令分别打印清单、校验报告、恢复报告。
- 命令由本任务加入 `commands.md` 的命令表。

---

## 4. 数据格式

### 4.1 备份目录

```
DEST/
  manifest.json                    最后写入；有它才算完整的备份
  factory.db                       在线备份得到的 SQLite 文件
  cas/sha256/<2>/<62>              CAS 文件，原样复制
  config/factory.json
  config/egress/upstreams.json     存在才有
  config/egress/prices.json        存在才有
  config/secrets/manifest.json     只有名字与分类，存在才有
  epochs/<commit>/resolutions.json            每个已构建 epoch 的冻结解析表
  epochs/<commit>/resolution-runtime.json     存在才有
  registry.bundle                  有治理配置时
  logs/jobs/<job_id>/<attempt>/... --with-logs 时
  logs/foreman/sessions/<id>/...   --with-logs 时，只含 logs/ 子目录
```

### 4.2 清单 `manifest.json`

规范 JSON：

```json
{
  "format": "mao-backup/1",
  "created_at": "2026-11-11T08:00:00.000Z",
  "by": "human",
  "source_root": "/srv/factory",
  "schema_version": 34,
  "files": [{"path": "cas/sha256/ab/…", "sha256": "sha256:…", "size": 12}],
  "registry": {"bundle": "registry.bundle", "head": "refs/heads/main",
               "refs": {"refs/factory/active": "<40>", "refs/heads/main": "<40>"}},
  "secrets": ["ANTHROPIC_API_KEY"],
  "with_logs": false
}
```

- `files`：除 `manifest.json` 外的每个文件，路径用 `/` 分隔、相对 `DEST`，按 `path` 升序。
- `schema_version`：备份库的 `PRAGMA user_version`（`mao.db` 用它记录已应用的迁移编号）。
- `registry`：没有治理配置时为 `null`。`refs` 是 `git for-each-ref --format=%(refname) %(objectname)` 的全部引用；`head` 是 `git symbolic-ref HEAD`（游离时为 `null`）。
- `secrets`：`secrets/manifest.json` 的键，升序；没有该文件时为 `[]`。

### 4.3 事件

| kind | 写在哪里 | data |
|---|---|---|
| `backup.created` | 源工厂 | `{dest, files, bytes}` |
| `backup.restored` | 恢复出的新工厂 | `{from: dest, created_at}` |

---

## 5. 行为

### 5.1 创建

1. `$FACTORY_ROOT/backend-active.json` 存在（Postgres 或 S3 后端）→ `BACKUP_UNSUPPORTED`。
2. `DEST` 存在且非空 → `BACKUP_TARGET_NOT_EMPTY`。先写到 `DEST.part/`，最后整体 `os.replace` 为 `DEST`；失败时删除 `DEST.part/`。
3. `LeaseStore.acquire("gc", owner=..., ttl_s=LEASE_TTL_S)`；拿不到 → `BACKUP_BUSY`（`detail.owner`）。用 `LeaseKeeper` 续约直到结束，最后 `release`。持有这个租约时 `retention.md` 的回收不会运行，CAS 中的字节不会被删。
4. `ctx.db.backup_to(DEST.part/factory.db)`。这是一致性的锚点：之后写入的 Bean 不在备份里，但备份里的每个 Bean 的字节此刻都在 CAS 中。
5. 打开备份出的 `factory.db`（只读），取 `payload_files` 中属于 `expired_at` 为空的 Bean 的 digest 集合；从 CAS 复制这些文件，以及 CAS 中其他所有文件（无论是否被引用），保持路径。复制后逐个重算 sha256；与文件名不符 → `BACKUP_SOURCE_CORRUPT`（`detail.path`），不产出备份。被引用的 digest 在 CAS 中缺失 → 同样 `BACKUP_SOURCE_CORRUPT`。
6. 复制 4.1 列出的配置文件和 `epochs/<commit>/` 下的两个 JSON（只复制这两个文件，不复制 `registry/` 检出目录）。`secrets/values/` 下的任何文件都不读取。
7. 有治理配置：`git -C <registry> bundle create DEST.part/registry.bundle --all`。读取 `refs` 与 `head`。
8. `with_logs`：复制每个 job attempt 的 `logs/` 与每个会话的 `logs/`。
9. 计算 `files`，写 `manifest.json`（`.part` 再 `os.replace`），然后 `DEST.part` → `DEST`。
10. 在源工厂写 `backup.created`。

### 5.2 校验

`verify_backup` 只读 `DEST`，不打开任何工厂：

| `kind` | 条件 |
|---|---|
| `manifest` | `manifest.json` 缺失、不是 JSON、`format` 不是 `mao-backup/1` |
| `missing` | 清单列出的文件不存在 |
| `mismatch` | 文件大小或 sha256 与清单不符 |
| `extra` | 存在清单没有列出的文件（`manifest.json` 除外） |
| `database` | `factory.db` 的 `PRAGMA integrity_check` 结果不是 `ok` |
| `blob` | 备份库中未过期 Bean 的某个 digest，在 `cas/` 下没有对应文件（`path` 为该 digest） |
| `bundle` | `registry` 不为 `null` 而 `git bundle verify` 失败 |
| `secret` | 任何路径含 `secrets/values`，或文件名为 `console.token` |

`manifest` 问题出现时只报这一条，不再继续。

### 5.3 恢复

1. `verify_backup(dest)`；`ok` 为假 → `BACKUP_CORRUPT`（`detail.problems`），不触碰 `root`。
2. `root` 存在且非空 → `BACKUP_TARGET_NOT_EMPTY`。`registry_dest` 缺省为 `root/registry`；它存在且非空 → 同样的错误。
3. 复制 `factory.db`、`cas/`、`epochs/`（两个 JSON），`config/` 下的文件放回根目录对应位置（`config/factory.json` → `root/factory.json`，依此类推）。`logs/` 放回 `jobs/` 与 `foreman/sessions/`。
4. 有 `registry`：`git init <registry_dest>`；`git -C <registry_dest> fetch <DEST>/registry.bundle "+refs/*:refs/*"`；`head` 不为 `null` 时 `git -C <registry_dest> symbolic-ref HEAD <head>` 再 `git -C <registry_dest> reset --hard`。之后每个 `refs` 项都必须 `rev-parse` 到清单里的值，否则 `BACKUP_CORRUPT`。
5. 改写 `root/factory.json` 的 `governance.registry` 为 `registry_dest` 的绝对路径（其他键不变，规范 JSON 写回）。
6. 用新根打开 `Context`（迁移照常执行，补齐备份之后新增的迁移）。清空 `leases` 表（源工厂的租约在新机器上没有意义）。
7. 有治理配置：对 `EpochStore.list()` 中 `retain` 为真的每个 commit 调 `materialize`（`resolutions.json` 已存在则保留，`governance.md` 5.2 第 5 条）。返回值的 `epochs` 是这些 commit，升序。
8. `Warehouse.verify()` 必须为空，否则 `BACKUP_CORRUPT`。
9. 写 `backup.restored`。`secrets_missing` = 清单的 `secrets`（恢复不会带回任何值，人必须重新放入 `secrets/values/`）。

`running` 的 job 与会话按原样恢复，由各自的恢复逻辑处理（`durable.md`、`supervisor.md` 的 `start`）。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `BACKUP_UNSUPPORTED` | 非 SQLite 状态库或非本地 CAS | — |
| `BACKUP_TARGET_NOT_EMPTY` | 备份目标或恢复目标存在且非空 | `path` |
| `BACKUP_BUSY` | `gc` 租约被别人持有 | `owner` |
| `BACKUP_SOURCE_CORRUPT` | 源 CAS 文件摘要不符或缺失 | `path` |
| `BACKUP_CORRUPT` | 恢复前校验失败，或恢复后的引用 / 仓库校验失败 | `problems` |

---

## 7. 验收测试

治理夹具（`tests/acceptance/m3/gov_world.py` 的 `open_world`），`LocalProvider`、`FakeClock`、`CounterIds`。秘密值用一个可识别的字符串，测试在备份目录的所有文件字节中搜索它。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m7/test_m7_04_backup.py` | `test_create_layout` | 清单的 `files` 与目录一致、摘要正确；含 `factory.db`、CAS 文件、`config/factory.json`、`registry.bundle`、冻结解析表；`secrets` 只有名字；任何文件都不含秘密值；源工厂有 `backup.created` |
| | `test_verify_detects_damage` | 完好时 `ok`；改一个 CAS 字节 → `mismatch`；删一个文件 → `missing`；多一个文件 → `extra`；`factory backup verify` 退出码 1 |
| | `test_restore_roundtrip` | 恢复到新根：Bean 可 `get`、可物化、`verify` 为空；`EpochStore.current()` 与源相同；`governance.registry` 指向恢复的仓库且 `refs/factory/active` 一致；`leases` 为空；`secrets_missing` 为清单的名字 |
| | `test_restore_refuses` | 目标非空 → `BACKUP_TARGET_NOT_EMPTY`；损坏的备份 → `BACKUP_CORRUPT` 且目标未被创建 |
| | `test_busy_and_unsupported` | `gc` 租约被别人持有 → `BACKUP_BUSY` 且没有留下 `DEST` 或 `DEST.part`；存在 `backend-active.json` → `BACKUP_UNSUPPORTED` |
| | `test_tombstones_backed_up` | 字节已被回收的墓碑不导致 `BACKUP_SOURCE_CORRUPT` 或 `blob` 问题；恢复后墓碑仍是墓碑 |
| | `test_cli_roundtrip` | `factory backup create`、`verify`、`restore` 依次退出码 0，`--json` 输出分别是清单、`{"ok": true, ...}`、恢复报告 |

```
uv run python tools/harness.py pytest tests/acceptance/m7/test_m7_04_backup.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 7`。

---

## 8. 错误示范与正确示范

```python
# ✖ shutil.copy(root / "factory.db", dest)：WAL 模式下拷到的是不一致的文件
# ✔ sqlite3 的在线备份 API
```

```python
# ✖ 先拷 CAS 再拷数据库：中间新封存的 Bean 在库里，字节却不在备份里
# ✔ 先在线备份数据库，再按备份库的引用复制 CAS；全程持有 gc 租约
```

```python
# ✖ 为了“恢复即可用”把 secrets/values 一起打包
# ✔ 只记名字；恢复报告列出 secrets_missing，由人放回
```

---

## 9. 风险与注意事项

- **备份目录本身是敏感数据**：它不含秘密值，但含 Bean 内容、transcript（`--with-logs`）和完整 Registry。存放位置的权限由人负责。
- **大小**：CAS 是全量复制。增量方案（硬链接、rsync）由人在备份目录之外实现。
- **版本**：恢复到更新的代码时，第 6 步会补跑新增迁移；恢复到更旧的代码（`schema_version` 高于现有迁移）不受支持，`Database.migrate` 报 `DB_TOO_NEW`。
- **租约**：备份持有 `gc` 租约的时间与 CAS 大小成正比，期间回收会报 `GC_BUSY` 并在下一轮重试。
