# 快速开始

活动项目是 Bun + TypeScript 业务控制面。Python 源码和旧夹具归档在 `removed/python/`。

## 前置条件

- Bun 1.3.14（本项目验收版本，Registry 和夹具使用 `Bun.YAML`）
- Podman 6 或 Kiri 0.2+
- 需要真实 Operator 会话时使用 Claude Code、Codex、Pi、Qwen Code、DeepSeek Harness、Kimi Code 或 ZCode 等官方 Harness

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

完整生产部署使用同一用户的 Bun systemd user service 和 rootless Podman/Kiri，参见 `deploy/mao-control-plane.service`。控制面与运行时共享宿主路径命名空间，项目来源、物料文件和厂长邮箱才能继续使用本地挂载。`deploy/mao-control-plane.Containerfile` 可用于构建便携 Web 镜像，容器化控制面需要额外的宿主路径映射设计。`/healthz` 是进程存活检查，`/readyz` 会检查 SQLite 和当前 Podman/Kiri 运行时。

## 准备 Podman

```bash
podman machine init --now podman-machine-default
bun run factory status
```

桌面进程继承旧 PATH 时设置 `MAO_PODMAN_EXECUTABLE`。

`bun run build:test-images` 使用默认 `oven/bun` 基础镜像时会先读取配置的 GeoIP 端点。中国大陆网络会并行探测配置的 Docker Hub 镜像，并选择能返回目标 manifest 且下载速度最快的镜像。GeoIP 支持按顺序配置多个端点，单个端点超时或返回异常时继续尝试后续端点。使用 `--base` 可以明确指定基础镜像。部署环境可以通过 `MAO_GEOIP_COUNTRY`、`MAO_GEOIP_URL`、`MAO_GEOIP_URLS` 或 `MAO_DOCKERHUB_MIRRORS` 指定区域、GeoIP 端点和镜像清单。

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

MAO 默认通过 `/run/kiri/docker.sock` 连接。headless Kiri 发行版如果把套接字放在 `/var/lib/kiri-daemon/.kiri/run/docker.sock`，控制面会自动发现该路径。自定义部署可以设置 `MAO_KIRI_SOCKET` 和 `MAO_DOCKER_EXECUTABLE` 指定套接字和客户端。安装后先运行 `MAO_CONTAINER_RUNTIME=kiri bun run factory runtime doctor`，再启动服务。官方 Linux 路径直接使用 Unix 套接字，无需桌面会话。

在 WSL 中运行工位时，保留示例的 Linux 数据目录。文件邮箱使用 Linux 目录权限保护宿主响应。MAO 由系统服务启动时，对该服务的运行账户授权，并给服务配置相同的环境变量。

共享部署时设置 `MAO_AUTH_TOKEN` 和 `MAO_CSRF_TOKEN`。需要多主体范围控制时再设置 `MAO_AUTH_SCOPES_JSON`，对象键是 Bearer token，值可以声明 `role`、`business_systems` 和 `projects`。启用范围控制后，即使来自回环地址也必须提供匹配的 Bearer token，范围凭据不能访问或修改声明范围之外的资源。

可以用项目自带验证命令构建夹具镜像，并在 Kiri 中验证工位和厂长邮箱。

```bash
export MAO_CONTAINER_RUNTIME=kiri
bun run build:test-images
bun run tools/verify-runtime.ts
bun run tools/verify-runtime.ts --foreman
```

若 Kiri 的镜像构建请求超时，可在同 CPU 架构的 Podman 主机上运行 `bun run build:test-images --save-to /tmp/mao-test-images`。将目录复制到 Kiri 主机，执行 `MAO_CONTAINER_RUNTIME=kiri bun run build:test-images --load-from /tmp/mao-test-images`，再运行同一条工位验收命令。

Registry 工位声明出网策略时，在 Linux 宿主设置 `MAO_EGRESS_RELAY_IMAGE`，指向当前引擎中已导入且含 Bun 的可信固定镜像。工位通过策略允许的地址出网。无需出网的工位可直接运行。

在 Web App 厂长配置中填写含所选官方 Harness 的固定镜像摘要。可信厂长默认使用 `bridge` 网络，可以在容器内自由下载仓库和项目工具，但不会使用宿主网络或宿主引擎套接字。需要离线运行时选择 `none`。Operator 容器需要固定白名单出网时选择 `egress`，并配置 `MAO_EGRESS_RELAY_IMAGE` 和 `MAO_HARNESS_EGRESS_HOSTS`。

在 MAO 服务的部署环境中配置所选 Harness 对应的 `MAO_FOREMAN_ANTHROPIC_API_KEY` 或 `MAO_FOREMAN_CODEX_API_KEY`。使用自定义模型入口时，同时配置对应的 `MAO_FOREMAN_ANTHROPIC_BASE_URL` 或 `MAO_FOREMAN_CODEX_BASE_URL`，并在 Web App 中保存匹配的地址。页面只显示凭证状态，业务项目所需密钥通过各自的资源请求配置。

## 交给厂长

```bash
bun run factory project intake --name "视频生产系统" --repo https://example.test/repo.git
bun run factory status
```

Web App 提供同样的 Intake、厂长对话、DAG、物料血缘、项目资源请求和运行时设置。`bun run acceptance:bun` 运行活动 Bun 验收套件。
