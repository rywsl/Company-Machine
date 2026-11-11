# retention：保留期与回收（M7-03）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M7-03**。
> 工厂长期运行后，CAS、job 目录、厂长会话目录会一直增长。架构（`docs/architecture.md` 第 3、7 节）给 Bean 定了生命周期 candidate → sealed / quarantined → expired：过期删载荷、留墓碑，血缘不断。本文件把它落地：谁会过期、谁绝不能动、怎样删才不会删掉别人还在用的字节。

---

## 1. 目的与范围

**做什么（M7-03）**

- `factory.json` 的 `retention` 段（人维护）。
- `Warehouse.expire`、Bean 的 `expired_at`、墓碑语义（4.2、5.3）。
- `mao.retention.collect`：按规则选出过期 Bean，回收不再被引用的 blob、旧的 job 与会话目录。
- 命令行：`factory gc [--dry-run] [--json]`。
- 守护进程按间隔回收（`ServeConfig.gc_every_s`）。

**不做什么**

| 不做 | 原因 |
|---|---|
| 在 Registry（Type、Policy）里声明保留期 | 需要改内核 schema（L0），只能由人发起；见 `plan/drift.md` D-32。在那之前保留期是操作员配置 |
| 删除事件、审计记录、ChangeSet、复核、审批 | 审计证据永久保留 |
| 删除 epoch 检出目录 | `reconciler.md` I12 |
| 删除镜像 | 由人用 `docker image prune`；`images.md` 的构建记录保留 |
| 回收未封存的 Bean | 它们只出现在失败或进行中的 job 里，随 job 目录处理；CAS 中的字节按 5.4 的引用规则判断 |

---

## 2. 依赖

| 依赖 | 用途 |
|---|---|
| `Warehouse`（`warehouse.md`） | Bean、标签、payload 索引、blob 存储 |
| `BlobStore`（`backends.md` 3.1） | `exists`；本任务增加 `delete` |
| `LeaseStore`（`durable.md`） | 资源 `gc`，同一时刻只有一个回收者 |
| `JobState` / `TERMINAL`（`runner.md`）、`RUN_TERMINAL`（`lines.md`） | 判断“在用” |
| `quarantines` 表（`scheduler.md`） | 隔离中的 Bean 不回收 |
| `SandboxProvider`（`sandbox.md`） | 本任务增加 `remove_volume` |

---

## 3. 接口

```python
# mao.warehouse（增补）
@dataclass
class Bean:
    ...
    expired_at: str | None = None        # 新字段，放在最后，缺省 None

class Warehouse:
    def expire(self, bean_id: str, *, by: str, reason: str) -> Bean
        # 已封存且未过期的 Bean：写 expired_at，写事件 bean.expired。返回墓碑。
        # 不删字节（字节由 collect 的 blob 清扫删除）。
        # 未封存 → BEAN_NOT_SEALED；已过期 → 原样返回，不重复写事件；不存在 → BEAN_NOT_FOUND
    def list(self, type_prefix: str | None = None, sealed_only: bool = True,
             include_expired: bool = False) -> list[Bean]   # 新参数；缺省不含墓碑

# mao.backends（增补）
class BlobStore(Protocol):
    ...
    def delete(self, digest: str) -> bool   # 删了返回 True；本来就没有返回 False

# mao.sandbox（增补）
class SandboxProvider(Protocol):
    ...
    def remove_volume(self, name: str) -> bool   # Docker：docker volume rm；Local：返回 False

# mao.retention
@dataclass(frozen=True)
class RetentionConfig:
    beans: dict[str, int | None]        # 类型 id（name@major）或 "*" → 天数；None 表示永久
    workspaces_days: int | None
    logs_days: int | None
    sessions_days: int | None
    @classmethod
    def load(cls, root: Path) -> "RetentionConfig"

@dataclass
class GcReport:
    dry_run: bool
    expired: list[str]                  # 本次过期的 Bean id，升序
    kept: dict[str, int]                # {"pinned": n, "in_use": n, "quarantined": n}
    blobs_deleted: int
    bytes_freed: int
    workspaces: list[str]               # 回收了 workspace 的 job id
    logs: list[str]                     # 回收了 logs 的 job id
    sessions: list[str]                 # 回收了目录的会话 id
    def to_json(self) -> dict[str, Any]

def collect(ctx: Context, config: RetentionConfig, *, dry_run: bool = False,
            by: str = "gc") -> GcReport
```

```
factory gc [--dry-run] [--json]
```

退出码 0。另一个回收者持有 `gc` 租约 → `GC_BUSY`，退出码 1。没有 `retention` 段 → `GC_NOT_CONFIGURED`，退出码 4。命令由本任务加入 `commands.md` 的命令表。

---

## 4. 数据格式

### 4.1 `factory.json` 的 `retention` 段

```json
{
  "retention": {
    "beans": {"note.raw@1": 30, "*": null},
    "workspaces_days": 14,
    "logs_days": 90,
    "sessions_days": 30
  }
}
```

- `beans` 的键是类型 id（`name@major`，与 `gate.md` 的类型 id 相同）或 `"*"`。没有匹配键且没有 `"*"` 的类型永久保留。值是非负整数天数或 `null`。
- 三个目录天数是非负整数或 `null`（`null` 表示不回收）。
- 未知键、负数、非整数 → `RETENTION_CONFIG_INVALID`（`detail.field`）。
- 示例里的数只说明形状；取值由操作员决定。

### 4.2 迁移 `034_retention.sql`

```sql
ALTER TABLE beans ADD COLUMN expired_at TEXT;
CREATE INDEX beans_expired ON beans(expired_at);
```

Postgres 后端用同名迁移（`backends.md` 的迁移规则）。

### 4.3 事件

| kind | data |
|---|---|
| `bean.expired` | `{bean_id, type, by, reason}` |
| `gc.done` | `GcReport.to_json()` 中列表换成长度 |

`dry_run` 不写任何事件。

---

## 5. 行为

### 5.1 选择过期 Bean

对每个已封存、未过期的 Bean：

1. 类型 id 取 `beans` 表的 `type` 去掉 minor。按 `beans[类型 id]`，否则 `beans["*"]`，得到天数 `d`；没有或为 `null` → 跳过。
2. `created_at > now - d 天` → 跳过（`now` 是 `ctx.clock.now()`）。
3. **钉住**：标签 `pin` 的值不在 `{"", "false", "0"}` → 保留，计入 `kept.pinned`。
4. **隔离**：`quarantines` 有该 Bean → 保留，计入 `kept.quarantined`。
5. **在用**：下列任一成立 → 保留，计入 `kept.in_use`：
   - 某个状态不在 `TERMINAL` 的 job 的 `inputs` 引用它；
   - 某个状态不在 `RUN_TERMINAL` 的 run 的 `inputs` 引用它；
   - 某个 `pending` 审批的 `payload` 引用它。
6. 其余的 Bean 用 `Warehouse.expire(id, by=by, reason="retention")` 过期。

一个 Bean 同时满足 3–5 中多条时，只计入最先满足的一项。

### 5.2 blob 清扫

在**一个写事务**里（`ctx.db.tx()`）：

1. 求“死 digest”：`payload_files` 中出现过，且引用它的每一行都属于 `expired_at` 不为空的 Bean。
2. 对每个死 digest 调 `BlobStore.delete`，在事务提交**之前**完成。删除成功的计入 `blobs_deleted` 与 `bytes_freed`（取 `payload_files.size`，同一 digest 只计一次）。
3. 每个写事务最多处理 1000 个死 digest；多出的分批，每批一个事务，重复第 1–2 步直到没有死 digest。
4. 只在 `payload_files` 中出现的 digest 才会被删。其他组件放进 CAS 的字节（没有 `payload_files` 行）一律不动。

`Warehouse.add` 的增补：它在写 `payload_files` 的同一个写事务里，对每个 digest 调 `BlobStore.exists`；不存在就从调用方给的源文件再 `put_file` 一次。因为清扫只在持有写事务时删字节，`add` 也在写事务里确认字节在场，两者被数据库的写锁串行化：新 Bean 不会引用一个正在被删的 blob。

### 5.3 墓碑语义

过期的 Bean 保留 `beans`、`payload_files`、标签、血缘、来源证明，只有字节可能被删除。

| 操作 | 对墓碑的行为 |
|---|---|
| `Warehouse.get` | 返回，`expired_at` 有值 |
| `Warehouse.list` | 缺省不含；`include_expired=True` 才含 |
| `Warehouse.materialize` | `BEAN_EXPIRED` |
| `Warehouse.verify` | 跳过墓碑的 `missing_blob` 检查（I1 因此不报） |
| `Runner.submit`、`LineRunner.start_run` 的输入 | `BEAN_EXPIRED`，不建 job / run |
| `JobCache.lookup` | 命中记录的任一输出 Bean 是墓碑 → 当作未命中（`cache.md` 增补），不写命中事件 |
| `Warehouse.lineage` | 照常返回；节点带 `expired: true` |
| 触发器 | 墓碑不触发 run |

`expired_at` 一旦写入就不再清除。墓碑不会“复活”：同样内容再次进入仓库会成为一个新 Bean。

### 5.4 目录回收

只处理已经结束足够久的对象：job 看 `jobs.updated_at <= now - 天数`，会话看 `foreman_sessions.ended_at <= now - 天数`（`ended_at` 为空的不处理）。

1. `workspaces_days`：状态在 `TERMINAL` 的 job，删除每个 attempt 目录下的 `in/`、`out/`、`workspace/`、`tmp/`，并对卷名（`runner.md` 4.2）调用 `provider.remove_volume`。`ctl/`、`logs/` 保留。
2. `logs_days`：状态在 `TERMINAL` 的 job，删除每个 attempt 的 `logs/`。
3. `sessions_days`：状态不是 `running` 的厂长会话，删除 `$FACTORY_ROOT/foreman/sessions/<id>/` 整个目录；`foreman_sessions` 行保留。

删除前确认目标的真实路径在 `$FACTORY_ROOT/jobs` 或 `$FACTORY_ROOT/foreman/sessions` 之下，且不是符号链接（符号链接本身删除，不跟随）。目录已不存在不算错误，也不计入报告。

### 5.5 一次回收

`collect`：

1. `LeaseStore.acquire("gc", owner=..., ttl_s=LEASE_TTL_S)`；失败 → `GC_BUSY`。结束时 `release`。`dry_run` 也取租约（保证报告与真实回收看到同一个世界）。
2. 5.1 → 5.2 → 5.4，按这个顺序。
3. `dry_run=True`：计算并返回同样的报告，但不调用 `expire`、`delete`、`remove_volume`，不删目录，不写事件。`blobs_deleted`、`bytes_freed` 是“将会删除”的数（把本次将过期的 Bean 当作已过期来计算）。
4. 写 `gc.done`。

### 5.6 守护进程

`ServeConfig` 增加 `gc_every_s: float | None`（缺省 `None`，关闭）。不为 `None` 且有 `retention` 段时，`tick` 在审计之后、心跳之前按间隔调用 `collect(ctx, RetentionConfig.load(root), by="serve")`。`GC_BUSY` 记日志并跳过本轮，不让 tick 失败。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `BEAN_EXPIRED` | 物化或使用一个墓碑 | `bean_id` |
| `RETENTION_CONFIG_INVALID` | `retention` 段格式不对 | `field` |
| `GC_NOT_CONFIGURED` | `factory gc` 时没有 `retention` 段 | — |
| `GC_BUSY` | `gc` 租约被别的持有者占用 | `owner` |
| `BEAN_NOT_SEALED` | `expire` 一个未封存的 Bean | `bean_id` |

`BEAN_NOT_FOUND` 沿用已有登记。

---

## 7. 验收测试

`registry-m2` 与治理夹具（`tests/acceptance/m3/gov_world.py` 的 `open_world`），`LocalProvider`、`FakeClock`、`CounterIds`。用 `FakeClock.advance` 让 Bean 变旧。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m7/test_m7_03_retention.py` | `test_config_validation` | 合法段被读出；负数、未知键 → `RETENTION_CONFIG_INVALID`；没有段时 `factory gc` 退出码 4 |
| | `test_expire_by_type_age` | 只有超龄且类型匹配的 Bean 过期；墓碑 `get` 得到 `expired_at` 与原 payload 索引；blob 被删；`bean.expired` 事件；`list` 缺省不含墓碑 |
| | `test_pin_quarantine_in_use_kept` | 钉住、隔离、被未终态 job 引用的超龄 Bean 都保留，`kept` 计数正确；job 终态后再回收即过期 |
| | `test_shared_blob_survives` | 两个内容相同的 Bean 一个过期一个未过期，blob 仍在；两个都过期后 blob 被删 |
| | `test_tombstone_rejected` | 以墓碑为输入 `submit` → `BEAN_EXPIRED`，没有新 job；`materialize` → `BEAN_EXPIRED`；`verify` 不报墓碑 |
| | `test_add_restores_deleted_blob` | 一个 digest 的 blob 已被清扫后，新 Bean 以同样内容 `add`，blob 重新出现且 `verify` 为空 |
| | `test_dry_run` | `collect(dry_run=True)` 的报告与随后真实回收的报告相同（除 `dry_run`）；dry-run 后 Bean 表、事件数与 CAS 不变 |
| | `test_directories` | 超龄终态 job 的 `workspace/` 被删而 `logs/` 保留；未终态 job 的目录不动；超龄会话目录被删、行保留 |

```
uv run python tools/harness.py pytest tests/acceptance/m7/test_m7_03_retention.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 8`。

---

## 8. 错误示范与正确示范

```python
# ✖ 先查出死 digest，事务外再慢慢删：中间有新 Bean 引用了同一个 digest
# ✔ 在同一个写事务里判定并删除；add 在写事务里确认字节在场
```

```python
# ✖ 过期就 DELETE FROM beans：血缘断了，I2 报错，审计无从追溯
# ✔ 写 expired_at，留墓碑
```

```python
# ✖ 把 CAS 里所有没被 payload_files 引用的文件都当垃圾
# ✔ 只删 payload_files 里出现过且全部引用者都已过期的 digest
```

---

## 9. 风险与注意事项

- **删错不可恢复**：回收之前先 `--dry-run`；`backup.md` 的备份是唯一的回退手段。
- **时钟**：“超龄”按 `ctx.clock`。时钟被拨快会让大量 Bean 一次过期；这是人的配置责任。
- **大表**：5.2 的查询是全表聚合。数据量大时由人加索引或分批，修改本文件。
- **未封存 Bean 的字节**：它们的 `payload_files` 行引用的 digest 不会被判死（引用者未过期），所以失败 job 的候选产物字节一直保留，直到人处理。这是有意的保守。
