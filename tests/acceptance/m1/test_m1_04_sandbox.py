"""M1-04 验收：沙箱（spec/system/sandbox.md 第 7 章）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mao.sandbox")

from mao.errors import FactoryError  # noqa: E402
from mao.sandbox import DockerProvider, LocalProvider, Mount, SandboxSpec  # noqa: E402
from tests.fakes.fakelib import docker_image_ids  # noqa: E402

IMAGE = "registry.local/machines/toolbox@sha256:" + "ab" * 32
PATH_VALUE = "/opt/factory/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
FORBIDDEN = ("--privileged", "--cap-add", "-v", "--volume", "--rm")


def _spec(tmp: Path, **kwargs) -> SandboxSpec:
    base = dict(
        name="mao-j-0001-1",
        image=IMAGE,
        mounts=(
            Mount(source=str(tmp / "in"), target="/factory/in", readonly=True),
            Mount(source=str(tmp / "out"), target="/factory/out", readonly=False),
        ),
        env={"FACTORY_JOB_ID": "j-0001", "B_EXTRA": "2", "A_EXTRA": "1"},
    )
    base.update(kwargs)
    return SandboxSpec(**base)


def _assert_baseline(argv: list[str]) -> None:
    assert argv[argv.index("--user") + 1] == "10001:10001"
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert "--read-only" in argv
    assert argv[argv.index("--pids-limit") + 1] == "256"
    assert "host" not in argv
    assert not any("docker.sock" in item.lower() for item in argv)
    for flag in FORBIDDEN:
        assert flag not in argv, flag


def test_build_run_argv_locks_security_and_order(tmp_path: Path) -> None:
    spec = _spec(tmp_path, cpus=1, memory_mb=256, workspace_volume="mao-j-0001-ws")
    argv = DockerProvider(tmp_path, docker="docker").build_run_argv(spec)
    expected = [
        "docker", "run",
        "--name", "mao-j-0001-1",
        "--detach",
        "--init",
        "--user", "10001:10001",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--pids-limit", "256",
        "--network", "none",
        "--tmpfs", "/tmp:rw,nosuid,size=256m",
        "--cpus", "1",
        "--memory", "256m",
        "--mount", f"type=bind,source={tmp_path / 'in'},target=/factory/in,readonly",
        "--mount", f"type=bind,source={tmp_path / 'out'},target=/factory/out",
        "--mount", "type=volume,source=mao-j-0001-ws,target=/workspace",
        "--env", "FACTORY_IN=/factory/in",
        "--env", "FACTORY_OUT=/factory/out",
        "--env", "FACTORY_CTL=/factory/ctl",
        "--env", "FACTORY_WORKSPACE=/workspace",
        "--env", "FACTORYCTL=/opt/factory/bin/factoryctl",
        "--env", "HOME=/tmp/home",
        "--env", f"PATH={PATH_VALUE}",
        "--env", "A_EXTRA=1",
        "--env", "B_EXTRA=2",
        "--env", "FACTORY_JOB_ID=j-0001",
        "--workdir", "/workspace",
        IMAGE,
        "/bin/sh", "-c", "mkdir -p /tmp/home && exec sleep 2147483647",
    ]  # fmt: skip
    assert argv == expected
    _assert_baseline(argv)
    # 调用方不能改写契约变量
    hijack = DockerProvider(tmp_path).build_run_argv(_spec(tmp_path, env={"FACTORY_IN": "/etc"}))
    assert "FACTORY_IN=/etc" not in hijack
    assert hijack.count("FACTORY_IN=/factory/in") == 1


def test_build_run_argv_rejects_egress(tmp_path: Path) -> None:
    with pytest.raises(FactoryError) as ei:
        DockerProvider(tmp_path).build_run_argv(_spec(tmp_path, network="egress"))
    assert ei.value.code == "EGRESS_UNSUPPORTED"
    assert ei.value.detail["network"] == "egress"


def test_build_run_argv_egress_switch_is_not_host(tmp_path: Path) -> None:
    argv = DockerProvider(tmp_path, allow_unproxied_egress=True).build_run_argv(
        _spec(tmp_path, network="egress")
    )
    assert argv[argv.index("--network") + 1] == "bridge"
    assert argv.count("--network") == 1
    _assert_baseline(argv)


def test_build_run_argv_rejects_docker_sock(tmp_path: Path) -> None:
    for mount in (
        Mount(source="/var/run/docker.sock", target="/var/run/docker.sock", readonly=True),
        Mount(source=str(tmp_path / "Docker.Sock"), target="/x", readonly=True),
        Mount(source=str(tmp_path / "in"), target="/run/docker.sock", readonly=True),
    ):
        with pytest.raises(FactoryError) as ei:
            DockerProvider(tmp_path).build_run_argv(_spec(tmp_path, mounts=(mount,)))
        assert ei.value.code == "SANDBOX_SPEC_INVALID"
    with pytest.raises(FactoryError) as ei:
        DockerProvider(tmp_path).build_run_argv(_spec(tmp_path, name="bad name"))
    assert ei.value.code == "SANDBOX_SPEC_INVALID"
    assert ei.value.detail["field"] == "name"


def test_image_ref_invalid(tmp_path: Path) -> None:
    for image in ("python:latest", "mao-test/tool:dev", "repo@sha256:" + "AB" * 32):
        with pytest.raises(FactoryError) as ei:
            DockerProvider(tmp_path).build_run_argv(_spec(tmp_path, image=image))
        assert ei.value.code == "IMAGE_REF_INVALID"
        assert ei.value.detail["image"] == image
    with pytest.raises(FactoryError) as ei:
        DockerProvider(tmp_path, docker=str(tmp_path / "no-such-docker")).resolve_image("python:latest")
    assert ei.value.code == "IMAGE_REF_INVALID"
    assert DockerProvider(tmp_path).build_run_argv(_spec(tmp_path, image="sha256:" + "cd" * 32))


def test_local_provider_rewrites_virtual_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAO_LEAK_PROBE", "leaked")
    for name in ("in", "out", "ctl", "workspace"):
        (tmp_path / name).mkdir()
    mounts = (
        Mount(source=str(tmp_path / "in"), target="/factory/in", readonly=True),
        Mount(source=str(tmp_path / "out"), target="/factory/out", readonly=False),
        Mount(source=str(tmp_path / "ctl"), target="/factory/ctl", readonly=False),
        Mount(source=str(tmp_path / "workspace"), target="/workspace", readonly=False),
    )
    sandbox = LocalProvider(tmp_path).open(_spec(tmp_path, mounts=mounts))
    code = (
        "import os, sys; print(os.environ['FACTORY_IN']); print(sys.argv[1]); "
        "print(os.environ.get('MAO_LEAK_PROBE', '-')); print(sys.executable); print(os.getcwd())"
    )
    out, err = tmp_path / "o.txt", tmp_path / "e.txt"
    result = sandbox.exec(
        ["python3", "-c", code, "/factory/in/note/note.txt"], timeout_s=60, stdout=out, stderr=err
    )
    sandbox.close()
    assert result.exit_code == 0, err.read_text(encoding="utf-8", errors="replace")
    assert result.timed_out is False
    assert result.duration_s >= 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == str(tmp_path / "in")
    assert lines[1] == str(tmp_path / "in" / "note" / "note.txt")
    assert lines[2] == "-"
    assert os.path.normcase(lines[3]) == os.path.normcase(sys.executable)
    assert os.path.normcase(lines[4]) == os.path.normcase(str(tmp_path / "workspace"))
    with pytest.raises(FactoryError) as ei:
        sandbox.exec(["python3", "-c", "pass"], timeout_s=5, stdout=out, stderr=err)
    assert ei.value.code == "SANDBOX_CLOSED"


@pytest.mark.docker
def test_docker_exec_is_non_root_without_sock(tmp_path: Path) -> None:
    images = docker_image_ids()
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    spec = _spec(tmp_path, name="mao-acceptance-m1-04", image="mao-test/tool@" + images["tool"])
    sandbox = DockerProvider(tmp_path).open(spec)
    try:
        out, err = tmp_path / "o.txt", tmp_path / "e.txt"
        result = sandbox.exec(["id", "-u"], timeout_s=60, stdout=out, stderr=err)
        assert result.exit_code == 0, err.read_text(encoding="utf-8", errors="replace")
        assert out.read_text(encoding="utf-8").strip() == "10001"
        result = sandbox.exec(
            ["/bin/sh", "-c", "test ! -e /var/run/docker.sock && echo nosock"],
            timeout_s=60,
            stdout=out,
            stderr=err,
        )
        assert result.exit_code == 0
        assert out.read_text(encoding="utf-8").strip() == "nosock"
        result = sandbox.exec(["/bin/sh", "-c", "touch /etc/x"], timeout_s=60, stdout=out, stderr=err)
        assert result.exit_code != 0
    finally:
        sandbox.close()
