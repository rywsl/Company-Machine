"""M12-03：常驻实例的维护、恢复与命令行（spec/system/instances.md 3.4、5.5、5.6）。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from tests.acceptance.m12.instance_world import SCHEDULER, STATIONS, World, serve_config


def _state(w: World, station: str) -> Path:
    return w.root / "instances" / "state" / station / "main.txt"


def _wait_started(w: World, station: str, n: int) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        path = _state(w, station)
        if path.is_file() and path.read_text(encoding="utf-8") == f"started {n}":
            return
        time.sleep(0.05)
    raise AssertionError(f"{station} 没有第 {n} 次启动")


def test_reconcile_starts_resident(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        report = w.instances.reconcile()
        listen, reply = w.live("res-listen"), w.live("res-reply")
        assert (listen.state, reply.state) == ("ready", "ready")
        assert (listen.mode, listen.job, listen.failures) == ("resident", None, 0)
        assert sorted(report["started"]) == sorted([listen.id, reply.id])
        _wait_started(w, "res-listen", 1)
        again = w.instances.reconcile()
        assert again["started"] == []
        assert len(w.instances.list(station=w.key("res-listen"))) == 1
        assert sorted(e["station"] for e in w.events("instance.started")) == sorted(
            [w.key("res-listen"), w.key("res-reply")]
        )
        assert w.instance("hook-bean") is None
    finally:
        w.close()


def test_resident_restart_backoff(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        w.instances.reconcile()
        first = w.live("res-listen")
        _wait_started(w, "res-listen", 1)
        w.drop(first.id, "x1", {"exit": 3})
        w.result(first.id, "x1")
        w.until(lambda: w.instance("res-listen").state == "failed")
        failed = w.instance("res-listen")
        assert (failed.id, failed.failures) == (first.id, 1)
        assert [(e["instance"], e["code"]) for e in w.events("instance.failed")] == [
            (first.id, "INSTANCE_DIED")
        ]
        w.instances.reconcile()
        assert w.instance("res-listen").id == first.id

        w.clock.advance(5)
        w.instances.reconcile()
        second = w.live("res-listen")
        assert second.id != first.id and (second.state, second.failures) == ("ready", 1)
        _wait_started(w, "res-listen", 2)
        w.drop(second.id, "x2", {"exit": 3})
        w.result(second.id, "x2")
        w.until(lambda: w.instance("res-listen").state == "failed")
        assert w.instance("res-listen").failures == 2
        w.clock.advance(5)
        w.instances.reconcile()
        assert w.instance("res-listen").state == "failed"
        w.clock.advance(5)
        w.instances.reconcile()
        third = w.live("res-listen")
        assert third.state == "ready"
        _wait_started(w, "res-listen", 3)
        w.drop(third.id, "x3", {"exit": 3})
        w.result(third.id, "x3")
        w.until(lambda: w.instance("res-listen").state == "crashed")
        crashed = w.instance("res-listen")
        assert crashed.failures == 3
        assert [e["failures"] for e in w.events("instance.crashed")] == [3]
        wakes = w.rows("SELECT signal, subject FROM wakes WHERE signal = 'instance'")
        assert [(r["signal"], r["subject"]) for r in wakes] == [
            ("instance", f"instance:{w.key('res-listen')}")
        ]
        w.clock.advance(3600)
        w.instances.reconcile()
        assert w.live("res-listen").id == crashed.id
        assert w.live("res-listen").state == "crashed"
    finally:
        w.close()


def test_resident_replaced_on_change(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        w.instances.reconcile()
        old = w.live("res-listen")
        changed = STATIONS["res-listen"].replace("timeout: 60s", "timeout: 90s")
        assert changed != STATIONS["res-listen"]
        snapshot = w.commit({"stations/res-listen.yaml": changed}, "longer listen timeout")
        report = w.instances.reconcile(snapshot)
        rows = {r.id: r for r in w.instances.list(station=w.key("res-listen"))}
        [new_id] = [key for key in rows if key != old.id]
        assert rows[new_id].state == "ready" and rows[new_id].digest != old.digest
        assert rows[old.id].state == "stopped"
        assert new_id in report["started"] and old.id in report["stopped"]
        assert [e["reason"] for e in w.events("instance.stopped")] == ["replaced"]
        reply = w.live("res-reply")
        assert reply.state == "ready" and len(w.instances.list(station=w.key("res-reply"))) == 1
    finally:
        w.close()


def test_resident_state_persists(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        w.instances.reconcile()
        first = w.live("res-listen")
        _wait_started(w, "res-listen", 1)
        restarted = w.instances.restart(first.id)
        assert (restarted.state, restarted.failures) == ("failed", 0)
        w.instances.reconcile()
        second = w.live("res-listen")
        assert second.id != first.id and second.state == "ready"
        _wait_started(w, "res-listen", 2)
        assert (w.root / "instances" / "state" / "res-listen" / "count").read_text(encoding="utf-8") == "2"
    finally:
        w.close()


def test_resident_job_runs_in_instance(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        scheduler = w.scheduler()
        job_id = w.submit("res-reply")
        assert job_id not in scheduler.admit(running=[])
        w.instances.reconcile()
        inst = w.live("res-reply")
        _wait_started(w, "res-reply", 1)
        assert job_id in scheduler.admit(running=[])
        job = w.runner.run(job_id)
        assert job.state.value == "SEALED", job.error
        assert w.output(job) == {"state": "started 1", "instance": inst.id}
        after = w.live("res-reply")
        assert (after.id, after.state, after.jobs_served, after.job) == (inst.id, "ready", 1, None)
        assert [(e["job"], e["reset"]) for e in w.events("instance.released")] == [(job_id, False)]

        busy = w.submit("res-reply")
        w.set_state([busy], "RUNNING")
        waiting = w.submit("res-reply")
        assert waiting not in scheduler.admit(running=[busy])
    finally:
        w.close()


def test_recover_on_start(tmp_path: Path) -> None:
    from mao.instances import Instances

    w = World(tmp_path)
    fresh = None
    try:
        job = w.run("pool-work")
        assert job.state.value == "SEALED", job.error
        pooled = w.instance("pool-work")
        w.instances.reconcile()
        resident = w.live("res-listen")
        _wait_started(w, "res-listen", 1)
        fresh = Instances(w.ctx, w.snapshot, runner_for=lambda: w.runner)
        handled = fresh.recover()
        assert pooled.id in handled and resident.id in handled
        assert w.instance("pool-work").state == "stopped"
        assert "recovered" in [e["reason"] for e in w.events("instance.stopped")]
        assert (w.live("res-listen").id, w.live("res-listen").state) == (resident.id, "ready")
    finally:
        if fresh is not None:
            fresh.close()
        w.close()


def test_instance_cli(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        w.instances.reconcile()
        listen = w.live("res-listen")
        code, body, err = w.cli("instance", "list", "--station", w.key("res-listen"))
        assert code == 0, err
        assert [(r["id"], r["state"], r["mode"]) for r in body] == [(listen.id, "ready", "resident")]

        code, body, err = w.cli("instance", "stop", w.key("res-listen"))
        assert code == 0, err
        assert (body["id"], body["state"]) == (listen.id, "stopped")
        w.instances.reconcile()
        assert w.live("res-listen") is None
        assert [e["reason"] for e in w.events("instance.stopped")] == ["manual"]

        code, body, err = w.cli("instance", "restart", w.key("res-listen"))
        assert code == 0, err
        assert body["state"] == "failed" and body["failures"] == 0
        w.clock.advance(60)  # 命令行用真实时钟写 next_restart_at
        w.instances.reconcile()
        assert w.live("res-listen").state == "ready"

        code, body, err = w.cli("instance", "stop", "in-missing")
        assert code == 1
        assert body["error"]["code"] == "INSTANCE_NOT_FOUND"
    finally:
        w.close()


def test_serve_tick_reconciles(tmp_path: Path) -> None:
    from mao.daemon import Daemon, InlineJobExecutor

    w = World(tmp_path)
    (w.root / "scheduler.json").write_text(json.dumps(SCHEDULER), encoding="utf-8")
    daemon = Daemon(
        w.ctx, serve_config(), executor=InlineJobExecutor(lambda job_id: w.runner), epochs=w.epochs
    )
    daemon.start()
    try:
        report = daemon.tick()
        assert len(report.instances["started"]) == 2
        ready = {r.station for r in daemon.instances.list(state="ready")}
        assert ready == {w.key("res-listen"), w.key("res-reply")}
        assert daemon.tick().instances["started"] == []
    finally:
        for record in daemon.instances.list():
            if record.state != "stopped":
                daemon.instances.stop(record.id)
        daemon.shutdown(timeout_s=5)
        w.close()
