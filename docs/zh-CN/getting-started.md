# 快速开始

活动项目是 Bun + TypeScript 业务控制面。Python 源码和旧夹具归档在 `removed/python/`。

## 前置条件

- Bun
- Podman 6 或 Kiri 0.2+
- 需要真实 Operator 会话时使用 Claude Code、Codex 等官方 Harness CLI

## 安装和验证

```bash
bun install
bun run verify:bun
pnpm check:web
pnpm build:web
```

## 启动本地工厂

```bash
bun run backend/src/index.ts
```

服务监听 `127.0.0.1:3000`，Web App 静态资源位于 `frontend/dist`。

## 准备 Podman

```bash
podman machine init --now podman-machine-default
bun run factory status
```

桌面进程继承旧 PATH 时设置 `MAO_PODMAN_EXECUTABLE`。

## 在 Linux 上使用 headless Kiri

无桌面环境可以安装 Kiri 官方 `kiri-lite` 包与 Docker 兼容 CLI。先授权运行 MAO 的账户访问 `kiri.service`，刷新登录会话后检查就绪状态。

```bash
sudo kiri host-access "$USER"
MAO_CONTAINER_RUNTIME=kiri bun run factory runtime ensure-ready
MAO_CONTAINER_RUNTIME=kiri bun run backend/src/index.ts
```

MAO 通过 `/run/kiri/docker.sock` 连接。自定义部署可以设置 `MAO_KIRI_SOCKET` 和 `MAO_DOCKER_EXECUTABLE` 指定套接字和客户端。官方 Linux 路径直接使用 Unix 套接字，无需桌面会话。

在 WSL 中运行工位时，将 `FACTORY_ROOT` 指向 Linux 文件系统，例如 `$HOME/.local/share/mao`。文件邮箱使用 Linux 目录权限保护宿主响应。

## 交给厂长

```bash
bun run factory project intake --name "视频生产系统" --repo https://example.test/repo.git
bun run factory status
```

Web App 提供同样的 Intake、厂长对话、DAG、物料血缘、项目资源请求和运行时设置。`bun run acceptance:bun` 运行活动 Bun 验收套件。
