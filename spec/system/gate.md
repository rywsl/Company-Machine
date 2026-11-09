# gate：来料检验与出厂检验（mao.gate）

> 规范性文档，遵循 `spec/system/README.md`。任务：**M1-06**。
> V5 的五个阶段用 `Check.stage` 的那五个字符串，不用别的名字。`inspect` 不封存。未封存的 Bean 不能当输入（R9）。

---

## 1. 目的与范围

本组件做两件事：

- **来料**：`check_inputs`。Bean 必须存在、已封存、类型兼容，并且满足工位 `requires`。任何一项失败都是 `INTAKE_REJECTED`。
- **出厂 / 外部入库**：`inspect` 与 `admit_external`。对候选产物跑完 V5，登记为未封存的 Bean，返回 `GateReport`。封存是 Runner 的事。

V5 在本里程碑的五个阶段，与里程碑文档里的“结构、validator、属性、accept、meta_schema”一一对应。`Check.stage` 只能是 README 4.7 写出的五个字符串。里程碑说的 meta_schema 落在 stage `consistency` 上，检查项的 `name` 是 `meta_schema`。不要自造 stage `meta_schema`。

| 顺序 | `stage` | 里程碑里的说法 | 查什么 |
|---|---|---|---|
| 1 | `structure` | 结构 | 文件齐全、没有多余文件、media_type 与类型声明匹配、`meta.json` 是对象 |
| 2 | `validator` | validator | 沙箱里跑类型自己的 validator，得到 facts |
| 3 | `property` | 属性 | `always` 与端口 `guarantees` 的命名属性 |
| 4 | `accept` | accept | 端口上的 accept 表达式 |
| 5 | `consistency` | meta_schema | JSON Schema，以及 meta 与实测 facts 不得矛盾 |

不做：judge（M1 没有）；回执核验（M2，`connectors.md`）；把报告本身封成 Bean（Runner / 后续任务）；在本组件里调用 `seal_many`。

架构文档 6.7 把“meta 符合 meta_schema”写在结构检查里，把“meta 与 facts 一致”写在契约里。README 的 `Check.stage` 优先：这两件事都用 `consistency`，结构阶段不跑 JSON Schema。

---

## 2. 依赖

只用 README 第 4 节已有的接口，外加 `warehouse.md` 增加的 `media_type_of`。

| 依赖 | 用什么 |
|---|---|
| `Snapshot` | `key`、`spec`、`doc`、`type_key`、`is_subtype`、`validators`、`properties`、`always`、`compiled` |
| `mao.values` | `bean_value`、`property_env`、`accept_env`、`params_value` |
| `Warehouse` | `get`、`add`、`media_type_of`。**不**调用 `seal_many`、`record_lineage` |
| `SandboxProvider` | `open` → `exec` → `close`。只按 README 4.5 的属性读 `ExecResult`（`exit_code`、`timed_out`），不要求 `isinstance` |
| `Clock`、`IdSource` | `admit_external` 用 `ids.new("j")`。检验过程本身不取当前时间 |
| `jsonschema.Draft202012Validator` | 已是运行时依赖（D2）。校验 `meta_schema` |
| `mrl.errors.EvalError` | 属性 / accept 求值失败 = 检验不通过，不是设施故障 |

禁止 `eval` / `exec`，禁止 `shell=True`，禁止在生产路径上把 validator 的 argv 翻译成宿主路径。路径翻译是 Provider 的事（README 第 5 节，`LocalProvider`）。本组件把 Spec 里的 `run` 原样传给 `exec`。

---

## 3. 接口

下面与 README 4.7 **逐字相同**。

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

`stage` 只允许那五个字符串。实现里用常量，不要拼写变体。

`to_json` 返回：

```python
{
    "job_id": str,
    "station": str,
    "attempt": int,
    "verdict": "pass" | "reject" | "error",
    "outputs": {端口: [bean_id, ...]},
    "facts": {bean_id: {fact: 值}},
    "checks": [{"port", "index", "stage", "name", "ok", "detail"}, ...],
    "feedback": str,
}
```

`digest` = `digest_bytes(canonical_bytes(to_json()))`。同一报告两次调用结果相同。

`outputs` 的键按端口名升序插入。`facts` 的键按 bean id 升序插入。只收录**已经 `add` 成功**的候选。结构检查没通过的候选没有 id，不出现在这两张表里。

---

## 4. 数据格式

### 4.1 候选目录

`inspect` 的 `out_dir` 对应沙箱里的 `/factory/out`（README 第 5 节）。

| 端口类型 | 候选根目录 | `Check.index` |
|---|---|---|
| 标量 `T` | `out_dir / <端口> /` | `0` |
| `list<T>` | `out_dir / <端口> / <i> /` | `i` |

`<i>` 是目录名，必须匹配 `^(0|[1-9][0-9]*)$`（`0` 可以，`01` 不行）。集合必须正好是 `0 .. n-1`，没有空缺。按数值排序，不按字符串排序（`10` 在 `2` 后面）。

每个候选根目录里：

- payload 文件按类型 Spec 的 `path` 放置；
- 可选 `meta.json`：UTF-8 JSON 对象。缺失则 meta 为 `{}`；
- 其他名字（包括 `bean.json`）都是多余文件。

`admit_external` 没有端口目录。调用方给出的 `files` 就是 payload 相对路径 → 宿主文件，`meta` 就是 meta。相当于一个标量候选，`port` 为 `""`，`index` 为 `0`。

### 4.2 类型 Spec 从哪一份文档取

端口类型字符串来自工位 `spec.inputs` / `spec.outputs`（原样，如 `note.text@1`、`list<note.text@1>`）。

Bean 上的类型名带 minor（`note.text@1.1`）。比较子类型之前先变成类型 id：去掉 minor，保留 `name@major`。`is_subtype` 自己不会去掉 minor（`snapshot.md` 5.4）。

出厂时登记用的类型名是 `type_key(类型 id)` 的 `name@version`，也就是该 major 的最新非 retired minor，例如 `note.text@1.1`。payload 规则、`meta_schema`、validator 都取这份文档（`snapshot.validators` / `snapshot.spec(type_key)`），不取更旧的 minor。

`list<T>` 只剥一层。`ref<T>`、`bundle`、嵌套 `list<list<T>>` 在 M1 不支持。

### 4.3 payload 规则匹配

规则来自类型文档的 `spec.payload`（没有这个键则规则为空）。`required` 缺省为 `True`。

路径按 `/` 分段。`*`、`?` 只在**同一段**里按 `fnmatchcase` 匹配，不跨越 `/`。`attachments/*` 匹配 `attachments/a.txt`，不匹配 `attachments/sub/a.txt`。没有 `*` 和 `?` 的规则是整段相等。

每个实际文件必须恰好匹配一条规则。零条 → 多余文件。两条 → 结构失败。`required: true` 的规则至少要有一个匹配。

media_type：`recorded = media_type_of(相对路径)`。规则有 `media_type` 时，`fnmatchcase(recorded, 规则的 media_type)` 必须为真。`text/plain` 只匹配它自己；`video/*` 匹配 `video/mp4`。规则没有 `media_type` 则不比较。

### 4.4 validator 协议

这是 `m1_data_helpers.COUNT_TEXT_SOURCE` 所实现的协议。夹具脚本的标记只是为了把各种退出情况写进文件内容；Gate 不解析这些标记，只看退出码和 stdout。

一次 validator 一个沙箱：

| 项 | 值 |
|---|---|
| `SandboxSpec.name` | `mao-gate-` 加由 job、attempt、port、index、validator 名组成的后缀；不在 `[A-Za-z0-9_.-]` 里的字符换成 `-` |
| `image` | validator 文档里的 `image`，原样 |
| `mounts` | 恰好一个：`source` 为宿主目录的绝对路径，`target` 为 `/bean`，`readonly=True`。该目录里有 `payload/<相对路径>` 和 `bean.json` |
| `network` | `"none"` |
| `exec` 的 argv | validator 的 `run` 列表，原样。例如 `["count-text", "/bean/payload/note.txt"]` |
| 超时 | 60 秒 |
| stdout / stderr | `workdir` 下的文件路径，传给 `exec` |

`bean.json` 在 validator 启动前写出，facts 仍是 `{}`（还没测到）。`count-text` 不读它，只读 argv 里的文件。

宿主目录用 `shutil.copyfile` 从候选根目录复制 payload，禁止 `read_bytes()`。

`exec` 返回后必须 `close()`，包括失败路径（`try` / `finally`）。

stdout 按 UTF-8 解码，`errors="replace"`，再去掉首尾空白。

| 结果 | 判定 | `detail` |
|---|---|---|
| `timed_out` 或 `exit_code is None` | 设施故障 | 以 `设施故障：` 开头 |
| 退出码不是 0 也不是 1，或 `open` / `exec` 抛异常 | 设施故障 | 以 `设施故障：` 开头，带退出码或异常类型名 |
| 退出码 0，stdout 不是 JSON 对象 | 设施故障 | 以 `设施故障：` 开头 |
| 退出码 0，键集合不等于 `emits`，或值的类型与 `facts_decl` 不符（判定同 `snapshot.md` 5.11） | 设施故障 | 以 `设施故障：` 开头 |
| 退出码 1 | 产物拒绝 | `拒绝：` + JSON 对象里的 `reason` 字符串；没有则用解码后的 stdout（最多 500 字） |
| 退出码 0 且 facts 合法 | 通过 | `""` |

多个 validator 按文档顺序执行。`emits` 有重叠且值不同 → 设施故障。事实合并进一张 dict。类型没有 validator 时跳过这一阶段，facts 为 `{}`。

夹具 `count-text` 的行为（供测试构造文件，不是 Gate 的分支）：

| 文件内容含 | 脚本行为 | Gate 应得到 |
|---|---|---|
| `CRASH` | 退出码 3 | `verdict="error"` |
| `REJECT` | 退出码 1，`reason` 为 `内容包含 REJECT` | `verdict="reject"`，detail 含该 reason |
| `BADJSON` | 退出码 0，stdout 不是 JSON | `verdict="error"` |
| `MISSING` / `EXTRA` / `WRONGTYPE` | 退出码 0，但 facts 形状不对 | `verdict="error"` |
| 其他 | `chars` = 去掉所有空白后的长度，`lines` = `splitlines()` 的行数 | 进入后续阶段 |

### 4.5 报告与反馈

`verdict`：

1. 任一检查是设施故障（validator 的 detail 以 `设施故障：` 开头，或沙箱在写出 Check 之前就失败并记了这样一条 Check）→ `"error"`。有 error 就不再是 reject。
2. 否则，有任何 `ok is False`，或缺必填端口 / 列表下标空缺 → `"reject"`。
3. 否则 `"pass"`。

`feedback`：`pass` 时为 `""`。否则是 Markdown。每个 `ok is False` 的 Check，其 `port`、`stage`、`name`、`detail` 四个字符串都要出现在反馈里（顺序不限）。这是下一次 attempt 的唯一文本来源，不要另写一套只有日志里才有的原因。

`checks` 的顺序必须稳定，按这个键排序：端口名升序；stage 顺序 `structure`、`validator`、`property`、`accept`、`consistency`；同一 stage 里 `index` 升序（端口级 accept 的 index 是 `-1`，排在该端口该 stage 的最后）；再按 `name` 升序。

某个候选在 `structure`、`validator` 或 `property` 失败后，**不再跑该候选的后续 stage**。其他候选和其他端口继续。端口级 accept 只在该端口每个已登记候选都通过了这三步、且没有设施故障时才求值；缺必填文件导致没有登记时，不跑 accept。accept 失败不阻止 `consistency`：已经通过前三步的候选仍然做 meta_schema 检查，这样一次报告里可以同时看到验收失败和 schema 失败。设施故障则该候选停止，并且整份报告的 `verdict` 是 `error`（即使别的端口已经有 reject）。

---

## 5. 行为

### 5.1 构造

保存五个参数。`workdir.mkdir(parents=True, exist_ok=True)`。不打开沙箱，不读 Registry。

### 5.2 端口类型

工位键先 `snapshot.key("Station", station_key)`。文档不存在则 `SPEC_NOT_FOUND` 原样冒泡（来料与出厂都一样）。

类型字符串：

- `list<内层>`：恰好一层，内层不再含 `<`。基数是 list。
- `ref<…>`、含 `bundle`、或超过一层的 `list` → `FactoryError("GATE_UNSUPPORTED", "M1 不检验这种类型表达式", type=原文)`。
- 其他：基数是 scalar。类型 id = 去掉 minor 后的 `name@major`（没有 `@` 则原样）。

`any` 在 M1 的来料里接受任意已封存 Bean 的类型 id（不调用 `is_subtype`）。出厂端口若是 `any`，同样 `GATE_UNSUPPORTED`（没有 payload 规则可执行，M1 不生产 `any`）。

### 5.3 `check_inputs`

不打开沙箱，不跑 validator，不看 `meta_schema`。架构文档 7.6：来料只确认已封存、类型兼容、满足 `requires`。

`inputs` 的值是 Bean id（`str`）或 id 的序列。返回值：标量端口是一个 `Bean`，列表端口是 `list[Bean]`，顺序与传入 id 相同。可选端口缺席则不出现在返回值里。

收集问题，不要在第一个问题时抛出。全部看完后，若问题非空：

```python
FactoryError("INTAKE_REJECTED", "来料检验未通过", problems=problems)
```

问题对象：

```python
{"port": str, "index": int, "reason": str, "detail": str, "bean_id": str}
```

没有 Bean 时 `bean_id` 为 `""`。`reason` 只使用下表的词。外层错误码永远是 `INTAKE_REJECTED`，不要抛 `BEAN_NOT_SEALED`（R1、R9）。

| `reason` | 条件 |
|---|---|
| `missing` | 必填端口不在 `inputs` 里 |
| `extra` | `inputs` 里有工位未声明的端口 |
| `cardinality` | 标量端口给了 list/tuple，或列表端口给了 `str` |
| `not_found` | `warehouse.get` 抛了 `BEAN_NOT_FOUND`（捕获它，不要让它冒泡） |
| `unsealed` | Bean 存在但 `sealed is False` |
| `type` | 类型 id 不能 `is_subtype` 到端口要求的类型 id（`any` 除外） |
| `requires` | 某个 `requires` 属性不成立，或求值抛了 `EvalError` |

`requires` 的求值：

1. 属性必须在 `snapshot.properties(bean 的类型 id)` 里，否则 `requires`。类型 id 由 Bean 的 `type` 去掉 minor 得到。`type_key` 失败（类型不在快照里）也是 `type`，不是 `TYPE_NOT_FOUND` 冒泡。
2. `compiled = properties(...)[name]`，`compiled.evaluate(property_env(bean))`。结果是 `True` 才算通过。`False` 或 `EvalError` 都是 `requires`，`detail` 里带属性名。
3. 未封存的 Bean 在求值之前就被 `unsealed` 拒绝，不会带着空 facts 去算 `requires`。

列表端口的每个元素单独检查，`index` 是元素序号。标量端口的 `index` 是 0。

通过时返回仓库里的 `Bean`（已封存，facts 是封存时写入的）。不复制文件。

### 5.4 `inspect` 的总顺序

1. `station = snapshot.key("Station", station_key)`。`params_value(snapshot, station, params)`：不合法则 `PARAM_INVALID` 冒泡，不产生报告。得到的 dict 用于 accept。
2. 读工位 `spec.outputs`。没有输出的工位：直接 `verdict="pass"`，`outputs={}`，`checks=()`。
3. 对每个输出端口（名字升序）按 5.5–5.8 检验。
4. 组装 `GateReport`。`station` 字段是规范化后的 Spec 键。`job_id`、`attempt` 原样抄入。
5. **不**调用 `seal_many`。通过的候选也保持 `warehouse.get(id).sealed is False`。`facts` 在报告里，不在仓库 Bean 上。

Runner 在 `verdict == "pass"` 之后自行：

```python
warehouse.seal_many(
    [(bean_id, report.facts[bean_id]) for bean_id in 全部输出 id],
    job_id=job_id,
    report_digest=report.digest(),
)
```

本组件不替它做。

### 5.5 一个候选的结构与登记

候选根目录不存在：

- 端口 `optional: true` 且整个端口目录都不存在：跳过该端口，不记失败；
- 否则记一条 `structure` / `payload` / `ok=False`，detail 说明缺少目录或缺少文件。不 `add`。

端口目录存在时：

1. 列表端口：只允许合法下标子目录。出现文件、非法目录名、或下标不连续 → `structure` / `index` / `ok=False`，该端口不再登记。
2. 对每个候选根：按 4.3 匹配文件。失败则 `structure` / `payload` / `ok=False`，不 `add`。
3. `meta.json` 存在但不是 UTF-8 JSON 对象 → `structure` / `meta` / `ok=False`，不 `add`。
4. 结构通过后才 `warehouse.add(BeanDraft(type=带 minor 的类型名, meta=meta, files={相对路径: 宿主文件}))`。然后记 `structure` / `payload` / `ok=True`。
5. 此后该 id 进入 `outputs[port]`（按 index 顺序）和 `facts`（先放 `{}`，validator 成功后再替换）。

`add` 若抛出 `PAYLOAD_PATH_INVALID` 或 `BEAN_TYPE_INVALID`，这是 Spec 与实现不一致，让它冒泡，不要写成 reject。

### 5.6 validator 与属性

对每个已登记候选：

1. 按 4.4 跑 `snapshot.validators(类型 id)`。每条 validator 一条 Check，`name` 是 validator 的 `name`（笔记夹具是 `count-text`）。设施故障则该候选停止，报告的 `verdict` 将是 `error`。退出码 1 则该候选停止，属于 reject。
2. 实测 facts 写进报告的 `facts[id]`。用这些 facts 构造**临时** Bean：字段与仓库返回的 Bean 相同，但 `facts` 是实测值，`sealed` 仍是 `False`。不把这个对象交给 `seal_many`。`property_env` / `accept_env` 会投影这些 facts（`snapshot.md` 5.9：投影不清空 facts）。
3. 属性集合 = `snapshot.always(类型 id) | 端口 guarantees`。对每个名字（升序）一条 Check，`stage="property"`，`name` 是属性名。`evaluate(property_env(临时 Bean))` 的结果必须是 `True`。`False` 或 `EvalError` → `ok=False`，detail 含属性名，该候选停止。这是产物问题，不是设施故障。

### 5.7 accept

在该端口全部候选的 property 之后、consistency 之前执行。端口级，一条 accept 一条 Check：`stage="accept"`，`name` 为 `accept[0]`、`accept[1]`…，`index=-1`，`port` 是端口名。

- 标量端口：`out` 是那一个临时 Bean。
- 列表端口：`out` 是该端口全部临时 Bean 的 list（按 index）。

表达式是 `snapshot.compiled(f"{station}/outputs.{port}.accept[{i}]")`。环境是 `accept_env(out, inputs, 规范化后的 params)`。结果必须是 `True`。`False` 或 `EvalError` → `ok=False`，失败即关闭（R8），detail 含 `accept[{i}]`。不要把 `EvalError` 当成 `True`，也不要当成设施故障。

### 5.8 consistency（meta_schema）

对每个仍在跑的候选一条 Check：`stage="consistency"`，`name="meta_schema"`。

JSON Schema 的取法与 `mrl.factory.sites.TypeEnv.meta_type` 相同：先看 `type_key(tid)` 那份文档的 `spec.meta_schema`；没有则沿内核 `extends` 找父类型的最新 minor。都没有则没有 schema。

有 schema 时，用 `Draft202012Validator(schema).iter_errors(meta)`。有任意错误 → `ok=False`，detail 含第一条错误的 `message`。`note.text@1.1` 的 schema 是 `additionalProperties: false`，未知键失败。`card@1` 要求 `title` 且 `minLength >= 1`。

另外：meta 与 facts 的同名键，值必须相等。`bool` 不与 `int` 比较。`int` 与 `float` 按数值相等（`3 == 3.0`）。不等 → `ok=False`，detail 含该键名。这是架构文档 6.7 的“自报 meta 与实测 facts 一致”，stage 仍是 `consistency`。

schema 通过且没有矛盾 → `ok=True`。没有 schema 也没有矛盾 → 仍记一条 `ok=True`，让这个阶段在报告里可见。

### 5.9 `admit_external`

1. `type_id` 先按 5.2 变成类型 id（若调用方传了 `note.text@1.1`，去掉 minor 再 `type_key`）。`type_key` 失败 → `TYPE_NOT_FOUND` 冒泡。
2. `job_id = ids.new("j")`，`attempt = 0`，`station = ""`。
3. 把 `files` 当作一个标量候选的 payload（相对路径规则同 `BeanDraft`），`meta` 当作 meta，跑 5.5 的结构、5.6 的 validator 与 `always`（没有工位 guarantees）、5.8 的 consistency。没有 accept。
4. 不封存。
5. `verdict == "pass"` 时返回 `(report, warehouse.get(id))`，该 Bean 仍未封存。否则返回 `(report, None)`。结构失败时没有 Bean，第二项也是 `None`。结构已通过但后续失败时，未封存的候选留在仓库里，第二项仍是 `None`（调用方以返回值为准，不要把留在库里的候选当成入库成功）。

`clock` 不参与判定。保留它是因为构造函数签名已经固定。

---

## 6. 错误

检验不通过不是异常。`inspect` / `admit_external` 用 `verdict` 表达 reject 和 error。

| 错误码 | 触发条件 | detail |
|---|---|---|
| `INTAKE_REJECTED` | `check_inputs` 有任一问题（未封存、不存在、类型不符、requires 不成立、端口缺失或基数不对） | `problems`：5.3 的对象列表 |
| `GATE_UNSUPPORTED` | 端口类型是 `ref`、嵌套 list，或出厂端口是 `any` | `type` |
| `PARAM_INVALID` | `inspect` 调用 `params_value` 失败（`snapshot.md` 抛出，原样传播） | `station`、`problems` |
| `SPEC_NOT_FOUND` | 工位键不在快照里（原样传播） | `key` |
| `TYPE_NOT_FOUND` | `admit_external` 的类型 id 不在快照里（原样传播） | `tid` |
| `BEAN_NOT_FOUND` | 只出现在 `check_inputs` 的内部；对外改写成 `INTAKE_REJECTED` / `not_found`，不要让调用方看到这个码 | — |

不要登记、也不要抛出 `BEAN_NOT_SEALED`。未封存是 `INTAKE_REJECTED` 的 `reason="unsealed"`。

---

## 7. 验收测试

文件：`tests/acceptance/m1/test_m1_06_gate.py`。

```bash
uv run python tools/harness.py pytest tests/acceptance/m1/test_m1_06_gate.py
```

测试自己提供一个满足 README 4.5 属性的 Provider，把 `count-text` 映射到 `write_count_text` 写出的脚本，并把 argv 里的 `/bean/...` 换成挂载源目录下的真实路径。Gate 的实现不得依赖这个映射。测试不要求 Docker，因此没有 `docker` 标记。

| 测试 | 验证 |
|---|---|
| `test_check_inputs_rejects_unsealed` | 未封存 → `INTAKE_REJECTED`，`problems[].reason == "unsealed"`；不存在的 id → `reason == "not_found"`，外层码仍是 `INTAKE_REJECTED` |
| `test_check_inputs_type_requires_cardinality` | 类型不符、`requires` 不成立（facts.chars 为 0）、标量端口传入列表 |
| `test_check_inputs_ok_and_list` | 已封存且 `non_empty` 的笔记可以通过 `shout`；`merge` 的列表端口返回 list |
| `test_inspect_pass_does_not_seal` | `shout` 的合格输出 `verdict="pass"`；Bean 仍未封存；facts 为 `chars`/`lines`；沙箱 `image`、`target=="/bean"`、`readonly`、`network=="none"`；`feedback == ""` |
| `test_inspect_validator_reject_and_infra` | 含 `REJECT` → reject 且 detail 含 `内容包含 REJECT`；含 `CRASH` 或 `BADJSON` → `verdict="error"` 且 detail 以 `设施故障：` 开头 |
| `test_inspect_property_accept_consistency` | 空白笔记失败在 `property`/`non_empty`；短于输入的笔记失败在 `accept`/`accept[0]`；非法 meta 失败在 `consistency`/`meta_schema` |
| `test_inspect_list_port` | `split` 两个下标通过且 `size(out) >= 2`；只有一个下标则 accept 失败；下标空缺则 structure 失败 |
| `test_admit_external` | `card@1` 带 `title` 通过且未封存；缺 `title` 时第二项是 `None`，`verdict="reject"` |
| `test_report_json_and_digest` | `to_json` 的键齐全；`digest` 等于规范 JSON 的摘要，且两次调用相同 |

---

## 8. 错误示范与正确示范

**8.1 未封存也拿去物化**

```python
# ✖ 候选 Bean 的 facts 还是空的，下游会把它当成已经出厂的物料
bean = warehouse.get(bean_id)
sandbox.mount(bean)          # 没有看 sealed
# ✔ 来料只走 check_inputs。未封存是 INTAKE_REJECTED，不是另一个错误码
try:
    beans = gate.check_inputs(station, inputs)
except FactoryError as e:
    assert e.code == "INTAKE_REJECTED"
    assert any(p["reason"] == "unsealed" for p in e.detail["problems"])
```

**8.2 检验通过就顺便封存**

```python
# ✖ inspect 的契约是只登记候选。这里封存会让 Runner 的 seal_many 遇到重复 facts，也会让失败的 attempt 留下已封存产物
if report.verdict == "pass":
    warehouse.seal_many(...)
# ✔ 返回报告就结束。仓库里 sealed 仍是 False
return report
```

**8.3 validator 输出坏了还继续**

```python
# ✖ 设施故障被当成“没有 facts”，后面的属性用空 facts 去算，可能误放行
try:
    facts = json.loads(stdout)
except Exception:
    facts = {}
# ✔ 不是 JSON 对象就是 verdict="error"，detail 以「设施故障：」开头，不封存
detail = f"设施故障：validator {name} 的输出不是 JSON 对象"
```

**8.4 自造 stage 名字**

```python
# ✖ README 的 Check.stage 没有 meta_schema 这个字符串。doccheck 与验收都按那五个词
Check(port, 0, "meta_schema", "meta_schema", False, "...")
# ✔ 阶段字符串用 consistency，名字再用 meta_schema
Check(port, 0, "consistency", "meta_schema", False, message)
```

---

## 9. 风险与注意事项

- **R9**：`check_inputs` 是来料的唯一入口。Runner 不得跳过它直接 `materialize`。
- **R3**：`EvalError` 是检验不通过。`JSONDecodeError`、非 0/1 的退出码、超时是设施故障。两种都不要吞掉变成成功。
- **R8**：属性与 accept 只用 `snapshot.compiled` 与 `property_env` / `accept_env`。不要 `eval` 工位 YAML 里的字符串。
- **R5**：候选文件的宿主路径用 `pathlib`。传给沙箱的是虚拟路径 `/bean/payload/...`。测试用的 Provider 可以翻译；Gate 不翻译。Windows 上 `subprocess` 仍禁止 `shell=True`。
- **R13**：本组件的验收测试不需要 Docker。不要给它们加 `skipif(no docker)`。实现若只在 Docker 存在时才跑 validator，验收会失败。
- **沙箱名字**：只含 Docker 允许的字符。`job_id` 里的字母已经是小写，但 validator 名可能含别的字符，按 4.4 替换。
- **不在事务里跑沙箱**：`add` 的事务在 validator 之前就结束。沙箱超时不得占着 SQLite 写锁（foundation 第 9 章）。
- **临时 Bean**：带实测 facts 的对象只活在这次调用的栈上。不要 `seal_many`，也不要把它 `add` 第二次（id 不含 facts，第二次 `add` 是幂等的，不会把 facts 写进去）。
