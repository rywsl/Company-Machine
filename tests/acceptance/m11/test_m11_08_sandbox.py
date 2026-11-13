"""M11-08 验收：机器声明的沙箱资源与镜像构建网络缺省值（spec/system/capabilities.md 第 7 章）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.delegation")

from tests.acceptance.m11.deleg_world import World  # noqa: E402


def _spec(w: World, job_id: str):
    [spec] = [spec for spec in w.specs if spec.name == f"mao-{job_id}-1"]
    return spec


def test_resources_reach_docker_argv(tmp_path: Path) -> None:
    from mao.sandbox import DockerProvider

    w = World(tmp_path, record=True)
    job = w.run("desk-big@1")
    assert job.state.value == "SEALED", job.error
    spec = _spec(w, job.id)
    assert (spec.shm_mb, spec.tmp_mb, spec.pids, spec.writable) == (512, 128, 1024, ("/home/app",))
    argv = DockerProvider(tmp_path, docker="docker").build_run_argv(spec)
    assert argv[argv.index("--pids-limit") + 1] == "1024"
    assert "/tmp:rw,nosuid,size=128m" in argv
    writable = "/home/app:rw,nosuid,size=128m,mode=1777"
    assert argv[argv.index(writable) - 1] == "--tmpfs"
    assert argv[argv.index("--shm-size") + 1] == "512m"
    assert argv.index("/tmp:rw,nosuid,size=128m") < argv.index(writable) < argv.index("--shm-size")
    assert argv.index("--shm-size") < argv.index("--cpus")
    for banned in ("--privileged", "--cap-add"):
        assert banned not in argv

    plain = w.run("upper@1")
    argv = DockerProvider(tmp_path, docker="docker").build_run_argv(_spec(w, plain.id))
    assert argv[argv.index("--pids-limit") + 1] == "256"
    assert "/tmp:rw,nosuid,size=256m" in argv and argv.count("--tmpfs") == 1
    assert "--shm-size" not in argv


def test_invalid_resources_fail_job(tmp_path: Path) -> None:
    w = World(tmp_path)
    for station, field in (("desk-badres@1", "pids"), ("desk-badwritable@1", "writable")):
        job = w.run(station)
        assert job.state.value in ("FAILED", "DEAD"), station
        assert job.error["code"] == "RESOURCES_INVALID", job.error
        assert job.error["detail"]["field"] == field


def test_image_build_network_default(tmp_path: Path) -> None:
    from mao.images import ImageConfig, build_argv

    w = World(tmp_path)
    config = ImageConfig.load(w.root)
    assert config.network == "default"
    argv = build_argv(
        "docker",
        context=tmp_path / "ctx",
        tag="mao-local/x:1",
        iidfile=tmp_path / "x.iid",
        network=config.network,
        labels={},
    )
    assert argv[:6] == ["docker", "build", "--pull=false", "--network", "default", "--iidfile"]
    w.set_config("images", {"network": "none"})
    assert ImageConfig.load(w.root).network == "none"
