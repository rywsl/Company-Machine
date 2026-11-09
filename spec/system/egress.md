# 出网、模型代理与秘密（egress.md）

> 规范性文档，M2。模块：`mao.egress`。
> 依据：架构文档 6.4（出网分级、模型代理）；README D12、D13、D14。
> 跨组件约定见 `README.md`。`SandboxSpec.network` 仍是 `"none" | "egress"`，签名不改。
> `connectors.md` 第 5.6 节规定“给谁、不给谁”。本文件规定秘密放在哪、怎样注入、代理怎样放行。

---

## 1. 目的与范围

**做什么（M2）**

- 按策略把出网编译成**域名 × 方法**白名单，经宿主上的正向代理放行。
- 模型代理：只转发沙箱里 OperatorRuntime **已经发起**的请求，注入模型密钥，按响应计量，实时停在工位预算上。
- 秘密注入：非模型秘密只经环境变量进入沙箱。模型秘密只留在模型代理里。
- 秘密值不得出现在事件、日志、Brief、job 记录、报告或异常消息里（D13）。

**不做什么**

| 不做 | 归属 |
|---|---|
| M1 的 `network=none` 行为 | 策略只有 `none` 时，与 M1 相同：不设代理、不注入秘密 |
| 未开代理时放行出网 | 仍是 `EGRESS_UNSUPPORTED`。唯一例外仍是 `allow_unproxied_egress`（D12） |
| 在审批、调度、契约测试里调用模型 | 那些模块禁止访问本代理。本代理也不构造提示词、不替控制面做决定 |
| 新的 Python 依赖 | 只用标准库（`http.server`、`ssl`、`urllib`）。不引入 requests |
| 自签 CA 的生成算法 | 标准库签不出证书。CA 由操作员放到指定路径；没有 CA 时按第 5.4 节失败关闭 |
| 服务型机器的入站 webhook 监听 | M4。`ingress` 在 M2 只表示 Source **拉取**时的出站白名单 |

D10 禁止控制面自己当 agent 去调模型。模型代理不违反这一点：请求的发起者是沙箱内的 CLI，代理只注入密钥并记账。`mao.approvals`、`mao.scheduler`、`mao.contract` **禁止** import 本模块的转发函数。

---

## 2. 依赖

| 依赖 | 用到的接口 |
|---|---|
| `Snapshot` | `key`、`spec`、`doc`（工位、策略、机器的 `needs.secrets`） |
| `SandboxSpec` / `DockerProvider.build_run_argv` | 只追加参数，不删 M1 的安全参数（D11） |
| `Runner` | `PROVISIONING` 时调用 `build_egress`；封存前查看预算停止标记 |
| `connectors.md` 5.6 | 按 `secret_class` 做角色复查。分类名必须是本文件的四个值 |
| `mao.base` | `Clock`、`canonical_json`、`utc_iso`；金额 `round(x, 6)`，比较用 `>` |
| `mao.db` | 迁移 `014_egress.sql` |

不读 `datetime.now()`。代理日志的时间用注入的 `Clock`。

---

## 3. 接口

### 3.1 秘密

```python
SecretClass = Literal["publish", "ingress", "model", "other"]

class SecretStore:
    def __init__(self, root: Path) -> None
        # root 是 FACTORY_ROOT。清单见第 4.1 节
    def names(self) -> list[str]                       # 升序
    def secret_class(self, name: str) -> SecretClass  # 未知名字 → SECRET_NOT_FOUND
    def inject(self, names: Sequence[str]) -> dict[str, str]
        # 返回 name → 值。调用方不得把这个 dict 放进日志、事件、异常或 job 行
    def redact(self, text: str) -> str
    def redact_obj(self, obj: Any) -> Any              # 递归处理 dict / list / str
    def install_log_filter(self) -> None               # logger 名 mao.* 的 Filter，验收测试不断言日志文本
```

`secret_class` 就是 `connectors.md` 说的“标为 publish / ingress 类”。另两类：`model` 永不注入沙箱；`other` 可以按策略名单注入。

### 3.2 出网计划

```python
@dataclass(frozen=True)
class AllowRule:
    tier: str                              # providers | web_read | publish | ingress
    host: str                              # 小写，无方案、无路径
    methods: frozenset[str]                # 大写方法名；含 "*" 表示任意方法

@dataclass(frozen=True)
class EgressPlan:
    network: Literal["none", "egress"]
    allow: tuple[AllowRule, ...]           # 按 (tier, host, 方法排序后的方法串) 升序
    secret_names: tuple[str, ...]          # 将注入沙箱的名字，升序。不含 model 类
    withheld: tuple[str, ...]              # 点名了但被扣下的 model 类名字，升序
    env: dict[str, str]                    # 只有代理 URL 与 NO_PROXY，没有任何秘密值

def build_egress(snapshot: Snapshot, station_key: str, store: SecretStore) -> EgressPlan
def egress_run_args(network: Literal["none", "egress"]) -> list[str]
    # 纯函数，见第 5.6 节
```

`Runner` 在 `PROVISIONING` 调用 `build_egress`。`network == "none"` 时不启动代理，`SandboxSpec.network` 为 `none`。否则 `network` 为 `egress`，并把 `plan.env` 与 `store.inject(plan.secret_names)` 合并进 `SandboxSpec.env`。合并后的 env **不**写入 `jobs` 表、事件或 `build_run_argv` 的持久化副本。

### 3.3 代理

```python
class EgressProxy:
    def __init__(self, plan_for: Callable[[str], EgressPlan], store: SecretStore, clock: Clock) -> None
        # plan_for(job_id) 由宿主提供。代理不读 Registry 目录
    def start(self, host: str = "127.0.0.1", port: int = 0) -> int
        # 只绑定 host。port 0 由系统分配。返回实际端口
    def close(self) -> None

class ModelProxy:
    def __init__(self, store: SecretStore, clock: Clock, root: Path) -> None
    def start(self, host: str = "127.0.0.1", port: int | None = 8080) -> int
        # 生产默认 8080，与示例 OperatorRuntime 的 ANTHROPIC_BASE_URL 一致
        # 测试传入 0。8080 被占用 → MODEL_PROXY_BIND_FAILED，不要悄悄换端口
    def close(self) -> None
    def spent(self, job_id: str) -> float
```

两个代理都是宿主进程，不是沙箱里的进程，也不是新的容器镜像。

### 3.4 CLI 与 factoryctl

```
factory secret ls [--json]                # 只打印名字与分类，不打印值
factory egress check STATION@N [--json]   # 打印 EgressPlan，env 里没有秘密
```

沙箱内：

```
factoryctl budget
```

打印 `/factory/ctl/budget.json`。文件不存在时退出码 2。成功退出码 0。只用标准库。

---

## 4. 数据格式

### 4.1 宿主文件（不进 SQLite）

```
$FACTORY_ROOT/secrets/manifest.json     {"S3_WRITE_KEY": "publish", "ANTHROPIC_API_KEY": "model"}
$FACTORY_ROOT/secrets/values/<NAME>     原始字节，UTF-8 文本，无换行。权限只限当前用户
$FACTORY_ROOT/egress/prices.json        可选。见下
$FACTORY_ROOT/egress/upstreams.json     模型名 → 上游
$FACTORY_ROOT/egress/profiles.json      可选。ingress 的名字 → 域名列表
$FACTORY_ROOT/egress/ca.pem             可选。只含公钥，可挂进沙箱
$FACTORY_ROOT/egress/ca.key             可选。私钥。禁止挂进沙箱，禁止写入事件
```

`manifest.json` 的值只能是 `publish` / `ingress` / `model` / `other`。名字必须匹配 `^[A-Z][A-Z0-9_]*$`。否则 `SECRET_MANIFEST_INVALID`。值文件缺失：`SECRET_NOT_FOUND`。

`prices.json`（没有这个文件也可以启动；没有单价时的行为见第 5.5 节）：

```json
{"claude-opus-5-5": {"input_per_mtok": 15.0, "output_per_mtok": 75.0}}
```

单价是**操作员写进文件的数**。本文件不内置任何模型的价格。

`upstreams.json`：

```json
{"claude-opus-5-5": {"url": "https://upstream.example", "secret": "ANTHROPIC_API_KEY", "header": "x-api-key"}}
```

`secret` 是名字，不是值。`header` 省略则 `MODEL_UPSTREAM_UNKNOWN`（不猜测 `Authorization`）。可以有一条 `"*"` 作兜底。

`profiles.json`：`{"webhook": ["hooks.example.com"]}`。例子策略里的 `ingress: [webhook]` 不是域名，要在这里展开。

### 4.2 迁移 `src/mao/migrations/014_egress.sql`

```sql
CREATE TABLE model_usage (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id     TEXT NOT NULL,
  at         TEXT NOT NULL,
  model      TEXT NOT NULL,
  cost_usd   REAL NOT NULL,
  tokens_in  INTEGER,
  tokens_out INTEGER
);
CREATE INDEX model_usage_job ON model_usage(job_id);
CREATE TABLE budget_stops (
  job_id TEXT PRIMARY KEY,
  code   TEXT NOT NULL,
  at     TEXT NOT NULL
);
```

这两张表不存提示词、请求体、响应体、URL 查询串或秘密值。

### 4.3 预算文件与事件

`/factory/ctl/budget.json`（宿主在每次计量后原子写入）：

```json
{"cap_usd": 5.0, "spent_usd": 0.1, "remaining_usd": 4.9}
```

`remaining_usd = round(cap_usd - spent_usd, 6)`。没有工位预算时不写这个文件。

| kind | data |
|---|---|
| `egress.allowed` | `{job, host, method, tier}` |
| `egress.denied` | `{job, host, method, code}` |
| `secret.withheld` | `{job, name, class}` |
| `model.metered` | `{job, model, cost_usd, spent_usd}` |
| `model.budget_exceeded` | `{job, spent_usd, cap_usd}` |

`host` 不含用户信息、路径和查询串。方法是大写单词。

---

## 5. 行为

### 5.1 编译白名单 `build_egress`

读工位策略的 `spec.egress`。允许的键只有 schema 里的五个：`none`、`providers`、`web_read`、`publish`、`ingress`。

1. `none: true` 且还有别的键：抛 `EGRESS_CONFIG_INVALID`。job 不得启动。
2. 只有 `none`，或出网对象为空：`EgressPlan(network="none", allow=(), secret_names=要注入的非模型名字, …)`。不因为“机器 needs 里写了域名”就升级成 `egress`。
3. 其他等级的值，逐项规范成 `(host, methods)`：

| 写法 | 含义 |
|---|---|
| 字符串列表 | 每个元素一个 host，方法用下表的等级默认 |
| `"from-machine"` | 取机器 `needs.egress` 里**同一等级**的 host 列表。机器没写：零个 host，不报错，也不改用别的等级 |
| `true` | 仅 `web_read` 合法，表示任意 host。其他等级出现 `true`：`EGRESS_CONFIG_INVALID` |
| `{hosts: [...], methods: [...]}` | `methods` 全改成大写。`methods` 缺省时用等级默认 |
| 其他 | `EGRESS_CONFIG_INVALID` |

等级默认方法（这是白名单的合同，不是数字门槛；对象形式可以收窄或放宽）：

| 等级 | 默认方法 |
|---|---|
| `providers` | `*`（任意方法） |
| `web_read` | `GET`、`HEAD` |
| `publish` | `GET`、`HEAD`、`PUT`、`POST`、`DELETE`（核验要读，发布要写） |
| `ingress` | `GET`、`HEAD` |

4. host 规范：去掉方案和路径，转小写，去掉末尾的点。含 `/` 或空格：`EGRESS_CONFIG_INVALID`。没有点、且不是 IP 的记号（如 `webhook`）不是 host，到 `profiles.json` 里展开。档案没有这个名字：`EGRESS_PROFILE_UNKNOWN`。
5. 匹配是**整 host 相等**。`fal.run` 不覆盖 `api.fal.run`。不支持 `*` 前缀。端口不参与比较：允许的 host 上任意端口都算这个 host。
6. 要注入的名字按 `connectors.md` 5.6：策略 `secrets` 是列表就用列表；是 `from-machine` 就用机器 `needs.secrets`。其中 `secret_class == "model"` 的名字放进 `withheld`，不放进 `secret_names`，并写 `secret.withheld`。
7. `network`：第 2 步为 `none` 时是 `none`，否则是 `egress`。

机器 `needs.egress` **不能**并进白名单。策略没写的域名，机器写了也不放行。

### 5.2 注入

- `inject` 只在 `SandboxProvider.open` 之前调用，返回值只进入当次 `SandboxSpec.env`。
- job 行、事件、Brief、检验报告只记录 `secret_names` 与 `withheld`，不记录值。
- 模型类秘密不出现在 `SandboxSpec.env` 里。OperatorRuntime 的 `env` 继续指向 `http://model-proxy:8080`（示例已如此）。容器里没有模型密钥。
- 启动失败需要记录 argv 时，把 `-e NAME=值` 换成 `-e NAME=***`，再对整段文本 `redact`。这就是风险 R17。

### 5.3 脱敏

`redact` 的算法：

1. 取出全部秘密值，按长度从长到短。
2. 在文本里把每个非空值替换成 `***`。先替换长的，避免短值拆开长值后留下尾巴。
3. `redact_obj` 只处理 `str` / `dict` / `list`。其他标量原样返回。
4. 写事件、`FactoryError` 的 message、`detail`、job.error、Brief 追加文本、检验报告之前，必须经过 `redact` / `redact_obj`。
5. `install_log_filter` 对 `mao.*` 的 `LogRecord.msg` 和 `args` 做同样的替换。README 3.5：验收测试不断言日志文本。过滤器仍然要装。

秘密值会进入进程内存。这是注入所必需的。禁止把值放进 `FactoryError.detail`，即使已经替换成 `***` 以外的形式（不要为了排错把值再编码回去）。

### 5.4 出网代理

代理是 HTTP 正向代理，只监听 `127.0.0.1`。

识别 job：请求必须带 `X-Factory-Job: <job_id>`。没有、或 job 不是 `PROVISIONING` / `RUNNING` / `AWAITING_APPROVAL`：拒绝，`EGRESS_JOB_INVALID`，HTTP 403，body 为 `{"code":"EGRESS_JOB_INVALID"}`。已 `SEALED` 的 job 不能再被记账或放行。

放行：

1. 从请求行取出方法与 host。查询串、用户信息、body **不记录**。
2. host 与方法命中该 job 的 `AllowRule` 才转发。`methods` 含 `*` 时任意方法可过。
3. 不跟随重定向。每一跳由客户端重新请求，再检查一次。
4. 拒绝：HTTP 403，body `{"code":"EGRESS_DENIED"}`，写 `egress.denied`。响应不回显 URL。
5. 允许：写 `egress.allowed`，再字节流转发。转发目标是**规则里的 host**，不用客户端在别的头里另给的 IP。

HTTPS 与方法：

- 明文 HTTP：方法可见，按上表检查。
- `CONNECT`：代理只看得见 `CONNECT` 和 host，看不见隧道里的方法。
  - 该 host 的规则含 `*`：允许隧道（`providers` 的默认）。
  - 规则限制了方法，且 `$FACTORY_ROOT/egress/ca.pem` 与 `ca.key` 都存在：代理用该 CA 终结 TLS，检查内层方法，再连上游。公钥可以只读挂进沙箱；私钥禁止出现在任何挂载里。
  - 规则限制了方法，但没有 CA：拒绝，`EGRESS_METHOD_OPAQUE`。不要改成“先放行再补记”。
- 本模块不生成 CA。测试用明文 HTTP 覆盖“域名 × 方法”。`CONNECT` 在没有 CA 时被拒绝，这条本身不需要证书。

容器怎样走到代理，见第 5.6 节。`NO_PROXY` 包含 `model-proxy`，模型流量不进这台出网代理。

### 5.5 模型代理与实时计量

1. 同样要求 `X-Factory-Job`，且 job 处于第 5.4 节的三种状态。
2. 转发前：工位没有 `limits.budget_usd` → 不转发，HTTP 402，`{"code":"MODEL_BUDGET_UNSET"}`，写 `budget_stops`。不要当成无限预算。
3. 已有 `budget_stops`，或 `spent > cap`（`>`，等于上限仍可再转发一次直到越过）：不转发，HTTP 402，`{"code":"BUDGET_EXCEEDED"}`。
4. 模型名取请求 JSON 的 `model`，没有则为 `""`。用它查 `upstreams.json`，没有再查 `"*"`。都没有：`MODEL_UPSTREAM_UNKNOWN`，不转发。
5. 上游请求加上 `header: secret值`。这个头不写日志。上游 URL 不经过出网白名单：模型流量不占用 `providers` / `web_read`（架构 6.4）。上游连接由宿主进程拨出。
6. 读完响应后计量，取第一个成功的来源：
   - JSON 数 `cost_usd`，或 `usage.cost_usd`；
   - `usage.input_tokens` 与 `usage.output_tokens`；
   - `usage.prompt_tokens` 与 `usage.completion_tokens`。
   流式响应：把含上述字段的事件加总。结束时仍没有用量：不把成本记成 0。写 `budget_stops(code=MODEL_USAGE_UNPARSEABLE)`，向沙箱返回 502 `{"code":"MODEL_USAGE_UNPARSEABLE"}`，**不**转发上游 body。
7. 只有 token、没有 `cost_usd` 时：`cost = round((in * input_per_mtok + out * output_per_mtok) / 1_000_000, 6)`。价格表没有这个模型：不转发成功结果，`BUDGET_PRICE_MISSING`，同样写入 `budget_stops`。禁止用 0 单价。
8. 在一个短事务里插入 `model_usage`，`spent` 为该 job 的 `sum(cost_usd)`。写 `model.metered`，更新 `budget.json`。
9. 若 `spent > cap`：写 `model.budget_exceeded` 和 `budget_stops(code=BUDGET_EXCEEDED)`。已经发出的这一次算进去，不能撤回。
10. `Runner` 在 `GATING` / 封存之前查询 `budget_stops`。有行则 job `FAILED`，`error.code` 用该行的码（`BUDGET_EXCEEDED` 或 `MODEL_USAGE_UNPARSEABLE` 或 `BUDGET_PRICE_MISSING` 或 `MODEL_BUDGET_UNSET`），不封存，不重试（D14 的停止语义）。transcript 里的成本**不再相加**，避免双重记账。没有模型用量时，仍按 M1 从 transcript 结算。

代理不保存请求体。计量失败时丢弃上游 body，是为了不把未计价的成功结果交给操作工。多花的一次上游调用记在第 9 章。

### 5.6 与沙箱参数

`egress_run_args`：

| network | 返回 |
|---|---|
| `none` | `["--network", "none"]` |
| `egress` | `["--network", "mao-egress", "--add-host=egress:host-gateway", "--add-host=model-proxy:host-gateway"]` |

`DockerProvider.build_run_argv` 在 M1 结果上：`network=none` 保持 M1；`network=egress` 时用上表**替换** `--network none`，不是追加成两个 `--network`。必须仍然没有 `--network host`、`--privileged`、`--cap-add`、docker.sock 挂载（D11）。

`network=egress` 但本进程没有活着的 `EgressProxy`：`DockerProvider.open` 抛 `EGRESS_UNSUPPORTED`（沿用 D12 的码，不新造一个“代理没开”的码）。`allow_unproxied_egress=True` 时可以不开代理直接出网，这是开发开关，验收测试不靠它。开关**不**关闭脱敏，也**不**把模型秘密注入沙箱。

`LocalProvider` 没有网络命名空间。测试把 `HTTP_PROXY` / `HTTPS_PROXY` 指到 `127.0.0.1:实际端口`。`plan.env` 在两种 provider 下都设置：

```text
HTTP_PROXY=http://egress:<port>
HTTPS_PROXY=http://egress:<port>
NO_PROXY=model-proxy,egress
```

`<port>` 是 `EgressProxy.start` 的返回值。不要写死出网代理端口。

### 5.7 角色复查的输入

`connectors.md` 5.6 已经规定何时 `POLICY_VIOLATION`。本模块只保证 `secret_class` 的返回值稳定：清单里是什么就返回什么，不按工位角色改写分类。角色判断留在 `Runner`，避免两处各写一套。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `SECRET_NOT_FOUND` | 名单里的名字没有值文件，或 `secret_class` 未知 | `name` |
| `SECRET_MANIFEST_INVALID` | 清单不是对象，或分类 / 名字不合法 | `path` |
| `SECRET_CLASS_INVALID` | 分类不是四个值之一（与上一码二选一：解析清单时用这个码） | `name, class` |
| `EGRESS_CONFIG_INVALID` | 策略出网的值无法规范，或 `none` 与其他等级并存 | `station, reason` |
| `EGRESS_PROFILE_UNKNOWN` | ingress 记号在 `profiles.json` 里没有 | `name` |
| `EGRESS_DENIED` | 代理拒绝（也写入事件与 HTTP body） | `job, host, method` |
| `EGRESS_METHOD_OPAQUE` | 需要检查 HTTPS 方法但没有 CA | `job, host` |
| `EGRESS_JOB_INVALID` | 缺少 job 头，或 job 不在可出网状态 | `job` |
| `EGRESS_UNSUPPORTED` | 要出网但代理没开，且没有开发开关（D12，不新造码） | `station` |
| `EGRESS_PROXY_UNAVAILABLE` | 绑定失败，或 `host-gateway` 参数无法写入 argv | `reason` |
| `MODEL_PROXY_BIND_FAILED` | 指定端口被占用（生产即 8080） | `port` |
| `MODEL_UPSTREAM_UNKNOWN` | 没有上游映射，或缺 `header` | `model` |
| `MODEL_BUDGET_UNSET` | 工位没有 `budget_usd` 却调用了模型代理 | `station` |
| `MODEL_USAGE_UNPARSEABLE` | 响应里没有可计量字段 | `job` |
| `BUDGET_PRICE_MISSING` | 只有 token，价格表没有该模型 | `model` |
| `BUDGET_EXCEEDED` | 实时花费超过工位预算（D14，不新造码） | `job, spent, cap` |

HTTP body 里的 `code` 与上表相同，且 body 里没有 host 以外的请求内容。`detail` 里没有秘密值。

---

## 7. 验收测试

夹具：`tests/fixtures/registry-m2/` 的 `tool-default`（`none`）、`video-default` 风格的 providers 名单、本地 Sink 的 publish 名单。秘密用测试值 `CANARY-SECRET-VALUE-0123456789`，分类覆盖 `other` 与 `model`。价格表由测试写入 `tmp_path`，不使用仓库里的单价。`LocalProvider` + 进程内代理，不依赖 Docker 守护进程。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m2/test_m2_07_egress.py` | `test_none_stays_none` | 策略只有 `none` 时 `network=="none"`，`egress_run_args` 为 `--network none`，不注入秘密 |
| | `test_allow_exact_host_and_method` | 明文代理：名单内 host + 允许的方法 200；另一 host、或 `web_read` 的 `POST`，403 且 `code=EGRESS_DENIED` |
| | `test_no_suffix_match` | `fal.run` 不放行 `api.fal.run` |
| | `test_machine_needs_do_not_expand_policy` | 机器多写的域名不在 `EgressPlan.allow` 里 |
| | `test_connect_without_ca_rejected_when_methods_limited` | publish host 的 `CONNECT` 在没有 CA 时 `EGRESS_METHOD_OPAQUE`；`providers`（方法 `*`）的 `CONNECT` 可以建隧道 |
| | `test_argv_has_no_host_network` | `egress_run_args("egress")` 含两个 `--add-host`，不含 `--network host` / `--privileged` |
| | `test_unproxied_still_denied` | 代理没启动且开关为假：`EGRESS_UNSUPPORTED` |
| | `test_model_secret_not_in_sandbox_env` | `inject` 的结果与 `plan.env` 都不含模型类的值；`withheld` 含有该名字 |
| | `test_model_proxy_meters` | 伪造上游返回 `usage.input_tokens`，花费等于测试价格表算出来的数；`budget.json` 同步更新 |
| | `test_missing_price_does_not_zero` | 只有 token、价格表没有该模型：`BUDGET_PRICE_MISSING`，`spent` 不加 0 |
| | `test_budget_stop_prevents_seal` | 越过 `budget_usd` 之后 `budget_stops` 有 `BUDGET_EXCEEDED`，后续请求 402，job 不封存 |
| | `test_sealed_job_header_rejected` | `X-Factory-Job` 指向已 `SEALED` 的 id：`EGRESS_JOB_INVALID` |
| | `test_secret_absent_from_records` | 迫使一条带秘密值的失败路径后，事件 JSON、job.error、`str(FactoryError)`、`redact` 的报告文本、Brief 字符串里都没有 `CANARY-SECRET-VALUE-0123456789` |

验收命令：

```
uv run python tools/harness.py pytest tests/acceptance/m2/test_m2_07_egress.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 13`。

日志文本不在验收范围内（README 3.5）。过滤器由单元测试覆盖即可。

---

## 8. 错误示范与正确示范

**8.1 秘密不进记录**

```python
# ✖ 错误：排错时把 env 或 argv 原样写进异常
raise FactoryError("SANDBOX_START_FAILED", f"docker 失败：{argv}")
# ✔ 正确：先把 -e NAME=值 换成 -e NAME=***，再 redact。detail 里只有名字
```

**8.2 白名单不能被机器声明撑大**

```python
# ✖ 错误：策略 ∪ 机器 needs.egress，否则示例产品连 fal.run 都上不了
hosts = policy_hosts | machine_hosts
# ✔ 正确：只编译策略。机器 needs 仅在策略值是 from-machine 时提供 host 列表
```

**8.3 看不见的方法不要当成已检查**

```python
# ✖ 错误：CONNECT 只核对了 host，却把 publish 的 PUT 限制记成已经执行
# ✔ 正确：方法集不是 * 且没有 CA 时，拒绝并返回 EGRESS_METHOD_OPAQUE
```

**8.4 模型密钥不进容器；没有单价不要记 0**

```python
# ✖ 错误：把 ANTHROPIC_API_KEY 放进 SandboxSpec.env，省掉代理
# ✖ 错误：解析不到 usage 时 cost = 0，job 继续封存
# ✔ 正确：模型类进入 withheld；解析不到就 MODEL_USAGE_UNPARSEABLE，不封存
```

**8.5 开发开关不是脱敏开关**

```python
# ✖ 错误：allow_unproxied_egress 时跳过 redact，方便看日志
# ✔ 正确：开关只跳过“必须有代理”这一条（D12）。D13 始终有效
```

---

## 9. 风险与注意事项

- **`docker inspect` 看得到注入的环境变量。** 所以模型密钥不能注入容器。能对守护进程执行 `docker inspect` 的人，等价于能读非模型秘密。控制面记录 inspect 输出时必须先 `redact`。不要为了躲开 inspect 去改 D11（不加特权、不挂 docker.sock）。
- **伪造 `X-Factory-Job`。** 能访问宿主网关的容器可以猜端口、改头。代理只接受仍在 `PROVISIONING` / `RUNNING` / `AWAITING_APPROVAL` 的 job，不能把花费记到已封存的 job。M2 不做 mTLS。
- **没有 CA 时，publish 的 HTTPS 方法无法核验**，调用会被拒绝。验收用明文 HTTP。操作员要跑真实的 S3，必须自己放入 CA，而不是打开 `allow_unproxied_egress`。
- **`host-gateway` 依赖 Docker 20.10+。** 写不进 argv 时 `EGRESS_PROXY_UNAVAILABLE`，不要退回 `--network host`。
- **计量晚于上游扣费。** 解析失败或越限的那一次，上游可能已经收费。失败关闭优先于“把未计价的成功交给操作工”。
- **Windows**：只绑定 `127.0.0.1`；路径用 `pathlib`；子进程参数列表，禁止 `shell=True`。
---

## 10. 任务拆分

| ID | 标题 | 依赖 | 规范章节 | 允许修改的路径 | 验收 | 规模 |
|---|---|---|---|---|---|---|
| M2-07 | 秘密分类、出网计划、出网代理与模型计量 | M1 全部 | 3–7 | `src/mao/egress.py`、`src/mao/migrations/014_egress.sql`、`src/mao/cli.py`、`src/mao/errors.py`、`src/mao/sandbox/**`（只增第 5.6 节的 argv 替换）、`tests/system/test_egress_*.py` | `test_m2_07_egress.py`（≥ 13 通过） | L |

`tests/acceptance/m2/` 属于预先写好的验收材料。任务执行者不得修改它们。
