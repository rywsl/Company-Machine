# 开发

活动产品使用 Bun 和 TypeScript。原 Python 实现和旧验收夹具保存在 `removed/python/`，只作为历史证据。

## 提交前验证

提交或推送前运行 Bun 验证。

```bash
bun run verify:bun
```

该命令运行 Bun 业务控制面、MRL、Podman 和 Kiri runtime 测试。完整活动验收入口为 `bun run acceptance:bun`。

## 运行时选择

默认使用 Podman。桌面进程继承旧 PATH 时可以指定 `MAO_PODMAN_EXECUTABLE`。

```bash
podman machine init --now podman-machine-default
bun run backend/src/index.ts
```

Kiri 支持 headless CLI 路径。

```powershell
$env:MAO_CONTAINER_RUNTIME = "kiri"
$env:MAO_KIRI_EXECUTABLE = "kiri"
bun run backend/src/index.ts
```

Linux 使用 Kiri 原生 headless 包时也可以直接导出配置后启动控制面。

```bash
export MAO_CONTAINER_RUNTIME=kiri
export MAO_KIRI_EXECUTABLE=kiri
bun run backend/src/index.ts
```

Linux 下 Kiri readiness 可以在没有 UI 的情况下启动 `kiri daemon`，或者复用 systemd 管理的官方服务。官方服务使用 `/run/kiri/docker.sock` 时设置 `MAO_KIRI_SOCKET=/run/kiri/docker.sock`，状态目录可设置为 `MAO_KIRI_DATA_DIR=/var/lib/kiri-daemon`。安装提供 `docker --context kiri` 时，可设置 `MAO_KIRI_MODE=docker-context`。

## 工程约束

不要新增依赖，不要在控制面调用 LLM API，不要拼接 shell 命令，不要为了通过验证削弱测试。不要把凭据写入日志、提交或回复。保留无关改动，提交和推送只在用户明确要求时进行。
