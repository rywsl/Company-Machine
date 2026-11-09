# MRL 1.0 语言规范（MAO Relation Language）

> 状态：1.0　|　日期：2026-11-09　|　参考实现：`src/mrl/`（`mrl` 1.0.0）
>
> MRL 是用来约束工厂系统边界的小型语言：**可运行、可验证、结果确定**。本文档规定它的值、类型、词法、文法、静态检查、求值语义、标准库、规则语言与错误码。
> 一致性用例：`spec/mrl/conformance/expr/`、`spec/mrl/conformance/rules/`；工厂 profile 的用例（`factory/`）见 [factory-profile.md](factory-profile.md)。
>
> 本文档中的“必须”“不得”是规范要求。实现与本文档不一致时，以一致性用例为准，并且必须修正其中一方。

## 阅读指引

| 我要…… | 读 |
|---|---|
| 写一个检查表达式（验收标准、命名属性、switch/until 条件、审批条件） | 2、5、6 |
| 写模板（幂等键、步骤参数、配方与运行时模板） | 7 |
| 写或修改内核、宪法规则（`.mrl` 文件） | 8，以及 6.7 标准库 |
| 看懂一个错误码 | 9 |
| 修改实现或一致性用例 | 全部；改完运行第 11 章的命令 |

**记号**：`e ⇒ v` 表示表达式 e 求值得到 v；`e ⇒ MRL-X` 表示得到错误 MRL-X。“静态”指编译期（解析 + 静态检查），“运行期”指求值期。值按 `mrl eval` 的显示格式书写：字符串带双引号；float 总带小数点或指数（`2.0`、`1e+16`），因此 `2` 是 int、`2.0` 是 float。

## 目录

1. 概述
2. 值与类型
3. 词法
4. 表达式文法
5. 静态检查
6. 求值语义与标准库
7. 模板（MRL-T）
8. 规则（MRL-R）
9. 错误码
10. 实现限制与宿主接口
11. 一致性用例格式
- 附录 A：一致性套件一览

---

## 1. 概述

### 1.1 范围

MRL 由三部分组成，共用同一套词法、值域与标准库：

| 部分 | 是什么 | 典型用途 | 宿主入口 | 命令行 |
|---|---|---|---|---|
| MRL-E 表达式 | CEL 语法的子集，语义另行精确规定 | 验收标准、命名属性、switch/until 条件、审批条件 | `compile_expr` | `mrl eval` |
| MRL-T 模板 | 文本 + `{{ 表达式 }}` 插值 | 幂等键、步骤参数、配方与运行时模板 | `compile_template` | `mrl template` |
| MRL-R 规则 | Datalog 风格的带类型关系规则 | 内核与宪法：从 Spec 抽取的事实推导诊断 `diag` | `Program.from_sources` | `mrl check`、`mrl run` |

本文档不规定：Spec 中哪些字段是表达式、各位置的类型环境、内核关系的含义——这些属于工厂 profile（factory-profile.md）。

### 1.2 设计目标

| 目标 | 含义 | 怎么保证 |
|---|---|---|
| 确定 | 同样的输入总是得到同样的结果：同样的值、同样的错误码、同样的规则输出与顺序 | 没有时间、随机数、环境变量、I/O；map 按键的码点序迭代；求值严格从左到右；规则输出按规范序排序；`sum`、`set` 聚合按规范序计算 |
| 全函数（有界） | 每次求值都会结束：要么得到值，要么得到一个 `MRL-*` 错误码 | 没有循环和递归函数；宏只遍历有限的集合；步数预算（6.8）；嵌套深度限制（4.7）；递归规则中不构造新值（8.7）；元组总数上限（8.6） |
| 失败即关闭 | 任何错误都使整个求值失败；宿主必须把错误当作“检查不通过” | 没有 CEL 式的错误吸收；`&&`、`\|\|` 只在左边已经决定结果时才跳过右边；规则求值遇到错误立即终止，不产生部分结果 |
| 可静态检查 | 能在求值前发现的错误都在编译期报告 | 名字、函数、元数、类型、记录字段、可选字段的保护、关系归属、安全性、分层，全部静态检查 |

实现必须做到：对任何输入，要么返回结果，要么报告 `MRL-*` 错误；不得以宿主语言的异常（例如栈溢出）结束。

### 1.3 与 CEL 的关系

MRL-E 的语法取自 CEL：运算符与优先级、`?:`、`in`、成员与下标、接收者形式的调用（`s.startsWith("a")`）、宏 `all/exists/exists_one/map/filter`、`has()`、关键字与保留字表都与 CEL 相同。语义按“确定、失败即关闭、可静态检查”重新规定，下表是有意的差异：

| 方面 | MRL 的规定 | 为什么 |
|---|---|---|
| 数值 | 只有 int（64 位）与 float（有限 binary64）。`+ - *` 两边混合时提升为 float；`/` 总是得到 float；`%` 只用于 int，向下取整 | CEL 不做隐式提升（`1 + 1.0` 是错误）。Spec 中的数大多来自 JSON，`60` 与 `60.0` 无法区分，`duration_s * 0.9` 必须直接可用 |
| 种类 | 没有 uint、bytes、时间、时长；没有 NaN、±∞；`-0.0` 规范化为 `0.0` | 种类越少，越难写错；避免 NaN 的比较陷阱 |
| 错误 | 任何错误立即终止求值 | CEL 的 `&&`、`\|\|` 会吸收错误（`error \|\| true` 为 true）。吸收让结果依赖于“哪个错误恰好被跳过”，失败即关闭更安全 |
| 宏 | `all` 遇到第一个 false 停止，`exists` 遇到第一个 true 停止；停止之前出现的错误就是整个宏的错误 | 同上：严格从左到右 |
| 比较 | `a < b < c` 是语法错误 | 避免误读为区间判断 |
| map | 键只能是 string；按键的码点序迭代；字面量中的重复键是错误 | 确定性 |
| 字符串字面量 | 只有 `\\ \" \' \n \r \t \uXXXX \UXXXXXXXX` 八种转义；没有原始字符串、三引号、八进制或 `\x` 转义 | 够用即可 |
| 静态类型 | 有记录类型（开放/封闭、可选字段）与定义性分析 | 字段拼写错误、读取可能不存在的字段在编译期发现 |
| 标准库 | 见 6.7；用 `glob()` 代替正则 `matches()` | 时间有保证的上界（`glob` 为 O(\|s\|·\|模式\|)），没有回溯 |
| 资源 | 步数预算、嵌套深度限制 | 全函数 |

### 1.4 与 Datalog 的关系

| 与 Datalog 相同 | 与 Datalog 不同（为什么） |
|---|---|
| `head :- body.` 规则与事实；大写开头的变量；`_` 匿名变量 | 关系必须先声明，列带类型（8.2）：类型错误在编译期发现 |
| 集合语义；分层否定；自底向上的半朴素求值；结果唯一 | 规则体严格从左到右：变量必须先绑定后使用（8.5），守卫的位置决定它在哪些绑定上被求值（8.9）——这样表达式的错误行为是确定的 |
| | 规则中可以使用 MRL-E 表达式：赋值、守卫、头部表达式 |
| | 聚合 `count/sum/min/max/set`（8.8） |
| | 模块与归属（8.3）：追加模块只会增加诊断，不会放松已有约束 |
| | 递归规则中不允许构造新值（8.7）：保证终止 |
| | 规则体不支持析取（写成多条规则）；运行期错误终止整个求值 |

### 1.5 一致性级别

| 级别 | 要求 |
|---|---|
| MRL-E/T | 实现第 2–7 章；通过 `expr.*` 与 `template.basic` 全部套件 |
| MRL 1.0 完整 | 再实现第 8 章；通过 `rules.*` 全部套件 |
| 工厂 profile | 在完整 MRL 1.0 之上实现 factory-profile.md；通过 `factory.*` 全部套件 |

符合某个级别的实现，对该级别的每个用例都必须给出相同的值、静态类型、警告码集合与错误码。错误消息与位置不要求相同。

---

## 2. 值与类型

### 2.1 值

值有七种：

| 种类 | 取值 | 字面量 | `type()` |
|---|---|---|---|
| null | 只有 `null` | `null` | `"null"` |
| bool | `true`、`false` | `true` `false` | `"bool"` |
| int | −2^63 … 2^63−1 | `42`、`-7` | `"int"` |
| float | 有限的 IEEE-754 binary64；没有 NaN、±∞、−0.0 | `4.25`、`1e3` | `"float"` |
| string | Unicode 码点序列 | `"a"`、`'a'` | `"string"` |
| list | 有序序列，元素可以是任意种类 | `[1, "a"]` | `"list"` |
| map | 键为 string 的映射，迭代顺序为键的码点序 | `{"a": 1}` | `"map"` |

- 所有值都不可变；没有引用，没有对象身份。
- bool 不是数：`true + 1 ⇒ MRL-TYPE`。
- “记录”只是静态类型（2.4），运行期的记录就是 map。
- 宿主值的转换：JSON/YAML 的 null、布尔、整数、浮点数、字符串、数组、对象依次对应 null、bool、int、float、string、list、map。整数超出 int64 → MRL-OVERFLOW；浮点数不是有限数 → MRL-DOMAIN；对象的键不是字符串 → MRL-TYPE；−0.0 变为 0.0。

### 2.2 数值模型

| 规则 | 说明 |
|---|---|
| int 检查溢出 | `+ - *`、一元 `-`、`abs`、`sum`、`int()`、`floor`、`ceil`、`round` 的结果超出 int64 → MRL-OVERFLOW |
| float 必须有限 | 结果为 ±∞ → MRL-DOMAIN；下溢得到 0.0 或次正规数，不报错 |
| 零只有一个 | 任何为零的 float 结果都是 `0.0`；−0.0 不存在：`string(-0.0) ⇒ "0.0"` |
| 提升 | `+ - *` 两边都是 int 时做整数运算；否则两边都转换为 float 再运算。int 转 float 取最近的 binary64，恰在中间时取偶数：`9007199254740993 * 1.0 ⇒ 9007199254740992.0` |
| `/` 总是 float | 两边先转换为 float 再除：`6 / 3 ⇒ 2.0` |
| 比较是精确的 | int 与 float 的 `==`、`<` 等按数学值比较，不经过转换：`9007199254740993 == 9007199254740992.0 ⇒ false` |

**float 的文本形式**（`string()`、模板插值、`mrl eval` 的显示）：取能精确往返的最短十进制数字串（与 Python 3 的 `repr(float)` 相同）。设科学记数法的十进制指数为 e：

- −4 ≤ e ≤ 15：定点形式，至少一位小数：`1.0`、`0.0001`、`1000000000000000.0`、`123456789.12345679`；
- 否则：`d[.ddd]e±XX`，指数带符号、至少两位：`1e+16`、`1.5e-05`、`5e-324`。

### 2.3 相等、顺序与身份

MRL 有四种比较关系，各有固定用途，不得混用：

| 关系 | 定义 | 用在 |
|---|---|---|
| 相等 | 数按数学值（`1` 与 `1.0` 相等）；其他种类不同即不等；list 长度相同且逐元素相等；map 键集相同且逐值相等 | `==`、`!=`、`in`；规则中对已绑定变量的赋值 `X = e`；`unique` 与集合函数；`set` 聚合的去重 |
| 有序比较 | 只对（数, 数）与（string, string）定义；数按数学值；string 按码点逐个比较，前缀较小 | `<`、`<=`、`>`、`>=`；`min`、`max`（函数与聚合）；`sort` |
| 规范序 | 全序：null < bool < 数 < string < list < map。false < true；数按数学值，相等时 int 在 float 前；string 按码点；list 逐元素字典序（前缀较小）；map 按“（键, 值）对的序列”字典序 | 规则输出的排序；`set` 聚合的结果顺序；`sum` 聚合的累加顺序；`union`、`intersect`、`difference` 的结果顺序 |
| 身份 | 种类与值都相同：int `1` 与 float `1.0` 是不同的身份；list、map 递归比较身份 | 关系元组的去重；原子匹配；聚合的分组与绑定去重；一致性用例中 `value` 的比较 |

例：`1 == 1.0 ⇒ true`；但在 `any` 列中 `1` 与 `1.0` 是两个不同的元组（8.2）。

参考实现（`values.py`）：相等为 `eq`，与它一致的哈希键为 `eq_key`（用于去重）；有序比较为 `compare`；规范序为 `order_key`；身份为 `ident_key`。修改其中任何一个，都必须同时检查上表“用在”一列的全部位置。

### 2.4 静态类型

| 类型 | 描述的值 | 说明 |
|---|---|---|
| `bool` `int` `float` `string` | 同名种类 | |
| `null` | 只有 null | null 字面量的类型 |
| `num` | int 或 float | 只用于静态类型，不能作列类型 |
| `dyn` | 任意值 | 静态未知，相关检查推迟到运行期。类型字符串中 `any` 是 `dyn` 的别名 |
| `list<T>` | 元素类型为 T 的 list | |
| `map<T>` | 值类型为 T 的 map | 键总是 string |
| 记录 `{a: T, b?: U}` | 带这些字段的 map | `b?` 是可选字段；末尾写 `...` 表示开放记录 |

记录类型：

- **封闭记录**（没有 `...`）：只能访问声明的字段，访问其他字段 → MRL-NO-FIELD（静态）。
- **开放记录**（`{a: int, ...}`）：未声明的字段类型为 `dyn`。
- **可选字段**：访问前必须用 `has()` 判断，否则 MRL-MAYBE-ABSENT 警告（5.4）。
- 记录类型来自宿主（例如工厂中的 Bean 类型），或来自键全是字符串字面量的 map 字面量（5.3）。

### 2.5 最小上界（lub）

条件表达式的两个分支、列表的元素用 lub 合并类型。按顺序取第一条匹配的规则：

| 情形 | lub(A, B) |
|---|---|
| A = B | A |
| 一边是 null | dyn |
| 都是数（int、float、num） | num |
| list<X>、list<Y> | list<lub(X, Y)> |
| map<X>、map<Y> | map<lub(X, Y)> |
| map 或记录、map 或记录（不相同） | map<dyn> |
| 其他 | dyn |

例：`[1, 2.0]` 的类型是 `list<num>`；`[1, "a"]` 是 `list<dyn>`；`b ? 1 : null` 是 `dyn`。

### 2.6 相容性（compat）与可比较性

`compat(S, D)`：静态类型为 S 的值能否放进需要 D 的位置。结果为 **yes**（可以）、**maybe**（静态放行，运行期检查）、**no**（静态错误 MRL-TYPE）。按顺序取第一条匹配的规则：

| # | 条件 | 结果 |
|---|---|---|
| 1 | D = dyn，或 S = D | yes |
| 2 | S = dyn | maybe |
| 3 | S = int，D = float | yes（int 会被规范化为 float，见 8.2） |
| 4 | S 是 int 或 float，D = num | yes |
| 5 | S = num，D 是 int 或 float | maybe |
| 6 | 都是 list，或都是 map | compat(S 的元素类型, D 的元素类型) |
| 7 | S 是记录，D = map<E> | 对每个字段 compat(字段类型, E)，开放记录再加上 compat(dyn, E)：有 no 即 no；否则有 maybe 即 maybe；否则 yes |
| 8 | 其他（包括 null 放进 string、两个不同的记录） | no |

compat 用于：宿主给定的期望类型（5.5）；规则中的常量、变量、表达式与聚合放进列（8.4、8.8）。

**可比较性**（`==`、`!=`、`in` 的静态检查）：两边有一边是 dyn 或 null，或类型相同，或都是数，或都是 list 且元素类型可比较，或都是 map/记录 → 可比较；否则 MRL-TYPE。例：`"1" == 1`、`[1] == ["a"]`、`[] == {}` 都是静态错误；`x == null` 对任何类型的 x 都合法。

### 2.7 类型字符串

宿主、命令行的 `--types` 与一致性用例用类型字符串给出根名字的类型：

```
type   = "bool" | "int" | "float" | "num" | "string" | "null" | "dyn" | "any"
       | "list" "<" type ">" | "map" "<" type ">" | record ;
record = "{" [ fields ] "}" ;
fields = "..." | field [ "," fields ] ;
field  = name [ "?" ] ":" type ;          (* "?" 紧跟在名字后面 *)
name   = 字母、数字、下划线组成的串 ;
```

- 记号之间只允许空格（不允许制表符、换行）。`...` 只能写在最后。
- 例：`{id: string, facts: {width: int, height: int}, meta: {title?: string, ...}}`、`list<{path: string, size: int}>`、`map<dyn>`。

---

## 3. 词法

表达式、模板插值与规则文件共用同一个词法分析器。记号有六类：int、float、string、标识符、运算符、语句结束符。

### 3.1 源文本、空白与注释

- 源文本是 Unicode 字符串。单个源（一个表达式、一个规则文件、模板中的一个插值）最多 1,048,576（2^20）个字符，超出 → MRL-LIMIT。
- 空白只有空格、制表符（U+0009）、回车（U+000D）、换行（U+000A）。其他字符（全角空格、不换行空格等）出现在字符串与注释之外 → MRL-SYNTAX。
- 注释：`//` 到行末。没有块注释，`/* */` 是语法错误。表达式中也可以写注释：第一行 `1 + // 注释`、第二行 `2`，合起来是值为 `3` 的表达式。
- 位置：行、列从 1 开始，列按码点计数。

### 3.2 标识符、关键字与保留字

- 标识符：`[A-Za-z_][A-Za-z0-9_]*`，只限 ASCII。`名字` → MRL-SYNTAX。
- 关键字：`true false null in`。
- 保留字（与 CEL 相同）：`as break const continue else for function if import let loop package namespace return var void while`。
- 关键字与保留字不能用作名字（根名字、宏变量、函数名、关系名），否则 MRL-SYNTAX；但可以用作 `.` 之后的字段名（`m.in`、`m.for`）、关系的列名、模块名的组成部分。
- 下列词不是保留字，只在特定位置有特殊含义：

| 词 | 特殊含义只出现在 | 其他位置 |
|---|---|---|
| `has` | 全局调用 `has(e.f)`（4.4） | 普通名字；`x.has(y)` 是普通的函数调用 |
| `all` `exists` `exists_one` `map` `filter` | 接收者形式的调用 `e.all(...)`（4.4） | 全局调用 `all(...)` 是普通的函数调用 |
| `count` `sum` `min` `max` `set` | 规则头部的顶层项（8.8） | 普通函数名：`sum`、`min`、`max` 是标准库函数；`count`、`set` 没有定义 |
| `module` `rel` | 规则文件中语句的开头（8.1） | 普通名字 |
| `input` `open` `doc` | 规则文件中 `@` 之后（8.2） | 普通名字 |

### 3.3 数字字面量

```
int   = "0" | [1-9] [0-9]* ;
float = digits "." digits [ exp ] | digits exp ;
exp   = ( "e" | "E" ) [ "+" | "-" ] digits ;
```

- 只有十进制；整数部分不能有前导零：`007`、`00.5` → MRL-SYNTAX；`0.5`、`0e5` 合法。
- `1.` 与 `.5` 都不是数字字面量（MRL-SYNTAX）。`1.e5` 是 `1` 后面跟字段选择 `.e5`。
- `e`、`E` 后面必须有数字：`1e` → MRL-SYNTAX。
- 数字后面紧跟字母或 `_` → MRL-SYNTAX：`12abc`、`0x10`、`1_000`。
- 字面量不带符号，`-` 是运算符（负整数字面量的折叠见 4.6）。
- int 字面量必须 ≤ 2^63−1（例外见 4.6），否则 MRL-OVERFLOW（静态）。
- float 字面量按 IEEE 最近舍入；结果为 ∞ → MRL-SYNTAX（`1e400`）；下溢得到 0.0（`1e-400 ⇒ 0.0`）。

### 3.4 字符串字面量

- 用 `"` 或 `'` 括起，两种完全等价。
- 不能跨行：未转义的换行（U+000A）→ MRL-SYNTAX；制表符、回车可以原样出现。
- 转义（两种引号中都可用）：

| 写法 | 字符 |
|---|---|
| `\\` | 反斜杠 |
| `\"`、`\'` | 双引号、单引号 |
| `\n`、`\r`、`\t` | 换行、回车、制表符 |
| `\uXXXX` | 4 位十六进制给出的码点 |
| `\UXXXXXXXX` | 8 位十六进制给出的码点 |

- 十六进制数字大小写均可；码点必须是 Unicode 标量值（≤ 10FFFF，且不在 D800–DFFF），否则 MRL-SYNTAX：`"\ud800"`。位数不足（`"\u12"`）→ MRL-SYNTAX。
- 其他转义（`\x41`、`\0`、`\a`……）→ MRL-SYNTAX。

### 3.5 运算符、语句结束符与 `:-`

运算符与标点按最长匹配切分：

```
==  !=  <=  >=  &&  ||  <  >  !  +  -  *  /  %  ?  :  .  ,  (  )  [  ]  {  }  =  ;  @
```

- 单独的 `&`、`|`，以及 `#`、`$`、`^`、`~` 等 → MRL-SYNTAX。`=` 只用于规则中的赋值；`@` 只用于注解；`;` 没有合法用法。
- **语句结束符**：`.` 后面紧跟空白、`//` 或源结束时，是语句结束符（规则文件使用）；否则是成员选择运算符。因此：
  - 表达式 `m. a` 是语法错误：`.` 后面是空格，成了语句结束符；
  - 规则文件中两条语句之间必须有空白：`p(1).q(2).` 是语法错误；
  - 规则末尾的 `L = M.language.` 是“选择 language，然后结束”；`limit("cap", 12.0).` 中 `12.0` 是 float。
- **`:-`** 不是一个记号，而是紧邻的 `:` 与 `-`（中间不能有空白），只在规则头之后被识别。`p(X) : - e(X).` → MRL-SYNTAX；`p(X):-e(X).` 合法。

### 3.6 正确与错误示范

```
// ✔ 正确
m.in                     // 关键字可以作字段名
'it\'s'                  // 单引号字符串中转义单引号
-9223372036854775808     // 最小的 int（负数折叠，4.6）
1e-400                   // 下溢得到 0.0

// ✖ 错误
in                       // MRL-SYNTAX：关键字不能作名字
m. a                     // MRL-SYNTAX：“.” 后面是空格，成了语句结束符
007                      // MRL-SYNTAX：前导零
1 # 2                    // MRL-SYNTAX：# 不是注释
9223372036854775808      // MRL-OVERFLOW：超出 int64
```

---

## 4. 表达式文法

### 4.1 EBNF

```
Expr    = Or [ "?" Or ":" Expr ] ;
Or      = And { "||" And } ;
And     = Rel { "&&" Rel } ;
Rel     = Add [ RelOp Add ] ;                  (* 不可结合 *)
RelOp   = "<" | "<=" | ">" | ">=" | "==" | "!=" | "in" ;
Add     = Mul { ( "+" | "-" ) Mul } ;
Mul     = Unary { ( "*" | "/" | "%" ) Unary } ;
Unary   = "!" Unary | "-" Unary | Member ;
Member  = Primary { "." Name [ Args ] | "[" Expr "]" } ;
Args    = "(" [ Expr { "," Expr } ] ")" ;
Primary = INT | FLOAT | STRING | "true" | "false" | "null"
        | Ident [ Args ]
        | "(" Expr ")"
        | "[" [ Expr { "," Expr } [ "," ] ] "]"
        | "{" [ Expr ":" Expr { "," Expr ":" Expr } [ "," ] ] "}" ;
```

- `Ident` 不能是关键字或保留字；`Name`（`.` 之后）可以是任何标识符，包括关键字与保留字。
- 调用的参数列表不允许末尾逗号（`max([], )` → MRL-SYNTAX）；列表与 map 字面量允许。
- 整个源必须恰好是一个表达式：`1 2` → MRL-SYNTAX；空源 → MRL-SYNTAX。

### 4.2 优先级与结合性

| 优先级（高→低） | 运算符 | 结合性 |
|---|---|---|
| 1 | `e.f`、`e.f(...)`、`f(...)`、`e[i]` | 左 |
| 2 | 一元 `!`、`-` | 右（前缀） |
| 3 | `*` `/` `%` | 左 |
| 4 | `+` `-` | 左 |
| 5 | `<` `<=` `>` `>=` `==` `!=` `in` | **不可结合** |
| 6 | `&&` | 左 |
| 7 | `\|\|` | 左 |
| 8 | `c ? a : b` | 右 |

- 比较运算符不可结合：`1 < 2 < 3`、`1 == 1 == true`、`1 < 2 == true` 都是 MRL-SYNTAX。要连用请加括号：`(1 == 1) == true ⇒ true`。
- `==` 与 `<` 同级（与 C 不同）。`!` 比 `==` 紧：`!true == false` 是 `(!true) == false ⇒ true`。
- `?:` 的中间部分属于 `Or` 层，不能直接是条件表达式：`true ? false ? 1 : 2 : 3` → MRL-SYNTAX，要写成 `true ? (false ? 1 : 2) : 3`。末尾部分可以：`false ? 1 : true ? 2 : 3 ⇒ 2`。

### 4.3 成员、下标与调用

- `e.f` 是字段选择，`e[i]` 是下标（list 用 int，map 与记录用 string）。语义见 5.4、6.5。
- `f(a, b)` 是全局调用。`e.f(a, b)` 是**接收者形式**，等价于 `f(e, a, b)`，对所有函数都可用：`"abc".size() ⇒ 3`，`[3, 1].sort() ⇒ [1, 3]`，`"a,b".split(",").size() ⇒ 2`。
- 函数名在独立的命名空间中：根名字 `size` 与函数 `size()` 不冲突。
- 宏（4.4）只有接收者形式；`has` 只有全局形式。

### 4.4 宏与 has()

| 写法 | 含义 |
|---|---|
| `e.all(v, p)` | e 的每个元素都使 p 为 true |
| `e.exists(v, p)` | 至少一个元素使 p 为 true |
| `e.exists_one(v, p)` | 恰好一个元素使 p 为 true |
| `e.filter(v, p)` | 使 p 为 true 的元素组成的 list |
| `e.map(v, t)` | 每个元素经 t 变换后组成的 list |
| `e.map(v, p, t)` | 先按 p 过滤，再按 t 变换 |
| `has(e.f)` | e 是否有字段 f |

- e 是 list 时，v 依次取各元素；e 是 map 时，v 依次取各键（按码点序）。
- v 必须是单个标识符（不能是关键字或保留字），只在 p、t 中可见。
- 参数个数不对、v 不是标识符 → MRL-MACRO：`[1].all(x)`、`[1].map(x)`、`[1].all(1, true)`、`[1].map(x.y, true)`。
- `has()` 必须恰好有一个参数，且是字段选择 `e.f`：`has(m)`、`has(m["k"])` → MRL-MACRO。
- 全局形式 `all(l, x, p)` 不是宏，而是对函数 `all` 的调用（没有这个函数，MRL-UNDEFINED）。

### 4.5 列表与 map 字面量

- `[e1, e2, …]`，允许末尾逗号：`[1, 2,] ⇒ [1, 2]`；`[,]`、`[1,,2]` → MRL-SYNTAX。
- `{k1: v1, …}`：键可以是任意表达式，求值结果必须是 string；允许末尾逗号。静态类型见 5.3，求值见 6.5。

### 4.6 整数字面量的范围与负数折叠

- 一元 `-` 后面紧跟（中间可以有空白）一个 int 字面量，且该字面量后面不是 `.` 或 `[` 时，二者折叠为一个负整数字面量。所以 `-9223372036854775808` 合法。
- 不折叠时 `-` 是普通的一元运算符：`-(9223372036854775808)` → MRL-OVERFLOW；`-5.abs()` 是 `-(5.abs()) ⇒ -5`（成员访问比一元 `-` 紧）。
- int 字面量（折叠前）大于 2^63 → MRL-OVERFLOW；等于 2^63 且没有被折叠 → MRL-OVERFLOW。
- float 字面量从不折叠：`-1.5` 是一元 `-` 作用于 `1.5`（这影响 8.7 中“常量”的判断）。

### 4.7 嵌套深度

解析器维护嵌套深度：顶层表达式为 1；下列位置每进入一层加 1：括号内的表达式、函数或宏的每个参数、列表元素、map 的键和值、下标表达式、`?:` 的末尾部分、未折叠的一元 `!`、`-` 的操作数。深度超过 64 → MRL-LIMIT（静态）。

例：63 层括号 `((…(1)…))` 合法，64 层是 MRL-LIMIT；63 个连续的 `!` 合法，64 个是 MRL-LIMIT。二元运算链（`a + b + c + …`）与成员链（`a.b.c…`）不增加深度。

---

## 5. 静态检查

### 5.1 输入与输出

- 输入：表达式的语法树；类型环境（根名字 → 类型，由宿主给出）；函数表；可选的期望类型。
- 输出：表达式的静态类型；警告列表；访问过的字段路径（宿主用来判断表达式读了哪些字段，例如工厂 profile 的 `W-PROPERTY-META`）。
- 检查收集全部诊断，不在第一个错误处停止；出错的子表达式按 `dyn` 继续推导，避免连锁错误。有任何 error 即编译失败（第一个 error 是主错误）；只有警告时编译成功，警告随编译结果返回。

### 5.2 名字解析与遮蔽

- 名字按“最内层的宏变量 → 环境中的根名字”解析；找不到 → MRL-UNDEFINED：`undefined_name`。
- 宏变量不得与环境中的根名字或外层的宏变量同名 → MRL-SHADOW：`x.all(x, x > 0)`、`[[1]].all(a, a.all(a, a > 0))`。在规则中，环境就是已绑定的规则变量（8.4），宏变量也不得与它们同名。
- 宏变量只在宏的谓词与变换中可见：`[1].all(x, true) && x > 0` → MRL-UNDEFINED。并列的两个宏可以使用同一个变量名：`[1].map(x, x) + [2].map(x, x) ⇒ [1, 2]`。
- 函数单独解析：未定义的函数 → MRL-UNDEFINED（`foo(1)`）。

### 5.3 类型推导规则

下表中“数”指 int、float 或 num。操作数不满足要求时报 MRL-TYPE，该节点按 dyn 继续。

| 构造 | 操作数要求 | 结果类型 |
|---|---|---|
| 字面量 | — | `int` `float` `string` `bool` `null` |
| 名字 | 已定义（5.2） | 环境中的类型 |
| `!a` | a 为 bool 或 dyn | bool |
| `-a` | a 为数或 dyn | int→int，float→float，num→num，dyn→num |
| `a && b`、`a \|\| b` | 两边为 bool 或 dyn | bool |
| `c ? x : y` | c 为 bool 或 dyn | lub(X, Y) |
| `a + b` | 见下表 | 见下表 |
| `a - b`、`a * b` | 两边为数或 dyn | 两边都是 int → int；有一边 float → float；否则 num（dyn 按 num 计） |
| `a / b` | 两边为数或 dyn | float |
| `a % b` | 两边为数或 dyn，且都不是 float | int |
| `<` `<=` `>` `>=` | 都是数，或都是 string，或有一边 dyn | bool |
| `==` `!=` | 可比较（2.6） | bool |
| `a in b` | b 为 list<E> 时 a 与 E 可比较；b 为 map 或记录时 a 为 string 或 dyn；b 为 dyn 时不限；b 为其他类型 → MRL-TYPE | bool |
| `[e1, …, en]` | — | list<lub(T1, …, Tn)>；`[]` 为 list<dyn> |
| `{k: v, …}` | 键为 string 或 dyn；字符串字面量键不得重复（MRL-DUP-KEY） | 键全是字符串字面量且非空 → 封闭记录 `{k1: V1, …}`（字段都必需）；否则 map<lub(各值类型)>；`{}` 为 map<dyn> |
| `e.f`、`e[i]`、`has(e.f)` | 见 5.4 | 见 5.4 |
| `e.all/exists/exists_one(v, p)` | e 为 list<E>（v: E）、map 或记录（v: string）、dyn（v: dyn），其他 → MRL-TYPE；p 为 bool 或 dyn | bool |
| `e.filter(v, p)` | 同上 | list<v 的类型> |
| `e.map(v, t)`、`e.map(v, p, t)` | 同上 | list<t 的类型> |
| `f(…)` | 见 5.6 | 由函数签名给出（6.7） |

`a + b` 的规则：

| A | B | 结果 |
|---|---|---|
| 数 | 数 | 同 `-`、`*` |
| string | string | string |
| list<X> | list<Y> | list<lub(X, Y)> |
| dyn | dyn | dyn |
| dyn | 数（或反过来） | num |
| dyn | string 或 list<X>（或反过来） | string 或 list<X>（假定 dyn 一边与另一边同类） |
| 其他组合 | | MRL-TYPE：`"a" + 1`、`[1] + 1`、`"a" * 2` |

### 5.4 记录字段访问与定义性分析

**字段选择 `e.f`**：

| e 的类型 | 结果 |
|---|---|
| 记录，f 已声明且必需 | f 的类型 |
| 记录，f 已声明但可选 | f 的类型；没有被 `has()` 保护时 MRL-MAYBE-ABSENT（警告） |
| 封闭记录，f 未声明 | MRL-NO-FIELD（静态） |
| 开放记录，f 未声明 | dyn |
| map<T> | T |
| dyn | dyn |
| 其他（int、list、null……） | MRL-TYPE：`[1].a` |

**下标 `e[i]`**：

| e 的类型 | i 的要求 | 结果 |
|---|---|---|
| list<T> | int、num 或 dyn | T |
| map<T> | string 或 dyn | T |
| 记录，i 是字符串字面量 | — | 字段已声明 → 字段类型；封闭且未声明 → MRL-NO-FIELD；开放且未声明 → dyn |
| 记录，i 不是字面量 | string 或 dyn | dyn |
| dyn | 不限 | dyn |
| 其他（包括 string） | | MRL-TYPE：`"abc"[0]` |

**`has(e.f)`**：e 可以是记录、map、dyn 或 null；e 是封闭记录且 f 未声明 → MRL-NO-FIELD；其他类型 → MRL-TYPE。结果为 bool。

**定义性分析**：检查器为每个子表达式维护“已知存在的字段路径”集合 G。路径是名字后接若干 `.字段` 的链，例如 `r.meta.style`；含下标或调用的表达式没有路径。访问可选字段 `p.f` 时，若 `p.f` 不在 G 中 → MRL-MAYBE-ABSENT。G 的传播：

| 构造 | 传播规则 |
|---|---|
| `a && b` | 检查 b 时，G 加上 T(a) |
| `a \|\| b` | 检查 b 时，G 加上 F(a) |
| `c ? x : y` | 检查 x 时加上 T(c)；检查 y 时加上 F(c) |
| 宏 | 谓词与变换继承外层的 G；宏内部的 `has()` 不向外传播 |

其中 T(e)、F(e) 分别是“e 为真时”“e 为假时”已知存在的路径：

| e | T(e) | F(e) |
|---|---|---|
| `has(p.f)` | {p.f} | ∅ |
| `a && b` | T(a) ∪ T(b) | F(a) ∩ F(b) |
| `a \|\| b` | T(a) ∩ T(b) | F(a) ∪ F(b) |
| `!a` | F(a) | T(a) |
| 其他（包括 `has(x.f) == true`） | ∅ | ∅ |

- MRL-MAYBE-ABSENT 是警告：表达式仍然编译成功，但运行期字段不存在时得到 MRL-NO-FIELD。工厂 profile 把它报告为 `W-EXPR`。
- 嵌套的可选字段要逐层保护：`has(r.b) && has(r.b.c) && r.b.c > 0`。`has(r.b.c)` 本身要读取 `r.b`，所以 `has(r.b)` 必须在前。
- 不需要判断存在性时，用 `get(r, "f", 默认值)`（6.7.4），它不读取可能不存在的字段。

设 `r: {a?: int, s?: string, n: int}`：

| 表达式 | 警告 |
|---|---|
| `r.a > 0` | MRL-MAYBE-ABSENT |
| `has(r.a) && r.a > 0` | 无 |
| `!has(r.a) \|\| r.a > 0` | 无 |
| `has(r.a) ? r.a : 0` | 无 |
| `r.a > 0 && has(r.a)` | MRL-MAYBE-ABSENT（保护必须在访问之前） |
| `has(r.a) \|\| r.a > 0` | MRL-MAYBE-ABSENT（右边求值时 a 恰好不存在） |
| `(has(r.a) \|\| has(r.s)) && r.a > 0` | MRL-MAYBE-ABSENT |
| `has(r.a) == true && r.a > 0` | MRL-MAYBE-ABSENT（只认 `has`、`&&`、`\|\|`、`!`） |
| `has(r.a) && [1].all(x, r.a > x)` | 无（宏继承外层的保护） |

### 5.5 期望类型

宿主可以为表达式指定期望类型 D（例如条件必须是 bool）。表达式类型 T 满足 compat(T, D) = no 时 → MRL-TYPE（“表达式类型为 int，这里需要 bool”）；maybe（例如 T 是 dyn；或 T 是 num 而 D 是 int）静态放行，结果的种类由宿主在运行期检查。

### 5.6 函数签名检查

- 参数个数不在函数允许的范围内 → MRL-ARITY：`size()`、`size(1, 2)`。
- 参数类型不符合 6.7 中的“静态规则” → MRL-TYPE。静态类型为 dyn 的参数总是放行。
- 先推导各参数的类型，再查找函数：参数中的错误与“函数未定义”会一起报告。

### 5.7 模板插值

模板的每个插值单独推导类型，必须是 string、int、float、num、bool 或 dyn，否则 MRL-TYPE（详见 7.3）。

### 5.8 正确与错误示范

设 `bean: {facts: {width: int, height: int, vcodec: string}, meta: {language?: string, ...}}`：

```
// ✔ 正确
bean.facts.width > bean.facts.height
has(bean.meta.language) && bean.meta.language == "zh-CN"
(bean.facts.width < bean.facts.height ? bean.facts.width : bean.facts.height) >= 1080

// ✖ 错误
bean.facts.widht > 0             // MRL-NO-FIELD：facts 是封闭记录，拼写错误在编译期发现
bean.facts.vcodec > 3            // MRL-TYPE：string 与 int 不能比较大小
bean.meta.language == "zh-CN"    // 警告 MRL-MAYBE-ABSENT：language 是可选字段
bean.meta.title == "x"           // 合法但不检查拼写：meta 是开放记录，title 的类型为 dyn
```

---

## 6. 求值语义与标准库

### 6.1 总则

- 求值是纯函数：结果只取决于表达式与根名字的值。
- **严格从左到右**、深度优先：二元运算（`&&`、`||` 除外）先求左边、再求右边、再运算；函数的全部参数从左到右求值后才调用，没有惰性参数（`get(m, "k", 1 / 0)` 总是 MRL-DIV-ZERO）；列表从左到右；map 字面量逐项先键后值。
- 任何错误立即终止整个求值，结果就是这个错误（错误码、消息、位置）。没有错误吸收，没有默认值。宿主必须把错误当作“不通过”。
- 运行期检查不依赖静态类型：每个运算都检查操作数的种类。宿主传入的值不符合声明的类型时，得到运行期错误，或按实际值计算的结果——宿主有责任传入符合类型的值。
- 根名字在类型环境中声明了、但宿主没有提供值 → MRL-UNDEFINED（运行期）。

### 6.2 算术

| 运算 | 操作数 | 结果 | 错误 |
|---|---|---|---|
| `a + b` | int、int | int | 溢出 → MRL-OVERFLOW |
| | 数、数（至少一个 float） | float | 不是有限数 → MRL-DOMAIN |
| | string、string | 拼接 | |
| | list、list | 拼接（元素原样，不去重） | |
| `a - b`、`a * b` | int、int → int；数、数 → float | | MRL-OVERFLOW；MRL-DOMAIN |
| `a / b` | 数、数 | float：两边先转换为 float 再除 | b 为 `0` 或 `0.0` → MRL-DIV-ZERO；MRL-DOMAIN |
| `a % b` | int、int | 向下取整的模：结果与除数同号，满足 a = ⌊a/b⌋·b + a % b | b 为 0 → MRL-DIV-ZERO；有 float → MRL-TYPE |
| `-a` | int → int；float → float | | `-(-9223372036854775808)` → MRL-OVERFLOW |
| 其他组合（bool、null、string 与数……） | | | MRL-TYPE |

| 表达式 | 结果 |
|---|---|
| `7 / 2` | `3.5` |
| `6 / 3` | `2.0` |
| `1 + 0.5` | `1.5` |
| `0.1 + 0.2` | `0.30000000000000004` |
| `-7 % 3` | `2` |
| `7 % -3` | `-2` |
| `"a" + "b"` | `"ab"` |
| `[1] + [2.5]` | `[1, 2.5]` |
| `9223372036854775807 + 1` | MRL-OVERFLOW（运行期） |
| `1 / 0` | MRL-DIV-ZERO（运行期） |
| `1.0 / 0.0` | MRL-DIV-ZERO（运行期） |
| `5 % 0` | MRL-DIV-ZERO（运行期） |
| `1e308 * 10.0` | MRL-DOMAIN（运行期） |
| `5.0 % 2` | MRL-TYPE（静态） |
| `"a" + 1` | MRL-TYPE（静态） |

### 6.3 相等、比较与成员判断

- `a == b`、`a != b`：按 2.3 的“相等”。运行期任何两个值都可以比较，从不报错（种类不同即不等）；静态检查见 2.6。
- `<`、`<=`、`>`、`>=`：只对（数, 数）、（string, string）定义，其他组合（包括 bool、null、list、map）→ MRL-TYPE。数按精确的数学值；string 按码点逐个比较，前缀较小。
- `a in b`：b 是 list → 是否有元素与 a 相等；b 是 map → a 必须是 string（否则 MRL-TYPE），判断 a 是否是 b 的键（不看值）；b 是其他种类 → MRL-TYPE。`"a" in "abc"` 不是子串判断，子串用 `contains()`。

| 表达式 | 结果 |
|---|---|
| `1 == 1.0` | `true` |
| `9007199254740993 == 9007199254740992.0` | `false` |
| `9007199254740993 > 9007199254740992.0` | `true` |
| `[1, 2.0] == [1.0, 2]` | `true` |
| `{"a": 1, "b": [2]} == {"b": [2.0], "a": 1}` | `true` |
| `null == 1` | `false` |
| `"B" < "a"` | `true` |
| `"ab" < "abc"` | `true` |
| `"z" < "中"` | `true` |
| `2.0 in [1, 2]` | `true` |
| `"a" in {"a": 1}` | `true` |
| `"1" == 1` | MRL-TYPE（静态） |
| `true < false` | MRL-TYPE（静态） |
| `"a" in "abc"` | MRL-TYPE（静态） |

### 6.4 逻辑运算与条件表达式

| 运算 | 语义 |
|---|---|
| `!a` | a 必须是 bool |
| `a && b` | 求 a（必须是 bool）。a 为 false 时结果为 false，不求 b；否则求 b（必须是 bool），结果为 b |
| `a \|\| b` | 求 a（必须是 bool）。a 为 true 时结果为 true，不求 b；否则求 b（必须是 bool），结果为 b |
| `c ? x : y` | 求 c（必须是 bool），然后只求被选中的分支 |

不是 bool → MRL-TYPE。左边出错就是出错，不会因为右边能决定结果而被吸收。

| 表达式 | 结果 |
|---|---|
| `false && (1 / 0 > 0)` | `false` |
| `true \|\| (1 / 0 > 0)` | `true` |
| `(1 / 0 > 0) && false` | MRL-DIV-ZERO（运行期） |
| `true ? 1 : 1 / 0` | `1` |
| `true \|\| false && false` | `true` |
| `1 && true` | MRL-TYPE（静态） |

### 6.5 字段选择、下标、has() 与字面量

| 运算 | 语义 | 错误 |
|---|---|---|
| `e.f` | e 必须是 map；返回键 f 的值 | 键不存在 → MRL-NO-FIELD；e 不是 map（包括 null）→ MRL-TYPE |
| `e[i]`，e 是 list | i 必须是 int（bool、float 都不行），且 0 ≤ i < size(e) | MRL-TYPE；越界（包括负数）→ MRL-INDEX |
| `e[i]`，e 是 map | i 必须是 string；返回键 i 的值 | MRL-TYPE；键不存在 → MRL-NO-FIELD |
| `e[i]`，e 是其他种类 | | MRL-TYPE |
| `has(e.f)` | 先求 e：map → f 是否是它的键；null → false；其他 → MRL-TYPE | |
| `[…]` | 从左到右求各元素 | |
| `{k: v, …}` | 逐项：求键（必须是 string）、检查重复、再求值 | 键不是 string → MRL-TYPE；重复 → MRL-DUP-KEY |

- `has(m.a)` 在 m 为 `{"a": null}` 时是 true：has 判断键是否存在，与值无关。
- 字段不存在时 `.` 与 `[]` 都是错误。要取默认值，用 `get(m, "k", 默认值)`。

| 表达式 | 结果 |
|---|---|
| `{"a": 1, "b": "x"}.b` | `"x"` |
| `[10, 20][1]` | `20` |
| `[10, 20][2]` | MRL-INDEX（运行期） |
| `[10, 20][-1]` | MRL-INDEX（运行期） |
| `{"a": 1}["b"]` | MRL-NO-FIELD（静态：字面量记录没有字段 b） |
| `{"a": 1, "a": 2}` | MRL-DUP-KEY（静态） |
| `{1: 2}` | MRL-TYPE（静态） |
| `[1, 2][0.5]` | MRL-TYPE（静态） |

键在运行期才确定的重复（`{k: 1, "a": 2}`，k 的值为 `"a"`）是运行期的 MRL-DUP-KEY。

### 6.6 宏

| 宏 | 遍历方式 | 结果 |
|---|---|---|
| `all` | 依次求谓词，遇到第一个 false 停止 | 没有 false 时为 true（空集合为 true） |
| `exists` | 依次求谓词，遇到第一个 true 停止 | 没有 true 时为 false（空集合为 false） |
| `exists_one` | 对**全部**元素求谓词 | 恰好一个 true |
| `filter` | 对全部元素求谓词 | 保持原顺序 |
| `map(v, t)` | 对全部元素求 t | 保持原顺序 |
| `map(v, p, t)` | 每个元素先求 p，为 true 才求 t | 保持原顺序 |

- 先求目标 e，必须是 list 或 map（否则 MRL-TYPE）；map 的元素是键，按码点序。
- 谓词的结果必须是 bool，否则 MRL-TYPE。
- 停止之前出现的错误就是整个宏的错误；停止之后的元素不再求值。

| 表达式 | 结果 |
|---|---|
| `[1, 2, 3].all(x, x > 0)` | `true` |
| `[].all(x, x > 0)` | `true` |
| `[1, 0].all(x, 1 / x > 2)` | `false` |
| `[0, 1].all(x, 1 / x > 2)` | MRL-DIV-ZERO（运行期） |
| `[2, 0].exists(x, 1 / x > 0.1)` | `true` |
| `[1, 2, 2].exists_one(x, x == 1)` | `true` |
| `[1, 0].exists_one(x, 1 / x > 0)` | MRL-DIV-ZERO（运行期） |
| `[3, 1, 2].filter(x, x >= 2)` | `[3, 2]` |
| `[1, 2, 3, 4].map(x, x % 2 == 0, x * x)` | `[4, 16]` |
| `{"b": 1, "a": 2}.map(k, k)` | `["a", "b"]` |
| `[[1, 2], [3]].all(l, l.all(x, x > 0))` | `true` |
| `[1].all(x, x)` | MRL-TYPE（静态） |

### 6.7 标准库

- 所有函数都是全函数：要么返回值，要么报告下表规定的错误码；没有副作用，结果只取决于参数。
- 每个函数都可以用接收者形式调用（4.3）。
- “静态规则”中，dyn 参数总是放行；表中没有写出的运行期错误是：参数种类不对 → MRL-TYPE。
- 宿主可以增加函数（10.4），但不能覆盖标准库中的名字。

#### 6.7.1 转换与类型

| 函数 | 静态规则 | 语义 | 运行期错误 |
|---|---|---|---|
| `size(x)` | string、list、map 或记录 → int | string：码点数（不是字素数）；list：元素数；map：键数 | |
| `int(x)` | 数或 string → int | int 原样；float 向零截断；string 必须**整串**匹配 `[+-]?[0-9]+`（只允许 ASCII 数字，不允许空白、小数点、下划线），按十进制解析，允许前导零 | 字符串不匹配 → MRL-CONVERT；超出 int64 → MRL-OVERFLOW |
| `float(x)` | 数或 string → float | float 原样；int 取最近的 float；string 必须**整串**匹配 `[+-]?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?` | 不匹配（`".5"`、`"1."`、`"inf"`、`"nan"`）→ MRL-CONVERT；结果不是有限数 → MRL-DOMAIN |
| `string(x)` | string、数或 bool → string | string 原样；int 十进制；float 按 2.2 的文本形式；bool 为 `"true"`、`"false"` | null、list、map → MRL-TYPE |
| `type(x)` | 任意 → string | 种类名：`"null"` `"bool"` `"int"` `"float"` `"string"` `"list"` `"map"` | 无 |

| 表达式 | 结果 |
|---|---|
| `size("中文")` | `2` |
| `int(-3.9)` | `-3` |
| `int("-7") + int("+7")` | `0` |
| `int(" 7")` | MRL-CONVERT（运行期） |
| `int("7.0")` | MRL-CONVERT（运行期） |
| `int(1e19)` | MRL-OVERFLOW（运行期） |
| `float("1e3")` | `1000.0` |
| `float(".5")` | MRL-CONVERT（运行期） |
| `float("1e400")` | MRL-DOMAIN（运行期） |
| `string(1.0)` | `"1.0"` |
| `string(1e16)` | `"1e+16"` |
| `string(0.00001)` | `"1e-05"` |
| `string(null)` | MRL-TYPE（静态） |
| `type(1.0)` | `"float"` |
| `int(true)` | MRL-TYPE（静态） |

#### 6.7.2 数值

| 函数 | 静态规则 | 语义 | 运行期错误 |
|---|---|---|---|
| `abs(x)` | 数 → 同类型（dyn → num） | 绝对值 | `abs(-9223372036854775808)` → MRL-OVERFLOW |
| `floor(x)`、`ceil(x)` | 数 → int | 向下、向上取整；int 原样 | 超出 int64 → MRL-OVERFLOW |
| `round(x)` | 数 → int | 四舍五入到最近的整数，**恰好一半时远离零**；按精确数学值判断；int 原样 | MRL-OVERFLOW |
| `min(l)`、`max(l)` | l 为 list<T>，T 为数、string 或 dyn → T | 列表中最小/最大的元素；多个元素相等时取最先出现的 | 列表为空 → MRL-DOMAIN；元素不是数或 string → MRL-TYPE；数与 string 混合 → MRL-TYPE |
| `min(a, b, …)`、`max(a, b, …)` | 两个及以上参数，全是数或全是 string → 各参数类型的 lub | 同上，在参数中比较 | 同上 |
| `sum(l)` | list<int> → int；list<float> → float；list<num>、list<dyn> 或 dyn → num；其他元素类型 → MRL-TYPE | 从 int `0` 开始按**列表顺序**依次相加：两边都是 int 时整数加法，否则 float 加法 | 元素不是数 → MRL-TYPE；MRL-OVERFLOW；MRL-DOMAIN |

- `min`、`max` 只有一个参数时，它必须是 list：`min(5)` → MRL-TYPE（静态）。
- `sum` 的结果种类取决于实际的元素：`sum([]) ⇒ 0`（int）；有 float 元素就从那里开始做 float 加法。

| 表达式 | 结果 |
|---|---|
| `floor(-1.5)` | `-2` |
| `ceil(-1.5)` | `-1` |
| `round(2.5)` | `3` |
| `round(-2.5)` | `-3` |
| `round(0.49999999999999994)` | `0` |
| `min(3, 1, 2)` | `1` |
| `max([1, 2.5])` | `2.5` |
| `min(1, 1.0)` | `1` |
| `min(1.0, 1)` | `1.0` |
| `max("a", "b")` | `"b"` |
| `min([])` | MRL-DOMAIN（运行期） |
| `min([1, "a"])` | MRL-TYPE（运行期） |
| `min(1, "a")` | MRL-TYPE（静态） |
| `sum([1, 2.5])` | `3.5` |
| `sum([0.1, 0.2, 0.3])` | `0.6000000000000001` |
| `sum([9223372036854775807, 1])` | MRL-OVERFLOW（运行期） |

#### 6.7.3 字符串

| 函数 | 静态规则 | 语义 | 运行期错误 |
|---|---|---|---|
| `startsWith(s, p)`、`endsWith(s, p)` | string, string → bool | 前缀、后缀判断；空串是任何串的前缀和后缀 | |
| `contains(s, t)` | string, string → bool | t 是否是 s 的子串；空串总是子串 | |
| `lower(s)`、`upper(s)` | string → string | 只转换 ASCII 字母 A–Z、a–z；其他字符不变 | |
| `trim(s)` | string → string | 去掉两端的空格、`\t`、`\r`、`\n`；其他空白（如全角空格）不去掉 | |
| `split(s, sep)` | string, string → list<string> | 按 sep 切分，保留空段；`"".split(",") ⇒ [""]` | sep 为空串 → MRL-DOMAIN |
| `join(l, sep)` | list<string>（或 list<dyn>、dyn）, string → string | 用 sep 连接；空列表得到 `""` | 元素不是 string → MRL-TYPE |
| `replace(s, old, new)` | string, string, string → string | 从左到右替换 old 的全部不重叠出现 | old 为空串 → MRL-DOMAIN |
| `glob(s, pattern)` | string, string → bool | 通配符匹配**整个** s，见下 | |
| `charsIn(s, allowed)` | string, string → bool | s 的每个码点都出现在 allowed 中；s 为空时为 true | |

`glob` 的模式语法：

| 模式 | 匹配 |
|---|---|
| `*` | 任意个（含 0 个）不是 `/` 的字符 |
| `**` | 任意个任意字符（含 `/`） |
| `?` | 恰好一个不是 `/` 的字符 |
| `\x` | 字面字符 x（例如 `\*`、`\?`；模式末尾单独的 `\` 匹配字面 `\`） |
| 其他字符 | 自身（包括 `[`、`]`、`{`：没有字符类与花括号展开） |

注意 MRL 字符串字面量本身也用 `\` 转义：模式 `a\?c` 在源码中写作 `"a\\?c"`。`glob` 的时间复杂度为 O(|s|·|pattern|)。

| 表达式 | 结果 |
|---|---|
| `"abc".startsWith("ab")` | `true` |
| `"abc".contains("")` | `true` |
| `upper("straße")` | `"STRAßE"` |
| `trim("  a b \n")` | `"a b"` |
| `split("a,b,,c", ",")` | `["a", "b", "", "c"]` |
| `split("abc", "")` | MRL-DOMAIN（运行期） |
| `join(["a", "b"], "-")` | `"a-b"` |
| `join([1], ",")` | MRL-TYPE（静态） |
| `replace("aaaa", "aa", "b")` | `"bb"` |
| `glob("refs/a.mp4", "refs/*")` | `true` |
| `glob("refs/a/b.mp4", "refs/*")` | `false` |
| `glob("refs/a/b.mp4", "refs/**")` | `true` |
| `glob("dir/a.mp4", "**/*.mp4")` | `true` |
| `glob("a.mp4", "**/*.mp4")` | `false` |
| `glob("a?c", "a\\?c")` | `true` |
| `glob("a", "[a]")` | `false` |
| `charsIn("a-b", "ab-")` | `true` |

#### 6.7.4 map

| 函数 | 静态规则 | 语义 |
|---|---|---|
| `keys(m)` | map 或记录 → list<string> | 键，按码点序 |
| `values(m)` | map<T> → list<T>；封闭且非空的记录 → list<各字段类型的 lub>；开放或空的记录 → list<dyn> | 值，按键的码点序 |
| `get(m, k, d)` | map 或记录, string, 任意 → lub(m 的值类型, d 的类型)；m 是记录时，值类型的取法同 `values` | k 是 m 的键时返回 m[k]，否则返回 d。三个参数都先求值 |

| 表达式 | 结果 |
|---|---|
| `keys({"b": 1, "a": 2, "C": 3})` | `["C", "a", "b"]` |
| `values({"b": 1, "a": 2})` | `[2, 1]` |
| `get({"a": 1}, "b", 0)` | `0` |
| `get(1, "a", 0)` | MRL-TYPE（静态） |

#### 6.7.5 列表与集合

| 函数 | 静态规则 | 语义 | 运行期错误 |
|---|---|---|---|
| `sort(l)` | list<T>，T 为数、string 或 dyn → 同类型 | 稳定的升序排序；数按精确数值，string 按码点 | 元素不全是数、也不全是 string → MRL-TYPE |
| `unique(l)` | list<T> → list<T> | 按“相等”去重，保留第一次出现的元素与顺序 | |
| `union(a, b)` | list<A>, list<B> → list<lub(A, B)> | 并集；按“相等”去重，保留第一次出现的元素（先 a 后 b）；结果按规范序排序 | |
| `intersect(a, b)` | 同上 | 交集：a 中与 b 的某个元素相等的元素（去重）；按规范序排序 | |
| `difference(a, b)` | 同上 | 差集：a 中不与 b 的任何元素相等的元素（去重）；按规范序排序 | |
| `isSubset(a, b)` | list, list → bool | a 的每个元素都与 b 的某个元素相等 | |

| 表达式 | 结果 |
|---|---|
| `sort([3, 1.5, 2])` | `[1.5, 2, 3]` |
| `sort(["b", "A", "a"])` | `["A", "a", "b"]` |
| `sort([2.0, 2, 1])` | `[1, 2.0, 2]` |
| `sort([1, "a"])` | MRL-TYPE（运行期） |
| `unique([1, 2, 1, 1.0, "a", "a"])` | `[1, 2, "a"]` |
| `union(["b", "a"], ["c", "a"])` | `["a", "b", "c"]` |
| `union([2.0], [2])` | `[2.0]` |
| `union([1, "a", null, true], [])` | `[null, true, 1, "a"]` |
| `intersect([3, 1, 2], [2, 3, 4])` | `[2, 3]` |
| `difference(["a", "b", "c"], ["b"])` | `["a", "c"]` |
| `isSubset([1], [1.0])` | `true` |

### 6.8 求值预算

- 每访问一个语法树节点计 1 步：字面量、名字、运算、调用、宏本身都算；宏的谓词与变换对每个元素各计一次。步数超过预算 → MRL-COST。默认预算 100,000 步。
- 预算按一次求值计算：一次表达式求值；一次模板渲染（全部插值共享）；规则中的一次表达式求值（8.9）。
- 例：`[1, 2, 3].map(x, x)` 需要 8 步（宏 1、列表 1、元素 3、变换 3）：预算为 7 时 MRL-COST，为 8 时得到 `[1, 2, 3]`。
- 函数内部的工作（`sort`、`glob`、`split` 等）不计步，但都是参数大小的多项式时间。
- 预算限制的是步数，不是值的大小：在嵌套的宏中反复拼接，可以用很少的步数构造出很大的字符串或列表。宿主应当限制输入值的大小，表达式作者不得依赖这种构造。

---

## 7. 模板（MRL-T）

### 7.1 语法

模板 = 普通文本 + 插值 `{{ 表达式 }}`。

- 插值从 `{{` 开始，到第一个“不在字符串字面量内、且括号已配平”的 `}}` 结束：字符串字面量（`"…"`、`'…'`，含反斜杠转义）中的 `}}` 不结束插值；`{ [ (` 与 `} ] )` 计入括号深度。所以 `{{ {"a": 1}.a }}` 与 `{{ "}}" }}` 都是一个完整的插值。
- 两个大括号之间必须是一个完整的表达式（第 4 章）。只有空白（`{{ }}`）→ MRL-SYNTAX；找不到结束的 `}}` → MRL-SYNTAX。插值内部的括号必须配平。
- 插值之外的文本原样输出：单独的 `}}`、`{`、`}` 都是普通文本。
- 没有转义语法。要输出字面的 `{{`，写成插值 `{{ "{{" }}`。
- 每个插值是一个独立的源：3.1 的长度限制、4.7 的嵌套深度分别计算。

### 7.2 怎样判断一个字符串是模板

宿主用 `is_template(s)` 判断：s 包含 `{{` 即是模板。不含 `{{` 的字符串是纯文本，渲染结果就是它本身。

### 7.3 静态规则

全部插值在同一个类型环境下检查（第 5 章，包括定义性分析的警告）。每个插值的类型必须是 string、int、float、num、bool 或 dyn，否则 MRL-TYPE：`{{ null }}`、`{{ [1] }}`、记录类型的插值都是静态错误。

### 7.4 渲染

从左到右求值各插值（共享一个步数预算），把结果转换为文本，与普通文本依次拼接：

| 值的种类 | 文本 |
|---|---|
| string | 原样 |
| int | 十进制，负数带 `-` |
| float | 2.2 的文本形式：`1.0`、`1e+16`、`0.3333333333333333` |
| bool | `true`、`false` |
| null、list、map | MRL-TYPE（运行期；只可能来自 dyn） |

任何一个插值出错，整个渲染失败。

### 7.5 示例

| 模板 | 环境 | 结果 |
|---|---|---|
| `a{{ x }}b` | `x = "-"` | `a-b` |
| `v={{ x }}` | `x = 1.5` | `v=1.5` |
| `{{ 1 / 3 }}` | | `0.3333333333333333` |
| `{{ "}}" }}` | | `}}` |
| `{{ "{{" }}` | | `{{` |
| `a }} b` | | `a }} b` |
| `{{ inputs.artifact.id }}:s3://{{ params.bucket }}/{{ params.prefix }}` | `inputs = {artifact: {id: "sha256:ab12"}}`，`params = {bucket: "media-archive", prefix: "explainers/r-0042/"}` | `sha256:ab12:s3://media-archive/explainers/r-0042/` |
| `{{ x }}` | `x = [1]`（dyn） | MRL-TYPE（运行期） |
| `{{ }}` | | MRL-SYNTAX（静态） |
| `a {{ x` | `x = 1` | MRL-SYNTAX（静态） |
| `{{ nope }}` | | MRL-UNDEFINED（静态） |

---

## 8. 规则（MRL-R）

### 8.1 文法与程序结构

```
Module     = "module" Name { "." Name } END { Statement } ;
Statement  = { Annotation } RelDecl | Clause ;
Annotation = "@" "input" | "@" "open" | "@" "doc" "(" STRING ")" ;
RelDecl    = "rel" RelName "(" [ Column { "," Column } ] ")" END ;
Column     = Name ":" ColType ;
ColType    = "bool" | "int" | "float" | "string" | "any"
           | "list" "<" ColType ">" | "map" "<" ColType ">" ;
Clause     = Head [ ":-" Literal { "," Literal } ] END ;
Head       = RelName "(" [ HeadTerm { "," HeadTerm } ] ")" ;
HeadTerm   = Expr | "count" "(" ")" | ( "sum" | "min" | "max" | "set" ) "(" Expr ")" ;
Literal    = Var "=" Expr | Expr ;
END        = 语句结束符 "."（3.5） ;
```

- 一个源文件就是一个模块，必须以 `module 名字.` 开头；名字可以用 `.` 分段：`module kernel.types.`。
- **程序** = 宿主一次给出的全部模块。模块名在程序内唯一（MRL-DUP）。宿主用 `Program.from_sources` 装配并检查程序，用 `Program.evaluate(facts)` 求值，用 `Model.rows(name)` 读取结果（10.3）。
- 注解只能写在 `rel` 前，否则 MRL-DECL；未知注解（如 `@output`）→ MRL-DECL。
- 关系名不能是关键字或保留字：`rel while(a: int).`、`if(1).` → MRL-SYNTAX。
- 规则体的每个 `Literal` 先按表达式解析，再按 8.4 分类为原子、否定原子、赋值或守卫。
- 没有规则体的子句是**事实**。事实的头部可以是表达式（`p(1 + 2).`），但不能含变量（8.5）。

### 8.2 关系声明与列类型

```
@doc("工位角色") @input rel role(s: string, r: string).
rel steps(line: string, n: int).
@open rel diag(code: string, subject: string, message: string).
```

| 列类型 | 接受的值 |
|---|---|
| `bool` `int` `string` | 同名种类 |
| `float` | float；int 会被规范化为 float |
| `any` | 任意值，原样保存 |
| `list<T>`、`map<T>` | list、map，元素按 T 逐个规范化 |

| 注解 | 含义 |
|---|---|
| `@input` | 由宿主提供事实；任何模块都不能为它写规则或事实 |
| `@open` | 只写关系：任何模块都可以为它写规则，但任何规则体都不能读取它 |
| `@doc("…")` | 文档字符串，不影响语义 |

- `num`、`dyn`、`null` 不是列类型 → MRL-DECL。
- 列名在关系内唯一（MRL-DECL）。关系可以没有列：`rel nonempty().`，它至多有一个元组 `()`。
- 关系名在整个程序内唯一（MRL-DUP），并且不能与函数同名：`rel size(x: int).` → MRL-DUP。
- `@input` 与 `@open` 不能同时使用（MRL-DECL）。

**规范化**：值放进列时——宿主事实、规则推导出的元组、原子匹配时的查找值——按列类型规范化：int 放进 float 列变为 float（list、map 内部递归处理），其他种类不符 → MRL-TYPE。规范化之后按身份（2.3）存储和比较，所以 float 列中的 `1` 与 `1.0` 是同一个元组，`any` 列中则是两个。

### 8.3 模块与归属

| 规则 | 违反时 |
|---|---|
| 规则（包括事实）只能写在声明该关系的模块中 | MRL-OWNERSHIP |
| `@input` 关系不能有规则或事实（只能由宿主提供） | MRL-OWNERSHIP |
| `@open` 关系可以在任何模块中写规则 | — |
| 任何规则体都不能读取 `@open` 关系（正向或否定） | MRL-OWNERSHIP |
| 读取其他模块的非 `@open` 关系 | 允许 |

**为什么这保证单调性**：设程序 P 已经通过检查，再追加一个模块 M。M 只能（1）为自己声明的新关系写规则，（2）为 `@open` 关系写规则。P 中的关系既不读取 M 的新关系（编写 P 时它们还不存在），也不读取任何 `@open` 关系；M 又不能为 P 的关系写规则。所以 P 中每个非 `@open` 关系的内容在追加 M 前后完全相同，`@open` 关系只会多出行。追加 M 的结果只可能是三种之一：静态错误（如 MRL-DUP、MRL-OWNERSHIP）；运行期错误（整个求值失败）；`@open` 关系（如 `diag`）多出一些行。工厂用这一点保证“宪法只会让检查更严”（factory-profile.md 1.2）。

```
module kernel.
@input rel station(s: string, role: string).
@open rel diag(code: string, subject: string).
diag("E-A", S) :- station(S, "bad").
```

```
module constitution.
diag("E-B", S) :- station(S, "sink").      // ✔ 向 @open 关系追加
```

```
module bad.
station("x", "sink").                      // ✖ MRL-OWNERSHIP：@input 关系只能由宿主提供
rel n(c: int).
n(count()) :- diag(_, _).                  // ✖ MRL-OWNERSHIP：不能读取 @open 关系
```

### 8.4 规则：头部与规则体

**变量**：以大写字母或 `_` 开头的名字（`X`、`Line`、`_tmp`）。单独的 `_` 是匿名变量：每次出现都是一个新的、互不相关的变量。小写开头的名字不是变量，出现在规则中 → MRL-UNDEFINED（`p(x) :- e(x).`）。

**规则体中的文字**（从左到右）：

| 写法 | 种类 | 条件 |
|---|---|---|
| `r(t1, …, tn)` | 正向原子 | r 是已声明的关系，并且以全局调用形式书写 |
| `!r(t1, …, tn)` | 否定原子 | 同上，前面是 `!` |
| `X = e` | 赋值 | 左边是一个变量 |
| 其他表达式 | 守卫 | 类型必须是 bool 或 dyn |

- 只有上面两种写法中的 `r(...)` 是原子。写在复合表达式里（`r(X) || q(X)`）或写成接收者形式（`X.r()`）时，r 被当作函数 → MRL-UNDEFINED。规则体不支持析取：写成两条规则。
- `f(...)` 中的 f 既不是关系也不是函数 → MRL-UNDEFINED，例如拼错的关系名 `egde(X, _)`。
- 原子的参数个数必须等于关系的列数（MRL-ARITY）。

**原子的参数**：

| 参数 | 正向原子 | 否定原子 |
|---|---|---|
| 未绑定的变量 | 绑定为该列的值；变量的类型为列类型 | MRL-UNSAFE |
| 已绑定的变量 | 按值匹配；变量类型与列类型的 compat 为 no → MRL-TYPE | 同左 |
| `_` | 匹配任意值 | 匹配任意值，即“不存在任何这样的元组” |
| 字面量常量 | 按值匹配；compat 为 no → MRL-TYPE | 同左 |
| 其他表达式 | 用已绑定的变量求值后匹配；表达式类型与列类型的 compat 为 no → MRL-TYPE | 同左 |

- 匹配时，查找值先按列类型规范化（8.2），再按身份比较：int 变量可以匹配 float 列中的 `1.0`。查找值无法规范化（例如 string 放进 int 列）时视为不匹配：正向原子没有结果，否定原子成立。
- 原子中首次出现的变量，在整个原子之后才算绑定：同一原子的表达式参数不能使用它（`e(X, X + 1)` → MRL-UNSAFE）。
- 同一原子中一个变量出现多次（`e(X, X)`）表示这些列的值相同。两列的类型不同（例如 int 与 float）时不要这样写，改用赋值比较：`e(X, Y), X = Y`。

**赋值 `X = e`**：

- X 未绑定：求 e，把结果绑定给 X；X 的类型为 e 的静态类型。
- X 已绑定：相等测试（2.3 的相等：已绑定 `1` 时 `X = 1.0` 成立）；静态要求两边可比较，否则 MRL-TYPE。
- `_ = e` → MRL-UNSAFE。

**守卫**：用当前绑定求值。静态类型与 bool 的 compat 不得为 no（MRL-TYPE）；运行期结果不是 bool → MRL-TYPE。结果为 false 时丢弃这个绑定。

**头部**：每一项是变量、字面量常量、表达式或聚合（8.8）。项的类型必须能放进对应的列（compat 不为 no，否则 MRL-TYPE）：例如 `p(X / 2)` 放进 int 列是静态错误（`/` 的结果是 float）。头部不能有 `_`（MRL-UNSAFE），头部的变量必须在规则体中绑定。

**含义**：对规则体的每个满足的绑定，求头部各项的值，按列类型规范化后加入头部关系。关系是集合：重复的元组只保留一个；多条规则可以定义同一个关系（它们的结果取并集）。

```
module t.
@input rel parent(p: string, c: string).
@input rel budget(s: string, usd: float).
rel grandparent(g: string, c: string).
rel doubled(s: string, usd: float).
rel key(k: string).
grandparent(G, C) :- parent(G, P), parent(P, C).
doubled(S, D) :- budget(S, B), D = B * 2, D > 5.
key(P + "/" + C) :- parent(P, C).
```

事实 `{parent: [[a, b], [b, c]], budget: [[a, 2], [b, 3.5]]}` 下：`grandparent = {("a", "c")}`，`doubled = {("b", 7.0)}`，`key = {("a/b"), ("b/c")}`。

### 8.5 安全性

规则体严格从左到右检查和求值。规则是安全的，当且仅当每个变量都在使用之前被绑定：

| 情形 | 违反时 |
|---|---|
| 守卫、赋值右边、原子的表达式参数中的变量，必须已被前面的文字绑定 | MRL-UNSAFE |
| 否定原子中的变量（`_` 除外）必须已被绑定 | MRL-UNSAFE |
| 头部的变量必须在规则体中绑定 | MRL-UNSAFE |
| 事实（没有规则体）不能含变量 | MRL-UNSAFE |
| `_` 只能直接作为原子的参数：不能出现在表达式、头部中，也不能被赋值 | MRL-UNSAFE |

能绑定变量的只有两种文字：正向原子（参数中未绑定的变量）与赋值（左边未绑定的变量）。

```
p(X) :- e(X), X > 1.          // ✔
p(X) :- X > 1, e(X).          // ✖ MRL-UNSAFE：守卫在绑定之前
p(X) :- !e(X).                // ✖ MRL-UNSAFE：否定不能绑定变量
p(X) :- X = Y + 1, e(Y).      // ✖ MRL-UNSAFE：Y 在使用之后才绑定
p(X, Y) :- e(X).              // ✖ MRL-UNSAFE：Y 没有绑定
p(X) :- e(X), X > _.          // ✖ MRL-UNSAFE：_ 不能用在表达式中
```

### 8.6 递归与求值

- **依赖图**：对每条规则，规则体中每个原子（正向或否定）的关系指向头部关系。
- 依赖图的每个强连通分量（SCC）是一**层**；各层按拓扑序从下往上求值，求一层时它依赖的层都已完整。
- 每层的求值：先用该层的全部规则求一遍；然后对**递归规则**（规则体中有正向原子属于本层）做半朴素迭代——每一轮让一个本层原子读取上一轮新增的元组，其余原子读取完整的关系，直到某一轮不再产生新元组。
- 集合语义、分层、递归中不构造新值，三者共同保证结果唯一且有限，与规则、事实、模块的书写顺序无关（唯一的例外见 8.8 中 `min`、`max` 的说明）。
- **元组上限**：每插入一个新元组时检查全部关系的元组总数（包括宿主事实），超过 5,000,000 → MRL-COST。

```
module t.
@input rel edge(a: string, b: string).
rel path(a: string, b: string).
path(A, B) :- edge(A, B).
path(A, C) :- path(A, B), edge(B, C).
```

事实 `{edge: [[a, b], [b, c], [c, d]]}` 下：`path = {(a,b), (a,c), (a,d), (b,c), (b,d), (c,d)}`。

### 8.7 分层与终止性

**分层（MRL-STRATIFY）**：否定原子的关系，以及聚合规则的规则体中任何原子的关系，不能与头部关系处于同一个 SCC（不能出现在递归环上，包括自环）。

```
p(X) :- e(X), !p(X).                  // ✖ MRL-STRATIFY：否定在自环上
p(X) :- e(X), !q(X).                  // ✖ 与下一条一起：p、q 互相否定
q(X) :- e(X), !p(X).
unreachable(N) :- node(N), !reach(N). // ✔ reach 在更低的层
```

**值构造限制（MRL-INVENTION）**：递归规则（规则体中至少有一个正向原子与头部在同一 SCC）不能构造新值：

- 头部的每一项只能是变量或字面量常量，不能是表达式或聚合；
- 赋值的右边只能是变量或字面量常量；
- 守卫与原子参数中的表达式不受限制——它们只过滤或查找，不产生新值。

字面量常量包括折叠后的负整数（4.6），但不包括 `-1.5`：它是一元表达式。需要负的浮点常量时，把它放进一个非递归的关系再读取。

非递归规则可以任意构造新值。限制的理由：递归中构造新值可能无限地产生新元组。

```
n(Y) :- n(X), Y = X + 1.              // ✖ MRL-INVENTION：每一轮都产生新的数
s(X + "!") :- s(X).                   // ✖ MRL-INVENTION
r(A, C) :- r(A, B), e(B, X), C = X.   // ✔ 只是复制已有的值
m(Y) :- n(X), Y = X * 10.             // ✔ 若 m 不在 n 的递归环上
```

### 8.8 聚合

- 头部顶层的 `count()`、`sum(e)`、`min(e)`、`max(e)`、`set(e)` 是聚合；头部中其余的项是**分组键**。一个头部可以有多个聚合。
- `count` 不带参数；其他聚合恰好一个参数，否则 MRL-ARITY。头部顶层的 `min(X, Y)` 也是 MRL-ARITY：要用标准库的两参数 `min`，先赋值 `Z = min(X, Y)`。
- 只有头部的顶层项是聚合。嵌套在表达式中（`count() + 1`）或写在规则体中时，`sum`、`min`、`max` 是标准库函数，`count`、`set` 是未定义的函数（MRL-UNDEFINED）。

**语义**：

1. 求出规则体的全部满足的绑定。两个绑定当且仅当所有变量——包括每个 `_` 对应的匿名变量——的值身份相同时，才视为同一个，只计一次。
2. 按分组键的值（身份）分组。
3. 每组对组内每个绑定求聚合的参数，得到一个值的多重集合，再按下表计算：

| 聚合 | 静态要求（否则 MRL-TYPE） | 结果类型 | 计算 |
|---|---|---|---|
| `count()` | — | int | 组内的绑定数 |
| `sum(e)` | e 为数或 dyn | e 为 int 或 float 时同 e，否则 num | 按规范序（2.3）依次相加，从 int `0` 开始：两边都是 int 时整数加法（MRL-OVERFLOW），否则 float 加法 |
| `min(e)`、`max(e)` | e 为数、string 或 dyn | e 的类型 | 最小、最大值（有序比较） |
| `set(e)` | — | list<e 的类型> | 按“相等”去重（数值相等时保留 int）、按规范序排列的 list |

- 聚合的结果还要能放进对应的列（compat 不为 no，否则 MRL-TYPE）。
- 运行期：`sum` 的值不是数、`min`/`max` 的值不是数或 string、`min`/`max` 中数与 string 混合 → MRL-TYPE。
- 组内同时有数值相等的 int 与 float（只可能来自 `any` 列或 num 类型的表达式）时，`min`、`max` 选中其中哪一个不作规定。需要确定的结果时，先用 `float()` 统一种类。
- **匿名变量也会使绑定不同**：`n(count()) :- e(X, _).` 数的是 e 的元组个数，不是不同 X 的个数。要数不同的值，先投影到辅助关系，或用 `set` 再取 `size`（见下例）。
- **空输入**：头部全是聚合（没有分组键）且没有任何绑定时，如果全部聚合都是 `count`、`sum`、`set`，产生一个元组（依次为 `0`、`0`、`[]`）；只要有一个 `min` 或 `max`，就不产生元组。有分组键时（包括常量分组键），没有绑定就没有元组。
- 聚合规则不能在递归环上（8.7）。

```
module t.
@input rel step(line: string, s: string, usd: float).
rel steps(line: string, n: int).
rel spend(line: string, usd: float).
rel rows(n: int).
rel line(l: string).
rel lines(n: int).
rel vals(vs: list<float>).
rel distinct_costs(n: int).
steps(L, count()) :- step(L, _, _).
spend(L, sum(U)) :- step(L, _, U).
rows(count()) :- step(L, _, _).          // 数的是绑定（即元组），不是不同的 L
line(L) :- step(L, _, _).
lines(count()) :- line(_).               // 先投影，再数不同的 L
vals(set(U)) :- step(_, _, U).
distinct_costs(N) :- vals(Vs), N = size(Vs).
```

事实 `{step: [[a, s1, 5.0], [a, s2, 3.0], [a, s3, 3.0], [b, s1, 1]]}` 下：

| 关系 | 结果 | 说明 |
|---|---|---|
| `steps` | `("a", 3)`、`("b", 1)` | |
| `spend` | `("a", 11.0)`、`("b", 1.0)` | 两个 3.0 来自不同的绑定（s 不同），都计入；b 的 `1` 进入 float 列后是 `1.0` |
| `rows` | `(4)` | |
| `lines` | `(2)` | |
| `vals` | `([1.0, 3.0, 5.0])` | 去重并按规范序排列 |
| `distinct_costs` | `(3)` | |

### 8.9 运行时错误

- 规则求值中的任何运行期错误——表达式出错、头部的值不能放进列、MRL-COST——都立即终止整个求值，不产生部分结果。宿主必须把它当作检查失败。
- 规则体从左到右求值，所以守卫的位置决定它在哪些绑定上被求值：

```
p(Y) :- e(X), nonzero(X), Y = 1 / X.   // ✔ 先过滤，0 不会到达除法
p(X) :- e(X), 1 / X > 0, nonzero(X).   // ✖ e 中有 0 时 MRL-DIV-ZERO：守卫在过滤之前
```

- 读取可能不存在的 map 字段之前先用 `has()`：`bean(I, F), has(F.mean_volume_db), F.mean_volume_db > -50.0`。
- 每次表达式求值（每个绑定上的每个守卫、赋值、头部表达式、原子的表达式参数、聚合参数）都有独立的步数预算（默认 100,000，6.8）。

### 8.10 警告：MRL-UNUSED

对每个既不是 `@input` 也不是 `@open` 的关系：

- 没有任何规则或事实，但在规则体中被使用 → MRL-UNUSED（它永远为空）；
- 既没有规则或事实，也没有被使用 → MRL-UNUSED。

有规则但没有被使用的关系是程序的输出，不警告。`@input`、`@open` 关系从不产生这个警告。

### 8.11 正确与错误示范

```
// ✔ 正确：先用 has() 再读取可能不存在的字段
lang(I, L) :- b(I, M), has(M.language), L = M.language.

// ✖ 错误：小写的 x 不是变量
p(x) :- e(x).                          // MRL-UNDEFINED

// ✖ 错误：规则体不支持析取
p(X) :- e(X), (f(X) || X > 1).         // MRL-UNDEFINED：f 被当作函数
// ✔ 正确：写成两条规则
p(X) :- e(X), f(X).
p(X) :- e(X), X > 1.

// ✖ 错误：头部顶层的 min 是聚合，只接受一个参数
s(min(X, Y)) :- e(X, Y).               // MRL-ARITY
// ✔ 正确
s(Z) :- e(X, Y), Z = min(X, Y).

// ✖ 错误：float 列的值放进 int 列
i(V) :- x(V).                          // x(v: float)、i(v: int)：MRL-TYPE（静态）
```

---

## 9. 错误码

阶段：**静态** = 编译期（解析、静态检查、规则程序的装配），**运行期** = 求值期。“警告”不使编译失败，其余都是错误。

| 错误码 | 阶段 | 含义 | 典型触发 |
|---|---|---|---|
| MRL-SYNTAX | 静态 | 词法或语法错误 | 非法字符、前导零、未结束的字符串、未知转义、关键字作名字、`a < b < c`、缺少语句结束符、`: -`、模板插值没有结束或为空、float 字面量溢出 |
| MRL-LIMIT | 静态 | 超出实现限制 | 源超过 2^20 个字符；嵌套深度超过 64 |
| MRL-UNDEFINED | 静态（也见于运行期） | 未定义的名字、函数或关系 | 拼错的根名字或函数；规则中的小写名字；未声明的关系；宿主声明了根名字却没有提供值（运行期） |
| MRL-ARITY | 静态 | 参数个数或关系元数不符 | `size()`；原子或头部的项数与列数不同；`count(X)`；头部的 `min(X, Y)` |
| MRL-MACRO | 静态 | 宏或 `has()` 的用法不正确 | `[1].all(x)`；宏变量不是标识符；`has(m)`、`has(m["k"])` |
| MRL-SHADOW | 静态 | 宏变量遮蔽已有的名字 | `x.all(x, …)`；与外层宏变量或已绑定的规则变量同名 |
| MRL-DECL | 静态 | 关系声明不合法 | 列类型为 `num`、`dyn`、`null`；列名重复；`@input @open`；未知注解；注解不在 `rel` 前 |
| MRL-DUP | 静态 | 重复定义 | 关系重复声明；模块名重复；关系与函数同名 |
| MRL-OWNERSHIP | 静态 | 违反关系归属 | 为其他模块的关系写规则；为 `@input` 关系写规则或事实；在规则体中读取 `@open` 关系 |
| MRL-UNSAFE | 静态 | 规则不安全 | 变量在绑定前使用；否定原子中有未绑定的变量；头部变量未绑定；事实含变量；`_` 用在表达式、头部或被赋值 |
| MRL-STRATIFY | 静态 | 无法分层 | 否定或聚合出现在递归环上（包括自环） |
| MRL-INVENTION | 静态 | 递归规则中构造了新值 | 递归规则的头部有表达式或聚合；赋值右边不是变量或字面量 |
| MRL-MAYBE-ABSENT | 静态（**警告**） | 访问可选字段但没有用 `has()` 保护 | `r.opt > 0` |
| MRL-UNUSED | 静态（**警告**） | 关系永远为空，或既没有定义也没有使用 | 声明了但没有规则或事实的关系 |
| MRL-TYPE | 静态与运行期 | 类型错误 | 运算符或函数的操作数类型不符；`%` 用于 float；值不能放进列；守卫不是 bool；模板插值是 null、list 或 map |
| MRL-NO-FIELD | 静态与运行期 | 字段不存在 | 封闭记录没有该字段（静态）；map 中没有该键（运行期，`.` 与 `[]`） |
| MRL-OVERFLOW | 静态与运行期 | 整数超出 int64 | 字面量过大（静态）；整数运算、`int()`、`floor/ceil/round`、`abs`、`sum` 溢出（运行期） |
| MRL-INDEX | 运行期 | 列表下标越界 | `l[2]`（长度 2）、`l[-1]` |
| MRL-DIV-ZERO | 运行期 | 除以零 | `x / 0`、`x / 0.0`、`x % 0` |
| MRL-DOMAIN | 运行期 | 结果不是有限的 float，或参数超出函数的定义域 | float 溢出；`min([])`；`split(s, "")`；`replace(s, "", t)`；`float("1e400")` |
| MRL-CONVERT | 运行期 | 字符串无法转换为数 | `int(" 7")`、`int("7.0")`、`float(".5")` |
| MRL-COST | 运行期 | 超出资源上限 | 求值步数超过预算；规则求值的元组总数超过 5,000,000 |
| MRL-DUP-KEY | 静态与运行期 | map 字面量中出现重复的键 | `{"a": 1, "a": 2}`（静态）；计算出的键重复（运行期） |
| MRL-FACT | 运行期 | 宿主提供的事实不符合关系声明 | 关系未声明或不是 `@input`；列数不对；值不能规范化为列类型（包括超出 int64、非有限 float） |

`mrl explain MRL-XXX` 输出一个错误码的阶段与说明。

---

## 10. 实现限制与宿主接口

### 10.1 实现限制

| 限制 | 值 | 超出时 |
|---|---|---|
| 单个源的长度（表达式、规则文件、模板中的一个插值） | 1,048,576（2^20）个字符 | MRL-LIMIT（静态） |
| 表达式嵌套深度（4.7） | 64 | MRL-LIMIT（静态） |
| 一次求值的步数预算（6.8） | 默认 100,000，宿主可以调整 | MRL-COST（运行期） |
| 规则求值的元组总数（8.6） | 5,000,000（含宿主事实） | MRL-COST（运行期） |
| int | −2^63 … 2^63−1 | MRL-OVERFLOW |
| float | 有限 binary64 | MRL-DOMAIN |

### 10.2 表达式与模板 API（`mrl.expr`）

| 接口 | 说明 |
|---|---|
| `compile_expr(text, env=None, functions=None, source="", expect=None) -> CompiledExpr` | 解析并静态检查。`env`：根名字 → `Type`；`functions`：函数表，默认为标准库；`source`：诊断中的源名；`expect`：期望类型（5.5）。失败时抛出 `StaticError` |
| `CompiledExpr.type`、`.warnings`、`.paths` | 静态类型；警告列表（`Diagnostic`）；访问过的字段路径集合 |
| `CompiledExpr.evaluate(values, budget=100000, convert=True)` | 求值。`values`：根名字 → 值；`convert=True` 时先把 Python/JSON 值转换为 MRL 值（2.1）。失败时抛出 `EvalError` |
| `compile_template(text, env=None, functions=None, source="") -> CompiledTemplate` | 解析并检查模板 |
| `CompiledTemplate.render(values, budget=100000, convert=True) -> str` | 渲染 |
| `evaluate(text, values=None, env=None)` | 便捷函数：没有给出类型的根名字按 dyn 处理 |
| `types.parse_type(s)` | 解析类型字符串（2.7），失败时抛出 `ValueError` |
| `parser.is_template(s)` | s 是否包含 `{{`（7.2） |

**错误对象**（`mrl.errors`）：

- `StaticError`：`.diagnostics` 是全部诊断（`Diagnostic(code, message, span, source)`，含警告）；`.code`、`.message` 是第一个 error。
- `EvalError`：`.code`、`.message`、`.span`（行、列）、`.source`。
- `Diagnostic.severity` 为 `"warning"`（MRL-MAYBE-ABSENT、MRL-UNUSED）或 `"error"`；`Diagnostic.to_json()` 给出 `{code, severity, message, source, line, col}`。

宿主约定：

- 编译一次、多次求值。表达式与环境（根名字与类型）一起构成编译单元；改变环境必须重新编译。
- 宿主必须按声明的类型提供全部根名字的值（6.1）。
- `EvalError` 一律视为“不通过”，不得改为默认值或忽略。

### 10.3 规则 API（`mrl.rules`）

| 接口 | 说明 |
|---|---|
| `Program.from_sources([(源名, 文本), …], functions=None) -> Program` | 解析全部模块并做全部静态检查（声明、归属、安全性、类型、分层）。失败时抛出 `StaticError`，含全部诊断 |
| `Program.relations` | 关系名 → 声明信息（列名与类型、`input`、`open`、所属模块、`doc`） |
| `Program.warnings` | 警告（MRL-UNUSED 等） |
| `Program.strata` | 求值的层（关系名列表的列表，从下到上） |
| `Program.evaluate(facts=None, budget=100000) -> Model` | 求值。`facts`：`{关系名: [[列值, …], …]}`，只能给 `@input` 关系，每一行必须是一个列表；`budget` 是每次表达式求值的步数预算。失败时抛出 `EvalError`（事实不合法时为 MRL-FACT） |
| `Model.rows(name)` | 关系的全部元组，按规范序（逐列）排序；未声明的关系抛出 `KeyError` |
| `Model.contains(name, row)` | 某个元组是否存在（先按列类型规范化） |
| `load_program(paths, functions=None)` | 从文件读取模块 |

### 10.4 扩展函数

宿主用 `stdlib.make_table(extra)` 在标准库之上增加函数；与标准库同名 → `ValueError`。每个函数是 `Function(name, min_args, max_args, sig, impl, doc)`：

- `min_args`、`max_args`：参数个数范围，`max_args=None` 表示不限。
- `sig(arg_types) -> Type`：静态规则；参数类型不对时抛出 `SigError(code, message)`（通常是 MRL-TYPE）。dyn 参数应当放行。
- `impl(*args) -> 值`：必须是纯函数与全函数，只接收与返回 MRL 值，只以 `EvalError("MRL-…")` 报告错误；不得读取时间、随机数、环境或 I/O，否则破坏确定性。
- 规则程序使用同一个函数表：关系不能与其中的任何函数同名（MRL-DUP）。工厂 profile 的扩展函数（`resolve`、`resolveVia`）见 factory-profile.md 第 5 章。

### 10.5 确定性保证

符合本规范的实现必须保证：

- 表达式与模板：同样的源、环境与值，得到同样的结果或同样的错误码。
- 规则：同样的模块集合与事实，得到同样的模型；与事实的顺序、规则与模块的书写顺序无关。
- 输出：`Model.rows` 按规范序排序；`set` 聚合、`sum` 聚合与集合函数按规范序计算。
- 唯一的例外是 8.8 中 `min`、`max` 在数值相等的 int 与 float 之间的选择。

### 10.6 命令行

| 命令 | 用途 |
|---|---|
| `mrl eval EXPR [--types M] [--env M] [--budget N] [--json]` | 求值一个表达式；M 是 YAML/JSON 映射，或 `@文件路径` |
| `mrl template TEXT [--types M] [--env M] [--json]` | 渲染一个模板 |
| `mrl check FILE.mrl… [--json]` | 静态检查规则模块 |
| `mrl run FILE.mrl… [--facts M] [--rel NAME]… [--json]` | 求值规则模块，输出非 `@input` 关系（或 `--rel` 指定的关系） |
| `mrl conformance [PATH…] [--json] [-q]` | 运行一致性用例（第 11 章） |
| `mrl explain CODE` | 解释错误码 |

退出码：0 通过；1 检查未通过（例如有失败的用例）；2 用法错误或输入无法读取；3 MRL 静态错误；4 MRL 运行期错误；5 内部错误。

```
$ uv run mrl eval 'x * 0.9' --types '{x: int}' --env '{x: 60}'
54.0
$ uv run mrl eval '1 / 0'
运行时错误 MRL-DIV-ZERO：除以零
$ uv run mrl template 'explainers/{{ run.id }}/' --env '{run: {id: r-0042}}'
explainers/r-0042/
```

规则文件 `path.mrl` 为 8.6 的例子，事实文件 `facts.yaml` 为 `{edge: [[a, b], [b, c]]}` 时：

```
$ uv run mrl check path.mrl
✔ 2 个关系，2 条规则，2 层
$ uv run mrl run path.mrl --facts @facts.yaml
path("a", "b").
path("a", "c").
path("b", "c").
```

---

## 11. 一致性用例格式

### 11.1 文件与套件

- 一致性用例放在 `spec/mrl/conformance/` 下，每个 `.yaml`（或 `.yml`）文件是一个套件，目录递归扫描，按路径排序。
- 文件按 YAML 1.2 core 规则读取：布尔值只有 `true`、`false`（及首字母大写、全大写的写法），`yes`、`on` 是字符串；整数只有十进制（`0x10` 是字符串）；不解析日期；`1.0` 是 float、`1` 是 int；重复的键是错误。
- 套件的顶层字段：

| 字段 | 必需 | 说明 |
|---|---|---|
| `suite` | 是 | 套件 id，全局唯一（例如 `expr.arith`） |
| `kind` | 是 | `expr`、`template`、`rules` 或 `factory` |
| `description` | 否 | 说明，引用本规范的章节；不参与判定 |
| `cases` | 是 | 用例列表；每个用例的 `id` 在套件内唯一 |

- 文件无法读取、缺少 `suite` 或 `cases`、`kind` 未知、套件 id 重复、用例 id 重复，都记为失败。

### 11.2 expr 与 template 用例

```yaml
suite: expr.arith
kind: expr
cases:
  - id: mixed-mul
    expr: 'x * 0.9'             # kind: template 时写 template: '…'
    types: {x: int}             # 可选：根名字 → 类型字符串（2.7）
    env: {x: 60}                # 可选：根名字 → 值；没有写类型的名字按 dyn
    budget: 100000              # 可选：步数预算，默认 100000
    expect: {value: 54.0, type: float}
```

| `expect` 字段 | 含义 |
|---|---|
| `value` | 求值或渲染的结果，按**身份**比较：`2` 与 `2.0` 不同；map 的键序无关 |
| `type` | 静态类型（类型字符串，按结构比较）；只用于 kind: expr |
| `warnings` | 警告码的多重集合必须恰好相等；`[]` 表示必须没有警告；不写则不检查 |
| `static_error` | 编译必须失败，且该码出现在 error 诊断中 |
| `error` | 编译必须成功，求值必须以该码失败 |

判定顺序：编译 → 若失败，当且仅当期望了 `static_error` 且码匹配时通过 → 检查 `warnings` → 检查 `type` → 若既没有 `value` 也没有 `error`，到此通过（只编译的用例） → 求值，比较 `error` 或 `value`。

### 11.3 rules 用例

```yaml
suite: rules.basics
kind: rules
cases:
  - id: join
    program: |                  # 单个模块（源名为 <id>.mrl）；或用 modules
      module t.
      @input rel parent(p: string, c: string).
      rel grandparent(g: string, c: string).
      grandparent(G, C) :- parent(G, P), parent(P, C).
    facts: {parent: [[a, b], [b, c], [b, d], [x, y]]}
    expect:
      relations: {grandparent: [[a, c], [a, d]]}
```

| 字段 | 含义 |
|---|---|
| `program` | 一个模块的源文本 |
| `modules` | 多个模块：`[{name: a.mrl, text: "…"}, …]`（与 `program` 二选一） |
| `facts` | `{关系: [[列值, …], …]}`，只能给 `@input` 关系 |
| `expect.relations` | `{关系: [行, …]}`：该关系的全部行必须**恰好**等于这些行（行不得重复） |
| `expect.contains` | 每一行都必须出现 |
| `expect.excludes` | 每一行都不得出现 |
| `expect.warnings` | 程序警告码的多重集合必须恰好相等 |
| `expect.static_error` | 程序装配必须失败，且该码出现在 error 诊断中 |
| `expect.error` | 求值必须以该码失败（包括 MRL-FACT） |

- 期望中的行先按关系的列类型规范化，再按身份比较：float 列可以写 `[1]`，等价于 `[1.0]`；`any` 列中 `1` 与 `1.0` 不同。
- 单列关系的行可以直接写值：`{p: [a, b]}` 等价于 `{p: [[a], [b]]}`；但单列的 list 值必须写全，例如 `{vals: [[[1, 2]]]}`。零列关系的一行写作 `[]`。

### 11.4 factory 用例

kind: factory 的格式（基准 Registry、补丁操作、诊断与解析结果的期望）见 [factory-profile.md](factory-profile.md) 第 8 章。

### 11.5 运行

```bash
uv run mrl conformance                                    # 全部套件（spec/mrl/conformance）
uv run mrl conformance spec/mrl/conformance/expr/arith.yaml   # 单个文件或目录
uv run mrl conformance --json                             # 机器可读的结果
uv run pytest                                             # 每个用例也是一个 pytest 用例
```

全部通过且至少有一个用例时退出码为 0，否则为 1；失败的用例逐个打印原因。

### 11.6 编写用例的规则

- 一个用例只验证一件事；`id` 说明验证的是什么。
- 负面用例写精确的错误码；涉及警告的用例写 `warnings`（包括 `[]`）。
- 值的种类要写对：期望 float 时写 `2.0`，不要写 `2`。
- 新增或修改语义时：先写用例（正面与负面），再改实现，最后更新本文档的对应章节。

```yaml
# ✔ 正确：静态类型与值都有断言，float 写成 54.0
- {id: mixed-mul, expr: 'x * 0.9', types: {x: int}, env: {x: 60}, expect: {value: 54.0, type: float}}

# ✖ 错误：54 是 int，与结果 54.0 的身份不同，用例会失败
- {id: mixed-mul, expr: 'x * 0.9', types: {x: int}, env: {x: 60}, expect: {value: 54}}

# ✖ 错误：r.y 没有用 has() 保护，会产生 MRL-MAYBE-ABSENT，与 warnings: [] 不符，用例失败
- {id: opt, expr: 'r.y == 1', types: {r: '{y?: int}'}, env: {r: {y: 1}}, expect: {value: true, warnings: []}}

# ✔ 正确：warnings 写出完整的多重集合
- {id: opt, expr: 'r.y == 1', types: {r: '{y?: int}'}, env: {r: {y: 1}}, expect: {value: true, warnings: [MRL-MAYBE-ABSENT]}}
```

---

## 附录 A：一致性套件一览

| 文件 | 套件 | 覆盖 |
|---|---|---|
| `expr/lexical.yaml` | `expr.lexical` | 字面量、转义、注释、关键字与保留字、数字格式（第 3 章） |
| `expr/arith.yaml` | `expr.arith` | int 与 float 混合、溢出、除法、取模、零的规范化（2.2、6.2） |
| `expr/compare.yaml` | `expr.compare` | 相等、有序比较、`in`、比较的不可结合（6.3、4.2） |
| `expr/logic.yaml` | `expr.logic` | 逻辑运算与条件表达式、严格从左到右的短路（6.4） |
| `expr/access.yaml` | `expr.access` | 字段选择、下标、`has()`、map 字面量、记录类型与定义性分析（5.4、6.5） |
| `expr/macros.yaml` | `expr.macros` | 五个宏、短路、遮蔽、宏变量的作用域（4.4、6.6） |
| `expr/static.yaml` | `expr.static` | 静态类型推导与类型错误（第 5 章） |
| `expr/stdlib.yaml` | `expr.stdlib` | 标准库每个函数的结果与边界（6.7） |
| `expr/limits.yaml` | `expr.limits` | 步数预算与嵌套深度（4.7、6.8） |
| `expr/architecture.yaml` | `expr.architecture` | docs/architecture.md 中出现的全部表达式，在真实的类型环境下编译并求值 |
| `expr/template.yaml` | `template.basic` | 模板的插值边界、渲染与错误（第 7 章） |
| `rules/basics.yaml` | `rules.basics` | 声明、事实、连接、常量、赋值、守卫、多模块（8.1–8.4） |
| `rules/syntax.yaml` | `rules.syntax` | 模块头、语句结束符、`:-`、注解、注释（8.1、3.5） |
| `rules/types.yaml` | `rules.types` | 列类型、规范化（int 进入 float 列）、静态与运行期类型错误（8.2、8.4） |
| `rules/safety.yaml` | `rules.safety` | 安全性与值构造限制（8.5、8.7） |
| `rules/negation.yaml` | `rules.negation` | 分层否定、匿名变量的否定、不可分层的程序（8.4、8.5、8.7） |
| `rules/recursion.yaml` | `rules.recursion` | 传递闭包、互递归、非线性递归、带守卫的递归（8.6） |
| `rules/aggregation.yaml` | `rules.aggregation` | 五种聚合、绑定去重、空输入、规范序求和（8.8） |
| `rules/modules.yaml` | `rules.modules` | 模块与归属、声明错误、宿主事实错误（8.2、8.3） |
| `rules/errors.yaml` | `rules.errors` | 运行期错误终止求值、守卫位置的影响（8.9） |
| `factory/*.yaml` | `factory.*` | 工厂 profile：抽取、内核与宪法规则、表达式位置、转换解析（见 factory-profile.md 第 8 章） |
