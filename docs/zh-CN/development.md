# 开发

活动产品使用 Bun 和 TypeScript。原 Python 实现和旧验收夹具保存在 `removed/python/`，只作为历史证据。

## 提交前验证

提交或推送前运行 Bun 验证。

```bash
bun run verify:bun
```

该命令运行 Bun 业务控制面、MRL、Podman 和 Kiri runtime 测试。完整活动验收入口为 `bun run acceptance:bun`。

在安装了官方 CLI 的主机上运行 `bun run verify:official-harness`，记录官方 Harness 版本并只完成 ACP `initialize` 协商，不发送模型提示词。

## 运行时选择

默认使用 Podman。桌面进程继承旧 PATH 时可以指定 `MAO_PODMAN_EXECUTABLE`。

```bash
podman machine init --now podman-machine-default
bun run backend/src/index.ts
```

Linux 无桌面环境使用 Kiri 官方 `kiri.service` 及其 Unix 套接字。安装步骤和连接配置见[快速开始](getting-started.md#在-linux-上使用-headless-kiri)。

## 工程约束

不要新增依赖，不要在控制面调用 LLM API，不要拼接 shell 命令，不要为了通过验证削弱测试。不要把凭据写入日志、提交或回复。保留无关改动，提交和推送只在用户明确要求时进行。
