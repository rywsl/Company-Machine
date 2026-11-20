[English](../en/cli.md) | **简体中文**

# 命令行参考

活动命令行运行在 Bun 上。工厂控制面入口是 `factory`，MRL 工具入口是
`mrl`。完整的历史 Python 命令和旧参数保存在 `removed/python/`，用于迁移
审计。

## 安装和入口

```bash
bun install
bun run factory --help
bun run mrl --help
```

`bun run factory serve` 会在 `127.0.0.1:3000` 启动 Bun Web API 和 Web App。

## `factory`

| 命令 | 用途 |
|---|---|
| `status` | 输出本地业务系统和厂长队列摘要。 |
| `serve` | 启动 Bun Web API 和 Web App。 |
| `runtime status` | 读取当前 Podman 或 Kiri 运行时健康状态。 |
| `runtime ensure-ready` | 请求当前运行时就绪。仍不可用时退出码为 `4`。 |
| `project intake --name NAME --repo URL` | 从仓库链接创建项目。 |
| `project intake --name NAME --folder PATH` | 从本地文件夹引用创建项目。 |
| `project intake --name NAME --file-ref REF` | 从上传文件引用创建项目。 |
| `business units ID` | 查看业务系统的 Operator 和 Machine 基础单元。 |
| `business run ID [--input REF]` | 创建业务运行并返回单元运行状态。 |
| `business runs ID` | 列出业务运行及单元状态。 |
| `business export ID` | 导出可移植的业务系统文档。 |
| `business graph ID --file PATH` | 写入厂长维护的 DAG 文档。 |
| `business artifact PROJECT_ID --file PATH` | 写入面向人和其他 Harness 的产物或物料。 |
| `business conversion ID --kind KIND --summary TEXT` | 记录产物到物料的转换。 |
| `foreman inbox [--limit N]` | 查看待处理的厂长消息。 |
| `foreman claim ID` / `foreman done ID` | 领取或完成厂长消息。 |
| `talk send TEXT` | 将人的消息放入已配置的官方 Harness 队列。 |
| `talk reply TEXT --turn-id ID` | 向对话线程追加厂长回复。 |

业务记录在 API 边界使用 JSON。Web App 将其展示为图表、时间线和物料血缘。

## `mrl`

Bun MRL CLI 接受内联 JSON 映射，也接受 `@FILE` 文件引用。

| 命令 | 用途 |
|---|---|
| `mrl eval EXPR [--env JSON|@FILE] [--types JSON|@FILE] [--budget N] [--json]` | 静态检查并求值表达式。 |
| `mrl template TEXT [--env JSON|@FILE] [--types JSON|@FILE] [--budget N] [--json]` | 渲染经过检查的模板。 |
| `mrl check FILE... [--json]` | 解析并检查 MRL 规则模块。 |
| `mrl run FILE... [--facts JSON|@FILE] [--rel NAME]... [--json]` | 使用输入事实求值规则模块。 |

退出码为 `0` 成功、`2` 用法错误、`3` MRL 静态错误、`4` 求值或规则运行时错误。

## 历史参考

旧 Python `factory`、`mrl` 和 `factoryctl` 实现保存在
[`removed/python/`](../../removed/python/)。Bun 控制面和 `bun run verify:bun`
都不会加载这些历史文件。
