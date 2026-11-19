# Bun/TypeScript MRL 运行时边界

本文记录 Bun 控制面逐步接管 MRL 表达式执行时的兼容边界。MRL 1.0 的语言语义仍以 [`mrl-1.0.md`](mrl-1.0.md) 为规范来源，TypeScript 实现不得为业务系统引入第二套表达式语言。

## 运行时入口

`backend/mrl/index.ts` 导出词法分析、表达式解析、求值和关系查询。业务侧通过 `compileExpr(source)` 编译一次，再调用返回对象的 `evaluate(environment)` 重复求值。需要一次性求值时使用 `evaluate(source, environment, options)`。

环境值只能是 JSON 可表示的 MRL 值，包括 `null`、布尔、有限数字、字符串、数组和字符串键对象。未绑定的名字产生 `MRL-UNDEFINED`。求值预算通过 `EvaluateOptions.budget` 注入，超过预算产生 `MRL-COST`，避免不受控的宏展开阻塞 Bun 事件循环。

## 当前兼容集合

实现覆盖 MRL-E 的基础表达式、短路逻辑、条件表达式、列表与记录、成员和下标访问、算术与比较、`in`、标准字符串和数值函数，以及 `all`、`exists`、`exists_one`、`map`、`filter` 宏。关系查询适配器 `queryRelation` 将关系行字段绑定为表达式根名字。

解析和运行时错误保留 MRL 错误码。调用方应向用户展示可读的错误消息和字段路径，不应把 AST 或原始 JSON 直接作为 UI 内容。

## 分阶段迁移

第一阶段仅接入 Bun 业务控制面和投影查询，旧 Python MRL 仍负责规则模块、工厂 profile 检查和完整一致性套件。第二阶段补齐静态类型检查、模板和规则程序 API，并以 `spec/mrl/conformance/` 的同一套用例做双实现回归。完成第二阶段后，控制面才能移除 Python MRL 运行时依赖。

## 性能与安全约束

解析结果可以缓存，但缓存键必须包含完整源文本和实现版本。求值不得调用宿主 `eval`、`Function` 或任意外部进程。宏和标准函数必须在受预算限制的 evaluator 内执行，错误对象不得包含密钥值或其他访问凭据。

## 验证

TypeScript 单元测试位于 `backend/mrl/mrl.test.ts`。每次变更应运行 `bun test backend/mrl/mrl.test.ts`，并在提交前运行仓库根目录的 `uv run python tools/harness.py verify`，确保 Python 参考实现、规范和验收映射仍保持一致。
