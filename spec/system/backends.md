# 可替换存储后端（backends.md）

> 规范性文档，M4。模块：`mao.backends`（S3/MinIO 的 CAS、Postgres 状态库、数据迁移）。
> 依据：架构文档 5.5、14.1；README D2、D8。
> M1–M3 的 `Warehouse` 与 `Database` 签名不改变。那些模块不得读取本文件的配置，也不得在未安装后端时访问 S3 或 Postgres。
> 不新增第三方依赖（D2）。S3 用标准库 HTTP。Postgres 用 `psql` 命令行，与 Docker 用 `docker` 命令行同一原则。

---

## 1. 目的与范围

**做什么（M4）**

- 在现有仓库接口后面换上可替换的 Blob 存储：本地目录（M1 行为）或 S3/MinIO。
- 在现有状态库接口后面换上 Postgres。SQLite 仍是 `Database(path)` 的行为。
- 把已有工厂从本地 CAS + SQLite 迁到新后端，校验通过才切换指针。校验失败不切换。

**不做什么**

| 不做 | 归属 |
|---|---|
| 修改 `Warehouse.__init__`、`Database.__init__`、`Context.open` 的签名 | 禁止。远程后端走本文件的 `open_context` |
| 让 M1–M3 代码 `import mao.backends` 才能启动 | 禁止（D8）。不配置后端时，那些路径与现在相同 |
| `boto3`、`psycopg`、MinIO SDK、ORAS | 禁止。架构 5.5 的 OCI 制品不在本里程碑 |
| 在异常或日志里写下访问密钥 | 禁止（D13） |
| 规定“迁多少行算通过”以外的业务门槛 | 校验是字节与行数一致，不另设抽样比例 |

---

## 2. 依赖

| 依赖 | 用到的接口 |
|---|---|
| `Warehouse` | 全部现有方法的**行为**保持。本文件用委托实现它们，不改签名 |
| `Database` | `migrate`、`tx`、`query`、`close` 的调用方式保持。SQLite 类本身不改 |
| `mao.base` | `digest_bytes`、`digest_file`、`canonical_json`、`Clock` |
| 标准库 | `urllib.request`、`hmac`、`hashlib`、`subprocess` |
| 外部命令 | `psql`。不在 PATH 时 `BACKEND_UNAVAILABLE`，禁止静默退回 SQLite 并声称迁移成功 |

---

## 3. 接口

```python
class BlobStore(Protocol):
    def put_file(self, path: Path) -> str          # 返回 "sha256:<hex>"；相同内容不重复写
    def get_file(self, digest: str, dest: Path) -> None
    def exists(self, digest: str) -> bool
    def verify(self) -> list[dict[str, Any]]       # 空列表表示完好

class LocalBlobStore:
    def __init__(self, root: Path) -> None         # $FACTORY_ROOT/cas/sha256/<2>/<62>，与 M1 布局相同

class S3BlobStore:
    def __init__(self, *, endpoint: str, bucket: str, region: str,
                 access_key_env: str, secret_key_env: str,
                 addressing: Literal["path", "virtual"] = "path",
                 timeout_s: float) -> None
        # 从环境变量读密钥。环境变量不存在 → BACKEND_CONFIG_INVALID。
        # 不接受把密钥作为普通字符串参数传入（避免出现在构造调用的日志里）。

class StateStore(Protocol):
    def migrate(self) -> int
    def tx(self) -> ContextManager[Any]            # 连接支持 execute(sql, params)、commit、rollback
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Any]
        # 行支持按列名取值，与 sqlite3.Row 的调用方用法相同
    def close(self) -> None

class PostgresStore:
    def __init__(self, *, dsn_env: str, psql: str = "psql", timeout_s: float) -> None
        # DSN 从环境变量读。psql 不在 PATH → BACKEND_UNAVAILABLE

def open_context(root: Path, *, provider: "SandboxProvider",
                 clock: "Clock | None" = None, ids: "IdSource | None" = None,
                 backend: Path | None = None) -> "Context"
    # backend 为 None，或指向的文件不存在：调用 Context.open，不读任何云配置。
    # 文件存在：按第 4.1 节构造 BlobStore / StateStore，再装配出满足 Context 字段的对象。
    # Context.open 的签名不增加参数。
```

`Warehouse` 的方法仍由调用方按 README 4.4 使用。`open_context` 返回的 `ctx.warehouse` 在配置了 S3 时，`put_file` / `get` / `seal_many` 的可观察结果与本地实现相同：digest 算法不变，`blob_path` 仍返回本地 `Path`（缓存文件，见 5.2）。`ctx.db.tx()` 在 Postgres 上返回的对象不是 `sqlite3.Connection`，但支持现有调用方使用的 `execute` 与按列名取值。M1 的 `Database.tx` 注解仍然是 `sqlite3.Connection`；那是 SQLite 类的签名，本文件不改它。

### 3.1 CLI

```
factory backend status [--json]
factory backend migrate --from local --to FILE [--dry-run] [--json]
factory backend verify [--json]
```

`--to` 是一份后端配置文件的路径，不是连接串。连接串放在环境变量里。`--dry-run` 做完复制与校验后不写指针。退出码：校验失败为 1，配置错误为 2，命令缺失为 4。

---

## 4. 数据格式

### 4.1 配置文件

`$FACTORY_ROOT/backend.json`（或 `--to` 指向的文件）。不存在则整个工厂保持 M1 布局。`Context.open` 不读这个文件。

```json
{
  "blobs": {
    "kind": "s3",
    "endpoint": "http://127.0.0.1:9000",
    "bucket": "mao",
    "region": "us-east-1",
    "addressing": "path",
    "access_key_env": "MAO_S3_ACCESS_KEY",
    "secret_key_env": "MAO_S3_SECRET_KEY",
    "timeout": "30s"
  },
  "state": {
    "kind": "postgres",
    "dsn_env": "MAO_PG_DSN",
    "psql": "psql",
    "timeout": "30s"
  }
}
```

`kind` 省略或为 `local` / `sqlite` 时用现有实现。`timeout` 是时长字符串。缺任一必填键 → `BACKEND_CONFIG_INVALID`。文件里禁止出现密钥的值；出现形如密钥的字符串 → `BACKEND_CONFIG_INVALID`，不连接。

指针文件 `$FACTORY_ROOT/backend-active.json` 只在迁移校验通过后写入，内容是配置文件的规范 JSON 摘要与 `switched_at`。`open_context` 读的是这个指针，不是人正在编辑的草稿。

### 4.2 对象键

S3 对象键与本地相对路径相同：`cas/sha256/<前 2 位>/<其余 62 位>`。值为原始字节，不是再包一层 JSON。`Content-Type` 为 `application/octet-stream`。用户元数据只放 `sha256`（hex，无前缀），便于核对。

### 4.3 Postgres 会话

`PostgresStore` 对每次 `tx()` 启动一个 `psql` 进程：

```
psql -X -q -v ON_ERROR_STOP=1 -d <dsn> --no-password
```

DSN 来自环境变量，不放在 argv 的可被 `ps` 看见的位置时仍可能泄漏。因此 DSN 通过 `PGPASSFILE` 指向一个仅当前用户可读的临时文件，argv 里只留主机与库名。临时文件在 `close` 时删除。禁止 `shell=True`。

SQL 仍写 `?` 占位符（与现有调用方一致）。适配器在送出前把 `?` 换成 `$1`、`$2`…，并把参数作为 psql 的变量绑定，不拼进 SQL 字符串。做不到绑定时，只允许适配器按 5.4 的转义规则替换；转义失败 → `SQL_DIALECT`，不发送。

SQLite 迁移文件已发布的不得修改。Postgres 使用同编号的翻译结果，规则见 5.3。翻译是纯函数，输入是 `src/mao/migrations/*.sql` 的文本。

### 4.4 迁移报告

规范 JSON，写入 CAS（本地或目标 BlobStore）并打印：

```json
{
  "tables": [{"name": "beans", "rows": 0, "digest": "sha256:…"}],
  "blobs": {"count": 0, "digest": "sha256:…"},
  "ok": true
}
```

表摘要：按主键升序，每行 `canonical_json` 之后串联，再 `digest_bytes`。Blob 摘要：按 digest 升序串联每个对象的 digest 与字节数。

---

## 5. 行为

### 5.1 装配

1. `open_context` 在 `backend is None` 或指针文件不存在时，调用 `Context.open` 并返回。不读取 `MAO_S3_*` 或 `MAO_PG_DSN`。这些变量存在也忽略。这是 D8：M1–M3 路径不假设后端存在。
2. 指针存在时，构造对应的 store，再构建 Context。`ctx.db.migrate()` 在打开时仍要调用，与现在相同。
3. `Warehouse.verify` 在 S3 实现上列出索引中的 digest，对每个做 `HEAD`，核对元数据里的 sha256 与对象大小。不一致列入返回值，不抛异常（与 M1 `verify` 的“返回问题清单”一致）。

### 5.2 S3

1. `put_file`：流式计算 digest（1 MiB 块，与 `digest_file` 相同）。`HEAD` 已存在且元数据 sha256 一致 → 不上传，返回原 digest。存在但 sha256 不一致 → `CAS_CONFLICT`，不覆盖。不存在 → `PUT`。PUT 使用 SigV4，签名范围是 `s3`，算法是标准库 `hmac` + SHA-256。只实现 `PutObject`、`GetObject`、`HeadObject`。不实现多段上传；单对象超过进程内存限制时，用分块读文件、分块签名的 payload hash，仍然是一次 `PutObject`。做不到的尺寸 → `BACKEND_UNAVAILABLE`，`detail.reason = "object-too-large"`，不要静默截断。
2. MinIO 用 `addressing: path`：`{endpoint}/{bucket}/{key}`。`virtual` 把桶放在主机名里。endpoint 必须是 `http` 或 `https`。其他方案 → `BACKEND_CONFIG_INVALID`。
3. `blob_path`：把对象下载到 `$FACTORY_ROOT/cas-cache/sha256/<2>/<62>`，返回该 `Path`。缓存不是真相来源。调用方按 M1 去读这个路径的字节。缓存文件的 digest 与请求不符 → 删掉缓存并 `CAS_MISSING`。
4. 密钥只在签名时从环境变量读取。签名字符串、URL、异常文本里出现密钥 → 视为缺陷。实现必须在抛出前扫描 `detail` 的字符串值。

### 5.3 SQL 翻译

对每个已发布的 `NNN_*.sql`，Postgres 适配器应用下列替换后执行。翻译结果不写回 sqlite 迁移文件。

| SQLite | Postgres |
|---|---|
| `INTEGER PRIMARY KEY` 且该列是 rowid 别名的单列主键 | `TEXT` 或原类型保持；本仓库主键都是 `TEXT`，保持 `TEXT PRIMARY KEY` |
| `AUTOINCREMENT` | 删除（本仓库的迁移不应出现；出现则 `SQL_DIALECT`） |
| `PRAGMA ...` | 不发送。版本号改记在 `schema_migrations(version INTEGER PRIMARY KEY)` |
| `user_version` | 不使用。`migrate()` 的返回值仍是已应用的最大编号 |
| 布尔按 0/1 存储 | 保持 INTEGER，不改成 BOOLEAN，避免调用方比较出错 |
| `?` 占位符 | 见 4.3 |

翻译器见到未在上表覆盖的 SQLite 专有语法（`WITHOUT ROWID`、`STRICT` 表、`INSERT OR IGNORE` 除外——`INSERT OR IGNORE` 译为 `INSERT ... ON CONFLICT DO NOTHING`）→ `SQL_DIALECT`，不执行半截迁移。

`schema_migrations` 与业务表的创建在同一个 `tx` 里。已记录的编号不重复执行。

### 5.4 参数转义（仅当不能用绑定变量时）

- 字符串：用标准 SQL 单引号，内部 `'` 加倍。拒绝含 NUL 的字符串。
- 整数与浮点：只用 `repr` 能被反向解析、且不含字母的形式。`nan` / `inf` → `SQL_DIALECT`。
- `None` → `NULL`。
- 其他类型 → `SQL_DIALECT`，不调用 `str` 拼进去。

优先使用绑定变量。转义是后备，不是默认。

### 5.5 数据迁移

`factory backend migrate --from local --to FILE`：

1. 取得写锁：在 SQLite 里插入 `backend_lock` 行（本文件的迁移 `032_backend_lock.sql` 只含这张表，本地库与目标库都执行）。已有未完成的锁 → `MIGRATE_IN_PROGRESS`。
2. 按外键顺序复制每张业务表。顺序由 SQLite 的 `sqlite_master` 与目标上已翻译的表结构决定，不是手写的表名清单（避免漏表）。
3. 复制 CAS：每个本地文件 `put_file` 到目标 BlobStore。
4. 在目标上计算 4.4 的摘要，与源的摘要比较。任一张表或 blob 集合不一致 → `MIGRATE_VERIFY_FAILED`，不写 `backend-active.json`，删除锁。目标上的数据保留，便于复查，但不被 `open_context` 使用。
5. `--dry-run`：做到第 4 步为止，同样不写指针。
6. 校验通过：写入指针，删除锁，写事件 `backend.switched`，`data` 只有摘要与时间，没有 DSN。
7. 切换之后，旧的 `factory.db` 与 `cas/` 不删除。回退是人把指针文件移走，使下一次 `open_context` 走 `Context.open`。本命令不提供“自动删云端”的开关。

复制过程中源库若有新写入，摘要会不一致，迁移失败。调用方应先停止工厂进程。本命令不强制杀进程；它只靠锁拒绝第二个迁移。

### 5.6 校验命令

`factory backend verify` 调用当前指针所指 store 的 `verify`，并重算 4.4 的表摘要与上次迁移报告比较（若报告还在）。不一致 → 退出码 1，打印问题清单。不修改数据。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `BACKEND_UNAVAILABLE` | `psql` 不在 PATH，S3 超时，或对象大到无法一次 PUT | `reason` |
| `BACKEND_CONFIG_INVALID` | 配置缺键、scheme 不对、或文件里出现密钥值 | `path, reason` |
| `MIGRATE_VERIFY_FAILED` | 行数或摘要与源不一致 | `table` 或 `blobs, source, target` |
| `MIGRATE_IN_PROGRESS` | 锁已存在 | `at` |
| `CAS_CONFLICT` | 同一键上的字节与 digest 不符 | `digest` |
| `CAS_MISSING` | 索引有、对象没有，或缓存与 digest 不符 | `digest` |
| `SQL_DIALECT` | 迁移含无法翻译的语法，或参数无法安全发送 | `file, reason` |

`detail` 与异常消息禁止含 DSN、访问密钥、签名 URL 的查询串。可以含 endpoint 的主机名与桶名。

---

## 7. 验收测试

S3 用测试内的标准库 HTTP 服务器实现 `PutObject` / `GetObject` / `HeadObject` 的子集，检查 SigV4 头存在即可，不要求对 AWS 公网。Postgres 用一个假的 `psql` 脚本：从 stdin 读 SQL，把结果以约定的 JSON 行写到 stdout，数据存在临时 SQLite 里。这样验收不依赖真实服务器，也零跳过。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m4/test_m4_04_backends.py` | `test_context_open_ignores_cloud_env` | 设置了 `MAO_S3_ACCESS_KEY` 仍调用 `Context.open` 时，不发生 HTTP；`open_context` 无指针文件时同样不发生 HTTP |
| | `test_s3_put_get_roundtrip` | 假 S3 上 `put_file` 再 `get_file`，字节相同；第二次 put 不重复上传（HEAD 之后无 PUT） |
| | `test_cas_conflict` | 假 S3 上同一键返回不同 sha256 元数据 → `CAS_CONFLICT`，对象未被覆盖 |
| | `test_blob_path_is_local_file` | `blob_path` 返回的 Path 可读，内容 digest 正确 |
| | `test_config_rejects_inline_secret` | 配置文件里写了访问密钥值 → `BACKEND_CONFIG_INVALID`，不发请求 |
| | `test_psql_missing` | PATH 上没有 `psql` → `BACKEND_UNAVAILABLE`，不创建 SQLite 冒充成功 |
| | `test_placeholder_not_concatenated` | 假 psql 收到的 SQL 不含参数原文被直接拼接的引号错误；含 `'` 的字符串被转义或绑定 |
| | `test_migrate_verify_and_pointer` | 本地两行与一个 blob 迁到假后端后，摘要一致，指针文件出现 |
| | `test_migrate_failure_keeps_pointer` | 假后端故意少写一个 blob → `MIGRATE_VERIFY_FAILED`，指针文件不存在，`open_context` 仍走本地 |
| | `test_pragma_not_sent` | 假 psql 的 stdin 不含 `PRAGMA`；`migrate()` 返回的版本号与 SQLite 路径相同 |
| | `test_no_third_party_import` | `mao.backends` 的导入图里没有 boto3、psycopg、minio |

```
uv run python tools/harness.py pytest tests/acceptance/m4/test_m4_04_backends.py
```

零失败、零跳过。

---

## 8. 错误示范与正确示范

**8.1 不要改 M1 构造函数**

```python
# ✖ 错误：给 Database.__init__ 增加 dsn 参数，或让 Context.open 读取 MAO_PG_DSN
# ✔ 正确：Context.open 与 Database(path) 保持原样。远程只走 open_context，且只在指针文件存在时
```

**8.2 校验失败不能切换**

```python
# ✖ 错误：复制到一半就写 backend-active.json，校验放到下次启动
# ✔ 正确：摘要不一致 → MIGRATE_VERIFY_FAILED，不写指针。下次 open_context 仍用本地库
```

**8.3 密钥不进参数与异常**

```python
# ✖ 错误：S3BlobStore(secret_key="...")，或 FactoryError(..., url=signed_url)
# ✔ 正确：只接收环境变量的名字。detail 里至多有 endpoint 主机名与 digest
```

**8.4 占位符不要拼进 SQL**

```python
# ✖ 错误：sql.replace("?", "'" + value + "'")
# ✔ 正确：交给 psql 绑定变量；做不到时按 5.4 转义，转义不了就 SQL_DIALECT
```

---

## 9. 风险与注意事项

- **`blob_path` 的缓存**。S3 实现返回的是缓存路径，不是 `s3://` URI。调用方若把返回值当成 CAS 真相并在上面写入，会破坏只读假设。缓存目录的文件在写完后应设为只读。
- **psql 与 Windows**。`psql` 的路径用 `pathlib`，argv 用列表。假 psql 在验收里放在 PATH 前，与假 `docker` 相同。
- **翻译不是第二份手写迁移**。新增 SQLite 迁移后，翻译器必须能处理它，否则 `SQL_DIALECT` 让 Postgres 路径失败，而不是悄悄跳过。已发布的 sqlite 文件仍然禁止修改（README 3.6）。
- **锁与崩溃**。进程在复制中被杀死会留下 `backend_lock`。人删除锁之前，`MIGRATE_IN_PROGRESS` 一直拒绝新的迁移。不要在超时后自动删锁并再迁一次（可能双写）。
- **D8 回归**。M1 验收不设置指针文件。任何在 `Warehouse.__init__` 或 `Database.__init__` 里探测云环境变量的改动都是缺陷。
