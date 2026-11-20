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

从官方发布页下载与 Linux 发行版和 CPU 架构对应的 `kiri-lite` 包，并安装 Docker 兼容 CLI。Debian 和 Ubuntu 使用 `sudo apt install ./KIRI_PACKAGE.deb`，Fedora 使用 `sudo dnf install ./KIRI_PACKAGE.rpm`。将示例文件名替换为下载的文件名，再给运行 MAO 的账户授予访问权限。 [Kiri 官方发布页](https://runkiri.com/releases/)

```bash
sudo systemctl enable --now kiri.service
sudo kiri host-access "$USER"
# 如 host-access 提示文件系统不支持 ACL，重新登录后继续
export FACTORY_ROOT="$HOME/.local/share/mao"
MAO_CONTAINER_RUNTIME=kiri bun run factory runtime ensure-ready
MAO_CONTAINER_RUNTIME=kiri bun run backend/src/index.ts
```

MAO 通过 `/run/kiri/docker.sock` 连接。自定义部署可以设置 `MAO_KIRI_SOCKET` 和 `MAO_DOCKER_EXECUTABLE` 指定套接字和客户端。官方 Linux 路径直接使用 Unix 套接字，无需桌面会话。

在 WSL 中运行工位时，保留示例的 Linux 数据目录。文件邮箱使用 Linux 目录权限保护宿主响应。MAO 由系统服务启动时，对该服务的运行账户授权，并给服务配置相同的环境变量。

可以用项目自带验证命令构建夹具镜像，并运行真实 Kiri 工位。

```bash
export MAO_CONTAINER_RUNTIME=kiri
bun run build:test-images
bun run tools/verify-runtime.ts
```

若 Kiri 的镜像构建请求超时，可在同 CPU 架构的 Podman 主机上运行 `bun run build:test-images --save-to /tmp/mao-test-images`。将目录复制到 Kiri 主机，执行 `MAO_CONTAINER_RUNTIME=kiri bun run build:test-images --load-from /tmp/mao-test-images`，再运行同一条工位验收命令。

Registry 工位声明出网策略时，在 Linux 宿主设置 `MAO_EGRESS_RELAY_IMAGE`，指向当前引擎中已导入且含 Bun 的可信固定镜像。工位通过策略允许的地址出网。无需出网的工位可直接运行。

容器化厂长使用官方 Claude Code 或 Codex 时，可在 MAO 服务的部署环境中分别配置 `MAO_FOREMAN_ANTHROPIC_API_KEY` 或 `MAO_FOREMAN_CODEX_API_KEY`。使用自定义模型入口时，同时配置对应的 `MAO_FOREMAN_ANTHROPIC_BASE_URL` 或 `MAO_FOREMAN_CODEX_BASE_URL`，并在 Web App 中保存匹配的地址。页面只显示凭证状态。

## 交给厂长

```bash
bun run factory project intake --name "视频生产系统" --repo https://example.test/repo.git
bun run factory status
```

Web App 提供同样的 Intake、厂长对话、DAG、物料血缘、项目资源请求和运行时设置。`bun run acceptance:bun` 运行活动 Bun 验收套件。
