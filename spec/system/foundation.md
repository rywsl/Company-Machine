# foundation：基础设施（mao.base / mao.errors / mao.db / mao.context）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M1-01**（`mao.base`、`mao.errors`、`mao.db`、静态约束）；
> `mao.context` 依赖仓库，由 **M1-03** 实现（见 3.4）。

---

## 1. 目的与范围

本组件提供所有控制面模块共用的底层能力：

- 规范 JSON、摘要、时间与时长（`mao.base`）；
- 可注入的时钟与 ID 源（`mao.base`）；
- 错误类型与错误码表（`mao.errors`）；
- SQLite 连接、事务与迁移（`mao.db`）；
- 把这些对象装配在一起的 `Context`（`mao.context`）；
- 一组由测试强制的**静态约束**（5.12），把 README 的 D3、D9、D10 和 §3.7 落实为可检查的规则。

不做的事：任何业务逻辑（Bean、Job、沙箱）；日志配置；命令行。

---

## 2. 依赖

- 标准库：`json`、`hashlib`、`datetime`、`time`、`re`、`secrets`、`base64`、`sqlite3`、`threading`、`contextlib`、`pathlib`、`os`。
- `mao.context` 额外依赖 `mao.warehouse.Warehouse`（M1-03）。它只把 `SandboxProvider` 当作属性保存，不 import `mao.sandbox`，类型注解放在 `TYPE_CHECKING` 下。
- 本组件**禁止** import `mao` 的其他模块，`mao.context` 除外。

---

## 3. 接口

### 3.1 `mao.base`

```python
UTC: datetime.tzinfo                     # = datetime.timezone.utc

def canonical_json(obj: Any) -> str
def canonical_bytes(obj: Any) -> bytes
def digest_bytes(data: bytes) -> str     # "sha256:<64 位小写 hex>"
def digest_file(path: Path) -> str
def utc_iso(dt: datetime) -> str
def parse_duration(text: str) -> float   # 秒

class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...

class SystemClock:                       # 实现 Clock
    def now(self) -> datetime
    def monotonic(self) -> float
    def sleep(self, seconds: float) -> None

class FakeClock:                         # 实现 Clock
    def __init__(self, start: datetime = datetime(2026, 1, 1, tzinfo=UTC)) -> None
    def now(self) -> datetime
    def monotonic(self) -> float
    def sleep(self, seconds: float) -> None
    def advance(self, seconds: float) -> None

class IdSource(Protocol):
    def new(self, prefix: str) -> str: ...

class RandomIds:                         # 实现 IdSource
    def new(self, prefix: str) -> str

class CounterIds:                        # 实现 IdSource
    def __init__(self) -> None
    def new(self, prefix: str) -> str
```

### 3.2 `mao.errors`

```python
CODES: dict[str, str]                    # 错误码 → 一句中文说明（静态字面量表）

class FactoryError(Exception):
    code: str
    message: str
    detail: dict[str, Any]
    def __init__(self, code: str, message: str, **detail: Any) -> None
    def to_json(self) -> dict[str, Any]  # {"code": ..., "message": ..., "detail": {...}}
```

### 3.3 `mao.db`

```python
class Database:
    path: Path
    def __init__(self, path: Path) -> None
    def migrate(self, migrations_dir: Path | None = None) -> int
    def tx(self) -> ContextManager[sqlite3.Connection]
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]
    def close(self) -> None

DEFAULT_MIGRATIONS: Path                 # = Path(mao.db 模块所在目录) / "migrations"
```

`migrate` 的 `migrations_dir` 参数是对 README 4.1 的兼容性扩展（默认值保持原语义），供测试使用。

### 3.4 `mao.context`（由 M1-03 实现）

```python
@dataclass
class Context:
    root: Path
    clock: Clock
    ids: IdSource
    db: Database
    warehouse: Warehouse
    provider: SandboxProvider
    @classmethod
    def open(cls, root: Path, *, provider: SandboxProvider,
             clock: Clock | None = None, ids: IdSource | None = None) -> "Context"
    def close(self) -> None
    def __enter__(self) -> "Context"
    def __exit__(self, *exc: object) -> None      # 调用 close()

def install_factoryctl(root: Path, source: Path | None = None) -> Path | None
FACTORYCTL_SOURCE: Path                  # = Path(mao 包目录) / "ctl" / "factoryctl.py"
```

---

## 4. 数据格式

### 4.1 迁移文件

- 位置：`src/mao/migrations/`（即 `DEFAULT_MIGRATIONS`）。
- 只看扩展名为 `.sql` 的文件，其他文件（如 `README.md`）忽略。
- `.sql` 文件名必须完全匹配 `^([0-9]{3})_[a-z0-9_]+\.sql$`，编号 ≥ 001。编号可以不连续（README 3.6 预留了区段），但不能重复。
- 文件内容是一组 SQL 语句，**禁止**包含 `BEGIN`、`COMMIT`、`ROLLBACK`、`PRAGMA user_version`（这些由 `migrate` 负责）。
- 已发布的迁移文件禁止修改；改表结构就新增文件。

### 4.2 `CODES` 表

`mao/errors.py` 中的一个字典字面量。键匹配 `^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$`，值是非空中文说明。每个键都必须以 `` `CODE` ``（反引号包围）的形式出现在 `spec/system/` 下某份规范的“错误”一章中。

### 4.3 `FACTORY_ROOT` 布局（由 `Context.open` 创建）

```
<root>/factory.db
<root>/cas/
<root>/jobs/
<root>/tmp/
<root>/tools/factory/bin/            factoryctl 安装在这里（源文件存在时）
```

---

## 5. 行为

### 5.1 `canonical_json` / `canonical_bytes`

1. 先递归检查 `obj`：
   - 允许的值：`None`、`bool`、`int`、`float`、`str`、`list`、`tuple`、`dict`；
   - `dict` 的键必须全是 `str`；
   - `float` 必须是有限数（不能是 NaN、±Inf）。
   - 不满足时抛出 `FactoryError("JSON_INVALID", ..., path=<出错位置，如 "$.a[2]">)`。
   - 这一步必须显式做，因为 `json.dumps` 会把整数键悄悄转成字符串。
2. 再返回 `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`。
   - `tuple` 序列化为数组；
   - 中文等非 ASCII 字符原样输出；
   - 结果没有换行，也没有尾随空白。
3. `canonical_bytes(obj)` = `canonical_json(obj).encode("utf-8")`。

示例：`canonical_json({"b": 1, "a": [1.5, "中"]})` = `{"a":[1.5,"中"],"b":1}`。

### 5.2 摘要

- `digest_bytes(data)` = `"sha256:" + hashlib.sha256(data).hexdigest()`。
- `digest_file(path)`：以二进制方式读取，每块 1 MiB（1048576 字节），流式更新 sha256，结果格式同上。文件不存在时让 `FileNotFoundError` 冒泡（这是调用方的编程错误）。

### 5.3 `utc_iso`

1. `dt.tzinfo is None` 或 `dt.utcoffset() is None` 时抛出 `ValueError`（编程错误，不登记错误码）。
2. 转换为 UTC：`d = dt.astimezone(UTC)`。
3. 返回 `f"{d.year:04d}-{d.month:02d}-{d.day:02d}T{d.hour:02d}:{d.minute:02d}:{d.second:02d}.{d.microsecond // 1000:03d}Z"`。
   - 毫秒是**截断**，不是四舍五入：`123999` 微秒输出 `.123`。
   - **禁止**用 `strftime("%Y")`，它在不同平台上对小年份的补零行为不同。

### 5.4 `parse_duration`

- 输入必须是 `str`，并且**完整**匹配 `^([0-9]+)(ms|s|m|h|d)$`：不允许空白、符号、小数、大写单位。
- 换算：`ms` → n/1000，`s` → n，`m` → 60n，`h` → 3600n，`d` → 86400n。返回 `float`。
- 允许前导零（`"007s"` → 7.0）和零（`"0s"` → 0.0）。
- 其他输入（包括非 `str`）一律抛出 `FactoryError("DURATION_INVALID", ..., text=repr(text))`。

| 输入 | 结果 |
|---|---|
| `"250ms"` | 0.25 |
| `"90m"` | 5400.0 |
| `"7d"` | 604800.0 |
| `""`、`"5"`、`"5x"`、`"-5s"`、`"1.5h"`、`" 5s"`、`"5S"`、`5` | `DURATION_INVALID` |

### 5.5 时钟

- `SystemClock`：`now()` = `datetime.now(UTC)`；`monotonic()` = `time.monotonic()`；`sleep(s)` = `time.sleep(s)`。
- `FakeClock(start)`：
  - `start` 必须带时区，否则 `ValueError`。
  - 内部状态是“已推进的秒数” `elapsed`，初始为 0.0。
  - `now()` 返回 `start + timedelta(seconds=elapsed)`，每次调用结果相同，直到时间被推进。
  - `monotonic()` 返回 `elapsed`。
  - `advance(s)`、`sleep(s)` 都把 `elapsed` 加上 `s`，不真的等待；`s < 0` 时 `ValueError`。
  - 线程安全：`advance`/`sleep` 在锁内更新。

### 5.6 ID 源

前缀必须匹配 `^[a-z]{1,8}$`，否则 `ValueError`。

- `CounterIds.new(prefix)`：每个前缀一个从 1 开始的计数器，返回 `f"{prefix}-{n:04d}"`。超过 9999 时自然变长（`j-10000`）。各前缀互不影响。线程安全。
- `RandomIds.new(prefix)`：返回 `prefix + "-" + base64.b32encode(secrets.token_bytes(10)).decode("ascii").lower()[:10]`。结果匹配 `^<prefix>-[a-z2-7]{10}$`。

### 5.7 `FactoryError` 与 `CODES`

- `FactoryError(code, message, **detail)`：
  - `code not in CODES` 时**显式** `raise AssertionError(f"未登记的错误码 {code}")`。不能用 `assert` 语句，因为它在 `-O` 下会被去掉。
  - 保存 `code`、`message`、`detail`（`dict(detail)`）。
  - `str(e)` = `f"{code}: {message}"`，实现方式是 `super().__init__(f"{code}: {message}")`。
- `to_json()` 返回 `{"code": code, "message": message, "detail": dict(detail)}`。`detail` 的值必须可以被 `canonical_json` 序列化，这由调用方保证。
- **错误码必须以字符串字面量作为第一个位置参数传入**，例如 `FactoryError("BEAN_NOT_FOUND", ...)`。**禁止**用变量或拼接，否则静态检查（5.12）无法核对。
- M1-01 至少登记本文件第 6 章列出的错误码。其他规范的错误码由各自的任务登记。

### 5.8 `Database` 的打开

1. `path.parent.mkdir(parents=True, exist_ok=True)`。
2. `conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)`。`isolation_level=None` 表示自动提交模式，事务完全由 `tx()` 显式管理。
3. `conn.row_factory = sqlite3.Row`。
4. 依次执行下面三条 PRAGMA：
   - `PRAGMA journal_mode=WAL`：返回值（小写比较）不是 `wal` 时，关闭连接并抛出 `FactoryError("DB_UNSUPPORTED", ..., mode=...)`；
   - `PRAGMA foreign_keys=ON`；
   - `PRAGMA busy_timeout=5000`。
5. 持有一把 `threading.RLock`，以及事务深度计数 `depth = 0`。

### 5.9 事务：`tx()` 与 `query()`

`tx()` 是上下文管理器，进入时先获取 RLock，退出时释放。

- **外层**（`depth == 0`）：
  - 执行 `BEGIN IMMEDIATE`；
    - 失败且是 `sqlite3.OperationalError`（`database is locked` / `busy`）时，抛出 `FactoryError("DB_BUSY", ...)`；
  - `depth = 1`，产出 `conn`；
  - 正常退出：`COMMIT`；
  - 异常退出：`ROLLBACK`，然后重新抛出原异常；
  - 无论哪种情况，最后都把 `depth` 复位为 0。
- **内层**（`depth ≥ 1`，同一线程嵌套调用）：
  - 执行 `SAVEPOINT sp_<depth>`，`depth += 1`；
  - 正常退出：`RELEASE sp_<n>`；
  - 异常退出：先 `ROLLBACK TO sp_<n>`，再 `RELEASE sp_<n>`，然后重新抛出；
  - 外层捕获内层异常后继续执行并正常退出时，外层提交的内容**不含**内层的修改。
- `query(sql, params)`：获取 RLock，执行 `conn.execute(sql, params).fetchall()`，返回 `list[sqlite3.Row]`。
  - 在事务内外都可以调用。
  - 约定只用于读；写操作必须写在 `with db.tx() as conn:` 中。
- `close()`：关闭连接。之后再使用属于编程错误。

### 5.10 `migrate(migrations_dir=None)`

1. `d = migrations_dir or DEFAULT_MIGRATIONS`。目录不存在时视为没有迁移文件。
2. 收集 `d` 下所有 `*.sql` 文件（不递归）：
   - 文件名不匹配 4.1 的正则、编号为 000、编号重复，都抛出 `FactoryError("DB_MIGRATION_INVALID", ..., file=<文件名>)`。
   - 按编号升序排列。
3. `current = PRAGMA user_version`。
   - 若 `current` 大于所有文件中的最大编号，且确实存在迁移文件，抛出 `FactoryError("DB_TOO_NEW", ..., version=current, latest=max)`。
   - 一个文件也没有、而 `current > 0` 时同样是 `DB_TOO_NEW`（`latest=0`）。
4. 对每个编号 `n > current` 的文件，按顺序执行：

   ```python
   conn.executescript("BEGIN IMMEDIATE;\n" + sql + f"\n;PRAGMA user_version = {n};\nCOMMIT;")
   ```

   - 出现 `sqlite3.Error` 时：若连接仍在事务中（`conn.in_transaction`），执行 `ROLLBACK`；然后抛出 `FactoryError("DB_MIGRATION_FAILED", ..., file=<文件名>, error=str(e))`。
   - 失败时，该文件的修改和版本号都不生效；之前已成功的文件保持已应用。
5. 返回 `PRAGMA user_version` 的最终值。重复调用不做任何事，返回同一个值。
6. `migrate` 在执行期间持有 RLock，并且不能在 `tx()` 内部调用（`depth > 0` 时 `RuntimeError`）。

### 5.11 `Context.open` 与 `install_factoryctl`（M1-03）

`Context.open(root, *, provider, clock=None, ids=None)`：

1. `root = Path(root).resolve()`；依次创建 `root`、`root/cas`、`root/jobs`、`root/tmp`、`root/tools/factory/bin`（`parents=True, exist_ok=True`）。
2. `db = Database(root / "factory.db")`；`db.migrate()`。
3. `install_factoryctl(root)`。
4. `clock = clock or SystemClock()`；`ids = ids or RandomIds()`。
5. `warehouse = Warehouse(root, db, clock)`。
6. 返回 `Context(root, clock, ids, db, warehouse, provider)`。`close()` 关闭 `db`。

`install_factoryctl(root, source=None)`：

1. `source = source or FACTORYCTL_SOURCE`。`source` 不是文件时返回 `None`，不创建目标。这是 M1-05 实现之前的正常情况。
2. `data = source.read_bytes().replace(b"\r\n", b"\n")`。
3. `target = root / "tools/factory/bin/factoryctl"`。
   - 目标已存在且内容等于 `data` 时，不写入，直接返回 `target`；
   - 否则先写到 `root/tmp/factoryctl.<pid>.part`，再 `os.replace` 到 `target`。
4. `os.chmod(target, 0o755)`，然后返回 `target`。

### 5.12 静态约束（由 `test_m1_01_foundation.py` 守护）

测试用 `ast` 解析 `src/mao/` 下的每个 `*.py`，检查下面的规则：

| 规则 | 适用范围 | 判定方法 |
|---|---|---|
| 禁止调用内建 `eval`、`exec`（D3） | 全部文件 | `ast.Call`，且 `func` 是 `ast.Name`，名字为 `eval` 或 `exec`。方法调用如 `sandbox.exec(...)` 不算 |
| 禁止 `shell=True`（README 3.7） | 全部文件 | 任何 `ast.Call` 的关键字参数 `shell` 的值是常量 `True` |
| 禁止 import `anthropic`、`openai`、`jinja2`（D3、D10） | 全部文件 | `import x` 或 `from x import ...`，其中 x 的第一段是这些名字 |
| 禁止 import `random`、`uuid`、`secrets`（D9） | 除 `mao/base.py`、`mao/ctl/factoryctl.py` 外 | 同上 |
| 禁止调用 `datetime.now()`、`datetime.utcnow()`、`time.time()`、`os.urandom()`（D9） | 除 `mao/base.py`、`mao/ctl/factoryctl.py` 外 | `ast.Call`，且 `func` 是名为 `now`/`utcnow` 的属性，所属对象是名字 `datetime` 或属性 `.datetime`；或 `func` 是 `time.time`；或 `func` 是 `os.urandom` |
| `FactoryError` 的第一个参数是已登记的字符串字面量（README 3.3） | 全部文件 | 调用名为 `FactoryError` 的 `ast.Call`（名字或属性），第一个位置参数必须是 `str` 常量，并且在 `CODES` 中 |

`mao/ctl/factoryctl.py` 运行在沙箱内，不能 import `mao`（README D7），所以 D9 的规则不约束它。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `JSON_INVALID` | `canonical_json` 的输入含不允许的值、非字符串键或非有限浮点数 | `path` |
| `DURATION_INVALID` | `parse_duration` 的输入不是合法时长 | `text` |
| `DB_UNSUPPORTED` | 无法启用 WAL 模式 | `mode` |
| `DB_BUSY` | `BEGIN IMMEDIATE` 等待超时（数据库被其他进程锁住） | `error` |
| `DB_MIGRATION_INVALID` | 迁移文件名不合法、编号为 000 或编号重复 | `file` |
| `DB_MIGRATION_FAILED` | 执行某个迁移文件时 SQLite 报错 | `file`、`error` |
| `DB_TOO_NEW` | 数据库版本高于现有迁移文件的最高编号 | `version`、`latest` |

`ValueError`、`AssertionError`、`RuntimeError` 表示编程错误，不登记错误码。包括：`utc_iso` 收到不带时区的时间、ID 前缀不合法、时钟拨回、未登记的错误码、在事务里调用 `migrate`。

---

## 7. 验收测试

文件：`tests/acceptance/m1/test_m1_01_foundation.py`。运行方式：`uv run python tools/harness.py pytest tests/acceptance/m1/test_m1_01_foundation.py`。

| 测试 | 验证 |
|---|---|
| `test_canonical_json_sorting_and_separators` | 5.1：键排序、紧凑分隔符、非 ASCII 原样、tuple → 数组 |
| `test_canonical_json_rejects_invalid` | 5.1：NaN/Inf、非字符串键、set、自定义对象 → `JSON_INVALID` |
| `test_canonical_bytes_is_utf8` | 5.1 |
| `test_digest_bytes_known_vector` | 5.2：空串的 sha256 |
| `test_digest_file_matches_bytes_and_streams` | 5.2：大于 1 MiB 的文件 |
| `test_utc_iso_formats_and_truncates` | 5.3：截断毫秒、时区换算、年份补零 |
| `test_utc_iso_rejects_naive` | 5.3 |
| `test_parse_duration_table` | 5.4：合法输入表 |
| `test_parse_duration_invalid` | 5.4：非法输入表 |
| `test_fake_clock` | 5.5：默认起点、`now` 稳定、`advance`/`sleep`、`monotonic`、负数报错 |
| `test_fake_clock_requires_aware_start` | 5.5 |
| `test_system_clock_is_aware_utc` | 5.5 |
| `test_counter_ids` | 5.6：各前缀独立计数、格式 |
| `test_random_ids_format_and_uniqueness` | 5.6 |
| `test_id_prefix_validation` | 5.6 |
| `test_factory_error_registered_and_unregistered` | 5.7 |
| `test_codes_table_shape` | 4.2：键格式、说明非空、包含第 6 章的错误码 |
| `test_codes_documented_in_specs` | 4.2：每个登记的错误码都出现在 `spec/system/*.md` 中 |
| `test_static_rules` | 5.12：全部静态约束（扫描整个 `src/mao/`） |
| `test_database_pragmas_and_rows` | 5.8、5.9：WAL、外键、busy_timeout、`sqlite3.Row` |
| `test_tx_commit_and_rollback` | 5.9 |
| `test_nested_tx_uses_savepoints` | 5.9：内层失败只回滚内层 |
| `test_migrate_applies_in_order_with_gaps` | 5.10：允许编号不连续、幂等 |
| `test_migrate_invalid_names` | 5.10：`DB_MIGRATION_INVALID` |
| `test_migrate_failure_rolls_back` | 5.10：`DB_MIGRATION_FAILED`，版本号不变 |
| `test_migrate_too_new` | 5.10：`DB_TOO_NEW` |
| `test_migrate_default_dir` | 5.10：默认目录可以执行 |

`Context.open` 与 `install_factoryctl` 的验收在 `test_m1_03_warehouse.py`（`test_context_open_layout`、`test_install_factoryctl_normalizes_newlines`）。

---

## 8. 错误示范与正确示范

**8.1 用 `json.dumps` 默认参数算哈希**

```python
# ✖ 键顺序、空白、ASCII 转义都不固定，同一个对象在不同地方算出不同的摘要
h = hashlib.sha256(json.dumps(obj).encode()).hexdigest()
# ✔ 一律经过 canonical_bytes
h = digest_bytes(canonical_bytes(obj))
```

**8.2 在组件里直接取当前时间或随机数**

```python
# ✖ 测试无法复现，输出无法逐字节比较
created_at = datetime.now(timezone.utc).isoformat()
job_id = "j-" + uuid.uuid4().hex[:10]
# ✔ 从注入的时钟和 ID 源取
created_at = utc_iso(self.clock.now())
job_id = self.ids.new("j")
```

**8.3 错误码不用字面量**

```python
# ✖ 静态检查核对不了，错误码可能根本没有登记
raise FactoryError(f"{kind.upper()}_NOT_FOUND", "找不到")
# ✔ 每个错误码都是字面量，并且登记在 CODES 里
raise FactoryError("BEAN_NOT_FOUND", f"Bean {bean_id} 不存在", bean_id=bean_id)
```

**8.4 嵌套事务直接再 BEGIN**

```python
# ✖ SQLite 不支持嵌套 BEGIN；或者内层失败把外层也一起回滚
with db.tx() as c:
    c.execute("BEGIN")            # sqlite3.OperationalError
# ✔ 嵌套调用 tx()，由它用 SAVEPOINT 处理
with db.tx() as c:
    c.execute("INSERT ...")
    try:
        with db.tx() as c2:
            c2.execute("INSERT ...")
            raise SomeError()
    except SomeError:
        pass                      # 外层提交时只包含第一条 INSERT
```

**8.5 修改已发布的迁移文件**

```text
✖ 直接编辑 001_warehouse.sql 增加一列：已有数据库的 user_version 已经是 1，新列永远不会被创建
✔ 新增 003_add_column.sql：ALTER TABLE ... ADD COLUMN ...
```

---

## 9. 风险与注意事项

- **Windows 与 WAL**：WAL 需要共享内存文件（`-wal`、`-shm`），网络盘上可能失败，这时报 `DB_UNSUPPORTED`。`FACTORY_ROOT` 应当放在本地磁盘。
- **多进程**：CLI 审批与 runner 可能同时写库。`BEGIN IMMEDIATE` 加 `busy_timeout=5000` 可以避免大多数死锁。连续出现 `DB_BUSY` 说明有长事务，应当缩短事务，而不是加大超时。
- **线程**：同一个 `Database` 可以跨线程使用（`check_same_thread=False`），并发由 RLock 串行化。不要在持有事务时做慢操作（运行子进程、等待沙箱）。
- **`executescript`**：它会先提交当前事务，所以 `migrate` 不能在 `tx()` 内调用（5.10 第 6 条）。
- **浮点与哈希**：`canonical_json` 用 Python 的 `repr` 表示浮点数，`1` 与 `1.0` 序列化结果不同、哈希也不同。需要稳定哈希的数据，要在进入哈希前就确定类型。
- **静态约束的边界**：5.12 检查的是常见写法，不是安全边界。例如 `getattr(time, "time")()` 能绕过检查，代码评审时要留意这类写法。
