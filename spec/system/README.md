# 工厂控制面规范（spec/system）

> **规范性文档。** 本目录规定工厂控制面（M1–M4）怎样实现：模块划分、接口、数据格式、协议和约定。
> 架构上的“为什么”见 `docs/architecture.md`；结构的合法性由 MRL 判定（`spec/mrl/factory-profile.md`）。
> 本文件中的决定对所有组件规范和任务卡都有约束力。要改其中任何一条，先写 ADR（`docs/adr/`），由人批准。

规范用语：**必须** / **禁止** 是硬性要求，违反即视为缺陷；**应当** 是默认做法，偏离要在代码注释里写明理由；**可以** 表示允许但不要求。

---

## 0. 文档地图

| 文档 | 组件 | Python 模块 | 里程碑 | 任务 |
|---|---|---|---|---|
| `README.md`（本文件） | 跨组件决定、约定、接口 | — | 全部 | — |
| `foundation.md` | 基础设施：规范 JSON、哈希、时钟、ID、错误码、SQLite、Context | `mao.base` `mao.errors` `mao.db` `mao.context` | M1 | M1-01 |
| `snapshot.md` | Registry 快照与运行期 MRL 求值环境 | `mao.snapshot` `mao.values` | M1 | M1-02 |
| `warehouse.md` | 仓库：CAS、Bean、封存、血缘 | `mao.warehouse` | M1 | M1-03 |
| `sandbox.md` | 沙箱：`SandboxProvider`、Docker 与 Local 实现 | `mao.sandbox` | M1 | M1-04 |
| `factoryctl.md` | 沙箱内的 `factoryctl` 与宿主侧 `CtlServer` | `mao.ctl` | M1 | M1-05 |
| `gate.md` | 来料检验与出厂检验（V5） | `mao.gate` | M1 | M1-06 |
| `operator.md` | OperatorRuntime 调用、transcript 解析、Station Brief | `mao.operator` `mao.brief` | M1 | M1-07 |
| `runner.md` | Job 状态机、收货（harvest）、重试、审批、事件 | `mao.jobs` `mao.runner` | M1 | M1-08 |
| `cli.md` | `factory` 命令行 | `mao.cli` | M1 | M1-09 |
| `testing.md` | 测试分层、替身（fake agent / fake tools）、测试镜像、夹具 Registry | `tests/` | M1 | M1-10 |
| `lines.md` 等 M2–M4 文档 | 产线执行、Source/Sink、缓存、V3/V4、治理、动态结构 | 见各文档 | M2–M4 | 见 `plan/tasks.yaml` |
| `corrections.md` | M1–M4 纠偏（对应 `plan/drift.md`） | 多个模块 | M5 | M5-01 |
| `commands.md` | 补齐的命令行 | `mao.cli` `mao.commands` | M5 | M5-02 |
| `durable.md` | 租约与持久化执行 | `mao.durable` | M5 | M5-03 |
| `daemon.md` | 守护进程与信号泵 | `mao.daemon` | M5 | M5-04 |
| `mcp.md` | MCP 操作面 | `mao.mcp` | M5 | M5-05 |
| `supervisor.md` | 厂长值守 | `mao.supervisor` | M5 | M5-06 |
| `autonomy.md` | 自治闭环端到端 | `mao.daemon` `mao.supervisor` | M5 | M5-07 |
| `runtimes.md` | 真实 CLI 接入：控制面变量、代理归属与网络 | `mao.operator` `mao.egress` | M6 | M6-01 |
| `images.md` | 厂长构建机器镜像 | `mao.images` | M6 | M6-02 |
| `live.md` | 真实 CLI 在 Docker 里的端到端 | 测试与 `tools/` | M6 | M6-03 |
| `observability.md` | 状态、指标、事件流 | `mao.status` | M7 | M7-01 |
| `console.md` | 本机只读 Web 控制台 | `mao.console` | M7 | M7-02 |
| `retention.md` | 保留期、墓碑与回收 | `mao.retention` | M7 | M7-03 |
| `backup.md` | 备份、校验与恢复 | `mao.backup` | M7 | M7-04 |

每份组件规范都必须按第 7 节的模板组织。

---

## 1. 已定决定（D1–D14）

这些决定已经做出。任务执行者**禁止**在实现中改变它们；发现它们行不通时，停下来写 `plan/journal/` 记录并阻塞任务（见 `plan/README.md`）。

**D1 语言与包。** 控制面用 Python ≥ 3.12，包名 `mao`，源码放在 `src/mao/`，与 `src/mrl/` 同仓库、同一个 `pyproject.toml`。命令行入口是 `factory = "mao.cli:main"`。

**D2 依赖。** 运行时依赖只允许：标准库、`pyyaml`、`jsonschema`、本仓库的 `mrl`。新增任何第三方依赖都必须先有 ADR。
- 没有 Docker SDK：调用 `docker` 命令行（子进程）。
- 没有 ORM：直接用标准库 `sqlite3`。
- 没有 psutil、requests、pydantic。

**D3 MRL 是唯一的表达式与规则语言。** Spec 里出现的表达式和模板（属性、accept、审批条件、任务模板、argv 模板、收货规则模板……）一律用 `mrl.expr.compile_expr` / `compile_template` 编译。类型环境由 `mrl.factory.sites` 定义（factory-profile 第 4 章）。
- **禁止**使用 Python `eval`/`exec`、Jinja、`str.format` 来渲染 Spec 中的模板。
- 运行期构造的值（第 4.3 节）必须符合同一套类型环境。这样，编译期通过的表达式，运行期不会因为字段缺失或类型不符而出错。

**D4 只在通过检查的快照上运行。** 控制面执行任何 job 之前，Registry 必须通过 `mrl.factory.check.check_registry`，并且零 error。
- 一次运行固定在一个快照（epoch）上，运行期间快照不可变。
- M1 的 epoch 是 Registry 目录树的内容哈希；M3 起换成 Git commit。

**D5 沙箱接口。** 所有“在隔离环境里执行”的操作都经过 `SandboxProvider`（第 4.5 节），一共两个实现：
- `DockerProvider`：生产实现，通过 `docker` 命令行驱动本地 Docker。
- `LocalProvider`：仅供测试，在宿主上直接运行进程，**没有任何隔离**。它通过路径映射和命令映射，模拟沙箱内的文件系统契约。
- **禁止**在生产配置中使用 `LocalProvider`。CLI 默认 `--provider docker`。

**D6 沙箱内文件系统契约。** 沙箱内的路径是固定的虚拟路径（第 5 节）。Spec 中的配方（`run`、`harvest`、`place`）只能使用这些路径。宿主侧的真实路径永远不出现在 Spec 中。

**D7 控制通道是文件邮箱。** 沙箱内的 `factoryctl` 与宿主之间只通过 `/factory/ctl` 下的文件通信：原子写入，即写临时文件后 rename。
- 沙箱没有网络也能工作；**禁止**为控制通道开放网络端口。
- `factoryctl` 是单文件、只用标准库的 Python 脚本，兼容 Python ≥ 3.8，**禁止** import `mao`。
- 所有运行 agent 操作工、需要收货的机器镜像，都必须带 `python3`（≥ 3.8）和 `/bin/sh`。

**D8 持久化。** 状态库是 `$FACTORY_ROOT/factory.db`（SQLite，WAL 模式），Blob 存在 `$FACTORY_ROOT/cas/`。
- 表结构用编号 SQL 迁移文件管理（第 3.6 节）。
- 生产后端（S3/MinIO、Postgres）是 M4 的事，M1–M3 的接口不得假设它们存在。

**D9 确定性。** 凡是影响输出的时间和 ID，都必须来自注入的 `Clock` 和 `IdSource`（第 4.1 节）。
- 组件内**禁止**直接调用 `datetime.now()`、`time.time()`、`uuid4()`、`random`。`mao.base` 内的 `SystemClock` 和 `RandomIds` 除外。
- 测试用 `FakeClock` 和 `CounterIds`，让输出可以逐字节比较。

**D10 不造 agent。** 控制面内**禁止**直接调用任何 LLM API。一切模型调用都发生在沙箱内、由 OperatorRuntime 启动的现成 CLI 里。judge、planner、厂长也一样。

**D11 安全基线不可降级。**
- **禁止**挂载 docker.sock，**禁止** `--privileged`、`--network host`、`--cap-add`。
- 容器以非 root 运行，带 `--cap-drop ALL` 和 `--read-only`。
- 输入只读。
- 完整的参数清单见 `sandbox.md`，由单元测试锁定。

**D12 出网。** M1 只实现 `none`：容器不联网。
- 策略要求其他出网等级时，DockerProvider 必须拒绝运行（错误码 `EGRESS_UNSUPPORTED`）。唯一的例外是配置里显式打开开发开关 `allow_unproxied_egress`。
- 带域名白名单的出网代理和模型代理在 M2 实现。

**D13 秘密。** M1 不向沙箱注入任何秘密。M2 引入秘密时，秘密只经由环境变量进入沙箱，并且：
- **禁止**写入事件、日志、Brief、job 记录或报告；
- **禁止**出现在异常消息里。

**D14 预算。** M1 的花费在事后结算：job 结束后从 transcript 读出成本。结算结果超出工位预算时，job 失败（`BUDGET_EXCEEDED`），产物不封存，也不重试。运行中的实时计量要等 M2 的模型代理。

---

## 2. 名词对照（架构文档 → 代码）

| 架构文档 | 代码 | 说明 |
|---|---|---|
| Registry 快照 / epoch | `mao.snapshot.Snapshot`、`Snapshot.epoch` | M1：`"e-" + 目录树哈希前 16 位` |
| Bean | `mao.warehouse.Bean` | 内容寻址；封存记录单独存放 |
| 封存（SEALED） | `seals` 表中的一行 | 一个 Bean 至少有一条封存记录，才算已封存 |
| 来料检验 / 出厂检验 | `Gate.check_inputs` / `Gate.inspect` | V5 |
| 工位沙箱 | `Sandbox`（由 `SandboxProvider.open` 得到） | 一次 attempt 一个 |
| Station Brief | `mao.brief.Brief` | `task` + `factory_contract` + `markdown` |
| OperatorRuntime 调用 | `mao.operator.Invocation` | 渲染后的 argv 与 env |
| 收货规则 | Recipe `outputs.<port>.harvest` | 由 `factoryctl harvest` 在沙箱内执行 |
| Job 状态机 | `mao.jobs.JobState` | 见 `runner.md` |

---

## 3. 共同约定

### 3.1 目录布局

仓库内：

```
src/mao/            控制面（本规范）
src/mrl/            MRL 参考实现（M0，已完成）
spec/system/        本目录
tests/system/       实现者写的单元测试（可以自由增加）
tests/acceptance/   预先写好的验收测试（受保护，只能由人修改）
tests/fakes/        测试替身（受保护）
tests/fixtures/     夹具：Registry、测试镜像、样例文件（受保护）
```

`FACTORY_ROOT` 的布局：根目录取值依次为 `--root`、环境变量 `FACTORY_ROOT`、当前目录下的 `.factory`。

```
$FACTORY_ROOT/
  factory.db                  SQLite（WAL）
  cas/sha256/<前2位>/<其余62位>  Blob，写入后不再修改
  jobs/<job_id>/<attempt>/    每次 attempt 一个目录
      in/  out/  ctl/  workspace/(仅 LocalProvider)  logs/
  tools/factory/bin/factoryctl  安装到沙箱 /opt/factory 的只读目录
  tmp/                        临时文件（同一文件系统，便于原子 rename）
```

### 3.2 标识、时间、JSON、哈希、金额

- **规范 JSON**：`json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`，编码为 UTF-8。凡是要计算哈希或逐字节比较的 JSON，都必须用它（`mao.base.canonical_json`）。
- **摘要**：`"sha256:" + 小写十六进制`。文件摘要按 1 MiB 分块流式计算。
- **时间**：UTC，RFC 3339，毫秒精度，以 `Z` 结尾，例如 `2026-11-09T12:00:00.000Z`（`mao.base.utc_iso`）。数据库里存这种字符串。
- **时长**：Spec 中的时长匹配 `^[0-9]+(ms|s|m|h|d)$`，由 `mao.base.parse_duration` 换算成秒（float）。
- **ID**：`<前缀>-<后缀>`。前缀固定如下：

  | 前缀 | 用于 |
  |---|---|
  | `j` | job |
  | `a` | approval |
  | `r` | ctl request |
  | `ev` | 事件 |

  `RandomIds` 的后缀是 10 位小写 base32，`CounterIds` 的后缀是 4 位十进制序号（`j-0001`）。Bean 没有 ID 前缀，Bean ID 就是它的摘要（见 `warehouse.md`）。
- **金额**：USD，float，存储前用 `round(x, 6)`。比较预算时一律用“超过”（`>`），等于上限不算超。

### 3.3 错误与错误码

- 控制面的可预期错误一律抛出 `mao.errors.FactoryError(code, message, **detail)`。
  - `code` 是大写蛇形（如 `BEAN_NOT_FOUND`），必须登记在 `mao/errors.py` 的 `CODES` 表里，附一句中文说明。
  - `message` 是给人看的中文句子。
  - `detail` 是可以序列化为 JSON 的上下文。
- 每份组件规范的“错误”一章都列出本组件会抛的全部错误码。有一条测试保证：源码里出现的每个错误码都已登记，每个登记的错误码都在某份规范里出现。
- 编程错误（断言失败、意料之外的异常）不要包装成 `FactoryError`，让它冒泡。CLI 把它报告为内部错误，退出码 5。
- **禁止**用裸 `except:` 或 `except Exception: pass` 吞掉错误。需要兜底的地方，必须把异常转换成带错误码的 job 失败，并记录事件。

### 3.4 时钟与 ID 注入

```python
class Clock(Protocol):
    def now(self) -> datetime: ...          # 带时区的 UTC 时间
    def monotonic(self) -> float: ...       # 秒，单调递增
    def sleep(self, seconds: float) -> None: ...

class IdSource(Protocol):
    def new(self, prefix: str) -> str: ...
```

- `SystemClock`、`RandomIds` 用于生产。
- `FakeClock(start=datetime(2026, 1, 1, tzinfo=UTC))`：`now()` 返回当前虚拟时间。`sleep(s)` 和 `advance(s)` 推进虚拟时间，不真的等待。
- `CounterIds()`：每个前缀单独计数，依次产生 `j-0001`、`j-0002`……
- 需要真实等待的地方（轮询子进程、轮询邮箱）也调用 `clock.sleep`，这样测试可以用 `FakeClock` 跑完而不真的等。例外是 `CtlServer` 的后台线程，见 `factoryctl.md`。

### 3.5 事件与日志

- 状态变化一律写入 `events` 表。表结构由 `runner.md` 定义，事件种类清单也在 `runner.md`。事件数据是规范 JSON。
- 控制面自身的诊断输出用标准库 `logging`，logger 名为 `mao.<模块>`。日志不是接口，测试不得断言日志文本。
- 沙箱内进程的 stdout/stderr 写入 `jobs/<job>/<attempt>/logs/`，transcript 也放在这里。

### 3.6 SQLite 与迁移

- `mao.db.Database(path)` 打开连接，并设置：
  - `PRAGMA journal_mode=WAL`
  - `PRAGMA foreign_keys=ON`
  - `PRAGMA busy_timeout=5000`
- 迁移文件放在 `src/mao/migrations/NNN_<名字>.sql`，按编号顺序执行，已执行到的版本号记在 `PRAGMA user_version`。
- **已发布的迁移文件禁止修改**，要改表结构就新增一个迁移。
- 编号分配：

  | 编号 | 归属 |
  |---|---|
  | `001` | `warehouse.md` |
  | `002` | `runner.md`，含 jobs / events / approvals |
  | `003`–`009` | 留给 M1 内的增补 |
  | `010`–`023` | M2–M3（已发布） |
  | `024`–`026` | M5-01 纠偏（`model_caps`、`run_sink_mode`、`backend_lock`） |
  | `030`、`031` | M5-03、M5-06 |
  | `033`、`034` | M6-02、M7-03 |

  `mao.db` 按 `user_version` 跳过编号不大于它的文件，所以**新迁移的编号必须大于仓库里已有的最大编号**，不得插空或复用（`plan/drift.md` D-33）。

- 所有写操作都在 `with db.tx():` 中完成。一个逻辑操作（例如“封存一个 job 的全部产物”）必须在同一个事务里。

### 3.7 子进程

- 子进程一律用 `subprocess.run` / `subprocess.Popen`，传参数列表，**禁止** `shell=True`。
- 必须设置超时。超时后终止进程，并把结果报告为超时，不要抛出原始的 `TimeoutExpired`。
- 输出按 UTF-8 解码，`errors="replace"`。

### 3.8 编码、换行与 Windows

- 开发机是 Windows 11 + Git Bash，CI 和生产是 Linux。路径一律用 `pathlib.Path`，**禁止**手工拼接 `/` 或 `\`。
- 文本文件按 UTF-8 读写，必须显式写 `encoding="utf-8"`。
- 会被复制进 Linux 容器的文件（`factoryctl`、测试镜像里的脚本、Dockerfile）必须是 LF 换行，由 `.gitattributes` 保证。安装 `factoryctl` 时还要再把 `\r\n` 规范成 `\n`。
- 宿主路径传给 docker 时，用 `--mount type=bind,source=<绝对路径>,target=<虚拟路径>`。`-v` 写法在 Windows 盘符路径上有歧义，**禁止**使用。

---

## 4. 跨组件接口（规范性）

本节固定组件之间互相调用的签名。组件规范可以增加方法，但**禁止**改变这里列出的签名和语义。所有数据类都用 `@dataclass(frozen=True)`，有特别说明的除外。

### 4.1 `mao.base`、`mao.errors`、`mao.db`、`mao.context`（foundation.md）

```python
# mao.base
def canonical_json(obj: Any) -> str
def canonical_bytes(obj: Any) -> bytes
def digest_bytes(data: bytes) -> str                  # "sha256:<hex>"
def digest_file(path: Path) -> str
def utc_iso(dt: datetime) -> str
def parse_duration(text: str) -> float                # 秒；格式不对 → FactoryError("DURATION_INVALID")
class SystemClock; class FakeClock; class RandomIds; class CounterIds   # 见 3.4

# mao.errors
CODES: dict[str, str]                                 # 错误码 → 中文说明
class FactoryError(Exception):
    code: str; message: str; detail: dict[str, Any]
    def __init__(self, code: str, message: str, **detail: Any) -> None   # 未登记的 code → AssertionError
    def to_json(self) -> dict[str, Any]               # {"code","message","detail"}

# mao.db
class Database:
    def __init__(self, path: Path) -> None
    def migrate(self) -> int                          # 返回迁移后的版本号
    def tx(self) -> ContextManager[sqlite3.Connection]
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]
    def close(self) -> None

# mao.context
@dataclass
class Context:                                        # 不冻结：持有连接
    root: Path
    clock: Clock
    ids: IdSource
    db: Database
    warehouse: "Warehouse"
    provider: "SandboxProvider"
    @classmethod
    def open(cls, root: Path, *, provider: "SandboxProvider",
             clock: Clock | None = None, ids: IdSource | None = None) -> "Context"
    def close(self) -> None
```

### 4.2 `mao.snapshot.Snapshot`（snapshot.md）

```python
class Snapshot:
    root: Path
    epoch: str
    result: mrl.factory.check.CheckResult
    @classmethod
    def load(cls, root: Path) -> "Snapshot"           # 有 error → FactoryError("REGISTRY_INVALID", diags=[...])
    def key(self, kind: str, ref: str) -> str         # "explainer@3" → "Station:explainer@3"；接受已完整的 key
    def doc(self, key: str) -> dict[str, Any]         # 原始文档的深拷贝
    def spec(self, key: str) -> dict[str, Any]        # doc(key)["spec"]
    def type_key(self, tid: str) -> str               # "video.mp4@1" → "Type:video.mp4@1.3"（同 major 的最新 minor）
    def is_subtype(self, t: str, s: str) -> bool      # 类型 ID 之间；自反；与内核 subtype 关系一致
    def facts_decl(self, tid: str) -> dict[str, str]  # 含继承
    def validators(self, tid: str) -> list[dict[str, Any]]   # 该类型最新 minor 自身声明的 validator（与内核语义一致）
    def properties(self, tid: str) -> dict[str, "CompiledExpr"]   # 可见的命名属性（含继承），已编译
    def always(self, tid: str) -> frozenset[str]
    def compiled(self, subject: str) -> "CompiledExpr | CompiledTemplate"
        # factory-profile 第 4 章的每一个表达式位置，按 subject 取出已编译的形式（编译期与运行期用同一份）
    def keys(self, kind: str | None = None) -> list[str]
        # M5-01 增补：本快照全部 Spec 键（"Station:upper@1" 这种形式），升序；给出 kind 时只列该 kind。
        # 其他模块要遍历 Spec 时只能用它，禁止读 Snapshot 的私有成员（plan/drift.md D-02）
```

### 4.3 `mao.values`（snapshot.md）

运行期的值必须与 `mrl.factory.sites` 中的类型一一对应：

```python
def bean_value(bean: "Bean") -> dict[str, Any]
    # {"id","type","facts","meta","payload":[{"path","digest","size","media_type"}]}
    # 未封存的候选 Bean 的 facts 为 {}
def inputs_value(inputs: Mapping[str, "Bean | Sequence[Bean]"]) -> dict[str, Any]
def params_value(snapshot: Snapshot, station_key: str, given: Mapping[str, Any]) -> dict[str, Any]
    # 填默认值、按声明类型校验；不合法 → FactoryError("PARAM_INVALID")
def property_env(bean) -> dict;  def accept_env(out, inputs, params) -> dict
def approval_env(phase, payload, inputs, params) -> dict
def recipe_env(inputs, params) -> dict;  def runtime_env(brief, limits, operator) -> dict
```

### 4.4 `mao.warehouse`（warehouse.md）

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

### 4.5 `mao.sandbox`（sandbox.md）

```python
@dataclass(frozen=True)
class Mount:
    source: str                # 宿主绝对路径，或 "volume:<名字>"
    target: str                # 沙箱内的虚拟路径（第 5 节）
    readonly: bool

@dataclass(frozen=True)
class SandboxSpec:
    name: str                  # 唯一名字，如 "mao-j-0001-1"
    image: str                 # "<repo>@sha256:<64位hex>"；LocalProvider 忽略
    mounts: tuple[Mount, ...]
    workdir: str = "/workspace"
    env: Mapping[str, str] = field(default_factory=dict)
    network: Literal["none", "egress"] = "none"
    cpus: float | None = None
    memory_mb: int | None = None
    workspace_volume: str | None = None   # Docker：挂到 /workspace 的命名卷（用镜像 workdir 的内容初始化）

@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None      # 超时时为 None
    timed_out: bool
    duration_s: float

class Sandbox(Protocol):
    name: str
    def exec(self, argv: Sequence[str], *, timeout_s: float, stdout: Path, stderr: Path,
             env: Mapping[str, str] | None = None, workdir: str | None = None) -> ExecResult
    def close(self) -> None    # 销毁容器；不删除 workspace 卷

class SandboxProvider(Protocol):
    name: str                  # "docker" | "local"
    def open(self, spec: SandboxSpec) -> Sandbox
    def remove_volume(self, name: str) -> None

class LocalProvider:           # 仅测试
    def __init__(self, root: Path, *, commands: Mapping[str, Sequence[str]] | None = None) -> None
class DockerProvider:
    def __init__(self, root: Path, *, docker: str = "docker", allow_unproxied_egress: bool = False) -> None
    def build_run_argv(self, spec: SandboxSpec) -> list[str]   # 纯函数，供测试锁定安全参数
```

### 4.6 `mao.ctl`（factoryctl.md）

- 沙箱内：`src/mao/ctl/factoryctl.py`。安装后成为 `/opt/factory/bin/factoryctl`。
- 宿主侧：

```python
class CtlServer:
    def __init__(self, ctl_dir: Path, handler: "CtlHandler", clock: Clock) -> None
    def poll_once(self) -> int                         # 处理当前所有请求，返回处理的条数（测试用，确定性）
    def start(self, interval_s: float = 0.2) -> None   # 后台线程
    def stop(self) -> None

class CtlHandler(Protocol):
    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None
        # 返回响应；返回 None 表示暂不答复（例如审批还没决定），下次轮询再问
```

### 4.7 `mao.gate`（gate.md）

```python
@dataclass(frozen=True)
class Check:
    port: str; index: int; stage: str; name: str; ok: bool; detail: str
    # stage ∈ {"structure", "validator", "property", "accept", "consistency"}

@dataclass(frozen=True)
class GateReport:
    job_id: str; station: str; attempt: int
    verdict: Literal["pass", "reject", "error"]   # error = 检验设施故障（不是产物的问题）
    outputs: dict[str, list[str]]                 # 端口 → 候选 Bean ID（已登记、未封存）
    facts: dict[str, dict[str, Any]]              # Bean ID → 实测 facts
    checks: tuple[Check, ...]
    feedback: str                                 # 给下一次 attempt 的反馈（Markdown）
    def to_json(self) -> dict[str, Any]
    def digest(self) -> str

class Gate:
    def __init__(self, snapshot: Snapshot, warehouse: Warehouse, provider: SandboxProvider,
                 clock: Clock, ids: IdSource, *, workdir: Path) -> None
    def check_inputs(self, station_key: str, inputs: Mapping[str, str | Sequence[str]]
                     ) -> dict[str, "Bean | list[Bean]"]     # 不合格 → FactoryError("INTAKE_REJECTED", problems=[...])
    def inspect(self, station_key: str, *, job_id: str, attempt: int, out_dir: Path,
                inputs: Mapping[str, "Bean | Sequence[Bean]"], params: Mapping[str, Any]) -> GateReport
    def admit_external(self, type_id: str, files: Mapping[str, Path], meta: Mapping[str, Any]
                       ) -> tuple[GateReport, "Bean | None"]  # 外部物料入库（M1：`factory bean import`）
```

### 4.8 `mao.operator` 与 `mao.brief`（operator.md）

```python
@dataclass(frozen=True)
class Brief:
    task: str; factory_contract: str; markdown: str

def render_brief(snapshot: Snapshot, station_key: str, *, inputs: Mapping[str, "Bean | Sequence[Bean]"],
                 params: Mapping[str, Any], attempt: int, feedback: str | None, budget_usd: float) -> Brief

@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]; env: dict[str, str]; timeout_s: float; transcript: str | None
    # transcript: None 或 "stream-json"

def build_invocation(snapshot: Snapshot, station_key: str, brief: Brief, *,
                     inputs: Mapping[str, "Bean | Sequence[Bean]"], params: Mapping[str, Any]) -> Invocation

@dataclass(frozen=True)
class TranscriptSummary:
    cost_usd: float; turns: int | None; is_error: bool; result: str | None; events: int; malformed: int

def parse_transcript(path: Path, fmt: str) -> TranscriptSummary
```

### 4.9 `mao.jobs` 与 `mao.runner`（runner.md）

```python
class JobState(StrEnum):
    PENDING; PROVISIONING; RUNNING; AWAITING_APPROVAL; GATING; SEALED; REJECTED; FAILED; DEAD
TERMINAL = {JobState.SEALED, JobState.DEAD}

@dataclass(frozen=True)
class Job:
    id: str; station: str; epoch: str; state: JobState; attempt: int
    inputs: dict[str, list[str]]; params: dict[str, Any]; outputs: dict[str, list[str]]
    cost_usd: float; error: dict[str, Any] | None; feedback: str | None
    created_at: str; updated_at: str

class Runner:
    def __init__(self, ctx: Context, snapshot: Snapshot) -> None
    def submit(self, station_ref: str, inputs: Mapping[str, str | Sequence[str]],
               params: Mapping[str, Any]) -> Job                # 先做来料检验，再创建 PENDING job
    def run(self, job_id: str) -> Job                          # 阻塞，直到 SEALED 或 DEAD
    def decide(self, approval_id: str, decision: Literal["approve", "deny"], *, by: str, note: str = "") -> None
```

### 4.10 `mao.cli`（cli.md）

- 命令行入口 `factory`。全局参数：`--root`、`--json`。
- 退出码：

  | 退出码 | 含义 |
  |---|---|
  | 0 | 成功 |
  | 1 | 业务失败（job DEAD、检验不通过、完整性问题） |
  | 2 | 用法错误 |
  | 3 | Registry 不合法 |
  | 4 | 环境或设施错误（如没有 Docker） |
  | 5 | 内部错误 |

---

## 5. 沙箱内文件系统契约

| 虚拟路径 | 读写 | 内容 |
|---|---|---|
| `/factory/in/<port>/` | 只读 | 输入 Bean：payload 文件按其 `path` 放置，另有 `bean.json`（`bean_value` 的规范 JSON）。`list<T>` 端口为 `/factory/in/<port>/<i>/…`，`i` 从 0 开始。Recipe 的 `place` 可以指定 `/factory/in/` 下的其他目录 |
| `/factory/out/<port>/` | 读写 | 候选产物：payload 文件按 `path` 放置，可选 `meta.json`。`list<T>` 端口为 `/factory/out/<port>/<i>/…` |
| `/factory/ctl/` | 读写 | 控制通道：`job.json`、`brief.md`、`contract.json`、`requests/`、`responses/`、`submissions/`、`log.jsonl`、`fail.json`、`harvest.json`、`harvest-result.json`（格式见 `factoryctl.md`） |
| `/workspace` | 读写 | 机器的工作区。Docker 下是命名卷，用镜像 `workdir` 的内容初始化；resume 重试时沿用 |
| `/opt/factory/bin/factoryctl` | 只读 | 控制通道客户端 |
| Runtime `inject.mount`（如 `/opt/operator`） | 只读 | 注入的 agent CLI |
| `/bean/payload/<path>`、`/bean/bean.json` | 只读 | 仅 validator 沙箱：被检验的候选 Bean |
| `/tmp` | 读写 | tmpfs；`HOME=/tmp/home` |

沙箱内总是设置这些环境变量：

| 变量 | 值 |
|---|---|
| `FACTORY_IN` | `/factory/in` |
| `FACTORY_OUT` | `/factory/out` |
| `FACTORY_CTL` | `/factory/ctl` |
| `FACTORY_WORKSPACE` | `/workspace` |
| `FACTORYCTL` | `/opt/factory/bin/factoryctl` |
| `FACTORY_JOB_ID` | 当前 job ID |
| `FACTORY_ATTEMPT` | 当前 attempt |
| `HOME` | `/tmp/home` |
| `PATH` | `/opt/factory/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` |

`LocalProvider` 的模拟规则：
- 把 argv 和 env 值中出现的上述虚拟路径前缀，替换成 attempt 目录下的真实路径。
- argv[0] 先查 `commands` 映射。默认映射是 `python3` → 当前解释器，`/opt/factory/bin/factoryctl` → 安装后的 factoryctl。

除此之外，它不提供任何隔离：没有只读挂载，没有网络限制，没有资源限制。

---

## 6. 共享数据形状

下面这些 JSON 会跨组件、跨进程传递。规范 JSON 写出，字段名固定。具体字段表在对应组件规范里，这里只列归属：

| 数据 | 文件或位置 | 归属规范 |
|---|---|---|
| `bean.json` | `/factory/in/<port>/bean.json`、`/bean/bean.json` | `snapshot.md`（即 `bean_value`） |
| `job.json` | `/factory/ctl/job.json` | `factoryctl.md` |
| `contract.json` | `/factory/ctl/contract.json` | `factoryctl.md` |
| ctl 请求与响应 | `/factory/ctl/requests/`、`responses/` | `factoryctl.md` |
| `harvest.json` / `harvest-result.json` | `/factory/ctl/` | `factoryctl.md` |
| validator 输出 | validator 的 stdout | `gate.md` |
| 检验报告 | CAS Blob（规范 JSON） | `gate.md` |
| 事件 | `events` 表 | `runner.md` |

---

## 7. 组件规范模板

每份组件规范都必须包含下面这些章节，章节名和顺序不变，便于任务卡按节号引用：

1. **目的与范围**：做什么、不做什么，以及属于哪个里程碑。
2. **依赖**：依赖哪些组件；只能用第 4 节列出的接口。
3. **接口**：完整的 Python 签名，包括类、方法、参数、返回值和异常。
4. **数据格式**：文件、表结构（SQL）、JSON 字段表。
5. **行为**：逐条写算法和状态转换，要具体到可以照着实现。
6. **错误**：错误码表，列出错误码、触发条件和 `detail` 字段。
7. **验收测试**：对应的 `tests/acceptance/...` 文件，以及其中每个测试验证什么。
8. **错误示范与正确示范**：至少 3 组 ✖/✔ 对照，取最容易犯的错。
9. **风险与注意事项**：平台差异、并发、性能、安全。

---

## 8. 测试分层

| 层 | 位置 | 谁写 | 规则 |
|---|---|---|---|
| MRL 一致性 | `spec/mrl/conformance/` | M0 已完成 | 只增不改（`harness lock` 校验） |
| 单元测试 | `tests/system/` | 任务执行者 | 自由增加；不得依赖网络 |
| 验收测试 | `tests/acceptance/mN/test_mN_XX_*.py` | 预先写好 | **受保护**；任务完成的定义是对应文件全部通过、零跳过 |
| 场景 | `plan/scenarios/` | 预先写好 | 受保护 |

pytest 标记：

- `docker`：需要 Docker 守护进程和测试镜像。没有 Docker 时自动跳过；但设置了 `MAO_REQUIRE_DOCKER=1` 时，不跳过而是直接失败。里程碑检验设置这个变量，所以 Docker 测试不能被悄悄跳过。
- `slow`：耗时超过 10 秒。

验收测试文件以 `pytest.importorskip("mao.<模块>")` 开头，所以模块还不存在时整个文件跳过，不影响全局 `pytest`。任务验收用 `harness pytest <文件>` 运行，并要求 `skipped == 0`。

替身、测试镜像和夹具 Registry 的完整说明见 `testing.md`。

---

## 9. 明确不做的事

- 控制面里不写任何 agent 逻辑，不直接调用模型 API（D10）。
- 不做 Web UI。人和厂长都通过 `factory` 命令行操作（M3 可以在其上加 MCP 包装）。
- M1 不做产线执行、缓存、Source/Sink、出网代理、秘密注入、两阶段工位、judge。它们分别属于 M2 到 M4，见 `plan/milestones.md`。
- 不为“以后可能需要”预留抽象。接口按本节点的需要设计，演进时走 ADR。
