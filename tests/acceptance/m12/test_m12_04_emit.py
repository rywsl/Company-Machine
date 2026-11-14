"""M12-04：常驻实例经 factoryctl emit 封存 Bean（spec/system/instances.md 3.3、5.7）。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from tests.acceptance.m12.instance_world import World


def _ready(w: World, station: str):
    w.instances.reconcile()
    inst = w.live(station)
    path = w.root / "instances" / "state" / station / "main.txt"
    deadline = time.monotonic() + 60
    while not path.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    return inst


def test_emit_seals_bean(tmp_path: Path) -> None:
    from mao.jobs import load_job
    from mao.runs import LineRunner, TriggerEngine

    w = World(tmp_path)
    try:
        inst = _ready(w, "res-listen")
        w.drop(inst.id, "e1", {"text": "pushed note\n", "labels": {"line": "note-trigger"}})
        result = w.result(inst.id, "e1")
        assert result["code"] == 0, result
        out = json.loads(result["stdout"])
        bean, job_id = out["bean"], out["job"]
        assert w.bean_text(bean) == "pushed note\n"
        job = load_job(w.ctx.db, job_id)
        assert job.state.value == "SEALED" and job.station == w.key("res-listen")
        found = job.outputs["note"]
        assert (found if isinstance(found, list) else [found]) == [bean]
        [row] = w.rows("SELECT instance, station, port, bean, trigger FROM emits")
        assert (row["instance"], row["station"], row["port"], row["bean"]) == (
            inst.id,
            w.key("res-listen"),
            "note",
            bean,
        )
        assert json.loads(row["trigger"]) == {"on": "emit", "instance": inst.id}
        assert w.ctx.warehouse.labels(bean) == {"line": "note-trigger"}
        assert [(e["bean"], e["job"]) for e in w.events("instance.emit")] == [(bean, job_id)]
        assert [e["job"] for e in w.events("job.adopted")] == [job_id]
        assert not (w.main_dir(inst.id) / "out" / "e1").exists()

        engine = TriggerEngine(w.ctx, w.snapshot, LineRunner(w.ctx, w.snapshot, w.runner, cache=None))
        assert len(engine.process()) == 1
    finally:
        w.close()


def test_emit_rejects_invalid(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        inst = _ready(w, "res-listen")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "note.txt").write_text("sneaky\n", encoding="utf-8")
        w.drop(inst.id, "p1", {"text": "x\n", "port": "nope"})
        w.drop(inst.id, "p2", {"dir": str(outside)})
        w.drop(inst.id, "p3", {"text": ""})
        for name, reason in (("p1", "port"), ("p2", "dir"), ("p3", "gate")):
            result = w.result(inst.id, name)
            assert result["code"] == 1, (name, result)
            assert result["stderr"].startswith("EMIT_INVALID"), (name, result)
            assert reason in result["stderr"], (name, result)
        assert w.rows("SELECT id FROM emits") == []
        assert w.events("instance.emit") == []
    finally:
        w.close()


def test_emit_rate_limit(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        inst = _ready(w, "res-listen")
        for n in range(4):
            w.drop(inst.id, f"r{n}", {"text": f"note {n}\n"})
            w.result(inst.id, f"r{n}")
        codes = [w.result(inst.id, f"r{n}")["code"] for n in range(4)]
        assert codes == [0, 0, 0, 1]
        assert w.result(inst.id, "r3")["stderr"].startswith("EMIT_RATE_LIMITED")
        w.clock.advance(61)
        w.drop(inst.id, "r4", {"text": "note 4\n"})
        assert w.result(inst.id, "r4")["code"] == 0
        assert len(w.rows("SELECT id FROM emits")) == 4
    finally:
        w.close()


def test_emit_requires_source_role(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        inst = _ready(w, "res-reply")
        w.drop(inst.id, "t1", {"text": "not a source\n"})
        result = w.result(inst.id, "t1")
        assert result["code"] == 1
        assert result["stderr"].startswith("EMIT_INVALID") and "role" in result["stderr"]
        assert w.rows("SELECT id FROM emits") == []
    finally:
        w.close()
