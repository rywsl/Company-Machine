# warehouse：仓库（mao.warehouse）

> 规范性文档，遵循 `spec/system/README.md`。任务：**M1-03**。
> CAS、Bean 身份、封存、血缘都在这一份里定死。风险 R2 的正确示范指向本章第 5 章的公式；R10 指向 `seal_many` 的单事务。

---

## 1. 目的与范围

本组件实现 `$FACTORY_ROOT` 上的内容寻址仓库：

- Blob 只写一次，按 SHA-256 存放；
- Bean 是 `{type, meta, payload}` 的内容地址，未封存时只是候选；
- 封存是 `seals` 表里的一行（README 名词表）；一次 `seal_many` 在同一个事务里完成；
- 血缘可向上、向下查询；
- `verify` 对照 CAS 与索引，不修改数据。

不做：来料 / 出厂检验（`gate.md`）；Job 状态机（`runner.md`，迁移 `002`）；S3 / MinIO（M4，R11）。M1 的后端就是本地目录加 SQLite。

`Context.open` 与 `install_factoryctl` 的算法在 `foundation.md` 5.11，由 M1-01 写在 `mao.context` 里。本任务提供它要装配的 `Warehouse`。验收测试 `test_context_open_layout`、`test_install_factoryctl_normalizes_newlines` 放在本任务的验收文件里（foundation 第 7 章已点名）。本任务的 `paths` **没有** `context.py`：装配与 5.11 不符时停下来记录，不要越界去改。

---

## 2. 依赖

- 标准库：`pathlib`、`os`、`shutil`、`json`、`hashlib`（只经由 `mao.base`）、`sqlite3`（只经由 `mao.db`）。
- `mao.base`：`canonical_json`、`canonical_bytes`、`digest_bytes`、`digest_file`、`utc_iso`、`Clock`。
- `mao.errors.FactoryError`、`mao.db.Database`。
- **禁止** import `mao.snapshot`、`mao.values`、`mao.gate`、`mao.sandbox`、`mao.context`。M1-02 与 M1-03 并行。`bean.json` 的字段表在本章写死，与 `bean_value` 相同，不通过 import 复用。
- **禁止**新增第三方依赖（D2）。不要用 `tempfile.gettempdir()` 做 CAS 的临时文件（可能跨卷，`os.replace` 在 Windows 上会失败，R5）。

---

## 3. 接口

下面与 README 4.4 **逐字相同**。

```python
@dataclass(frozen=True)
class PayloadFile:
    path: str; digest: str; size: int; media_type: str

@dataclass(frozen=True)
class Bean:
    id: str                    # "sha256:<hex>"，内容地址，算法见 warehouse.md
    type: str                  # 带 minor 的类型名，如 "video.mp4@1.3"
    meta: dict[str, Any]
    payload: tuple[PayloadFile, ...]   # 按 path 排序
    facts: dict[str, Any]      # 首次封存时的实测值；未封存时为 {}
    sealed: bool
    created_at: str

@dataclass(frozen=True)
class BeanDraft:
    type: str; meta: dict[str, Any]; files: Mapping[str, Path]   # payload 相对路径 → 宿主文件

class Warehouse:
    def __init__(self, root: Path, db: Database, clock: Clock) -> None
    def put_file(self, path: Path) -> str              # 返回摘要；重复内容不重复存储
    def blob_path(self, digest: str) -> Path
    def add(self, draft: BeanDraft) -> Bean            # 登记候选 Bean（未封存）；幂等
    def get(self, bean_id: str) -> Bean                # 不存在 → FactoryError("BEAN_NOT_FOUND")
    def seal_many(self, items: Sequence[tuple[str, dict[str, Any]]], *,
                  job_id: str | None, report_digest: str) -> None   # [(bean_id, facts)]，同一事务
    def materialize(self, bean_id: str, dest: Path, *, layout: Literal["port", "bean"]) -> None
    def record_lineage(self, job_id: str, inputs: Mapping[str, Sequence[str]],
                       outputs: Mapping[str, Sequence[str]]) -> None
    def lineage(self, bean_id: str, direction: Literal["up", "down"]) -> dict[str, Any]
    def list(self, *, type_prefix: str | None = None, sealed_only: bool = True) -> list[Bean]
    def verify(self) -> list[dict[str, Any]]           # CAS 与索引的一致性问题清单；空表示完好
```

额外的纯函数（README 允许组件规范增加签名之外的函数，不改变上面的签名）：

```python
def media_type_of(path: str) -> str
```

`BeanDraft` 没有 `media_type` 字段，仓库也不能读 Type Spec（不依赖快照）。`media_type` 因此只由 payload 相对路径决定，见 5.3。`Gate` 用同一个函数核对类型声明。

`Warehouse` 保存 `root`（`Path(root).resolve()`）、`db`、`clock`。不在构造函数里 `migrate`；调用方（`Context.open` 或测试）先 `db.migrate()`。

---

## 4. 数据格式

### 4.1 CAS 布局

```text
$FACTORY_ROOT/cas/sha256/<前 2 位十六进制>/<其余 62 位>
$FACTORY_ROOT/tmp/                         同卷临时文件，供 os.replace
```

`digest` 匹配 `^sha256:[0-9a-f]{64}$`。设 `hex = digest[7:]`，则

```python
blob_path(digest) == root / "cas" / "sha256" / hex[:2] / hex[2:]
```

只用 `pathlib` 拼接。禁止 `f"{root}/cas/sha256/{hex[:2]}/{hex[2:]}"`（R5）。大写十六进制、长度不对、缺少 `sha256:` 前缀 → `DIGEST_INVALID`，`blob_path` 不猜。

Blob 写入后不再修改、不再删除（M1 没有回收）。

### 4.2 `bean.json` 与 Bean 身份

`materialize` 写出的 `bean.json` 是下面这个对象的 `canonical_bytes`（UTF-8，无尾随换行）。字段与 README 4.3 的 `bean_value` 相同。未封存时 `facts` 是 `{}`，因为库里的 `facts` 就是 `{}`。

```python
{
    "id": bean.id,
    "type": bean.type,
    "facts": bean.facts,
    "meta": bean.meta,
    "payload": [
        {"path": f.path, "digest": f.digest, "size": f.size, "media_type": f.media_type}
        for f in bean.payload
    ],
}
```

Bean id **不是**这个对象的哈希（id 不能自己哈希自己）。身份对象只有三把键（R2）：

```python
{
    "type": type_name,          # 带 minor，如 "note.text@1.1"
    "meta": meta,               # 调用方给出的映射，原样进入规范 JSON
    "payload": [                # 按 path 升序（码点序）
        {"digest": digest, "media_type": media_type, "path": path, "size": size},
        ...
    ],
}
id = digest_bytes(canonical_bytes(身份对象))
```

`canonical_json` 会再按键排序，所以上面的键顺序不影响 id。`size` 与 `media_type` 都参与身份：同一字节、不同路径或不同 `media_type` 是不同的 Bean。来源证明、facts、封存记录、`created_at` 都不参与 id（架构文档 A.11）。

### 4.3 迁移 `001_warehouse.sql`

文件名必须匹配 foundation 4.1。内容不得包含 `BEGIN` / `COMMIT` / `ROLLBACK` / `PRAGMA user_version`。不要引用 `jobs` 表（那是迁移 `002`，本文件先执行）。

```sql
CREATE TABLE beans (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    meta_json TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    sealed INTEGER NOT NULL DEFAULT 0 CHECK (sealed IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE payload_files (
    bean_id TEXT NOT NULL REFERENCES beans(id),
    path TEXT NOT NULL,
    digest TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0),
    media_type TEXT NOT NULL,
    PRIMARY KEY (bean_id, path)
);

CREATE TABLE seals (
    id INTEGER PRIMARY KEY,
    bean_id TEXT NOT NULL REFERENCES beans(id),
    job_id TEXT,
    report_digest TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    sealed_at TEXT NOT NULL
);

CREATE UNIQUE INDEX seals_dedupe ON seals(bean_id, report_digest, ifnull(job_id, ''));

CREATE TABLE lineage (
    job_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    port TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    bean_id TEXT NOT NULL REFERENCES beans(id),
    PRIMARY KEY (job_id, direction, port, ordinal)
);

CREATE INDEX beans_type ON beans(type);
CREATE INDEX seals_bean ON seals(bean_id);
CREATE INDEX lineage_bean ON lineage(bean_id);
```

`meta_json` / `facts_json` 存 `canonical_json` 的文本。读回用 `json.loads`。`sealed` 为 1 当且仅当 `seals` 里至少有一行该 `bean_id`。

### 4.4 `verify` 的问题对象

返回的每一项都有字符串字段 `kind`。没有问题就返回 `[]`。不抛 `FactoryError`。

| `kind` | 其余字段 | 含义 |
|---|---|---|
| `missing_blob` | `digest`、`bean_id` | payload 引用的摘要在 CAS 中没有文件 |
| `digest_mismatch` | `path`（相对 `root` 的 POSIX 路径）、`expected`、`actual` | 文件内容的 `digest_file` 与路径所暗示的摘要不同 |
| `id_mismatch` | `bean_id`、`expected`（库存 id）、`actual`（按 4.2 重算） | 索引与身份公式不一致 |
| `seal_flag` | `bean_id`、`sealed` | `beans.sealed` 与“是否有封存行”不一致 |
| `facts_unsealed` | `bean_id` | `sealed = 0` 但 `facts_json` 不是 `{}` |

按 `(kind, path, bean_id, digest)` 排序，缺的字段当空串。孤儿 Blob（`put_file` 之后没有 Bean 引用）不是问题。

### 4.5 `lineage` 的返回值

```python
{
    "id": bean_id,
    "direction": "up",             # 或 "down"
    "jobs": [                      # 按 job_id 升序
        {
            "job_id": "j-0001",
            "inputs": {"note": ["sha256:…"]},    # 端口名升序；id 保持记录时的顺序
            "outputs": {"note": ["sha256:…"]},
        }
    ],
    "beans": ["sha256:…"],         # 可达的其他 Bean，不含自己，升序
}
```

没有血缘时 `jobs` 与 `beans` 都是空列表。dict 相等性与键顺序无关；列表顺序有关。

---

## 5. 行为

### 5.1 `put_file`

1. `digest = digest_file(path)`。文件不存在让 `FileNotFoundError` 冒泡。禁止把整个文件读进内存再哈希（R20）。
2. `dest = blob_path(digest)`。`dest` 已存在：不打开、不覆盖、不比较，直接返回 `digest`。内容若已被篡改，由 `verify` 报告，`put_file` 仍不改写。
3. 否则：`root/tmp` 不存在就创建。把源文件按 1 MiB 分块复制到 `root/tmp/<hex>.<pid>.part`（同卷）。复制结束后 `os.replace` 到 `dest`。`dest.parent.mkdir(parents=True, exist_ok=True)` 在 replace 之前。
4. replace 之前若 `dest` 已被其他进程创建好：删掉临时文件，返回 `digest`，不覆盖。
5. 返回 `digest`。

空文件的摘要是 `sha256:` 加空字节串的 SHA-256。重复调用返回同一个字符串，且 CAS 里仍然只有一个文件。

### 5.2 `blob_path`

只做 4.1 的路径计算和格式检查。不要求文件已经存在。格式不对 → `FactoryError("DIGEST_INVALID", "摘要格式不合法", digest=digest)`。

### 5.3 `media_type_of`

取 payload 相对路径**最后一段**里最后一个 `.` 之后的后缀，转成小写。没有 `.` 就没有后缀。

| 后缀 | media_type |
|---|---|
| `txt` | `text/plain` |
| `json` | `application/json` |
| `md` | `text/markdown` |
| `mp4` | `video/mp4` |
| `srt` | `application/x-subrip` |
| `vtt` | `text/vtt` |
| 其他 | `application/octet-stream` |

这张表覆盖 `spec/examples/registry` 与 M1 夹具里出现的后缀。不要用 `mimetypes`（Windows 注册表会让同一后缀得到不同结果，R5）。`Gate` 再拿类型 Spec 里声明的 `media_type`（可以是 `video/*` 这种 fnmatch）与这个结果比较；对不上是结构检查失败，不是仓库改类型。

### 5.4 payload 路径

`BeanDraft.files` 的键是沙箱里的相对路径，不是宿主路径。必须同时满足：

- 非空，且不含 `\`、`:`、`*`、`?`、`"`、`<`、`>`、`|`；
- `PurePosixPath` 的每个分段都不是 `""`、`.`、`..`；
- 不是绝对路径（不以 `/` 开头）。

否则 `FactoryError("PAYLOAD_PATH_INVALID", "payload 路径不合法", path=path)`。宿主侧的 `Path` 值用 `pathlib` 打开，可以是 Windows 路径；那是值，不是键。

### 5.5 `add`

1. `type` 必须完整匹配 `^[A-Za-z0-9][A-Za-z0-9._-]*@[0-9]+\.[0-9]+$`（`note.text@1.1`、`video.mp4@1.3` 都通过）。只有 major、没有 minor → `FactoryError("BEAN_TYPE_INVALID", "Bean 类型必须带 minor", type=type)`。仓库不检查这个类型在不在 Registry 里。
2. `canonical_json(meta)` 失败时，把 `JSON_INVALID` 原样抛出（`mao.base` 已经登记）。
3. 按 5.4 检查每个路径。对每个文件 `put_file`，`size = Path(文件).stat().st_size`，`media_type = media_type_of(路径)`。
4. 按 4.2 计算 `id`。
5. 在一个 `with self.db.tx()` 里：
   - 若 `beans` 已有该 id：不改 `created_at`、不改 `facts`、不改 `sealed`，直接 `get` 返回（幂等）。内容相同则 id 相同，所以不会出现“同 id 不同 payload”。
   - 否则插入 `beans`（`facts_json` 为 `canonical_json({})`，`sealed` 为 0，`created_at = utc_iso(self.clock.now())`）和 `payload_files`。
6. 返回 `get(id)`。未封存，`facts == {}`，`sealed is False`，`payload` 按 `path` 升序。

`get` 返回的 `meta` / `facts` 是新的 dict。调用方修改它们不影响下一次 `get`。

### 5.6 `get` / `list`

`get`：没有行 → `FactoryError("BEAN_NOT_FOUND", f"Bean {bean_id} 不存在", bean_id=bean_id)`。有行则组装 `Bean`。`payload` 按 `path` 升序。`sealed` 看列 `sealed`，不临时数 `seals`（不一致由 `verify` 报告）。

`list(type_prefix=None, sealed_only=True)`：

- `sealed_only` 为真时只要 `sealed = 1`；
- `type_prefix` 不是 `None` 时，保留 `bean.type.startswith(type_prefix)` 的项。用 Python 的 `startswith`，不要用未转义的 SQL `LIKE`（`_` 会误匹配）。`note.text` 匹配 `note.text@1.1`，不匹配 `note@1.0`。`note` 两者都匹配。
- 排序：`created_at` 升序，再 `id` 升序。

### 5.7 `seal_many`

整个函数体在**一个** `with self.db.tx()` 里。任何失败都回滚这一次调用的全部封存（R10）。调用方若已经持有外层事务，这里变成 SAVEPOINT（foundation 5.9），于是 Runner 可以把 `seal_many` 和 `record_lineage` 包在同一个外层事务里。

`report_digest` 必须通过 5.2 的格式检查，否则 `DIGEST_INVALID`，且一个 Bean 都不封。

`items` 为空：成功返回，不写行。

对每一对 `(bean_id, facts)`，按给出的顺序：

1. Bean 不存在 → `BEAN_NOT_FOUND`，整批回滚。
2. `canonical_json(facts)` 失败 → `JSON_INVALID`，整批回滚。
3. 该 Bean 还没有封存行：
   - 把 `beans.facts_json` 更新为 `canonical_json(facts)`，`sealed` 置 1；
   - 插入 `seals`（`sealed_at = utc_iso(self.clock.now())`，`job_id` 允许 `None`）。
4. 已经封存：
   - `canonical_json(facts)` 必须与库存 `facts_json` 完全相同，否则 `FactoryError("FACTS_CONFLICT", "封存 facts 与首次封存不一致", bean_id=bean_id)`，整批回滚。不更新 `facts`，不改 `created_at`。
   - 相同，且 `(bean_id, report_digest, job_id)` 已有封存行：跳过（幂等）。
   - 相同，但是新的 job 或新的报告摘要：再插入一行 `seals`。`sealed` 保持 1。

一次调用里同一个 `bean_id` 出现两次且 facts 不同：按顺序处理，第二条触发 `FACTS_CONFLICT` 并回滚第一条。不要提供 `seal_one`。

### 5.8 `materialize`

1. `bean = get(bean_id)`。不存在则 `BEAN_NOT_FOUND`，不创建 `dest`。
2. `layout` 不是 `"port"` 或 `"bean"` → `ValueError`。
3. `dest.mkdir(parents=True, exist_ok=True)`。`dest` 已是文件 → `FactoryError("DEST_INVALID", "物化目标不是目录", dest=str(dest))`。
4. 布局：
   - `"port"`：每个 payload 文件写到 `dest / 相对路径`，`bean.json` 写到 `dest / "bean.json"`。这是 `/factory/in/<端口>/` 的形状。
   - `"bean"`：payload 写到 `dest / "payload" / 相对路径`，`bean.json` 写到 `dest / "bean.json"`。这是 validator 沙箱 `/bean/` 的形状。
5. 复制用 `shutil.copyfile`，不要 `read_bytes()`。父目录要创建。相对路径在 5.4 已经禁止 `..`，不会逃出 `dest`。
6. `bean.json` 的字节就是 4.2 的 `canonical_bytes`。已存在的同名文件覆盖。不删除 `dest` 里无关的文件。

列表端口由调用方按序号建子目录，再对每个 Bean 用 `"port"` 布局。本方法不解释列表。

### 5.9 `record_lineage` / `lineage`

`record_lineage` 在一个事务里：

1. `DELETE FROM lineage WHERE job_id = ?`（同一 job 再记一次就是替换，不是追加）。
2. 对 `inputs` 的每个端口（按端口名升序），按序列顺序插入 `direction = 'in'`，`ordinal` 从 0 起。`outputs` 同样，`direction = 'out'`。
3. 每个 bean id 都必须已存在，否则 `BEAN_NOT_FOUND`，整次替换回滚（旧的该 job 行也回到删除前）。不要求已封存：Runner 可以在外层事务里先 `seal_many` 再 `record_lineage`。
4. 不写事件。事件是 `runner.md` 的事。

`lineage(bean_id, direction)`：

1. `get(bean_id)`，不存在则 `BEAN_NOT_FOUND`。
2. `direction` 不是 `"up"` / `"down"` → `ValueError`。
3. `"up"`：从“输出里有它的 job”走到那些 job 的输入 Bean，再递归。`"down"`：从“输入里有它的 job”走到那些 job 的输出 Bean，再递归。
4. 用 visited 集合防环。`jobs` 是走过的 job 的完整输入输出（不是只含相关端口），按 `job_id` 升序。`beans` 是除起点外访问到的 Bean id，升序。
5. 不返回类型、不返回 facts。调用方要细节就 `get`。

### 5.10 `verify`

只读。不持有写事务。对每个 `cas/sha256/<2>/<62>` 普通文件：期望摘要是 `"sha256:" + 父目录名 + 文件名`，`digest_file` 不同则 `digest_mismatch`。对每个 payload 行：Blob 不存在则 `missing_blob`。对每个 Bean：按 4.2 重算 id，不同则 `id_mismatch`；`sealed` 列与封存行的有无不一致则 `seal_flag`；未封存但 facts 不是 `{}` 则 `facts_unsealed`。

重算 id 时使用库存的 `type`、`json.loads(meta_json)`、`payload_files` 的四列。不要重新 `media_type_of`（库存的 media_type 就是当时写入的；公式变了应由 `id_mismatch` 暴露，而不是悄悄重推）。

### 5.11 与 `Context.open` 的衔接

`Context.open`（foundation 5.11）在 `db.migrate()` 之后执行 `Warehouse(root, db, clock)`。构造函数不得要求 Registry、不得打开 Docker、不得调用 `datetime.now()`。`created_at` 与 `sealed_at` 只用注入的 `clock`。

`install_factoryctl` 的换行规范化不在本模块。验收测试直接调用 `mao.context.install_factoryctl`，期望与 foundation 5.11 一致：`\r\n` 换成 `\n`，内容相同则不重写，源文件不存在则返回 `None`。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `BEAN_NOT_FOUND` | `get` / `seal_many` / `materialize` / `lineage` / `record_lineage` 引用了不存在的 Bean | `bean_id` |
| `DIGEST_INVALID` | 摘要不是 `sha256:` 加 64 位小写 hex | `digest` |
| `PAYLOAD_PATH_INVALID` | payload 相对路径含 `..`、反斜杠、盘符或绝对路径 | `path` |
| `BEAN_TYPE_INVALID` | `type` 不是带 minor 的类型名 | `type` |
| `JSON_INVALID` | `meta` 或 `facts` 不能做规范 JSON（`mao.base` 抛出，原样传播） | `path` |
| `FACTS_CONFLICT` | 再次封存时 facts 的规范 JSON 与首次不同 | `bean_id` |
| `DEST_INVALID` | `materialize` 的目标已存在且不是目录 | `dest` |

编程错误（`ValueError`、`FileNotFoundError`、`AttributeError`）不包装。

---

## 7. 验收测试

文件：`tests/acceptance/m1/test_m1_03_warehouse.py`。

```bash
uv run python tools/harness.py pytest tests/acceptance/m1/test_m1_03_warehouse.py
```

| 测试 | 验证 |
|---|---|
| `test_put_file_cas_layout_and_idempotent` | 4.1 的路径；空文件摘要；第二次 `put_file` 不另建文件 |
| `test_bean_id_and_add_get` | 4.2 的身份公式；`text/plain`；未封存 facts 为 `{}`；`created_at` 来自 `FakeClock`；深拷贝；再次 `add` 不改 `created_at` |
| `test_payload_path_and_type_rejected` | `PAYLOAD_PATH_INVALID`、`BEAN_TYPE_INVALID`、`DIGEST_INVALID`、`BEAN_NOT_FOUND` |
| `test_seal_many_transaction_and_conflict` | 一批封存后 `sealed` 与 facts；未知 id 整批回滚；`FACTS_CONFLICT` 不改首次 facts；相同 facts 的第二次封存成功 |
| `test_lineage_up_and_down` | 4.5 的形状；向上看到输入、向下看到输出；引用未知 Bean 时整次写入回滚 |
| `test_materialize_layouts` | `"port"` 与 `"bean"` 的相对路径；`bean.json` 字节等于 4.2 |
| `test_list_and_verify` | `type_prefix` 与 `sealed_only`；篡改 Blob → `digest_mismatch`；改 `meta_json` → `id_mismatch` |
| `test_context_open_layout` | foundation 5.11：目录、`factory.db`、`warehouse` 非空、`close` |
| `test_install_factoryctl_normalizes_newlines` | CRLF 变成 LF；源缺失返回 `None` |

---

## 8. 错误示范与正确示范

**8.1 用测试里的例子特判 Bean id**

```python
# ✖ 换一组 meta 就对不上，而且违反 R2
if meta == {} and type_ == "note.text@1.1":
    return "sha256:3b0c…"
# ✔ 对 {type, meta, payload} 的规范 JSON 求 SHA-256
return digest_bytes(canonical_bytes({"type": type_, "meta": meta, "payload": records}))
```

**8.2 封存循环写到一半**

```python
# ✖ 第三个失败时前两个已经提交
for bean_id, facts in items:
    seal_one(bean_id, facts)
# ✔ 一个 tx，失败就回滚整批
with self.db.tx() as conn:
    for bean_id, facts in items:
        ...
```

**8.3 在 Windows 上拼 CAS 路径，或把临时文件写到系统临时目录**

```python
# ✖ D:\factory/cas/sha256/ab/cd 混用分隔符；C:\Users\...\Temp 与 D:\factory 跨卷时 os.replace 失败
dest = f"{root}/cas/sha256/{h[:2]}/{h[2:]}"
tmp = Path(tempfile.gettempdir()) / "blob.part"
# ✔
dest = root / "cas" / "sha256" / h[:2] / h[2:]
tmp = root / "tmp" / f"{h}.{os.getpid()}.part"
os.replace(tmp, dest)
```

**8.4 把大文件一次读进内存**

```python
# ✖ 视频有几个 GB
digest = digest_bytes(path.read_bytes())
# ✔
digest = digest_file(path)
shutil.copyfile(src, tmp)
```

---

## 9. 风险与注意事项

- **R5 / S3**：开发机是 Windows。Blob 路径、payload 键、`bean.json` 都走 `pathlib` 与 POSIX 相对路径。文本用 `encoding="utf-8"`。不要用 `mimetypes`。`os.replace` 的源和目标都在 `root` 下。
- **R10**：`seal_many` 自己就是一个事务。不要拆成多次 `commit`。血缘与封存要一起成功时，由调用方再包一层 `db.tx()`。
- **R20**：`put_file`、`materialize`、`verify` 都分块读写。验收测试用小文件，实现仍不得 `read_bytes()`。
- **R2**：身份公式只有 4.2 那一种。不要把 facts、job id、时间放进哈希。
- **并发**：短事务 + WAL。不要在 `tx()` 里 `copyfile` 大文件；先把 Blob 落到 CAS，再开事务写索引。`put_file` 的文件复制在事务外。
- **不回收**：M1 不删除 Blob，也不把 `sealed` 改回 0。隔离、过期是后续里程碑的事。
- **`media_type_of` 与类型 Spec 可能不一致**：例如 Spec 要求 `video/mp4` 而文件名是 `clip.bin`。仓库仍按后缀记录 `application/octet-stream`。拒绝是 `Gate` 的结构检查，不是 `add` 的错误。
