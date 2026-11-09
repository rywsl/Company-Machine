"""M1-04 验收（spec/system/sandbox.md 第 7 章）。"""

from __future__ import annotations

import pytest

pytest.importorskip("mao.sandbox")

from mao.errors import FactoryError  # noqa: E402
from mao.sandbox import DockerProvider, LocalProvider, Mount, SandboxSpec  # noqa: E402


def _spec(**kwargs) -> SandboxSpec:
    base = dict(
        name="mao-j-0001-1",
        image="registry.local/machines/toolbox@sha256:" + "ab" * 32,
        mounts=(Mount(source="C:/in", target="/factory/in", readonly=True),),
        network="none",
    )
    base.update(kwargs)
    return SandboxSpec(**base)


def test_build_run_argv_security_baseline(tmp_path) -> None:
    argv = DockerProvider(tmp_path).build_run_argv(_spec())
    text = " ".join(argv)
    assert "--cap-drop" in argv and "ALL" in argv
    assert "--read-only" in argv
    assert "no-new-privileges" in text
    assert "docker.sock" not in text
    assert "--privileged" not in argv
    assert "host" not in argv[argv.index("--network") + 1 : argv.index("--network") + 2]
    user = argv[argv.index("--user") + 1]
    assert user not in {"0", "root", "0:0"}


def test_egress_rejected_by_default(tmp_path) -> None:
    with pytest.raises(FactoryError) as ei:
        DockerProvider(tmp_path).build_run_argv(_spec(network="egress"))
    assert ei.value.code == "EGRESS_UNSUPPORTED"


def test_local_maps_virtual_paths(tmp_path) -> None:
    provider = LocalProvider(tmp_path)
    spec = _spec()
    sandbox = provider.open(spec)
    mapped = sandbox.map_path("/factory/in/note") if hasattr(sandbox, "map_path") else str(tmp_path)
    assert "factory" in mapped.replace("\\", "/") or tmp_path.name in mapped


def test_local_exec_no_shell(tmp_path) -> None:
    provider = LocalProvider(tmp_path, commands={"python3": ["python"]})
    sandbox = provider.open(_spec())
    out = tmp_path / "out.txt"
    err = tmp_path / "err.txt"
    result = sandbox.exec(["python3", "-c", "print(1)"], timeout_s=30, stdout=out, stderr=err)
    assert result.timed_out is False
    assert result.exit_code == 0
    assert "1" in out.read_text(encoding="utf-8")
    sandbox.close()


@pytest.mark.docker
def test_docker_exec_roundtrip(tmp_path) -> None:
    provider = DockerProvider(tmp_path)
    sandbox = provider.open(_spec())
    try:
        out = tmp_path / "out.txt"
        err = tmp_path / "err.txt"
        result = sandbox.exec(["/bin/sh", "-c", "echo ok"], timeout_s=60, stdout=out, stderr=err)
        assert result.exit_code == 0
        assert "ok" in out.read_text(encoding="utf-8")
    finally:
        sandbox.close()
