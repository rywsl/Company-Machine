# 缓存与复用（cache.md）

> 规范性文档，M2。模块：`mao.cache`。
> 依据：架构文档 6.8（缓存键）、9.4（转换的实例级命中）、9.6（转换升级后产生新的缓存键）。
> 跨组件约定见 `README.md`；run 的推进见 `lines.md`。
> 生产路径的接线（`open_line_runner` 带 `JobCache`）与 `factory run` 的参数和输出见 `wiring.md`（M10），冲突时以 `wiring.md` 为准。

---

## 1. 目的与范围

**做什么（M2）**

- 为每个可缓存的 job 计算确定的**缓存键** `job_key`，并记录在 job 上。
- 在提交 job 之前查找缓存。命中时直接复用已封存的产物：不创建新 job，不花钱。
- 支持 `variant`（“再做一版”）和失效（按键、job、工位、Bean 四种范围）。
- 转换节点同样走缓存，这就是架构文档 9.4 的“实例级命中”：同一个 Bean 被同一版本的转换处理过，就直接复用结果。

**不做什么**

- 不做负缓存：失败的 job 不缓存，下次照常执行。
- 不缓存 `source`、`sink`、`planner`、`judge` 角色的 job（第 5.1 节）。
- 不回收 Bean。复用的只是“哪个 job 产出过哪些 Bean”这层关系，Bean 的保留期由 Warehouse 管理（M4）。
- 不跨工厂共享缓存。

---

## 2. 依赖

| 依赖 | 接口 |
|---|---|
| `Snapshot` | `key`、`doc`、`spec` |
| `mao.values.params_value` | 参数规范化：填入默认值，并按声明类型转换 |
| `mao.jobs`（`jobs` 表） | 读 job 的 `state`、`station`、`inputs`、`outputs`、`created_at` |
| `Warehouse` | `get`：确认被复用的产物仍然存在且已封存 |
| `mao.scheduler`（`scheduler.md`，可选） | `is_quarantined(bean_id)`：被隔离的 Bean 不能被复用 |
| `mao.base` | `canonical_json`、`digest_bytes`、`Clock`、`IdSource` |

---

## 3. 接口

```python
CACHEABLE_ROLES = frozenset({"transform", "converter"})

def spec_digest(doc: Mapping[str, Any]) -> str
    # 去掉 metadata.lifecycle、metadata.description、metadata.labels 后，对规范 JSON 求摘要

@dataclass(frozen=True)
class KeyMaterial:
    station: str; station_digest: str
    machine: str; machine_digest: str; image: str; recipe: str
    runtime: str; runtime_digest: str; model: str
    policy: str; policy_digest: str
    inputs: dict[str, list[str]]       # 端口 → Bean ID 列表（端口按名排序；列表保持原顺序）
    params: dict[str, Any]             # params_value 规范化之后的参数
    variant: str                       # 没有时为 ""
    def to_json(self) -> dict[str, Any]          # 第 4.2 节，含 "v": 1
    def key(self) -> str                         # "jk:" + digest_bytes(canonical_bytes(to_json()))[7:]

class JobCache:
    def __init__(self, ctx: Context) -> None
    def material(self, snapshot: Snapshot, station_key: str, inputs: Mapping[str, str | Sequence[str]],
                 params: Mapping[str, Any], *, variant: str | None = None) -> KeyMaterial
        # 角色不在 CACHEABLE_ROLES → FactoryError("CACHE_NOT_CACHEABLE")
    def key(self, snapshot: Snapshot, station_key: str, inputs, params, *, variant: str | None = None) -> str
    def bind(self, job_id: str, material: KeyMaterial) -> None   # 提交 job 后立即调用：记录它的键
    def lookup(self, key: str) -> "Job | None"                    # 第 5.3 节
    def invalidate(self, scope: Literal["key", "job", "station", "bean"], value: str, *,
                   reason: str, by: str) -> str                   # 返回失效记录 id（"ci-0001"）
    def explain(self, job_id: str) -> dict[str, Any]              # {"key", "material", "hits": [...]}
```

`lines.md` 第 5.3 节第 4.3 步使用 `key` 与 `lookup`：命中时，节点直接 `DONE`，`job_id` 为被复用的 job，`cache_hit = 1`；不命中时提交 job，然后调用 `bind`。

**CLI（M2）**

```
factory cache ls [--station S@N] [--json]
factory cache explain JOB_ID [--json]
factory cache invalidate (--key K | --job J | --station S@N | --bean B) --reason TEXT
factory run STATION@N ... [--cache] [--variant V]      # 单工位运行：默认不查缓存（保持 M1 行为），加 --cache 才查
factory line run ... [--no-cache]                      # 产线运行：默认查缓存
```

---

## 4. 数据格式

### 4.1 迁移 `src/mao/migrations/011_cache.sql`

```sql
CREATE TABLE job_keys (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id     TEXT NOT NULL UNIQUE,
  job_key    TEXT NOT NULL,
  variant    TEXT NOT NULL DEFAULT '',
  material   TEXT NOT NULL,                 -- KeyMaterial.to_json() 的规范 JSON
  created_at TEXT NOT NULL
);
CREATE INDEX job_keys_key ON job_keys(job_key);
CREATE TABLE cache_invalidations (
  id      TEXT PRIMARY KEY,
  scope   TEXT NOT NULL CHECK (scope IN ('key', 'job', 'station', 'bean')),
  value   TEXT NOT NULL,
  max_seq INTEGER NOT NULL,                 -- 只作用于 job_keys.seq <= max_seq 的记录
  reason  TEXT NOT NULL,
  by      TEXT NOT NULL,
  at      TEXT NOT NULL
);
```

### 4.2 键材料（`KeyMaterial.to_json()`）

```json
{
  "v": 1,
  "station": "Station:explainer@3", "station_digest": "sha256:…",
  "machine": "Machine:openmontage@7", "machine_digest": "sha256:…",
  "image": "registry.local/machines/openmontage@sha256:456f…", "recipe": "animated-explainer",
  "runtime": "OperatorRuntime:claude-code@4", "runtime_digest": "sha256:…", "model": "claude-opus-5-5",
  "policy": "Policy:video-default@2", "policy_digest": "sha256:…",
  "inputs": {"brief": ["sha256:41ab…"]},
  "params": {},
  "variant": ""
}
```

- `model` 取工位 `operator.model`，没有时为 `""`；`image` 取机器 `runtime.image`，用 compose 时为 `""`。
- `key = "jk:" + <hex>`，其中 `<hex>` 是 `canonical_bytes(material)` 的 SHA-256 十六进制。

---

## 5. 行为

### 5.1 哪些 job 可以缓存

| 角色 | 缓存 | 原因 |
|---|---|---|
| `transform`、`converter` | ✔ | 产物封存后不可变；同样的输入再算一遍只是浪费钱 |
| `source` | ✘ | 外部世界会变，每次摄入都可能得到新物料 |
| `sink` | ✘ | 有对外副作用。重复发布由幂等键挡住（`connectors.md`），而不是靠缓存 |
| `planner`、`judge` | ✘ | 产出与 run 绑定（plan 带 `metadata.run`），或者本身是抽样评分 |

### 5.2 键的组成与不变性

键材料逐项决定，下表就是规范：

| 变化 | 键是否改变 |
|---|---|
| 工位、机器、运行时、策略文档的任何内容（除下一行） | 改变 |
| 只改 `metadata.lifecycle` / `description` / `labels` | **不变**：弃用不应让缓存失效 |
| 机器镜像 digest、配方名 | 改变：都在机器文档里，`image` 和 `recipe` 也单独列出，便于阅读 |
| 模型 | 改变 |
| 输入 Bean 或其顺序（`list<T>`） | 改变 |
| 参数的书写顺序；显式写出默认值与省略 | **不变**：先经 `params_value` 规范化 |
| `variant` | 改变 |
| epoch | **不变**：没变的工位跨 epoch 照样命中，这正是以新 epoch 重建 run 时“已完成的工位不重复花钱”的依据 |
| 输出类型新增了更严格的 validator（minor 升级） | **不变**：架构文档 8.3，已封存的 Bean 不受影响 |
| 转换工位升级为新版本 | 改变：新版本的转换不复用旧版本的结果（架构文档 9.6） |

### 5.3 查找 `lookup(key)`

1. 候选：`job_keys.job_key = key`，并且对应 job 的状态为 `SEALED`。按 `job_keys.seq` 升序，越早越优先。
2. 排除被失效的候选。一条失效记录 I 对候选 C 生效，当且仅当 `C.seq <= I.max_seq`，并且：
   - `scope = key`：`I.value == key`
   - `scope = job`：`I.value == C.job_id`
   - `scope = station`：`I.value` 是 C 所属 job 的工位 Spec 键
   - `scope = bean`：`I.value` 出现在 C 所属 job 的 `inputs` 或 `outputs` 中
3. 排除产物已经不能用的候选：任一输出 Bean 不存在、未封存，或已被隔离（`scheduler.md`）。
4. 返回第一个剩下的候选对应的 `Job`；没有候选时返回 `None`。
5. `lookup` 只读，不写任何东西。命中事件由调用方写：`lines.md` 写 `run.node`，并带上 `cache_hit: true`。

### 5.4 失效 `invalidate`

- `max_seq` 取当时 `job_keys` 的最大 `seq`，表里没有记录时为 0。所以失效只作用于**已有**的记录：之后用同样的键重新执行并封存的 job，照样可以被复用。
- `scope = station` 的 `value` 必须是完整的 Spec 键（`Station:x@N`）；`scope = bean` 的 `value` 必须是 `sha256:` 开头的 Bean ID。格式不对时报 `CACHE_INVALIDATION_INVALID`。
- 失效记录只增不删，并写事件 `cache.invalidated {id, scope, value, reason, by}`。

### 5.5 绑定 `bind`

- 提交 job 后立即调用，与写入 `run_nodes.SUBMITTED` 在同一个事务里。
- 同一个 job 不能绑定两次（`job_id UNIQUE`），重复调用报 `CACHE_BIND_CONFLICT`。
- 用 `--no-cache` 运行的 job 也要绑定。“不查缓存”不等于“不进缓存”，下次运行仍然可以复用它的产物。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `CACHE_NOT_CACHEABLE` | 为 `source`/`sink`/`planner`/`judge` 工位计算键 | `station, role` |
| `CACHE_INVALIDATION_INVALID` | 失效范围或取值格式不对 | `scope, value` |
| `CACHE_BIND_CONFLICT` | 同一个 job 绑定两次 | `job` |

---

## 7. 验收测试

`tests/acceptance/m2/test_m2_05_cache.py`。夹具：`tests/fixtures/registry-m2/`，`LocalProvider`。

| 测试 | 验证什么 |
|---|---|
| `test_material_shape` | `to_json()` 的字段集合与取值与第 4.2 节一致；`key()` 等于规范 JSON 的 SHA-256 |
| `test_key_invariances` | 参数书写顺序、显式默认值、只改 lifecycle/description/labels 都不改变键 |
| `test_key_sensitivity` | 镜像 digest、模型、配方、策略、输入 Bean、`list` 输入顺序、variant 分别改变键 |
| `test_not_cacheable_roles` | sink、source 工位计算键报 `CACHE_NOT_CACHEABLE` |
| `test_rebuild_hits_everything` | 成功的 run 用 `rebuild` 重建后，所有可缓存节点都命中，没有新 job，`spent_usd == 0`；sink 节点照常执行（由幂等键去重，见 `connectors.md`） |
| `test_variant_reruns_one_step` | 只给一个步骤加 variant：该步骤重新执行；下游因为输入 Bean 相同（替身工具是确定性的）而命中 |
| `test_converter_instance_hit` | 两条产线遇到同一个 Bean 的同一种转换：转换只执行一次 |
| `test_invalidation_scopes` | 四种范围各自让已有记录不再命中；失效之后新封存的同键 job 可以再次命中 |
| `test_no_cache_still_binds` | `--no-cache` 的 job 也写入 `job_keys`，下一次可被复用 |
| `test_factory_run_cache_flag` | `factory run` 默认不查缓存（M1 行为不变），`--cache` 时命中并输出被复用的 job id |

验收命令：`uv run python tools/harness.py pytest tests/acceptance/m2/test_m2_05_cache.py`，断言 `out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 10`。

---

## 8. 错误示范与正确示范

**8.1 键里必须包含会影响产物的一切，且只包含这些**

```python
# ✖ 错误：只用工位名和输入算键——机器升级（镜像变了）后仍然命中旧产物，行为偏移被缓存“藏”起来
key = H(station_name, inputs)
# ✖ 错误：把 epoch 或时间放进键——每个新 epoch 全部重跑，以新 epoch 重建 run 时会重复花钱
key = H(..., snapshot.epoch, clock.now())
# ✔ 正确：按第 4.2 节的字段计算；工位/机器/运行时/策略用去掉 lifecycle 等字段后的文档摘要
```

**8.2 参数先规范化再进键**

```python
# ✖ 错误：直接用调用方给的参数 —— {"lang": "en"} 与 {"lang": "en", "fmt": "srt"}（fmt 默认就是 srt）得到不同的键
material.params = dict(given)
# ✔ 正确
material.params = params_value(snapshot, station_key, given)
```

**8.3 失效只作用于已有记录**

```python
# ✖ 错误：按键永久拉黑——之后修好的工位再也不能被复用
blacklist.add(key)
# ✔ 正确：失效记录带 max_seq，只覆盖当时已有的 job_keys 行
```

**8.4 Sink 不进缓存**

```python
# ✖ 错误：Sink “命中缓存”就不执行——目标端的内容可能已经被人删了，而回执核验也被跳过
# ✔ 正确：Sink 每次都执行；重复发布由幂等键与回执记录决定（connectors.md）
```

---

## 9. 风险与注意事项

- **非确定性的生产者**：agent 工位同样的输入可能产出不同的结果。缓存会固定第一次封存的结果，这正是我们想要的：不为同一个需求重复付费。需要新结果时，用 `variant`。
- **上游悄悄变化**：外部 provider 的变化不会改变缓存键，所以缓存会掩盖这类漂移。漂移由定时 canary 发现（`contract-tests.md`），canary 运行时**不查缓存**。
- **键材料变更**：今后键材料加字段时，`v` 必须加一。旧记录保留，但不会再命中，相当于一次全量失效。这样的修改需要 ADR。
- **文档摘要与 YAML 写法**：摘要基于解析后的文档，与注释、缩进、键的顺序无关；但改一个标量的写法（例如 `1` 改成 `1.0`）会改变摘要，因为类型变了。

---

## 10. 任务拆分

| ID | 标题 | 依赖 | 规范章节 | 允许修改的路径 | 验收 | 规模 |
|---|---|---|---|---|---|---|
| M2-05 | 缓存键、查找、失效，以及与 LineRunner、`factory run` 的集成 | M2-03 | 全部 | `src/mao/cache.py`、`src/mao/migrations/011_cache.sql`、`src/mao/runs.py`（只改第 5.3 节第 4.3 步的缓存调用）、`src/mao/cli.py`、`src/mao/errors.py`、`tests/system/test_cache_*.py` | `test_m2_05_cache.py`（≥ 10 通过） | M |
