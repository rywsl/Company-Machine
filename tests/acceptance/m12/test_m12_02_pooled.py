"""M12-02：pooled 实例的租借、reset、拷入拷出与准入（spec/system/instances.md 5.2–5.4）。"""

from __future__ import annotations

from pathlib import Path

from tests.acceptance.m12.instance_world import World


def test_pooled_reuses_instance(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        first = w.run("pool-work", text="first\n")
        second = w.run("pool-work", text="second\n")
        assert first.state.value == "SEALED", first.error
        assert second.state.value == "SEALED", second.error
        one, two = w.output(first), w.output(second)
        assert one["instance"] and one["instance"] == two["instance"]
        assert (one["resets"], two["resets"]) == (0, 1)
        assert one["residue"] == [] and two["residue"] == []
        assert two["input"] == "second\n"
        [record] = w.instances.list(station=w.key("pool-work"))
        assert (record.mode, record.state, record.jobs_served, record.job) == ("pooled", "ready", 2, None)
        assert record.sandbox == f"mao-inst-{record.id}"
        assert [e["instance"] for e in w.events("instance.started")] == [record.id]
        released = w.events("instance.released")
        assert [(e["job"], e["reset"]) for e in released] == [(first.id, True), (second.id, True)]
        assert [e["job"] for e in w.events("instance.leased")] == [first.id, second.id]
    finally:
        w.close()


def test_pooled_reset_failure_destroys(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        first = w.run("pool-fail")
        assert first.state.value == "SEALED", first.error
        [gone] = w.instances.list(station=w.key("pool-fail"))
        assert gone.state == "stopped"
        assert [e["reason"] for e in w.events("instance.stopped")] == ["reset"]
        failed = w.events("instance.failed")
        assert [(e["instance"], e["code"]) for e in failed] == [(gone.id, "SERVICE_FAILED")]
        second = w.run("pool-fail")
        assert second.state.value == "SEALED", second.error
        assert w.output(second)["instance"] != w.output(first)["instance"]
        assert len(w.instances.list(station=w.key("pool-fail"))) == 2
    finally:
        w.close()


def test_pooled_stage_in_out(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        job = w.run("pool-work", text="staged input\n")
        assert job.state.value == "SEALED", job.error
        body = w.output(job)
        assert body["input"] == "staged input\n"
        assert body["inputs"] == ["note"]
        assert (body["ctl_exit"], body["decision"]) == (0, "approve")
        assert body["job"] == job.id
        [record] = w.instances.list(station=w.key("pool-work"))
        host = w.root / "instances" / record.id / "job"
        for part in ("in", "out", "ctl"):
            assert list((host / part).iterdir()) == [], part
        attempt = w.root / "jobs" / job.id / "1"
        assert (attempt / "out" / "note" / "note.txt").is_file()
    finally:
        w.close()


def test_pooled_admission_cap(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        ones = [w.submit("pool-work") for _ in range(3)]
        twos = [w.submit("pool-two") for _ in range(3)]
        busy = [ones[0], twos[0]]
        w.set_state(busy, "RUNNING")
        chosen = w.scheduler().admit(running=busy)
        assert [j for j in chosen if j in ones] == []
        assert [j for j in chosen if j in twos] == [twos[1]]
    finally:
        w.close()


def test_pooled_idle_ttl(tmp_path: Path) -> None:
    w = World(
        tmp_path,
        instances={
            "health_every_s": 1,
            "idle_ttl_s": 120,
            "reset_timeout_s": 30,
            "backoff_base_s": 5,
            "backoff_max_s": 600,
        },
    )
    try:
        job = w.run("pool-work")
        assert job.state.value == "SEALED", job.error
        [record] = w.instances.list(station=w.key("pool-work"))
        w.clock.advance(60)
        w.instances.reconcile()
        assert w.instances.list(station=w.key("pool-work"))[0].state == "ready"
        w.clock.advance(61)
        report = w.instances.reconcile()
        assert record.id in report["stopped"]
        assert w.instances.list(station=w.key("pool-work"))[0].state == "stopped"
        assert [e["reason"] for e in w.events("instance.stopped")] == ["idle"]
    finally:
        w.close()


def test_exec_timeout_destroys_instance(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        job = w.run("pool-slow", {"sleep": "20"})
        assert job.state.value in ("FAILED", "DEAD"), job.state
        [record] = w.instances.list(station=w.key("pool-slow"))
        assert record.state == "stopped"
        assert [e["reason"] for e in w.events("instance.stopped")] == ["timeout"]
    finally:
        w.close()
