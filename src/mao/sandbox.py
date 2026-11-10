"""沙箱：Docker argv 契约与本地测试替身。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from mao.base import SystemClock
from mao.errors import FactoryError

_IMAGE = re.compile(r"^(?:.+@)?sha256:[0-9a-f]{64}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SOCK = "docker" + ".sock"
_PATH = "/opt/factory/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_FIXED_ENV = (
    ("FACTORY_IN", "/factory/in"),
    ("FACTORY_OUT", "/factory/out"),
    ("FACTORY_CTL", "/factory/ctl"),
    ("FACTORY_WORKSPACE", "/workspace"),
    ("FACTORYCTL", "/opt/factory/bin/factoryctl"),
    ("HOME", "/tmp/home"),
    ("PATH", _PATH),
)


@dataclass(frozen=True)
class Mount:
    source: str
    target: str
    readonly: bool


@dataclass(frozen=True)
class SandboxSpec:
    name: str
    image: str
    mounts: tuple[Mount, ...]
    workdir: str = "/workspace"
    env: Mapping[str, str] = field(default_factory=dict)
    network: str = "none"
    cpus: float | None = None
    memory_mb: int | None = None
    workspace_volume: str | None = None


@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None
    timed_out: bool
    duration_s: float


class DockerProvider:
    name = "docker"

    def __init__(
        self,
        root: Path,
        *,
        docker: str = "docker",
        allow_unproxied_egress: bool = False,
        clock: object | None = None,
    ) -> None:
        self.root = Path(root)
        self.docker = docker
        self.allow_unproxied_egress = allow_unproxied_egress
        self.clock = clock if clock is not None else SystemClock()

    def build_run_argv(self, spec: SandboxSpec) -> list[str]:
        self._validate(spec)
        network = "none"
        if spec.network == "egress":
            if not self.allow_unproxied_egress:
                raise FactoryError("EGRESS_UNSUPPORTED", "M1 不允许出网", station="", network=spec.network)
            network = "bridge"
        elif spec.network != "none":
            raise FactoryError("SANDBOX_SPEC_INVALID", "未知网络模式", field="network")
        argv = [
            self.docker,
            "run",
            "--name",
            spec.name,
            "--detach",
            "--init",
            "--user",
            "10001:10001",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--pids-limit",
            "256",
            "--network",
            network,
            "--tmpfs",
            "/tmp:rw,nosuid,size=256m",
        ]
        if spec.cpus is not None:
            argv.extend(["--cpus", str(spec.cpus if spec.cpus != 1 else "1")])
        if spec.memory_mb is not None:
            argv.extend(["--memory", f"{spec.memory_mb}m"])
        for mount in spec.mounts:
            flag = f"type=bind,source={mount.source},target={mount.target}"
            if mount.readonly:
                flag += ",readonly"
            argv.extend(["--mount", flag])
        if spec.workspace_volume:
            argv.extend(["--mount", f"type=volume,source={spec.workspace_volume},target=/workspace"])
        for key, value in _FIXED_ENV:
            argv.extend(["--env", f"{key}={value}"])
        for key in sorted(spec.env):
            if key in {item[0] for item in _FIXED_ENV}:
                continue
            argv.extend(["--env", f"{key}={spec.env[key]}"])
        argv.extend(
            [
                "--workdir",
                spec.workdir,
                spec.image,
                "/bin/sh",
                "-c",
                "mkdir -p /tmp/home && exec sleep 2147483647",
            ]
        )
        return argv

    def resolve_image(self, image: str) -> str:
        if _IMAGE.fullmatch(image) is None:
            raise FactoryError("IMAGE_REF_INVALID", "镜像引用不合法", image=image)
        if not shutil.which(self.docker) and not Path(self.docker).exists():
            raise FactoryError("DOCKER_UNAVAILABLE", "找不到 docker", docker=self.docker, error="not found")
        if self._inspect(image):
            return image
        if "@" in image:
            bare = image.split("@", 1)[1]
            if self._inspect(bare):
                return bare
        raise FactoryError("IMAGE_NOT_FOUND", "找不到镜像", image=image)

    def _inspect(self, image: str) -> bool:
        try:
            proc = subprocess.run(
                [self.docker, "image", "inspect", "--format", "{{.Id}}", image],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise FactoryError(
                "DOCKER_UNAVAILABLE", "docker inspect 超时", docker=self.docker, error=str(exc)
            ) from exc
        text = proc.stdout.decode("utf-8", errors="replace").strip()
        return proc.returncode == 0 and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", text))

    def prepare_inject_volume(self, image: str, volume: str) -> None:
        return None

    def open(self, spec: SandboxSpec) -> _DockerSandbox:
        resolved = self.resolve_image(spec.image)
        running = SandboxSpec(
            name=spec.name,
            image=resolved,
            mounts=spec.mounts,
            workdir=spec.workdir,
            env=spec.env,
            network=spec.network,
            cpus=spec.cpus,
            memory_mb=spec.memory_mb,
            workspace_volume=spec.workspace_volume,
        )
        argv = self.build_run_argv(running)
        proc = subprocess.run(argv, capture_output=True, timeout=120, check=False)
        if proc.returncode != 0:
            raise FactoryError(
                "DOCKER_FAILED",
                "docker run 失败",
                argv=argv,
                stderr=proc.stderr.decode("utf-8", errors="replace"),
                exit_code=proc.returncode,
            )
        return _DockerSandbox(self, running)

    def remove_volume(self, name: str) -> None:
        return None

    def _validate(self, spec: SandboxSpec) -> None:
        if _NAME.fullmatch(spec.name) is None:
            raise FactoryError("SANDBOX_SPEC_INVALID", "沙箱名字不合法", field="name")
        if _IMAGE.fullmatch(spec.image) is None:
            raise FactoryError("IMAGE_REF_INVALID", "镜像引用不合法", image=spec.image)
        for mount in spec.mounts:
            if _SOCK in mount.source.lower() or _SOCK in mount.target.lower():
                raise FactoryError("SANDBOX_SPEC_INVALID", "禁止挂载容器套接字", field="mounts")


class _DockerSandbox:
    def __init__(self, provider: DockerProvider, spec: SandboxSpec) -> None:
        self.provider = provider
        self.spec = spec
        self.name = spec.name
        self._closed = False

    def exec(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        stdout: Path,
        stderr: Path,
        env: Mapping[str, str] | None = None,
        workdir: str | None = None,
    ) -> ExecResult:
        if self._closed:
            raise FactoryError("SANDBOX_CLOSED", "沙箱已关闭", name=self.name)
        command = [
            self.provider.docker,
            "exec",
            "--user",
            "10001:10001",
            "--workdir",
            workdir or self.spec.workdir,
        ]
        for key in sorted(env or {}):
            command.extend(["--env", f"{key}={env[key]}"])
        command.append(self.name)
        command.extend(argv)
        start = self.provider.clock.monotonic()
        try:
            proc = subprocess.run(command, capture_output=True, timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired:
            subprocess.run([self.provider.docker, "kill", self.name], capture_output=True, check=False)
            self._closed = True
            Path(stdout).write_bytes(b"")
            Path(stderr).write_bytes(b"")
            return ExecResult(None, True, self.provider.clock.monotonic() - start)
        Path(stdout).write_bytes(proc.stdout)
        Path(stderr).write_bytes(proc.stderr)
        return ExecResult(proc.returncode, False, self.provider.clock.monotonic() - start)

    def close(self) -> None:
        subprocess.run([self.provider.docker, "rm", "-f", self.name], capture_output=True, check=False)
        self._closed = True


class LocalProvider:
    name = "local"

    def __init__(
        self, root: Path, *, commands: Mapping[str, Sequence[str]] | None = None, clock: object | None = None
    ) -> None:
        self.root = Path(root)
        self.commands = dict(commands or {})
        self.clock = clock if clock is not None else SystemClock()

    def open(self, spec: SandboxSpec) -> _LocalSandbox:
        return _LocalSandbox(self, spec)

    def remove_volume(self, name: str) -> None:
        return None

    def prepare_inject_volume(self, image: str, volume: str) -> None:
        return None


class _LocalSandbox:
    def __init__(self, provider: LocalProvider, spec: SandboxSpec) -> None:
        self.provider = provider
        self.spec = spec
        self.name = spec.name
        self._closed = False
        self._mounts = {mount.target: mount.source for mount in spec.mounts}

    def exec(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        stdout: Path,
        stderr: Path,
        env: Mapping[str, str] | None = None,
        workdir: str | None = None,
    ) -> ExecResult:
        if self._closed:
            raise FactoryError("SANDBOX_CLOSED", "沙箱已关闭", name=self.name)
        start = self.provider.clock.monotonic()
        command = [self._rewrite(arg) for arg in argv]
        if command and command[0] == "python3":
            command[0] = sys.executable
        child_env = {
            "FACTORY_IN": self._mounts.get("/factory/in", ""),
            "FACTORY_OUT": self._mounts.get("/factory/out", ""),
            "FACTORY_CTL": self._mounts.get("/factory/ctl", ""),
            "FACTORY_WORKSPACE": self._mounts.get("/workspace", ""),
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "COMSPEC": os.environ.get("COMSPEC", ""),
        }
        child_env = {key: value for key, value in child_env.items() if value}
        cwd = self._mounts.get(workdir or self.spec.workdir, self._mounts.get("/workspace"))
        try:
            proc = subprocess.run(
                command,
                cwd=cwd,
                env=child_env,
                timeout=timeout_s,
                check=False,
                capture_output=True,
            )
        except subprocess.TimeoutExpired:
            Path(stdout).write_bytes(b"")
            Path(stderr).write_bytes(b"")
            return ExecResult(None, True, self.provider.clock.monotonic() - start)
        Path(stdout).write_bytes(proc.stdout or b"")
        Path(stderr).write_bytes(proc.stderr or b"")
        return ExecResult(proc.returncode, False, self.provider.clock.monotonic() - start)

    def close(self) -> None:
        self._closed = True

    def _rewrite(self, arg: str) -> str:
        for target, source in self._mounts.items():
            if arg == target or arg.startswith(target + "/"):
                rest = arg[len(target) :].lstrip("/")
                return str(Path(source) / rest) if rest else str(source)
        return arg
