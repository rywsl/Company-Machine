"""M12-01：主进程、alive、instance 配置校验（spec/system/instances.md 3.1、4.1、5.1）。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from tests.acceptance.m12.instance_world import IMAGE, World

WRAP = ["/bin/sh", "-c", 'mkdir -p /tmp/home && exec "$@"', "sh"]
SLEEP = ["/bin/sh", "-c", "mkdir -p /tmp/home && exec sleep 2147483647"]


def _spec(tmp: Path, **kw):
    from mao.sandbox import Mount, SandboxSpec

    return SandboxSpec(
        name="mao-inst-in-0001",
        image=IMAGE,
        mounts=(Mount(source=str(tmp / "in"), target="/factory/in", readonly=True),),
        **kw,
    )


def test_run_argv_with_command(tmp_path: Path) -> None:
    from mao.sandbox import DockerProvider

    provider = DockerProvider(tmp_path, docker="docker")
    plain = provider.build_run_argv(_spec(tmp_path))
    assert plain[-len(SLEEP) :] == SLEEP
    assert plain[-len(SLEEP) - 1] == IMAGE
    with_cmd = provider.build_run_argv(_spec(tmp_path, command=("python3", "/app/listen.py", "--port", "80")))
    assert with_cmd[: -len(WRAP) - 4] == plain[: -len(SLEEP)]
    assert with_cmd[-len(WRAP) - 4 :] == [*WRAP, "python3", "/app/listen.py", "--port", "80"]


def test_instance_config_rules() -> None:
    from mao.errors import FactoryError
    from mao.instances import instance_config

    base = {"image": IMAGE}

    def machine(**runtime):
        return {"form": "tool", "runtime": {**base, **runtime}, "recipes": {}}

    assert instance_config(machine()).mode == "once"
    assert instance_config(machine(instance="per_job")).mode == "once"
    pooled = instance_config(machine(instance="pooled", reset="sh /app/reset.sh", pool_size=2))
    assert (pooled.mode, pooled.pool_size, pooled.reset) == ("pooled", 2, ("sh", "/app/reset.sh"))
    resident = instance_config(machine(instance="resident", command=["python3", "/app/m.py"]))
    assert (resident.mode, resident.command, resident.restart_max, resident.emit_per_min) == (
        "resident",
        ("python3", "/app/m.py"),
        5,
        60,
    )
    assert resident.health == () and resident.health_timeout_s == 30
    cases = {
        "instance": machine(instance="forever"),
        "reset-missing": machine(instance="pooled"),
        "reset-resident": machine(instance="resident", reset=["true"]),
        "pool_size": machine(instance="once", pool_size=2),
        "resident-only": machine(instance="pooled", reset=["true"], restart={"max": 2}),
        "argv": machine(instance="resident", command="python3 /app/m.py"),
        "reset-quote": machine(instance="pooled", reset="sh -c 'rm -rf /x'"),
    }
    for reason, spec in cases.items():
        with pytest.raises(FactoryError) as ei:
            instance_config(spec)
        assert (ei.value.code, ei.value.detail.get("reason")) == ("INSTANCE_INVALID", reason), reason
    with pytest.raises(FactoryError) as ei:
        instance_config(machine(instance="pooled", reset=["true"], pool_size=0))
    assert ei.value.detail.get("reason") == "pool_size"
    with pytest.raises(FactoryError) as ei:
        instance_config(machine(instance="pooled", reset=["true"], emit_per_min=10))
    assert ei.value.detail.get("reason") == "resident-only"


def test_local_command_alive(tmp_path: Path) -> None:
    from mao.base import FakeClock
    from mao.sandbox import LocalProvider, Mount, SandboxSpec

    script = tmp_path / "main.py"
    flag = tmp_path / "flag.txt"
    script.write_text(
        "import sys, time\nfrom pathlib import Path\nflag = Path(sys.argv[1])\n"
        "while not flag.exists():\n    time.sleep(0.05)\nsys.exit(0)\n",
        encoding="utf-8",
    )
    provider = LocalProvider(
        tmp_path / "root", commands={"main": [sys.executable, str(script)]}, clock=FakeClock()
    )
    work = tmp_path / "work"
    work.mkdir()
    spec = SandboxSpec(
        name="mao-inst-local",
        image=IMAGE,
        mounts=(Mount(source=str(work), target="/workspace", readonly=False),),
        command=("main", str(flag)),
    )
    sandbox = provider.open(spec)
    try:
        assert sandbox.alive() is True
        flag.write_text("stop", encoding="utf-8")
        deadline = time.monotonic() + 30
        while sandbox.alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sandbox.alive() is False
    finally:
        sandbox.close()

    flag.unlink()
    again = provider.open(spec)
    assert again.alive() is True
    again.close()
    assert again.alive() is False
    attached = provider.attach(spec)
    assert attached.alive() is True


def test_invalid_instance_job_dead(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        job = w.run("pool-bad")
        assert job.state.value == "DEAD"
        assert job.error["code"] == "INSTANCE_INVALID"
        assert job.error["detail"]["reason"] == "reset-missing"
        assert w.rows("SELECT id FROM instances") == []
    finally:
        w.close()
