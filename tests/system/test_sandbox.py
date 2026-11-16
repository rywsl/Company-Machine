"""M1-04 单元：mao.sandbox。模块缺失时整文件跳过。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("mao.sandbox")
pytest.importorskip("mao.errors")

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


def test_provider_names(tmp_path) -> None:
    assert LocalProvider(tmp_path).name == "local"
    assert DockerProvider(tmp_path).name == "docker"


def test_argv_drops_capabilities(tmp_path) -> None:
    argv = DockerProvider(tmp_path).build_run_argv(_spec())
    assert "--cap-drop" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "--privileged" not in argv


def test_argv_is_read_only_and_not_root(tmp_path) -> None:
    argv = DockerProvider(tmp_path).build_run_argv(_spec())
    assert "--read-only" in argv
    user = argv[argv.index("--user") + 1]
    assert user not in {"0", "root", "0:0"}


def test_network_none(tmp_path) -> None:
    argv = DockerProvider(tmp_path).build_run_argv(_spec())
    assert argv[argv.index("--network") + 1] == "none"


def test_egress_rejected_without_switch(tmp_path) -> None:
    with pytest.raises(FactoryError) as ei:
        DockerProvider(tmp_path, allow_unproxied_egress=False).build_run_argv(_spec(network="egress"))
    assert ei.value.code == "EGRESS_UNSUPPORTED"


def test_mapped_host_paths_skip_virtual_rewrite(tmp_path: Path) -> None:
    """解释器和脚本保持宿主路径，虚拟参数仍按挂载替换。"""
    script = tmp_path / "tools" / "factory" / "bin" / "factoryctl"
    script.parent.mkdir(parents=True)
    script.write_text("import sys\nprint('ok')\nprint(sys.argv[1])\n", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "note.txt").write_text("hi", encoding="utf-8")
    provider = LocalProvider(
        tmp_path,
        commands={"tool": [sys.executable, str(script)]},
    )
    spec = SandboxSpec(
        name="keep-host",
        image="sha256:" + "ab" * 32,
        mounts=(Mount(source=str(workspace), target="/workspace", readonly=False),),
        command=(),
    )
    sandbox = provider.open(spec)
    out, err = tmp_path / "out.txt", tmp_path / "err.txt"
    try:
        result = sandbox.exec(["tool", "/workspace/note.txt"], timeout_s=30, stdout=out, stderr=err)
    finally:
        sandbox.close()
    assert result.exit_code == 0, err.read_text(encoding="utf-8", errors="replace")
    assert out.read_text(encoding="utf-8").splitlines() == ["ok", str(workspace / "note.txt")]
