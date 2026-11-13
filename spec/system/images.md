# images：从 Registry 构建机器镜像与操作工注入镜像（M6-02）

> 规范性文档，遵循 `spec/system/README.md`（下称 README）。任务：**M6-02**。
> 架构 11 节把“接入新产品（上游常常不提供 Docker，需要自己写 Dockerfile）”列为厂长的职责。但厂长在沙箱里，没有 docker.sock（D11），Spec 又要求镜像按 digest 固定。M1–M5 里没有任何路径能把厂长写的 Dockerfile 变成一个可以写进 Spec 的引用。本文件补上这条路径。
> `images.network` 的缺省值改为 `"default"`，见 `capabilities.md` 5.7（M11）。

---

## 1. 目的与范围

**做什么（M6-02）**

- `mao.images.ImageBuilder`：把 Registry 某个提交里的一个目录当作构建上下文，在宿主上 `docker build`，得到 `<repo>/<slug>@sha256:<镜像 ID>` 形式的引用（`sandbox.md` 5.3 的回退规则能解析它）。
- 上下文校验：位置、大小、基础镜像必须按 digest 固定。
- 按上下文摘要复用已有构建；每日构建次数上限。
- 记录：`images` 表、构建日志（打码）。
- MCP 工具 `image_build`（仅厂长）、命令 `factory image build|ls`。

**不做什么**

| 不做 | 归属 |
|---|---|
| 把构建出的引用写进 Spec | 厂长自己改 Registry，再经 ChangeSet 提交（T2：`Machine.runtime.image`） |
| 推送到远端镜像仓库、签名 | 不做。本地 Docker 引擎即仓库 |
| 让沙箱访问 docker.sock，或在沙箱里构建 | 禁止（D11） |
| 构建时的出网白名单 | 不做。构建要么不出网，要么由人在配置里放开（4.1） |

---

## 2. 依赖

| 依赖 | 用途 |
|---|---|
| `Context`（README 4.1） | 时钟、ids、数据库 |
| `EpochStore`（`governance.md`） | Registry 仓库路径、active |
| `DockerProvider.resolve_image`（`sandbox.md` 5.3） | 验证引用可以被沙箱使用 |
| `redact`（`supervisor.md` 3.4） | 构建日志打码 |
| `McpServer`、`Session.source_repo`（`mcp.md`、`supervisor.md` 5.4） | `image_build` 工具 |
| git、docker 命令行；`tarfile`（标准库） | 取上下文、构建 |

---

## 3. 接口

### 3.1 `mao.images`

```python
@dataclass(frozen=True)
class ImageConfig:
    repo: str                              # 引用的仓库前缀，缺省 "mao-local"
    network: str                           # "none" | "default"，缺省 "none"
    timeout_s: float                       # 缺省 1800
    max_context_mb: int                    # 缺省 200
    max_builds_per_day: int                # 缺省 20
    allow_unpinned_bases: tuple[str, ...]  # 缺省 ()
    @classmethod
    def load(cls, root: Path) -> "ImageConfig"   # factory.json 的 "images" 段，可省略

@dataclass(frozen=True)
class ImageBuild:
    id: str                    # "ib-0001"
    path: str                  # Registry 内的相对路径（POSIX）
    commit: str                # 40 位
    context_digest: str        # "sha256:<64 hex>"
    state: str                 # "ok" | "cached" | "failed"
    image_id: str | None       # "sha256:<64 hex>"
    ref: str | None            # "<repo>/<slug>@sha256:<64 hex>"
    log: str                   # 日志文件相对 $FACTORY_ROOT 的路径
    error: dict[str, Any] | None
    by: str
    at: str
    def to_json(self) -> dict[str, Any]

class ImageBuilder:
    def __init__(self, ctx: Context, epochs: EpochStore, config: ImageConfig, *,
                 docker: str = "docker") -> None
    def build(self, path: str, *, commit: str | None = None, by: str) -> ImageBuild
        # commit 缺省为 active。失败的构建也返回记录（state == "failed"），不抛 IMAGE_BUILD_FAILED；
        # 上下文不合法、超过上限在调用 docker 之前抛错
    def list(self) -> list[ImageBuild]          # 按 id 升序

def context_digest(path: Path) -> str
def dockerfile_bases(text: str) -> list[str]
def build_argv(docker: str, *, context: Path, tag: str, iidfile: Path, network: str,
               labels: Mapping[str, str]) -> list[str]
    # 纯函数
```

### 3.2 MCP 工具（`mcp.md` 3.3 的增补）

| 工具 | 参数 | 调用 | 角色 |
|---|---|---|---|
| `image_build` | `path`、`commit` | 先按 `supervisor.md` 5.4 取回提交，再 `ImageBuilder.build(path, commit=commit, by=session.actor)` | 仅 `foreman` |

成功（含 `failed` 状态的构建）时 `structuredContent.result` 为 `ImageBuild.to_json()` 加上 `log_tail`（日志最后 100 行，已打码）。上下文不合法、超过上限 → `isError`。

### 3.3 命令行

```
factory image build PATH [--commit SHA] [--json]
factory image ls [--json]
```

`build` 的 `by` 取 `$FACTORY_ACTOR`，没有则 `human:cli`。构建状态为 `failed` 时退出码 1。

---

## 4. 数据格式

### 4.1 `factory.json` 的 `images` 段（人维护，可省略）

```json
{"images": {"repo": "mao-local", "network": "none", "timeout_s": 1800, "max_context_mb": 200,
            "max_builds_per_day": 20, "allow_unpinned_bases": []}}
```

- `network: "default"` 让构建步骤使用 Docker 的缺省构建网络（可以下载依赖）。这是人对供应链风险的明确授权，缺省关闭。
- `allow_unpinned_bases`：允许不带 digest 的 `FROM` 镜像名的精确列表（例如本机构建的基础镜像）。生产应为空。
- 类型不对 → `IMAGE_CONFIG_INVALID`，`detail.field`。

### 4.2 迁移 `src/mao/migrations/033_images.sql`

```sql
CREATE TABLE images (
  id              TEXT PRIMARY KEY,
  path            TEXT NOT NULL,
  "commit"        TEXT NOT NULL,
  context_digest  TEXT NOT NULL,
  state           TEXT NOT NULL,
  image_id        TEXT,
  ref             TEXT,
  log             TEXT NOT NULL,
  error           TEXT,
  by              TEXT NOT NULL,
  at              TEXT NOT NULL
);
CREATE INDEX images_context ON images(context_digest, state);
```

### 4.3 上下文摘要

对上下文目录下每个普通文件（递归，按 POSIX 相对路径升序）写一行 `<相对路径>\t<x 或 ->\t<sha256 hex>\n`，第二列：文件系统上任一执行位为 1 时为 `x`，否则为 `-`；Windows 上恒为 `-`（复用只发生在同一台宿主上，跨平台不一致不影响正确性）。整段文本的 sha256 即 `context_digest`。不含目录本身、修改时间、属主。

### 4.4 引用与标签

- `slug`：`path` 把 `/` 换成 `-`，只保留 `[a-z0-9.-]`（其余字符换成 `-`），转小写。`machines/openmontage/build` → `machines-openmontage-build`。
- 标签：`<repo>/<slug>:ctx-<context_digest 的前 12 位 hex>`，方便人查看。
- 引用：`<repo>/<slug>@sha256:<镜像 ID 的 64 位 hex>`。本地构建的镜像没有仓库 digest，`resolve_image` 回退到镜像 ID（`sandbox.md` 5.3、S4）。

### 4.5 日志

`$FACTORY_ROOT/images/<build_id>.log`：docker 的 stdout 与 stderr 合并，逐行打码后写入。

---

## 5. 行为

### 5.1 取上下文

1. `path` 规范化为 POSIX 相对路径。为空、绝对路径、含 `..` 段、以 `constitution/` 开头或等于 `constitution` → `IMAGE_CONTEXT_INVALID`，`detail.reason = "path"`。
2. `git cat-file -e <commit>^{commit}` 失败 → `COMMIT_NOT_FOUND`。`git ls-tree -d <commit> -- <path>` 没有输出（目录不存在）→ `IMAGE_CONTEXT_INVALID`，`reason = "missing"`。然后 `git -C <registry> archive --format=tar <commit> -- <path>`。每条 git 命令的超时是 `governance.git_timeout_s`。
3. 用 `tarfile` 解到 `$FACTORY_ROOT/images/ctx/<build_id>/`。成员是符号链接、硬链接、设备文件，或解开后的路径跑出目标目录 → `IMAGE_CONTEXT_INVALID`，`reason = "link"`。解开时在 POSIX 上按 tar 成员的 mode 设置权限（git 记录的执行位因此进入构建上下文）。
4. 上下文根下没有 `Dockerfile` → `IMAGE_CONTEXT_INVALID`，`reason = "no-dockerfile"`。
5. 普通文件大小之和超过 `max_context_mb` → `IMAGE_CONTEXT_INVALID`，`reason = "too-large"`。

### 5.2 基础镜像固定

`dockerfile_bases(text)`：按行解析（忽略注释与续行后的空白），对每条 `FROM`（大小写不敏感）取镜像位置的词，跳过 `--platform=...` 这类选项；遇到 `AS name` 记下阶段名。返回镜像词列表（按出现顺序）。

每个镜像词必须满足其一，否则 `IMAGE_CONTEXT_INVALID`，`reason = "unpinned-base"`，`detail.base`：

- 匹配 `^.+@sha256:[0-9a-f]{64}$`；
- 是之前某个阶段的名字；
- 等于 `scratch`；
- 精确出现在 `allow_unpinned_bases` 里。

含 `${` 或 `$` 变量的镜像词（`FROM ${BASE}`）一律视为未固定。

### 5.3 复用与上限

1. 计算 `context_digest`。`images` 中已有同摘要、`state == "ok"` 的记录，且 `docker image inspect <image_id>` 成功 → 插入一条 `state == "cached"` 的新记录（`image_id`、`ref` 同原记录，日志写一行说明），返回它。不调用 `docker build`。
2. 最近 24 小时内 `state` 为 `ok` 或 `failed` 的记录数 `>= max_builds_per_day` → `IMAGE_BUILD_LIMITED`，`detail.count`。`cached` 不计数。

### 5.4 构建

1. `argv = build_argv(docker, context=ctx_dir, tag=..., iidfile=ctx_dir.parent / f"{build_id}.iid", network=config.network, labels={"mao.context": digest, "mao.path": path, "mao.commit": commit})`：

   ```text
   [docker, "build", "--pull=false", "--network", network, "--iidfile", iidfile,
    "--label", "mao.commit=<commit>", "--label", "mao.context=<digest>", "--label", "mao.path=<path>",
    "-t", tag, "-f", <ctx>/Dockerfile, <ctx>]
   ```

   标签按键名升序。`network` 为 `none` 或 `default`。argv 里**不得**出现 `--secret`、`--ssh`、`--allow`、`--build-arg`、`--network host`、`--privileged`。
2. 以参数列表执行，不经 shell，超时 `timeout_s`。stdout、stderr 逐行打码写日志（4.5）。
3. 退出码为 0 且 iid 文件内容匹配 `^sha256:[0-9a-f]{64}$` → `state = "ok"`，`image_id` 为其内容，`ref` 按 4.4。否则 `state = "failed"`，`error = {"code": "IMAGE_BUILD_FAILED", "detail": {"exit_code": ..., "timed_out": ...}}`。
4. 插入 `images` 行，写事件 `image.built`（`{id, path, state, ref}`）。删除解开的上下文目录。返回记录。

### 5.5 厂长的使用方式

1. 在会话克隆里写 `machines/<name>/build/Dockerfile`（基础镜像按 digest 固定），提交。
2. 调用 `image_build`（`path` 为该目录，`commit` 为自己的提交），拿到 `ref`。
3. 把 `ref` 写进 `machines/<name>/machine.yaml` 的新版本，提交，`changeset_propose`。镜像变更是 T2（`risk.mrl`：`Machine.runtime.image`），要过 V4 conformance、交叉复核和金丝雀。

构建本身不改变任何 epoch。构建出的镜像没被 Spec 引用之前，对工厂没有影响。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `IMAGE_CONFIG_INVALID` | `images` 段类型不对 | `field` |
| `IMAGE_CONTEXT_INVALID` | 5.1、5.2 的任一条 | `reason`（`path` / `missing` / `link` / `no-dockerfile` / `too-large` / `unpinned-base`）、`base` |
| `IMAGE_BUILD_LIMITED` | 超过每日构建次数 | `count` |
| `IMAGE_BUILD_FAILED` | `docker build` 失败或超时（写在记录的 `error` 里，不抛出） | `exit_code, timed_out` |

---

## 7. 验收测试

需要 Docker 与测试镜像 `mao-test/tool:dev`（`tools/build_test_images.py`）。验收命令设置 `MAO_REQUIRE_DOCKER=1`。治理夹具 `tests/acceptance/m3/gov_world.py`；构建上下文由测试提交进夹具 Registry 的副本，`allow_unpinned_bases` 设为 `["mao-test/tool:dev"]`。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m6/test_m6_02_images.py` | `test_build_argv_is_fixed` | `build_argv` 的形状与 5.4 第 1 条一致；没有任何禁止的参数 |
| | `test_context_digest_stable` | 修改时间不同、内容相同 → 摘要相同；内容变化 → 摘要不同；格式为 `sha256:<64 hex>` |
| | `test_unpinned_base_rejected` | `FROM python:3.12-alpine`、`FROM ${BASE}` → `unpinned-base`，没有调用 docker；`allow_unpinned_bases` 中的名字、带 digest 的引用、阶段名、`scratch` 通过 |
| | `test_context_path_rejected` | `../x`、绝对路径、`constitution`、不存在的路径、没有 Dockerfile 的目录分别得到 5.1 的 `reason` |
| | `test_build_and_reuse` | 第一次 `ok`，引用匹配 4.4，`resolve_image(ref)` 得到镜像 ID；同一上下文第二次 `cached`、镜像 ID 相同、`docker build` 只跑了一次；日志文件存在 |
| | `test_build_failure_recorded` | `RUN exit 3` → 记录 `failed`、`IMAGE_BUILD_FAILED`、`exit_code != 0`；`factory image build` 退出码 1 |
| | `test_daily_limit` | `max_builds_per_day = 1`：第二个不同的上下文 → `IMAGE_BUILD_LIMITED`；复用不计数 |
| | `test_mcp_image_build` | 厂长会话（`source_repo` 为克隆）调用 `image_build`：取回克隆里的提交并构建成功；复核者的工具清单里没有 `image_build` |

```
uv run python tools/harness.py pytest tests/acceptance/m6/test_m6_02_images.py
```

断言：`out.failed == 0 && out.errors == 0 && out.skipped == 0 && out.passed >= 8`。

---

## 8. 错误示范与正确示范

```python
# ✖ 给厂长的沙箱挂 docker.sock，让它自己 docker build
mounts.append(Mount("/var/run/docker.sock", "/var/run/docker.sock"))
# ✔ 厂长提交 Dockerfile，宿主按固定的 argv 构建
ImageBuilder(ctx, epochs, config).build("machines/x/build", commit=sha, by=session.actor)
```

```python
# ✖ 基础镜像用标签，今天和明天构建出来的不是同一个东西
FROM node:22
# ✔
FROM node:22-bookworm-slim@sha256:<64 hex>
```

```python
# ✖ 用 docker build 的输出文本里找 "Successfully built" 取镜像 ID
# ✔ --iidfile
```

```python
# ✖ 构建成功就自动改 machine.yaml 并合入
# ✔ 构建只产出引用；改 Spec 走 ChangeSet（T2）
```

---

## 9. 风险与注意事项

- **供应链**：`network: "default"` 时构建步骤可以下载任意内容。基础镜像固定只锁住了起点。需要更强保证时（例如只允许内部镜像源），由人改本文件。
- **磁盘**：每次不同的上下文都会产生一个镜像。清理随 M7 的保留策略（`retention.md`）。
- **BuildKit 与本地镜像**：`--pull=false` 时本地已有的基础镜像不会被重新拉取。`allow_unpinned_bases` 只应列出本机构建的镜像。
- **Windows**：上下文路径用 `pathlib`；tar 成员名一律按 POSIX 解释；执行位取 tar 里的 mode。
