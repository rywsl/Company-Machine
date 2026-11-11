"""M5-07 验收：自治闭环（spec/system/autonomy.md 第 7 章）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mao.supervisor")
pytest.importorskip("mao.daemon")

from tests.acceptance.m2.world import seal_input  # noqa: E402
from tests.acceptance.m3.gov_world import (  # noqa: E402
    ROLLOUT,
    ROOT,
    changeset_doc,
    commit_change,
    constitution_edit,
    fixture_text,
    upsert,
)
from tests.acceptance.m5.auto_world import (  # noqa: E402
    REVIEW_ACTION,
    auto_world,
    commit_actions,
    pass_pipeline,
    propose_action,
    session_dir,
    supervisor,
    write_script,
)

DROP = ("stations/note-to-text.yaml", "lines/note-pipeline.yaml")
CANARY = dict(ROLLOUT, canary={"percent": 100, "min_runs": 1})
SCHEDULER = {
    "pools": {"cpu": None, "memory_mb": None, "gpu": None},
    "compartments": [],
    "breakers": [],
    "wip": [],
    "rates": [],
    "run_cap": None,
    "toxic_failures": None,
    "tick_s": None,
}
SERVE = {
    "tick_s": 0.0,
    "max_parallel_jobs": 2,
    "audit_every_s": None,
    "canary_every_s": None,
    "docker_timeout_s": 60.0,
    "supervise": True,
}


def _daemon(w, pipeline):
    from mao.daemon import Daemon, InlineJobExecutor, ServeConfig
    from mao.jobs import load_job
    from mao.runner import Runner

    (w.ctx.root / "scheduler.json").write_text(json.dumps(SCHEDULER), encoding="utf-8")
    config = ServeConfig(**dict(SERVE, supervise=False))

    def runner_for(job_id: str):
        return Runner(w.ctx, w.epochs.materialize(load_job(w.ctx.db, job_id).epoch))

    return Daemon(w.ctx, config, executor=InlineJobExecutor(runner_for), epochs=w.epochs, pipeline=pipeline)


def _until(step, done, limit: int = 30) -> bool:
    for _ in range(limit):
        if done():
            return True
        step()
    return done()


def test_missing_converter_loop(tmp_path: Path) -> None:
    from mao.foreman import ConversionStore, WakeQueue

    converter = {"stations/note-to-text.yaml": fixture_text("stations/note-to-text.yaml")}
    write_script(
        tmp_path,
        {
            "roles": {
                "foreman": commit_actions(converter, "add note-to-text converter")
                + [propose_action([upsert("Station", "note-to-text")], rollout=CANARY)],
                "reviewer": [REVIEW_ACTION],
            }
        },
    )
    w = auto_world(tmp_path, drop=DROP)
    pipeline = pass_pipeline(w)

    draft_text = {"lines/note-pipeline.yaml": fixture_text("lines/note-pipeline.yaml")}
    draft = commit_change(w.repo, draft_text)
    human = pipeline.propose(
        changeset_doc(w.base, [upsert("Line", "note-pipeline")]), draft, by="human:acceptance", timeout_s=60
    )
    assert pipeline.advance(human.id).state == "rejected"
    [request] = ConversionStore(w.ctx).list(status="open")
    assert (request.from_type, request.to_type) == ("note.raw@1", "note.text@1")

    daemon = _daemon(w, pipeline)
    daemon.start()
    sup = supervisor(w, pipeline)
    sup.start()

    report = daemon.tick()
    conversion = [wake for wake in WakeQueue(w.ctx).list(status="open") if wake.signal == "conversion"]
    assert [wake.subject for wake in conversion] == [request.id]
    assert conversion[0].id in report.wakes

    foreman = sup.run_once()
    assert foreman is not None and foreman.state == "ok", foreman and foreman.error
    assert (foreman.role, foreman.actor) == ("foreman", "fake-agent@1")
    [cs_id] = foreman.proposed

    def step() -> None:
        daemon.tick()
        sup.run_once()

    assert _until(step, lambda: pipeline.get(cs_id).state == "canary"), pipeline.get(cs_id).report
    assert pipeline.approvals(cs_id) == ["OperatorRuntime:fake-reviewer@1"]

    bean = seal_input(w.ctx, w.snapshot, "note.text@1")
    w.ctx.warehouse.label(bean, "line", "note-trigger", by="acceptance")
    assert _until(step, lambda: pipeline.get(cs_id).state == "active"), pipeline.get(cs_id).report
    cs = pipeline.get(cs_id)
    assert cs.proposer == "fake-agent@1"
    assert w.epochs.current() == cs.commit
    canary_runs = w.ctx.db.query("SELECT state FROM runs WHERE epoch = ?", (cs.commit,))
    assert canary_runs and all(row["state"] == "SUCCEEDED" for row in canary_runs)
    assert "Station:note-to-text@1" in w.epochs.materialize(cs.commit).keys("Station")

    resolved = next(item for item in ConversionStore(w.ctx).list() if item.id == request.id)
    assert resolved.status.value == "resolved"
    signals = {
        wake.signal: wake.status
        for wake in WakeQueue(w.ctx).list()
        if wake.signal in {"conversion", "review"}
    }
    assert signals == {"conversion": "done", "review": "done"}
    sessions = [(item.role, item.actor, item.state) for item in sup.sessions()]
    assert sessions == [("foreman", "fake-agent@1", "ok"), ("reviewer", "fake-reviewer@1", "ok")]

    again = commit_change(w.repo, draft_text, parent=w.epochs.current())
    retry = pipeline.propose(
        changeset_doc(w.epochs.current(), [upsert("Line", "note-pipeline")]),
        again,
        by="human:acceptance",
        timeout_s=60,
    )
    assert pipeline.advance(retry.id).state == "verified"
    sup.shutdown()
    daemon.shutdown(timeout_s=5)


def test_constitution_stays_human(tmp_path: Path) -> None:
    from mao.foreman import WakeQueue

    write_script(
        tmp_path,
        {
            "default": commit_actions(constitution_edit(), "relax the constitution")
            + [propose_action([upsert("Station", "upper")], allow_error=True)]
        },
    )
    w = auto_world(tmp_path)
    pipeline = pass_pipeline(w)
    WakeQueue(w.ctx).file("drift", "acceptance", {"id": "acceptance"})
    sup = supervisor(w, pipeline)
    sup.start()
    record = sup.run_once()
    sup.shutdown()
    assert record.state == "ok", record.error
    assert record.proposed == ()
    assert pipeline.list() == []
    saved = json.loads((session_dir(w, record.id) / "workspace" / "propose.json").read_text(encoding="utf-8"))
    assert saved[0]["isError"] is True
    assert json.loads(saved[0]["content"][0]["text"])["code"] in {
        "CONSTITUTION_FORBIDDEN",
        "FOREMAN_FORBIDDEN",
    }
    assert w.epochs.current() == w.base


def test_serve_once_hosts_supervisor(tmp_path: Path) -> None:
    from mao.foreman import WakeQueue
    from tests.fakes.fakelib import local_commands

    write_script(tmp_path, {"default": []})
    w = auto_world(tmp_path)
    config_path = w.ctx.root / "factory.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["serve"] = SERVE
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    (w.ctx.root / "scheduler.json").write_text(json.dumps(SCHEDULER), encoding="utf-8")
    commands = tmp_path / "local-commands.json"
    commands.write_text(json.dumps(local_commands()), encoding="utf-8")
    wake = WakeQueue(w.ctx).file("other", "serve-once", {"id": "serve-once"})
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mao",
            "--root",
            str(w.ctx.root),
            "--provider",
            "local",
            "--local-commands",
            str(commands),
            "--json",
            "serve",
            "--once",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        timeout=300,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["session"] is not None
    assert out["session"]["state"] == "ok"
    assert out["session"]["wake_id"] == wake.id
    rows = w.ctx.db.query("SELECT state, role FROM foreman_sessions")
    assert [(row["state"], row["role"]) for row in rows] == [("ok", "foreman")]
    assert WakeQueue(w.ctx).get(wake.id).status == "done"
