"""缓存命中发生在提交之前，不再求值 admit。"""

from __future__ import annotations

from pathlib import Path

from mao.runs import InlineExecutor, open_line_runner
from tests.acceptance.m14.admit_world import World


def test_cache_hit_does_not_reevaluate_admit(tmp_path: Path) -> None:
    world = World(tmp_path)
    car, firmware = world.car(), world.firmware()
    lines = open_line_runner(world.ctx, world.snapshot)
    executor = InlineExecutor(lines.runner)
    line = world.snapshot.key("Line", "vehicle-flash@1")
    first = lines.run_to_completion(lines.start_run(line, {"car": car, "firmware": firmware}).id, executor)
    assert first.state.value == "SUCCEEDED", first.error

    calls: list[str] = []
    original = world.snapshot.compiled

    def counting(subject: str):
        compiled = original(subject)
        if "/admit[" not in subject:
            return compiled

        class _Probe:
            def evaluate(self, env):
                calls.append(subject)
                return compiled.evaluate(env)

        return _Probe()

    world.snapshot.compiled = counting
    second = lines.run_to_completion(lines.start_run(line, {"car": car, "firmware": firmware}).id, executor)
    assert second.state.value == "SUCCEEDED", second.error
    flash = next(node for node in lines.nodes(second.id) if node.node_id == "flash")
    assert flash.cache_hit is True
    assert calls == []
