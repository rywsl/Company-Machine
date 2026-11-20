# Bun/TypeScript MRL 运行时边界

本文记录 Bun 控制面逐步接管 MRL 表达式执行时的兼容边界。MRL 1.0 的语言语义仍以 [`mrl-1.0.md`](mrl-1.0.md) 为规范来源，TypeScript 实现不得为业务系统引入第二套表达式语言。

## 运行时入口

`backend/mrl/index.ts` 导出词法分析、表达式解析、静态检查、求值、模板、MRL-R 规则程序和关系查询。业务侧通过 `compileExpr(source, options)` 编译一次，再调用返回对象的 `evaluate(environment)` 重复求值。需要一次性求值时使用 `evaluate(source, environment, options)`。模板使用 `compileTemplate(text, options)`，插值只允许 string、数值、bool 或 dyn。规则程序通过 `buildRuleProgram([{name, source}])` 构建，再用 `evaluate(facts)` 固定点求值并从 `RuleModel.rowsOf(name)` 查询关系。`backend/mrl/cli.ts` 提供 `bun run mrl eval|template|check|run`，CLI 的映射输入使用 JSON 或 `@FILE` 引用。

环境值只能是 JSON 可表示的 MRL 值，包括 `null`、布尔、有限数字、字符串、数组和字符串键对象。未绑定的名字产生 `MRL-UNDEFINED`。求值预算通过 `EvaluateOptions.budget` 注入，超过预算产生 `MRL-COST`，避免不受控的宏展开阻塞 Bun 事件循环。

## 当前兼容集合

实现覆盖 MRL-E 的基础表达式、短路逻辑、条件表达式、列表与记录、成员和下标访问、算术与比较、`in`、标准字符串和数值函数，以及 `all`、`exists`、`exists_one`、`map`、`filter` 宏。静态 checker 支持根环境类型、记录可选字段、`has()` 定义性保护、期望类型、宏变量遮蔽、函数元数和稳定诊断码。关系查询适配器 `queryRelation` 将关系行字段绑定为表达式根名字。模板支持文本与 `{{ expression }}` 插值，并在编译期拒绝列表、map、record 和 null 插值。

解析和运行时错误保留 MRL 错误码。调用方应向用户展示可读的错误消息和字段路径，不应把 AST 或原始 JSON 直接作为 UI 内容。

## Factory Registry 运行时

Registry 的活动结构检查位于 `backend/mrl/factory/`。`paths.ts` 解析 `MRL_SPEC_DIR` 或仓库内的 `spec/`，`yamlio.ts` 使用 Bun 原生 YAML 解析器处理 YAML 1.2 文档，`registry.ts` 按工厂 profile 的目录布局读取 Spec，`schema.ts` 解析 `spec/schemas/` 的 Draft 2020-12 结构约束，`check.ts` 输出布局、YAML、schema、重复键和宪法缺失诊断。

命令行入口是 `bun run factory registry check <registry>`，加上 `--json` 输出稳定的机器可读诊断。`backend/mrl/factory.test.ts` 使用 `tests/fixtures/registry-m1` 和 `tests/fixtures/registry-m2` 锁定 YAML、布局和 schema 阶段。内核事实抽取、宪法求值、表达式位置类型环境、转换解析和完整诊断排序仍需在后续阶段接入同一个 Registry 结果，不得恢复 Python 入口。

旧 Python MRL 和 factory profile 实现只保存在 `removed/python/`，不参与活动控制面启动。表达式静态检查、模板 API、MRL-R 规则和 Registry 结构检查由 TypeScript 测试锁定边界。

## 性能与安全约束

解析结果可以缓存，但缓存键必须包含完整源文本和实现版本。求值不得调用宿主 `eval`、`Function` 或任意外部进程。宏和标准函数必须在受预算限制的 evaluator 内执行，错误对象不得包含密钥值或其他访问凭据。

## 验证

TypeScript 单元测试位于 `backend/mrl/mrl.test.ts`、`backend/mrl/checker.test.ts`、`backend/mrl/rules.test.ts` 和 `backend/mrl/factory.test.ts`。CLI 可用 `bun run mrl --help`、`bun run mrl eval`、`bun run mrl template`、`bun run mrl check`、`bun run mrl run` 和 `bun run factory registry check tests/fixtures/registry-m1` 验证。每次变更应运行 `bun test backend/mrl`，活动提交前运行 `bun run verify:bun`。历史 Python 参考实现和旧映射位于 `removed/python/`。
