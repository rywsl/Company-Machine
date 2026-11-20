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

## 使用 headless Kiri

```powershell
$env:MAO_CONTAINER_RUNTIME = "kiri"
$env:MAO_KIRI_EXECUTABLE = "kiri"
bun run backend/src/index.ts
```

Linux readiness 可以启动 `kiri daemon`。Kiri 只提供 `kiri` Docker context 时设置 `MAO_KIRI_MODE=docker-context`。

## 交给厂长

```bash
bun run factory project intake --name "视频生产系统" --repo https://example.test/repo.git
bun run factory status
```

Web App 提供同样的 Intake、厂长对话、DAG、物料血缘、项目资源请求和运行时设置。`bun run acceptance:bun` 运行活动 Bun 验收套件。
