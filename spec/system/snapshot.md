# snapshot：Registry 快照与运行期求值环境（mao.snapshot / mao.values）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M1-02**。
> 结构是否合法只由 `mrl.factory.check.check_registry` 判定（D4）。子类型、属性继承、facts 继承只从检查结果的关系里取（风险 R19），禁止在 Python 里另写一套。

---

## 1. 目的与范围

本组件把一个已经通过检查的 Registry 固定成一次运行使用的快照，并提供与 `mrl.factory.sites` 相同类型环境的运行期值。

做：

- `Snapshot.load`：跑 `check_registry`；有 error 就拒绝；计算 epoch；缓存文档与已编译表达式。
- 从 `CheckResult.model` 读取 `type_key`、`subtype`、`type_fact`、`type_prop`、`type_always_p`、`type_validator`。
- `compiled`：factory-profile 第 4 章的每一个表达式位置，编译期与运行期是同一份 `CompiledExpr` / `CompiledTemplate`。
- `mao.values`：把 Bean、输入、参数、审批、配方、操作工环境变成 MRL 能求值的 Python 值。

不做：Bean 的存储与封存（`warehouse.md`）；来料 / 出厂检验（`gate.md`）；产线、幂等键、switch / until 的**执行**（M2–M4）。那些位置的表达式仍然要在 `compiled` 里能取到，执行留给后续组件。

---

## 2. 依赖

- 标准库：`pathlib`、`copy`、`dataclasses`（仅类型注解需要时）。
- `mao.base.canonical_json` / `canonical_bytes` / `digest_bytes` / `digest_file`（epoch 与本文件不直接哈希 Bean；Bean 身份在 `warehouse.md`）。
- `mao.errors.FactoryError`。
- `mrl.factory.check.check_registry`、`CheckResult`、`mrl.factory.registry.load_registry`。
- `mrl.factory.sites.TypeEnv`、`site_env`、`mrl.factory.extract.Site`。
- `mrl.expr.compile_expr`、`compile_template`；`mrl.stdlib.make_table`。
- **禁止**在模块顶层 import `mao.warehouse`、`mao.gate`、`mao.sandbox`。`Bean` 只出现在 `TYPE_CHECKING` 或文档里。`bean_value` 按属性读取，这样 M1-02 与 M1-03 可以并行实现。
- **禁止** `eval` / `exec`、Jinja、`str.format` 渲染 Spec 模板（D3、R8）。

---

## 3. 接口

下面两段与 README 4.2、4.3 **逐字相同**。实现不得改名、改参数、改返回形状。其后的说明只把它们展开成可执行的算法。

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
```

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

`Snapshot` 不是 dataclass。三个公开属性在 `load` 里赋值，之后不再改。`result` 是 `check_registry` 的返回值，原样保存。

`mao.values` 的函数都是模块级函数，不挂在 `Snapshot` 上。

---

## 4. 数据格式

### 4.1 本组件不建表、不写文件

快照是内存对象。Registry 目录只读。epoch 不写入 Registry。

### 4.2 epoch

`epoch` 匹配 `^e-[0-9a-f]{16}$`。它是 Registry **目录树**内容哈希的前 16 个十六进制字符，前面加 `e-`（README D4、名词表）。算法见 5.2。哈希输入是文件字节，不是规范化换行之后的文本。

### 4.3 类型 id 与 Spec 键

| 说法 | 例子 | 谁用 |
|---|---|---|
| 类型 id | `note.text@1` | `type_key` / `is_subtype` / `facts_decl` / `validators` / `properties` / `always` 的参数 |
| 带 minor 的类型名 | `note.text@1.1` | Bean 的 `type` 字段；**不是** `type_key` 的参数 |
| Spec 键 | `Type:note.text@1.1`、`Station:shout@1` | `doc` / `spec` / `compiled` 的主体前缀 |

`type_key` 的返回值是 Spec 键，不是类型 id。内核关系 `type_key(id, key)` 的第一列是类型 id，第二列是最新非 retired minor 的 Spec 键（`kernel/refs.mrl`）。

### 4.4 表达式主体

主体字符串以 `result.extraction.sites` 为准（`mrl.factory.extract` 实现 factory-profile 第 3–4 章）。本组件不另扫 YAML。对照表只便于阅读，冲突时以 `sites` 里的 `subject` 为准：

| `Site.kind` | 主体 | 模板 |
|---|---|---|
| `property` | `Type:…/properties.<名>` | 否 |
| `accept` | `Station:…/outputs.<端口>.accept[<i>]` | 否 |
| `equivalence` | `Station:…/equivalence[<i>]` | 否 |
| `approval` | `Station:…/approval.propose` 或 `…/approval.in_run` | 否 |
| `idempotency` | `Station:…/idempotency_key` | 是 |
| `step-param` | `Line:…/steps.<步骤>.params.<名>` | 是 |
| `switch` | `Line:…/steps.<步骤>.switch.cases[<i>]` | 否 |
| `until` | `Line:…/steps.<步骤>.until` | 否 |
| `recipe-template` | `Machine:…/recipes.<配方>.<路径>` | 是 |
| `runtime-template` | `OperatorRuntime:…/invoke.argv[<i>]`、`…/env.<名>` | 是 |

笔记夹具（`m1_data_helpers.notes_registry_files`）至少包含：

- `Type:note@1.0/properties.non_empty`
- `Type:note.text@1.0/properties.short`、`Type:note.text@1.1/properties.short`
- `Station:shout@1/outputs.note.accept[0]`
- `Station:split@1/outputs.parts.accept[0]`
- `Station:stamp@1/outputs.card.accept[0]`
- `Station:shout@1/approval.in_run`
- `Machine:toolbox@1/recipes.shout.run[3]`（文本是 `{{ params.suffix }}`）

---

## 5. 行为

### 5.1 `Snapshot.load`

1. `root = Path(root).resolve()`。目录不存在时，`check_registry` 抛出的 `FileNotFoundError` 原样冒泡（调用方的编程错误）。
2. `result = check_registry(root)`。禁止自己读 YAML 再拼一套诊断来代替这次调用。
3. `errors = [d for d in result.diags if d.severity == "error"]`。非空则抛出

   ```python
   FactoryError(
       "REGISTRY_INVALID",
       f"Registry 有 {len(errors)} 个错误，不能作为运行快照",
       diags=[d.to_json() for d in errors],
   )
   ```

   警告和 info **不**拒绝加载（D4 只要求零 error）。`diags` 里只有 error，顺序与 `result.diags` 中的相对顺序一致。
4. 按 5.2 计算 `epoch`。
5. `reg = load_registry(root)`。对每个 `doc.key` 非空的文档，`docs[doc.key] = doc.data`。通过检查的 Registry 没有重复键（否则已是 `E-DUPLICATE` error，第 3 步已经拒绝）。
6. 按 5.8 编译 `result.extraction.sites` 的每一项，放进 `compiled_cache`。
7. 返回实例。`result.model` 或 `result.extraction` 为 `None` 时 `AssertionError`（检查器契约被破坏，是编程错误）。

`load` 不写盘，不改 Registry 里的文件。

### 5.2 epoch（目录树哈希）

对 `root` 下每个**普通文件**（`rglob`）：

1. `is_symlink()` 为真的项跳过，不跟随链接。目录跳过。
2. 相对路径用 `path.relative_to(root).as_posix()`（只用 `/`，禁止把 `\` 写进哈希）。
3. 摘要用 `digest_file`（1 MiB 分块，R20），不要 `read_bytes()`。

清单是 `{相对路径: digest_file 的返回值}`。路径集合由 `canonical_bytes` 的键排序固定，不要依赖 `rglob` 的顺序。

```text
tree = digest_bytes(canonical_bytes(清单))    # "sha256:" + 64 位小写 hex
epoch = "e-" + tree.split(":", 1)[1][:16]
```

包含布局外的文件（样例、脚本、临时丢进去的 `notes/extra.txt`）。不包含 mtime、权限位、绝对路径。因此：

- 同一棵树写到两个目录，epoch 相同；
- 改任何一个文件的字节，epoch 就变；
- 只改文件名的大小写在 Windows 上可能撞车，那是文件系统的限制，哈希本身仍按 `as_posix()` 的字符串区分。

换行不规范化。要稳定 epoch，写入方必须自己用 LF（夹具 `write_registry` 已经这样写）。这是 R5：不要为了“跨平台相同”去改哈希输入，否则内容不同也会得到同一个 epoch。

### 5.3 `key` / `doc` / `spec`

`key(kind, ref)`：

- `ref` 中含有 `:`：视为已经完整的 Spec 键，原样返回。不再加前缀，也不检查 `kind` 是否匹配（README：“接受已完整的 key”）。
- 否则返回 `f"{kind}:{ref}"`。不检查该键是否存在，不补版本。

`doc(key)`：`copy.deepcopy(self.docs[key])`。键不存在 → `FactoryError("SPEC_NOT_FOUND", f"找不到 Spec {key}", key=key)`。返回值被调用方修改不得影响下一次 `doc` / `spec`。

`spec(key)`：`copy.deepcopy(self.doc(key)["spec"])`。文档没有 `spec` 键是编程错误（通过检查的文档必有），让 `KeyError` 冒泡。

### 5.4 `type_key` / `is_subtype` / `facts_decl` / `always`

关系行从 `self.result.model.rows(名字)` 读取。行里的字符串就是 Python `str`。不要把 Spec 键传给这些方法冒充类型 id。

- `type_key(tid)`：在 `type_key` 行 `(id, key)` 里找 `id == tid`。没有 → `FactoryError("TYPE_NOT_FOUND", f"类型 {tid} 在快照中不存在", tid=tid)`。`note.text@1.1`（带 minor）不在该关系里，同样是 `TYPE_NOT_FOUND`。不要自行去掉 minor。
- `is_subtype(t, s)`：存在 `subtype` 行 `(t, s)` 则为真。内核已经包含自反（`subtype(T, T) :- tid(T)`）和传递。未知 id、带 minor 的类型名、Spec 键，一律返回 `False`，不抛异常。不要按名字前缀猜测（R19 的错误示范）。`any` 不是这个关系的成员；`T <: any` 由类型表达式的 `tsub` 处理，不在 `is_subtype` 里特判。
- `facts_decl(tid)`：先 `type_key(tid)`（未知则 `TYPE_NOT_FOUND`）。再收集 `type_fact` 行 `(t, f, ft)` 中 `t == tid` 的项，得到 `{f: ft}`。这已经含继承（`kernel/types.mrl` 的 `type_fact`）。同一 fact 不会有两种类型，否则检查阶段已是 error，`load` 到不了这里。
- `always(tid)`：先 `type_key(tid)`。`frozenset(p for t, p in rows("type_always_p") if t == tid)`。含继承。父类型没有子类型声明的 `always`。

笔记夹具（`note.text@1.1` 未退役）：

| 调用 | 结果 |
|---|---|
| `type_key("note.text@1")` | `Type:note.text@1.1` |
| `type_key("note@1")` | `Type:note@1.0` |
| `is_subtype("note.text@1", "note@1")` | `True` |
| `is_subtype("note@1", "note.text@1")` | `False` |
| `is_subtype("note.text@1", "note.text@1")` | `True` |
| `facts_decl("note.text@1")` | `{"chars": "int", "lines": "int"}` |
| `facts_decl("card@1")` | `{}` |
| `always("note.text@1")` | `frozenset({"non_empty"})` |
| `always("note@1")` | `frozenset()` |

`retire_note_text_1_1=True` 时，`type_key("note.text@1")` 为 `Type:note.text@1.0`，`always("note.text@1")` 为空（`always` 写在 1.1 上）。`short` 仍可见，因为 1.0 自己声明了它。

### 5.5 `validators`

1. `key = type_key(tid)`（未知则 `TYPE_NOT_FOUND`）。
2. 返回 `copy.deepcopy(spec(key).get("validators") or [])`，保持文档中的顺序。
3. 这是**该 Spec 键自己的** validator 列表，不含父类型，不含更旧的 minor。与内核一致：`concrete` / `emitted` 只看 `type_key` 指向的那份文档（`kernel/types.mrl`），而 `type_validator` 关系里会同时出现旧 minor 的行——那些行不是 `validators(tid)` 的返回值。
4. 笔记夹具：`validators("note.text@1")` 恰有一项，`name == "count-text"`，`run == ["count-text", "/bean/payload/note.txt"]`。`validators("note@1") == []`。退役 1.1 之后，返回的是 `Type:note.text@1.0` 上的那一份 `count-text`，不是空列表。

返回值被修改不得影响下一次调用。

### 5.6 `properties`

1. `type_key(tid)` 先确认类型存在。
2. 对 `type_prop` 的每一行 `(t, p, owner)`，若 `t == tid`：
   - `owner_key = type_key(owner)`（`owner` 是类型 id）；
   - 属性表达式的主体是 `f"{owner_key}/properties.{p}"`；
   - 值是 `compiled(该主体)`，类型为 `mrl.expr.CompiledExpr`。
3. 子类型不能重定义父类型的属性（否则检查阶段已是 `E-EXTENDS`）。每个名字至多一条。
4. 返回的 dict 按属性名字母序插入。笔记夹具中 `properties("note.text@1")` 的键集合是 `{"non_empty", "short"}`。`non_empty` 的 `source_text` 是 `bean.facts.chars > 0`，主体是 `Type:note@1.0/properties.non_empty`（定义在父类型上）。`short` 的主体是最新 minor 的 `…/properties.short`。

### 5.7 `compiled` 的查找

`compiled(subject)` 只查 5.8 建好的缓存。没有就 `FactoryError("SUBJECT_UNKNOWN", f"快照中没有表达式位置 {subject}", subject=subject)`。不要在查找时重新编译，也不要因为主体“看起来像”就现解析。

### 5.8 加载时编译（第 4 章的全部位置）

对 `result.extraction.sites` 的每一项 `site`（这就是 factory-profile 第 4 章的全部位置）：

1. `types, expect, problem = site_env(site, TypeEnv(extraction, model))`。
2. `problem` 非空、或 `compile_expr` / `compile_template` 抛出 `StaticError`：检查阶段本应已经报 `E-EXPR`，`load` 不该走到这里。让异常冒泡（`StaticError` 或 `AssertionError`），不要吞掉，不要把表达式当成真。
3. `functions = make_table()`，与 `check_sites` 相同。不要传入额外函数，不要换一套标准库。
4. `site.template` 为真：`compile_template(site.text, types, functions)`，缓存 `CompiledTemplate`。否则 `compile_expr(site.text, types, functions, expect=expect)`，缓存 `CompiledExpr`。
5. 缓存键是 `site.subject`。同一主体出现两次是抽取器的缺陷，`AssertionError`。

因此运行期 `compiled(subject).evaluate(env)` / `.render(env)` 用的就是检查器编译过的那份文本和类型环境。失败即关闭：求值抛出的 `mrl.errors.EvalError` 由调用方当成“不成立”，本组件不捕获。

`cache.md` 里“参数按声明类型转换”与 README 4.3 冲突时，以 README 为准：`params_value` **校验**，不把 `int` 收成 `float`。

### 5.9 `bean_value` / `inputs_value`

`bean_value` 是字段投影，**不**因为 `sealed is False` 再把 facts 清掉。README 4.3 那句“未封存的候选 Bean 的 facts 为 `{}`”描述的是仓库不变量：`Warehouse.add` / `get` 返回的未封存对象，其 `facts` 字段就是 `{}`。投影结果自然是 `{}`。出厂检验在封存前要拿实测 facts 求值，做法是构造一个 **带 facts 的临时对象** 传进来（`gate.md`），而不是让 `bean_value` 再滤一次。临时对象不写入仓库。

```python
{
    "id": bean.id,
    "type": bean.type,
    "facts": copy.deepcopy(dict(bean.facts)),
    "meta": copy.deepcopy(dict(bean.meta)),
    "payload": [
        {"path": f.path, "digest": f.digest, "size": f.size, "media_type": f.media_type}
        for f in bean.payload
    ],
}
```

`payload` 的顺序与 `bean.payload` 相同（仓库保证按 `path` 升序）。缺属性是调用方的编程错误，让 `AttributeError` 冒泡。

`inputs_value(inputs)`：对每个端口，

- 值是 `list` 或 `tuple`：`[bean_value(b) for b in 值]`（`list<T>` 端口）；
- 否则当作单个 Bean：`bean_value(值)`。

不要用 `isinstance(v, Bean)`（不能 import 仓库）。不要把 `str` 当成 Bean。返回新的 dict，修改它不影响入参。

### 5.10 `params_value`

1. `key = snapshot.key("Station", station_key)`，再 `decls = snapshot.spec(key).get("params") or {}`。只看工位自己的 `spec.params`，不把配方参数合并进来。
2. 只承认五个类型：`string` / `int` / `float` / `num` / `bool`，对应 5.11 的判定。这与 `mrl.factory.sites.PARAM_TYPES` 一致。
3. 对 `decls` 的每个名字（按名字升序收集错误，返回的 dict 按声明顺序插入，声明顺序即 YAML 映射的顺序）：
   - 名字在 `given` 里：取 `given[name]`，做类型校验；
   - 否则若声明里有 `default` 键：取默认值，同样校验；
   - 否则记一条问题 `reason="missing"`。
4. `given` 中多出来的名字：`reason="unknown"`。
5. 类型不符：`reason="type"`，并带 `expected`（声明的类型名）和 `value`（原值，须能被 `canonical_json` 序列化；不能序列化时改为 `repr(value)` 的字符串）。
6. 有任何问题就抛出 `FactoryError("PARAM_INVALID", "工位参数不合法", station=key, problems=[...])`。`problems` 的每一项是 `{"name", "reason", ...}`。一项不合法就不要返回部分结果。
7. 合法时返回新 dict。不修改 `given`。`bool` 不是 `int`，也不是 `num`。`float` 不接受 `int`。`num` 接受 `int` 和 `float`，不接受 `bool`。

笔记工位 `Station:shout@1`，`given={"weight": 1}` 时返回：

| 名字 | 值 | 原因 |
|---|---|---|
| `suffix` | `"!"` | 工位默认 |
| `times` | `2` | 工位默认 |
| `ratio` | `0.5` | 工位默认，且必须是 `float` |
| `weight` | `1` | 调用方给出；`num` 接受 `int` |
| `loud` | `False` | 工位默认 |

`given={}`、`{"weight": True}`、`{"weight": 1, "ratio": 1}`、`{"weight": 1, "nope": 1}` 都是 `PARAM_INVALID`。

### 5.11 类型判定（参数与 facts 共用这一套，facts 的使用方是 `gate.md`）

先判断 `isinstance(v, bool)`，再判断 `int` / `float`。Python 里 `bool` 是 `int` 的子类，顺序反了会把 `True` 当成整数。

| 声明 | 接受 |
|---|---|
| `string` | `str` |
| `int` | `int` 且不是 `bool` |
| `float` | `float` |
| `num` | `int` 或 `float`，且不是 `bool` |
| `bool` | `bool` |

### 5.12 五个环境构造函数

它们只组 dict，不求值。求值用 `compiled(...).evaluate` / `.render`，`convert=True`（默认）会把这些 dict 转成 MRL 值。字段集合必须与 `mrl.factory.sites.site_env` 一致，否则检查期通过的表达式会在运行期 `EvalError`。

| 函数 | 返回的根名字 | 对应 `Site.kind` |
|---|---|---|
| `property_env(bean)` | `{"bean": bean_value(bean)}` | `property` |
| `accept_env(out, inputs, params)` | `out`、`inputs`、`params` | `accept` |
| `approval_env(phase, payload, inputs, params)` | `proposal` 或 `request`，加 `inputs`、`params` | `approval` |
| `recipe_env(inputs, params)` | `inputs`、`params` | `recipe-template` |
| `runtime_env(brief, limits, operator)` | `brief`、`limits`、`operator` | `runtime-template` |

细节：

- `accept_env` 的 `out`：`list` / `tuple` 则逐个 `bean_value`，否则单个 `bean_value`。`inputs` 走 `inputs_value`。`params` 是 `dict(params)` 的拷贝（浅拷贝一层；值不改）。
- `approval_env`：`phase == "propose"` 时根名字是 `proposal`；`phase == "in_run"` 时根名字是 `request`。其他 phase → `ValueError`（编程错误）。不检查 `payload` 的字段；缺字段由求值失败即关闭。`inputs` / `params` 的处理与 `accept_env` 相同。
- `recipe_env`：`{"inputs": inputs_value(inputs), "params": dict(params)}`。配方端口的 Bean 形状与工位端口相同，所以共用 `inputs_value`。
- `runtime_env` **不转换类型**。`brief` 若有属性 `task` 和 `factory_contract`（`mao.brief.Brief` 满足这一点，但本模块不 import 它），用属性；否则按映射取这两个键。`limits` 取 `max_turns`、`timeout_s`、`budget_usd`。`operator` 取 `model`、`runtime`。缺键让 `KeyError` 冒泡。调用方保证 `max_turns` 与 `timeout_s` 是 `int`，`budget_usd` 是 `float`，与 `RUNTIME_ENV` 一致。

`equivalence`、`idempotency`、`step-param`、`switch`、`until` 没有单独的构造函数（README 4.3 没列）。它们的值形状以 `site_env` 为准，由后续组件在调用 `compiled` 时自己组装。M1-02 只保证这些主体能 `compiled` 出来。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `REGISTRY_INVALID` | `check_registry` 的结果里有 severity 为 `error` 的诊断 | `diags`：这些诊断的 `to_json()` 列表 |
| `SPEC_NOT_FOUND` | `doc` / `spec` / `params_value` 用的 Spec 键不在快照中 | `key` |
| `TYPE_NOT_FOUND` | 类型 id 不在内核 `type_key` 关系里（含误传带 minor 的类型名） | `tid` |
| `SUBJECT_UNKNOWN` | `compiled` 的主体不在本次抽取的表达式位置里 | `subject` |
| `PARAM_INVALID` | 工位参数缺必填、多了未知名，或类型不符 | `station`、`problems` |

`ValueError`、`KeyError`、`AttributeError`、`AssertionError`、`StaticError`、`FileNotFoundError` 不包装成 `FactoryError`。

---

## 7. 验收测试

文件：`tests/acceptance/m1/test_m1_02_snapshot.py`。

```bash
uv run python tools/harness.py pytest tests/acceptance/m1/test_m1_02_snapshot.py
```

夹具 Registry 由 `tests/acceptance/m1/m1_data_helpers.py` 写出。该文件声明：Registry 必须零 error、零 warning，由 `test_notes_registry_is_clean` 守护。

| 测试 | 验证 |
|---|---|
| `test_notes_registry_is_clean` | `write_notes_registry` 的目录能 `load`；`result.ok`；error 与 warning 都是 0 |
| `test_load_rejects_invalid_registry` | 不复制宪法 → `REGISTRY_INVALID`，`diags` 里有 `E-CONSTITUTION` |
| `test_epoch_is_tree_hash` | epoch 匹配 `^e-[0-9a-f]{16}$`，且等于 5.2 的独立计算 |
| `test_epoch_ignores_absolute_path_and_changes_with_content` | 两棵相同的树 epoch 相同；加一个非 Spec 文件后 epoch 改变且仍符合 5.2 |
| `test_key_doc_spec_and_deep_copy` | `shout@1` 与已完整的键；`doc` / `spec` 深拷贝 |
| `test_type_key_latest_and_retired` | 最新 minor、退役后落到 1.0、带 minor 的参数是 `TYPE_NOT_FOUND` |
| `test_is_subtype_matches_kernel` | 自反、`note.text@1 <: note@1`、反向为假、未知 id 与带 minor 的字符串为假 |
| `test_facts_validators_properties_always` | 继承来的 facts；validator 只取最新 minor；属性含父类型；`always` 含继承；退役后 `always` 为空 |
| `test_compiled_covers_sites_and_envs` | 每个 `extraction.sites` 都能 `compiled`；属性、accept、审批求值与配方 / runtime 模板渲染和 5.12 一致 |
| `test_compiled_unknown_subject` | `SUBJECT_UNKNOWN` |
| `test_bean_and_inputs_value` | 字段投影、深拷贝、未封存但自带 facts 的对象不会被清空；标量与列表输入 |
| `test_params_value_defaults_and_rejects` | 5.10 的默认值表与四种 `PARAM_INVALID` |
| `test_envs_match_sites` | `approval_env` 的根名字、未知 phase 的 `ValueError`、`runtime_env` 接受映射和属性对象 |

---

## 8. 错误示范与正确示范

**8.1 在 Python 里重写子类型**

```python
# ✖ 按名字前缀猜。card 不会 extends note，但 "note" 前缀对不上的反例也会在别的类型上猜错
def is_subtype(t, s):
    return t == s or t.startswith(s.split("@")[0] + ".")
# ✔ 只查内核已经算出的关系
return any(a == t and b == s for a, b in self.result.model.rows("subtype"))
```

**8.2 运行期另编译一份表达式**

```python
# ✖ 类型环境与检查器不一致时，检查通过的表达式会在运行期炸，或者更糟：检查没看到的名字被求值
ok = compile_expr(text, {"out": DYN}).evaluate(env)
# ✔ 用加载时按 site_env 编译好的那一份
ok = snapshot.compiled("Station:shout@1/outputs.note.accept[0]").evaluate(accept_env(out, inputs, params))
```

**8.3 把参数类型“收”一下好让测试通过**

```python
# ✖ README 要求校验。int 收成 float 之后，声明为 float 的参数会悄悄接受 1
if ptype == "float" and isinstance(value, int) and not isinstance(value, bool):
    value = float(value)
# ✔ 类型不符就是 PARAM_INVALID。1 不是 float，1 可以是 num
raise FactoryError("PARAM_INVALID", "工位参数不合法", station=key, problems=problems)
```

**8.4 目录树哈希拼 Windows 路径**

```python
# ✖ 同一棵树在 Windows 和 Linux 上 epoch 不同；盘符还会进哈希
line = str(path.relative_to(root)) + digest
# ✔ 相对路径只用 /
rel = path.relative_to(root).as_posix()
manifest[rel] = digest_file(path)
```

---

## 9. 风险与注意事项

- **R19**：`type_prop` 的第三列是定义者的类型 id，不是 Spec 键。属性文本在 `type_key(owner)` 那份文档上，不在子类型文档上找同名键（子类型文档里可能没有这把键）。
- **R8**：`compiled` 是运行期取表达式的唯一入口。组件里出现 `compile_expr(spec 里抄出来的字符串)` 就是缺陷。
- **R5**：epoch 用 `as_posix()` 和原始字节。不要把 `\` 规范化成 `/` 之外的形式，也不要在哈希前把 CRLF 换成 LF。
- **并行任务**：`mao.values` 不能 import `mao.warehouse`。`materialize` 写出的 `bean.json` 与 `bean_value` 同形，由 `warehouse.md` 自己组装，以免 M1-03 反过来依赖本任务。
- **警告**：`W-EXPR` 不阻止 `load`。求值时可选字段缺失会 `EvalError`，调用方按失败即关闭处理，不要当成 `True`。
- **大 Registry**：编译发生在 `load`，不在每次 `compiled` 调用。不要对每个文件 `read_bytes()` 来算 epoch。
