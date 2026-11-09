# sandbox：沙箱（mao.sandbox）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M1-04**。
> 模块：`mao.sandbox`。依据：README D5、D11、D12、第 4.5 节、第 5 节；`plan/risks.md` 的 R5、R7、S3、S4。
> 架构文档 5.3、6.4 只作背景。与 README 冲突时以 README 为准。

---

## 1. 目的与范围

本组件把“在隔离环境里执行”收成一个接口，供检验、操作工和 Runner 使用。

- `SandboxProvider`：打开一次 attempt 的沙箱，在里面执行命令，结束后销毁容器。
- `DockerProvider`：生产实现。只调用 `docker` 命令行，不使用 Docker SDK（README D2）。
- `LocalProvider`：仅测试。在宿主上直接跑进程，**没有任何隔离**，用路径映射和命令映射模拟第 5 节的虚拟路径。

不做的事：

| 不做 | 归属 |
|---|---|
| 出网代理、域名白名单、模型代理 | M2 |
| 秘密注入 | M2（本组件不读取、不记录任何秘密） |
| 资源队列、GPU、进程数以外的配额调度 | M2 调度器 |
| 决定挂什么、跑什么命令 | `runner.md`、`gate.md` |
| 在生产配置或 CLI 默认值里使用 `LocalProvider` | README D5；CLI 默认 `--provider docker` |

安全基线不可降级（D11、R7）。下面任何一条都不允许因为“容器里写不了文件”或“需要 docker”而放开：

- argv 里必须有 `--cap-drop ALL`、`--read-only`、非 root 用户、`--security-opt no-new-privileges`；
- argv 里禁止出现 `docker.sock`、`--privileged`、`--network host`、`--cap-add`；
- 禁止 `-v`（Windows 盘符有歧义，R5），只用 `--mount`。

---

## 2. 依赖

- 标准库：`subprocess`、`os`、`shutil`、`re`、`threading`、`pathlib`。
- `mao.base`：`Clock`、`SystemClock`。时长用 `time.monotonic` 的差值时，优先用注入的 `clock.monotonic()`。
- `mao.errors`：`FactoryError`。
- **禁止** import Docker SDK、`psutil`、`requests`。**禁止** `shell=True`。**禁止** import `mao.runner`、`mao.ctl`、`mao.gate`（沙箱不知道 job）。

`DockerProvider.__init__` 与 `LocalProvider.__init__` 的参数以 README 4.5 为准。下面两个关键字是兼容性扩展（默认值不改变原语义），与 `Database.migrate` 的可选参数同一写法：

- `clock: Clock | None = None`：缺省 `SystemClock()`。只用于 `ExecResult.duration_s` 和超时后的时间差。
- `LocalProvider` 的 `commands` 已在 README 中。

---

## 3. 接口

README 4.5 的签名照抄，不改名、不改含义。本组件只增加方法。

```python
@dataclass(frozen=True)
class Mount:
    source: str                # 宿主绝对路径，或 "volume:<名字>"
    target: str                # 沙箱内的虚拟路径（README 第 5 节）
    readonly: bool

@dataclass(frozen=True)
class SandboxSpec:
    name: str                  # 唯一名字，如 "mao-j-0001-1"
    image: str                 # "<repo>@sha256:<64位hex>"；LocalProvider 忽略
    mounts: tuple[Mount, ...]
    workdir: str = "/workspace"
    env: Mapping[str, str] = field(default_factory=dict)
    network: Literal["none", "egress"] = "none"
    cpus: float | None = None
    memory_mb: int | None = None
    workspace_volume: str | None = None   # Docker：挂到 /workspace 的命名卷

@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None      # 超时时为 None
    timed_out: bool
    duration_s: float

class Sandbox(Protocol):
    name: str
    def exec(self, argv: Sequence[str], *, timeout_s: float, stdout: Path, stderr: Path,
             env: Mapping[str, str] | None = None, workdir: str | None = None) -> ExecResult
    def close(self) -> None    # 销毁容器；不删除 workspace 卷

class SandboxProvider(Protocol):
    name: str                  # "docker" | "local"
    def open(self, spec: SandboxSpec) -> Sandbox
    def remove_volume(self, name: str) -> None

class LocalProvider:           # 仅测试
    name: str                  # = "local"
    def __init__(self, root: Path, *, commands: Mapping[str, Sequence[str]] | None = None,
                 clock: Clock | None = None) -> None
    def open(self, spec: SandboxSpec) -> Sandbox
    def remove_volume(self, name: str) -> None          # 空操作
    def prepare_inject_volume(self, image: str, volume: str) -> None   # 空操作

class DockerProvider:
    name: str                  # = "docker"
    def __init__(self, root: Path, *, docker: str = "docker",
                 allow_unproxied_egress: bool = False, clock: Clock | None = None) -> None
    def build_run_argv(self, spec: SandboxSpec) -> list[str]   # 纯函数，不调用 docker
    def resolve_image(self, image: str) -> str
    def prepare_inject_volume(self, image: str, volume: str) -> None
    def open(self, spec: SandboxSpec) -> Sandbox
    def remove_volume(self, name: str) -> None
```

`build_run_argv` 只读 `self.docker` 与 `self.allow_unproxied_egress`，不启动子进程，不访问文件系统。单元测试和验收测试锁的就是它返回的列表。

---

## 4. 数据格式

### 4.1 镜像引用

两种字符串可以出现在 `docker run` 的镜像位置：

| 形式 | 正则 | 谁产生 |
|---|---|---|
| 仓库 digest 引用 | `^.+@sha256:[0-9a-f]{64}$` | Spec、`SandboxSpec.image` |
| 本地镜像 ID | `^sha256:[0-9a-f]{64}$` | `resolve_image` 的回退结果（S4） |

`python:3.12-alpine`、`mao-test/tool:dev`、带大写 hex 的 digest，都不是合法引用。解析规则见 5.3。

### 4.2 命名卷

| 卷 | 名字 | 挂载点 | 读写 |
|---|---|---|---|
| 工作区 | 由调用方传入 `workspace_volume` | `/workspace` | 读写 |
| 操作工注入 | 由调用方传入（Runner 用 `mao-op-` + digest 前 12 位） | Spec 里的 `inject.mount`，夹具是 `/opt/operator` | 只读 |

卷名必须匹配 `^[A-Za-z0-9][A-Za-z0-9_.-]+$`。

### 4.3 沙箱用户

容器内用户固定为数值 uid/gid **10001:10001**。镜像不必在 `/etc/passwd` 里有这个名字；`docker run --user 10001:10001` 用数值即可。夹具镜像 `studio` 把 `/workspace` 的属主设成 10001，这样用镜像内容初始化的工作区卷对沙箱用户可写（第 5.4 节）。

### 4.4 始终注入的环境变量

调用方放进 `spec.env` 的变量会传进沙箱，但下表这些键**始终由本组件写成表中的值**，调用方不能改掉（防止把 `FACTORY_IN` 指到别的地方）：

| 变量 | 值 |
|---|---|
| `FACTORY_IN` | `/factory/in` |
| `FACTORY_OUT` | `/factory/out` |
| `FACTORY_CTL` | `/factory/ctl` |
| `FACTORY_WORKSPACE` | `/workspace` |
| `FACTORYCTL` | `/opt/factory/bin/factoryctl` |
| `HOME` | `/tmp/home` |
| `PATH` | `/opt/factory/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` |

`FACTORY_JOB_ID`、`FACTORY_ATTEMPT` 只在调用方放进 `spec.env` 时才出现。M1 不注入秘密；本组件不识别、不过滤秘密名字（调用方根本不该传入）。

---

## 5. 行为

### 5.1 `open`、`exec`、`close`

`DockerProvider.open(spec)`：

1. `resolved = resolve_image(spec.image)`。
2. 若 `spec.workspace_volume` 非空：`docker volume create <name>`（已存在则忽略）。
3. 若已有同名容器：`docker rm -f <name>`，再创建。这只为测试重复运行；生产上名字应当唯一。
4. `argv = build_run_argv(replace(spec, image=resolved))`，然后 `subprocess.run(argv, timeout=120)`。
5. 退出码不是 0：抛出 `DOCKER_FAILED`，`detail` 里有 `stderr`（截断到 4000 字符）和 `argv` 的副本。argv 里没有秘密（M1 不注入）。
6. 返回的 `Sandbox.exec` 用 `docker exec`（5.8）。`close` 执行 `docker rm -f <name>`，**不**删除 `workspace_volume`（README 4.5）。

`LocalProvider.open` 不创建容器。它记住 `spec`，按 5.6 做路径映射。`close` 终止还在跑的子进程，不删除目录。

`exec` 的 `timeout_s` 必须 `> 0`，否则 `SANDBOX_SPEC_INVALID`。超时后终止进程，返回 `ExecResult(exit_code=None, timed_out=True, duration_s=...)`，**禁止**把 `subprocess.TimeoutExpired` 冒泡（README 3.7）。`duration_s` 是 `clock.monotonic()` 的差值，`>= 0`。stdout/stderr 以二进制写入给定路径（父目录不存在则创建）。调用方用 UTF-8、`errors="replace"` 读文本。

`exec` 在 `close` 之后调用：`SANDBOX_CLOSED`。

### 5.2 `build_run_argv`（验收测试锁定这一列表）

先检查，任一失败都抛错，不返回部分 argv：

1. `spec.network == "egress"` 且 `not self.allow_unproxied_egress`：`EGRESS_UNSUPPORTED`，`detail.network = "egress"`。
2. `spec.network` 不是 `"none"` 或 `"egress"`：`SANDBOX_SPEC_INVALID`，`detail.field = "network"`。
3. `spec.name` 不匹配 `^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$`：`SANDBOX_SPEC_INVALID`，`field = "name"`。
4. `spec.image` 不匹配 4.1 的两种正则之一：`IMAGE_REF_INVALID`，`detail.image = spec.image`。
5. `spec.workdir` 不是以 `/` 开头的绝对虚拟路径：`SANDBOX_SPEC_INVALID`，`field = "workdir"`。
6. `cpus` 非空时必须 `> 0`；`memory_mb` 非空时必须是 `>= 1` 的 `int`。否则 `SANDBOX_SPEC_INVALID`。
7. 每个 `Mount`：
   - `target` 必须以 `/` 开头，且不含 `docker.sock`（大小写不敏感）；
   - `source` 不含 `docker.sock`（大小写不敏感）；
   - `source` 以 `volume:` 开头时，冒号后的名字必须是合法卷名；
   - 否则 `source` 必须是宿主绝对路径（`Path(source).is_absolute()`），且不含逗号（`--mount` 用逗号分隔）；
   - 两个 mount 的 `target` 相同：`SANDBOX_SPEC_INVALID`。
8. `workspace_volume` 非空时：名字必须合法；若已有 mount 的 `target` 是 `/workspace`：`SANDBOX_SPEC_INVALID`。
9. `spec.env` 的键必须匹配 `^[A-Za-z_][A-Za-z0-9_]*$`，值必须是 `str` 且不含 `\x00`。

然后按**固定顺序**组装列表。少一个标志、多一个标志、换顺序，验收测试都失败。

```text
[self.docker, "run",
 "--name", spec.name,
 "--detach",
 "--init",
 "--user", "10001:10001",
 "--cap-drop", "ALL",
 "--security-opt", "no-new-privileges",
 "--read-only",
 "--pids-limit", "256",
 "--network", "none" 或 "bridge",
 "--tmpfs", "/tmp:rw,nosuid,size=256m",
 # cpus 非空时："--cpus", format(float(cpus), "g")
 # memory_mb 非空时："--memory", f"{memory_mb}m"
 # 按 spec.mounts 的原顺序，每个一条 "--mount", <mount 串>
 # workspace_volume 非空时再加一条工作区卷
 # 契约环境变量，顺序固定为 4.4 表的行序：每个 "--env", "KEY=VALUE"
 # spec.env 里不在契约表中的键，按键名升序：每个 "--env", "KEY=VALUE"
 "--workdir", spec.workdir,
 spec.image,
 "/bin/sh", "-c", "mkdir -p /tmp/home && exec sleep 2147483647"]
```

细则：

- `network == "none"` 时 `--network` 的下一个参数是 `none`；`egress` 且开关打开时是 `bridge`。永远不写 `host`。
- `--cpus` 用 `format(float(spec.cpus), "g")`：`1` 和 `1.0` 都是 `"1"`，`1.5` 是 `"1.5"`。
- mount 串：
  - 绑定：`type=bind,source=<source 原样>,target=<target>`，只读时末尾加 `,readonly`；
  - 卷：`type=volume,source=<卷名>,target=<target>`，只读时同样加 `,readonly`。
  - `source` 原样写入，不 `resolve`，不改斜杠。规范化是 `open` 的调用方的事。
- 工作区卷那条是 `type=volume,source=<workspace_volume>,target=/workspace`，**不**加 `readonly`。
- 禁止出现的参数：`--privileged`、`--cap-add`、`-v`、`--volume`、`--network host`、任何含 `docker.sock` 的字符串。也禁止 `--rm`（`exec` 还要这个容器；`close` 负责删除）。
- 容器命令固定是 `mkdir -p /tmp/home && exec sleep 2147483647`。不用 `infinity`（busybox 的 `sleep` 不保证认识它）。`--init` 让 docker 注入 tini，回收僵尸进程。
- `self.docker` 原样作为 argv[0]（可以是绝对路径）。

`allow_unproxied_egress=True` 只把网络从 `none` 换成 `bridge`。安全标志一条都不能少。这是开发开关，不是出网代理（D12）。

### 5.3 镜像解析（S4）

`resolve_image(image)`：

1. 形式不合法：`IMAGE_REF_INVALID`。
2. 运行 `[docker, "image", "inspect", "--format", "{{.Id}}", image]`，超时 60 秒。
   - 退出码 0，且 stdout 去空白后匹配 `^sha256:[0-9a-f]{64}$`：返回**原来的** `image`（仓库 digest 可用）。
3. 否则若 `image` 含 `@`，取 `@` 后面的 `sha256:<64hex>` 再 inspect 一次。
   - 成功：返回这个镜像 ID（本地构建的镜像没有仓库 digest，S4）。
4. 两次都失败：`IMAGE_NOT_FOUND`，`detail.image = image`。
5. `docker` 可执行文件不存在，或 `docker version` 失败：`DOCKER_UNAVAILABLE`。inspect 超时也是 `DOCKER_UNAVAILABLE`。

`open` 把解析结果放进 `spec.image` 再调用 `build_run_argv`。因此本地测试镜像写成 `mao-test/tool@sha256:<image ID>` 时，实际跑的是镜像 ID。

### 5.4 工作区卷

Docker 的命名卷在**第一次**挂到镜像里已有内容的路径时，会把镜像该路径的内容复制进卷。之后同一个卷再挂，不再覆盖。这就是 resume 能接着用工作区的原因。

- 调用方把卷名放在 `workspace_volume`。本组件 `volume create`，再按 5.2 挂到 `/workspace`。
- 不在宿主上事先 `docker cp`。复制由 Docker 在卷为空时完成。
- 夹具镜像 `tests/fixtures/images/studio/Dockerfile` 把 `/workspace` 的属主设为 10001，并留下 `README.factory` 和 `drafts/`。卷初始化之后，沙箱用户能在 `/workspace` 里写文件。
- 根文件系统是 `--read-only`。可写的只有：显式的读写挂载、`workspace_volume`、`/tmp` 的 tmpfs。不要为了可写去加 `--user root`（R7）。
- 大文件应当放在命名卷里，不要放在绑定挂载上（S3）。验收不依赖绑定挂载的性能。
- `close` 不删这个卷。`remove_volume` 才删。卷不存在时 `remove_volume` 直接返回；`docker volume rm` 因其他原因失败才抛 `VOLUME_REMOVE_FAILED`。

`LocalProvider` 没有卷。调用方把宿主目录挂成 `target=/workspace` 的 bind mount，本组件只做路径替换（5.6）。

### 5.5 操作工注入卷

夹具镜像 `tests/fixtures/images/operator/Dockerfile` 把 `fake-agent` 放在 `/opt/operator/bin/fake-agent`。生产上 Claude Code 也放在这个挂载点。内容要先进命名卷，再**只读**挂进机器容器。

`prepare_inject_volume(image, volume)`：

1. `resolved = resolve_image(image)`；`docker volume create <volume>`（已存在则忽略）。
2. 跑一个**初始化容器**，让 Docker 在卷为空时把镜像的 `/opt/operator` 复制进卷：

   ```text
   [docker, "run", "--rm", "--name", volume + "-init",
    "--network", "none",
    "--user", "10001:10001",
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
    "--mount", f"type=volume,source={volume},target=/opt/operator",
    resolved, "/bin/true"]
   ```

   卷为空时，Docker 在启动前把镜像里该路径的内容（含 `chmod 0755` 的 `fake-agent`）复制进卷。卷非空时不覆盖，所以重复调用是安全的。
3. 这个初始化容器同样禁止 `--privileged`、`--cap-add`、`--network host`、`docker.sock`。它不是 job 沙箱，可以没有 `--read-only`（卷要接收首次复制）。禁止 `--user root`：复制由守护进程在启动前完成，不需要 root。
4. 退出码不是 0：`DOCKER_FAILED`。
5. 调用方随后在 job 的 `SandboxSpec.mounts` 里放 `Mount(source="volume:"+volume, target=inject.mount, readonly=True)`。job 容器因此在 `/opt/operator` 上只读。

`LocalProvider.prepare_inject_volume` 什么都不做。测试靠 `commands` 把 `/opt/operator/bin/fake-agent` 映射到 `tests/fakes/fake_agent.py`（见 `testing.md`）。

### 5.6 `LocalProvider` 的路径映射

没有任何隔离：没有只读、没有网络限制、没有 cgroup。只做两件事。

**命令映射。** `exec` 的 `argv[0]` 先查映射，再做路径替换。默认映射：

| argv[0] | 替换成 |
|---|---|
| `python3` | `[sys.executable]` |
| `/opt/factory/bin/factoryctl` | `[sys.executable, str(root / "tools" / "factory" / "bin" / "factoryctl")]` |

构造参数 `commands` 里的键覆盖默认映射。值是参数列表，接到 argv 其余元素前面。`tests/fakes/fakelib.py` 的 `local_commands()` 就是这样把 `upper-text`、`count-text`、`check-task`、`/opt/operator/bin/fake-agent` 指到夹具脚本的。

**路径映射。** 从 `spec.mounts` 建立 `target → source`。另外把 `/tmp` 映射到 `root / "tmp" / "sandboxes" / spec.name / "tmp"`（不存在则创建，含 `home` 子目录）。按 target 长度从长到短替换 argv 和 env 值里的前缀：

- 值等于 target，或 target 后面接着 `/` 或 `\`，才替换；
- 替换结果是 `source + 剩余部分`，剩余部分的斜杠改成宿主的 `os.sep`。

因此 `FACTORY_IN=/factory/in` 在有对应 mount 时会变成该 mount 的 `source`。没有对应 mount 的虚拟路径保持原样（命令会失败，这是调用方的错）。

`exec` 的环境**不是**宿主环境的拷贝。只包含映射之后的沙箱 env，外加：

- Windows 上如果宿主有 `SYSTEMROOT`、`WINDIR`、`PATHEXT`、`COMSPEC`，原样拷贝这四个，否则解释器启动不了；
- `PYTHONIOENCODING=utf-8`。

工作目录是 `workdir`（缺省 `spec.workdir`）做同样的前缀替换。替换后的目录不存在则创建。

### 5.7 出网与资源

- M1 只实现 `none`（D12）。策略要求其他等级时，调用方必须把 `network` 设为 `"egress"`，于是 `build_run_argv` 抛 `EGRESS_UNSUPPORTED`，除非构造 `DockerProvider` 时 `allow_unproxied_egress=True`。
- 开关打开后使用 Docker 的默认桥接网络，没有代理、没有域名白名单、没有计量。禁止写成 `--network host`。
- `cpus`、`memory_mb` 来自调用方。本组件不读 Policy。不设置 GPU、不设置 `--cap-add`。
- `--pids-limit 256` 是基线的一部分，不能因为 Spec 没写就拿掉。

### 5.8 `docker exec`

```text
[docker, "exec",
 "--user", "10001:10001",
 "--workdir", <workdir>,
 # exec() 的 env 参数，键名升序，每个 "--env", "KEY=VALUE"
 spec.name,
 *argv]
```

- 不使用 `shell=True`。argv 原样作为 exec 的参数，不做路径替换（容器里就是虚拟路径）。
- 超时：`Popen.wait(timeout)`。超时后 `kill` 本地客户端，再 `docker kill <name>`（容器里的进程可能杀不掉，因为已经 `--cap-drop ALL`）。这次 exec 返回 `timed_out=True`。容器被 kill 之后，后续 `exec` 抛 `SANDBOX_CLOSED`；`close` 仍然 `docker rm -f`。
- `docker` 退出码 137 且我们没有主动超时：当作进程被外部杀掉，`exit_code=137`，`timed_out=False`。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `EGRESS_UNSUPPORTED` | `network="egress"` 且没有打开 `allow_unproxied_egress` | `network` |
| `SANDBOX_SPEC_INVALID` | 名字、挂载、workdir、资源、环境变量不合法，或挂载涉及 docker.sock | `field`，以及触发它的 `value`（若有） |
| `IMAGE_REF_INVALID` | 镜像字符串不是 4.1 的两种形式 | `image` |
| `IMAGE_NOT_FOUND` | 仓库 digest 和本地镜像 ID 都 inspect 不到 | `image` |
| `DOCKER_UNAVAILABLE` | 找不到 docker，或 `docker version` / inspect 超时 | `docker`、`error` |
| `DOCKER_FAILED` | `docker run` 或初始化容器退出码不是 0 | `argv`、`stderr`、`exit_code` |
| `SANDBOX_CLOSED` | `close` 之后再 `exec`，或超时 kill 之后再 `exec` | `name` |
| `VOLUME_REMOVE_FAILED` | `docker volume rm` 失败，且不是“卷不存在” | `volume`、`stderr` |

超时不是错误码：它是 `ExecResult.timed_out=True`。调用方（Runner）把它记成自己的 `OPERATOR_TIMEOUT`。

---

## 7. 验收测试

文件：`tests/acceptance/m1/test_m1_04_sandbox.py`。模块不存在时 `pytest.importorskip("mao.sandbox")` 跳过整个文件。

| 测试 | 验证 |
|---|---|
| `test_build_run_argv_locks_security_and_order` | 5.2：完整 argv 与规定顺序一致；含 `--cap-drop ALL`、`--read-only`、`--user 10001:10001`、`no-new-privileges`；不含 `docker.sock`、`--privileged`、`--cap-add`、`--network host` |
| `test_build_run_argv_rejects_egress` | 5.7：`EGRESS_UNSUPPORTED` |
| `test_build_run_argv_egress_switch_is_not_host` | 开关打开时网络是 `bridge`，安全标志仍在，仍然没有 `host` |
| `test_build_run_argv_rejects_docker_sock` | 挂载源含 `docker.sock` → `SANDBOX_SPEC_INVALID`，不返回 argv |
| `test_image_ref_invalid` | 5.3：`python:latest` → `IMAGE_REF_INVALID` |
| `test_local_provider_rewrites_virtual_paths` | 5.6：`FACTORY_IN` 被换成 mount 的 source；`python3` 走当前解释器 |
| `test_docker_exec_is_non_root_without_sock` | `@pytest.mark.docker`。真的 `open`/`exec`：`id -u` 是 `10001`，容器里没有 `/var/run/docker.sock` |

Docker 测试在没有守护进程时由 `tests/conftest.py` 跳过；`MAO_REQUIRE_DOCKER=1` 时改为失败（R13）。镜像未构建时测试失败并指向 `tools/build_test_images.py`，不跳过。

---

## 8. 错误示范与正确示范

**8.1 为了能写文件就提权**

```python
# ✖ 根文件系统只读，于是改成 root，安全基线被拆掉
argv += ["--user", "root"]
# ✔ 工作区用命名卷，镜像里该目录的属主是 10001（第 5.4 节）
spec = SandboxSpec(..., workspace_volume="mao-j-0001-ws")
```

**8.2 用 `-v` 挂 Windows 路径**

```python
# ✖ D:\factory\in:/factory/in:ro 在 Git Bash 与 Docker Desktop 上都会拆错
argv += ["-v", f"{host}:/factory/in:ro"]
# ✔ --mount，source 用绝对路径原样
argv += ["--mount", f"type=bind,source={host},target=/factory/in,readonly"]
```

**8.3 出网做成 host 网络**

```python
# ✖ “egress” 被写成 host，容器能看到宿主的所有网卡和 docker.sock 所在的命名空间
argv += ["--network", "host"]
# ✔ M1 拒绝 egress；开发开关也只给 bridge，并且仍然 --cap-drop ALL
raise FactoryError("EGRESS_UNSUPPORTED", "M1 不支持出网", network="egress")
```

**8.4 镜像只用标签**

```python
# ✖ mao-test/tool:dev 没有 digest，换一次构建内容就变了，Spec 锁不住
image = "mao-test/tool:dev"
# ✔ 先仓库 digest，再回退到本地镜像 ID（第 5.3 节）
image = resolve_image("mao-test/tool@sha256:" + local_id.removeprefix("sha256:"))
```

**8.5 把宿主环境整份拷进 LocalProvider**

```python
# ✖ 宿主的 API key 会进“沙箱”
env = os.environ.copy()
# ✔ 只用 SandboxSpec.env 映射之后的那一组，外加 Windows 启动解释器必需的四个变量
```

---

## 9. 风险与注意事项

- **R5 Windows。** `--mount source=` 不要用 `-v`。路径含逗号时直接 `SANDBOX_SPEC_INVALID`，不要试图转义。`LocalProvider` 替换前缀时把剩余部分的斜杠换成 `os.sep`。
- **R7 基线。** `build_run_argv` 是唯一组装 `docker run` 的地方。禁止在 `open` 里再拼一套 argv。初始化容器（5.5）也不得挂 docker.sock。
- **R13。** 验收命令设置 `MAO_REQUIRE_DOCKER=1`。不要在测试里 `pytest.skip` Docker；模块缺失只靠文件开头的 `importorskip`。
- **S3。** 绑定挂载在 Docker Desktop 上很慢。工作区用命名卷，输入输出才用绑定挂载。
- **S4。** 本地 `docker build` 的镜像没有 `RepoDigests`。必须先 inspect 仓库引用，再 inspect 镜像 ID。
- **超时与权限。** `--cap-drop ALL` 之后，容器里的进程杀不掉兄弟进程。超时由宿主 `docker kill` 整个容器，而不是在容器里 `kill -9`。
- **同名容器。** `open` 会 `rm -f` 同名容器。生产名字必须带 job id 和 attempt，避免删掉别人的容器。
- **时钟。** 等待子进程是真实等待（`Popen.wait`），不用 `clock.sleep` 去等 docker。`FakeClock` 不会让 `docker exec` 变快。
