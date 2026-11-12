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
        network_argv = self._network_argv(spec)
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
            *network_argv,
            "--tmpfs",
            "/tmp:rw,nosuid,size=256m",
        ]
        if spec.cpus is not None:
            argv.extend(["--cpus", str(spec.cpus if spec.cpus != 1 else "1")])
        if spec.memory_mb is not None:
            argv.extend(["--memory", f"{spec.memory_mb}m"])
        for mount in spec.mounts:
            if mount.source.startswith("volume:"):
                flag = f"type=volume,source={mount.source.split(':', 1)[1]},target={mount.target}"
            else:
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
        found = self._inspect_id(image)
        if found:
            digest = image.split("@", 1)[1] if "@" in image else ""
            if digest == found:
                return found
            return image
        if "@" in image:
            bare = image.split("@", 1)[1]
            if self._inspect(bare):
                return bare
        raise FactoryError("IMAGE_NOT_FOUND", "找不到镜像", image=image)

    def _inspect(self, image: str) -> bool:
        return self._inspect_id(image) is not None

    def _inspect_id(self, image: str) -> str | None:
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
        if proc.returncode == 0 and re.fullmatch(r"sha256:[0-9a-f]{64}", text):
            return text
        return None

    def prepare_inject_volume(self, image: str, volume: str) -> None:
        resolved = self.resolve_image(image)
        self._volume_create(volume)
        argv = [
            self.docker,
            "run",
            "--rm",
            "--name",
            volume + "-init",
            "--network",
            "none",
            "--user",
            "10001:10001",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=volume,source={volume},target=/opt/operator",
            resolved,
            "/bin/true",
        ]
        proc = subprocess.run(argv, capture_output=True, timeout=120, check=False)
        if proc.returncode != 0:
            raise FactoryError(
                "DOCKER_FAILED",
                "操作工卷初始化失败",
                argv=argv,
                stderr=proc.stderr.decode("utf-8", errors="replace")[:4000],
                exit_code=proc.returncode,
            )

    def _volume_create(self, name: str) -> None:
        proc = subprocess.run(
            [self.docker, "volume", "create", name],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            raise FactoryError(
                "DOCKER_FAILED",
                "docker volume create 失败",
                argv=[self.docker, "volume", "create", name],
                stderr=proc.stderr.decode("utf-8", errors="replace")[:4000],
                exit_code=proc.returncode,
            )

    def reap(self, job_id: str, attempt: int) -> int:
        name = f"mao-{job_id}-{attempt}"
        removed = 0
        for argv in (
            [self.docker, "rm", "-f", name],
            [self.docker, "network", "rm", name],
        ):
            try:
                proc = subprocess.run(argv, capture_output=True, timeout=60, check=False)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if proc.returncode == 0:
                removed += 1
        return removed

    def open(self, spec: SandboxSpec) -> _DockerSandbox:
        if spec.network == "egress" and not self.allow_unproxied_egress and not self._proxy_ready(spec):
            raise FactoryError("EGRESS_UNSUPPORTED", "出网代理没有启动", station=spec.name)
        resolved = self.resolve_image(spec.image)
        if spec.workspace_volume:
            self._volume_create(spec.workspace_volume)
        subprocess.run([self.docker, "rm", "-f", spec.name], capture_output=True, timeout=60, check=False)
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

    def remove_volume(self, name: str) -> bool:
        proc = subprocess.run(
            [self.docker, "volume", "rm", name],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if proc.returncode == 0:
            return True
        err = proc.stderr.decode("utf-8", errors="replace")
        if "no such volume" in err.lower():
            return False
        raise FactoryError("VOLUME_REMOVE_FAILED", "删除卷失败", name=name, stderr=err)

    def _network_argv(self, spec: SandboxSpec) -> list[str]:
        if spec.network == "none":
            return ["--network", "none"]
        if spec.network != "egress":
            raise FactoryError("SANDBOX_SPEC_INVALID", "未知网络模式", field="network")
        if self.allow_unproxied_egress:
            return ["--network", "bridge"]
        if self._proxy_ready(spec):
            from mao.egress import egress_run_args

            argv = []
            endpoints = getattr(self, "proxies", None)
            alias = getattr(endpoints, "alias", None) or getattr(self, "egress_alias", None)
            if alias:
                argv.extend([f"--add-host=egress:{alias}", f"--add-host=model-proxy:{alias}"])
            argv.extend(egress_run_args("egress"))
            return argv
        raise FactoryError("EGRESS_UNSUPPORTED", "M1 不允许出网", station="", network=spec.network)

    def _proxy_ready(self, spec: SandboxSpec) -> bool:
        raw = str(spec.env.get("HTTP_PROXY") or spec.env.get("http_proxy") or "")
        if "://" not in raw:
            return False
        port = raw.rsplit(":", 1)[-1]
        if not port.isdigit():
            return False
        from mao.egress import proxy_online

        return proxy_online(str(getattr(self, "proxy_host", "127.0.0.1")), int(port))

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

    def remove_volume(self, name: str) -> bool:
        del name
        return False

    def prepare_inject_volume(self, image: str, volume: str) -> None:
        return None

    def reap(self, job_id: str, attempt: int) -> int:
        del job_id, attempt
        return 0


class _LocalSandbox:
    def __init__(self, provider: LocalProvider, spec: SandboxSpec) -> None:
        self.provider = provider
        self.spec = spec
        self.name = spec.name
        self._closed = False
        tmp = provider.root / "tmp" / "sandboxes" / spec.name / "tmp"
        (tmp / "home").mkdir(parents=True, exist_ok=True)
        mounts = {mount.target: mount.source for mount in spec.mounts}
        mounts["/tmp"] = str(tmp)
        self._mounts = tuple(sorted(mounts.items(), key=lambda item: len(item[0]), reverse=True))

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
        command = [self._rewrite(arg) for arg in self._map_argv(argv)]
        child_env = self._child_env(env)
        cwd = self._cwd(workdir)
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

    def _map_argv(self, argv: Sequence[str]) -> list[str]:
        if not argv:
            return []
        head = argv[0]
        mapped = self.provider.commands.get(head)
        if mapped is None and head == "python3":
            mapped = [sys.executable]
        if mapped is None and head == "/opt/factory/bin/factoryctl":
            script = self.provider.root / "tools" / "factory" / "bin" / "factoryctl"
            mapped = [sys.executable, str(script)]
        if mapped is None:
            return list(argv)
        return [str(item) for item in mapped] + list(argv[1:])

    def _child_env(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        merged: dict[str, str] = {}
        merged.update({key: str(value) for key, value in self.spec.env.items()})
        if extra:
            merged.update({key: str(value) for key, value in extra.items()})
        merged.update(
            {
                "FACTORY_IN": "/factory/in",
                "FACTORY_OUT": "/factory/out",
                "FACTORY_CTL": "/factory/ctl",
                "FACTORY_WORKSPACE": "/workspace",
                "FACTORYCTL": "/opt/factory/bin/factoryctl",
                "HOME": "/tmp/home",
                "PATH": _PATH,
            }
        )
        child = {key: self._rewrite(value) for key, value in merged.items()}
        if os.name == "nt":
            for name in ("SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC"):
                host = os.environ.get(name)
                if host:
                    child[name] = host
        child["PYTHONIOENCODING"] = "utf-8"
        git_bin = shutil.which("git")
        if git_bin:
            child["PATH"] = str(Path(git_bin).parent) + os.pathsep + child.get("PATH", "")
        return child

    def _cwd(self, workdir: str | None) -> str | None:
        raw = workdir or self.spec.workdir
        rewritten = self._rewrite(raw)
        if rewritten == raw and raw.startswith("/"):
            return None
        path = Path(rewritten)
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _rewrite(self, arg: str) -> str:
        for target, source in self._mounts:
            if arg == target or arg.startswith(target + "/") or arg.startswith(target + "\\"):
                rest = arg[len(target) :].lstrip("/\\").replace("/", os.sep).replace("\\", os.sep)
                return str(Path(source) / rest) if rest else str(source)
        return arg
