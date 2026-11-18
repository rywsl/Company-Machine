"""M17-04：两个真实 job 的池输出隔离、reset 失败销毁和旧租约拒绝。"""

from __future__ import annotations

from pathlib import Path

from mao.errors import FactoryError
from mao.jobs import load_job
from tests.acceptance.m12.instance_world import World


def test_pooled_jobs_isolate_outputs_and_reset_failures(tmp_path: Path) -> None:
    world = World(tmp_path)
    try:
        first = world.run("pool-work", text="canary-one\n")
        second = world.run("pool-work", text="canary-two\n")
        assert first.state.value == "SEALED", first.error
        assert second.state.value == "SEALED", second.error
        one, two = world.output(first), world.output(second)
        assert one["input"] == "canary-one\n"
        assert two["input"] == "canary-two\n"
        assert "canary-one" not in two["input"]
        assert one["residue"] == [] and two["residue"] == []
        assert one["instance"] == two["instance"]

        failed = world.run("pool-fail")
        assert failed.state.value == "SEALED", failed.error
        gone = world.instance("pool-fail")
        assert gone is not None and gone.state == "stopped"

        job = load_job(world.ctx.db, world.submit("pool-work"))
        spec = world.runner._spec_for_instance(job) if hasattr(world.runner, "_spec_for_instance") else None
        if spec is None:
            from mao.sandbox import SandboxSpec

            spec = SandboxSpec(name="late-handle", image="unused", mounts=())
        lease = world.instances.acquire(job, spec)
        canary = lease.host_dir / "job" / "out" / "canary.txt"
        canary.parent.mkdir(parents=True, exist_ok=True)
        canary.write_text("secret-canary", encoding="utf-8")
        world.instances.release(lease)
        assert not canary.exists()
        again = world.instances.acquire(load_job(world.ctx.db, world.submit("pool-work")), spec)
        try:
            lease.stage_out(tmp_path / "late")
        except FactoryError as exc:
            assert exc.code == "LEASE_LOST"
        else:
            raise AssertionError("old instance handle still staged output")
        world.instances.release(again)
    finally:
        world.close()
