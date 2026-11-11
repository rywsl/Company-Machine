"""M7-01 验收：状态、指标与事件流（spec/system/observability.md 第 7 章）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.status")

from tests.acceptance.m2.world import FIXTURE, open_factory  # noqa: E402
from tests.acceptance.m3.gov_world import insert_run, open_world  # noqa: E402

TODAY = "2026-11-10T08:00:00.000Z"
YESTERDAY = "2026-11-09T23:59:59.000Z"

METRICS = {
    "mao_up",
    "mao_jobs",
    "mao_runs",
    "mao_changesets",
    "mao_wakes",
    "mao_safe_mode",
    "mao_spend_usd_total",
    "mao_foreman_sessions_total",
    "mao_alerts",
}


class SafeOn:
    def safe_mode(self) -> dict:
        return {"on": True, "since": TODAY, "until": None, "level": 2}


def _job(ctx, job_id: str, state: str, *, at: str = TODAY, cost: float = 0.0) -> None:
    ctx.db.query(
        "INSERT INTO jobs (id, station, epoch, state, attempt, inputs, params, outputs, created_at, updated_at, "
        "cost_usd) VALUES (?, 'Station:upper@1', 'x', ?, 1, '{}', '{}', '{}', ?, ?, ?)",
        (job_id, state, at, at, cost),
    )


def _usage(ctx, job_id: str, at: str, cost: float) -> None:
    ctx.db.query(
        "INSERT INTO model_usage (job_id, at, model, cost_usd, tokens_in, tokens_out) VALUES (?, ?, 'm', ?, 1, 1)",
        (job_id, at, cost),
    )


def _session(ctx, session_id: str, at: str, cost: float, state: str = "ok") -> None:
    ctx.db.query(
        "INSERT INTO foreman_sessions (id, wake_id, role, runtime, actor, model, epoch, state, cost_usd, started_at) "
        "VALUES (?, 'wk-0001', 'foreman', 'OperatorRuntime:fake-agent@1', 'fake-agent@1', 'm', 'x', ?, ?, ?)",
        (session_id, state, cost, at),
    )


def _failed_wake(ctx) -> None:
    from mao.foreman import WakeQueue

    queue = WakeQueue(ctx)
    wake = queue.file("other", "broken", {"id": "broken"})
    assert queue.next(by="supervisor").id == wake.id
    queue.fail(wake.id, code="SESSION_FAILED", by="supervisor")


def _cli(root: Path, capsys, *args: str) -> tuple[int, str]:
    from mao.cli import main

    capsys.readouterr()
    code = main(["--root", str(root), *args])
    return code, capsys.readouterr().out


def test_status_counts(tmp_path: Path) -> None:
    from mao.foreman import WakeQueue
    from mao.status import factory_status

    w = open_world(tmp_path)
    for job_id, state in [
        ("j-1", "PENDING"),
        ("j-2", "RUNNING"),
        ("j-3", "SEALED"),
        ("j-4", "SEALED"),
        ("j-5", "DEAD"),
    ]:
        _job(w.ctx, job_id, state)
    insert_run(w.ctx, "r-1", epoch=w.base, state="RUNNING", created_at=TODAY)
    insert_run(w.ctx, "r-2", epoch=w.base, state="SUCCEEDED", created_at=TODAY)
    insert_run(w.ctx, "r-3", epoch=w.base, state="SUCCEEDED", created_at=TODAY)
    queue = WakeQueue(w.ctx)
    queue.file("other", "a", {"id": "a"})
    done = queue.file("other", "b", {"id": "b"})
    queue.done(done.id, by="supervisor")
    status = factory_status(w.ctx, epochs=w.epochs)
    assert status["jobs"] == {"DEAD": 1, "PENDING": 1, "RUNNING": 1, "SEALED": 2}
    assert list(status["jobs"]) == sorted(status["jobs"])
    assert status["runs"] == {"RUNNING": 1, "SUCCEEDED": 2}
    assert status["wakes"] == {"done": 1, "open": 1}
    assert status["changesets"] == {}
    assert status["epochs"]["active"] == w.base
    assert status["serve"]["online"] is False and status["supervisor"]["online"] is False
    assert status["at"].startswith("2026-11-10T")


def test_status_alerts_order(tmp_path: Path, capsys) -> None:
    from mao.status import factory_status

    w = open_world(tmp_path)
    _job(w.ctx, "j-dead", "DEAD")
    _failed_wake(w.ctx)
    status = factory_status(w.ctx, epochs=w.epochs, pipeline=SafeOn())
    assert [a["code"] for a in status["alerts"]] == ["SAFE_MODE", "WAKES_FAILED", "DLQ_NONEMPTY"]
    assert [a["severity"] for a in status["alerts"]] == ["error", "error", "warning"]
    assert status["alerts"][0]["detail"] == {"level": 2}
    assert status["alerts"][1]["detail"] == {"count": 1}
    assert status["alerts"][2]["detail"] == {"count": 1}
    code, out = _cli(w.ctx.root, capsys, "--json", "status")
    assert code == 1
    assert [a["code"] for a in json.loads(out)["alerts"]] == ["WAKES_FAILED", "DLQ_NONEMPTY"]


def test_status_without_governance(tmp_path: Path, capsys) -> None:
    from mao.status import factory_status

    ctx, _snapshot, _runner, _clock = open_factory(tmp_path, FIXTURE)
    _job(ctx, "j-dead", "DEAD")
    status = factory_status(ctx)
    assert status["epochs"] is None and status["safe_mode"] is None
    assert [a["code"] for a in status["alerts"]] == ["DLQ_NONEMPTY"]
    code, out = _cli(ctx.root, capsys, "--json", "status")
    assert code == 0
    assert json.loads(out)["jobs"] == {"DEAD": 1}


def test_spend_today(tmp_path: Path) -> None:
    from mao.status import factory_status

    w = open_world(tmp_path)
    _job(w.ctx, "j-model", "SEALED", cost=0.5)
    _usage(w.ctx, "j-model", TODAY, 0.5)
    _job(w.ctx, "j-old-model", "SEALED", at=YESTERDAY, cost=1.0)
    _usage(w.ctx, "j-old-model", YESTERDAY, 1.0)
    _job(w.ctx, "j-transcript", "SEALED", cost=0.25)
    _job(w.ctx, "j-old-transcript", "SEALED", at=YESTERDAY, cost=0.75)
    _session(w.ctx, "fs-0001", TODAY, 0.4)
    _usage(w.ctx, "fs-0001", TODAY, 0.4)
    _session(w.ctx, "fs-0002", YESTERDAY, 2.0)
    spend = factory_status(w.ctx, epochs=w.epochs)["spend"]
    assert spend["foreman_today_usd"] == 0.4
    assert spend["today_usd"] == 1.15


def test_metrics_text(tmp_path: Path) -> None:
    from mao.status import render_metrics

    w = open_world(tmp_path)
    for job_id, state in [("j-1", "SEALED"), ("j-2", "DEAD"), ("j-3", "SEALED"), ("j-4", "PENDING")]:
        _job(w.ctx, job_id, state)
    text = render_metrics(w.ctx, epochs=w.epochs)
    lines = text.splitlines()
    typed = {line.split()[2] for line in lines if line.startswith("# TYPE ")}
    helped = {line.split()[2] for line in lines if line.startswith("# HELP ")}
    assert typed == METRICS and helped == METRICS
    samples = [line for line in lines if line and not line.startswith("#")]
    assert {line.split("{")[0].split(" ")[0] for line in samples} <= METRICS
    assert [line for line in samples if line.startswith("mao_up")] == [
        'mao_up{component="serve"} 0',
        'mao_up{component="supervisor"} 0',
    ]
    assert [line for line in samples if line.startswith("mao_jobs")] == [
        'mao_jobs{state="DEAD"} 1',
        'mao_jobs{state="PENDING"} 1',
        'mao_jobs{state="SEALED"} 2',
    ]
    assert "mao_safe_mode 0" in samples
    assert 'mao_alerts{code="DLQ_NONEMPTY"} 1' in samples


def test_tail_events(tmp_path: Path) -> None:
    from mao.errors import FactoryError
    from mao.status import tail_events

    w = open_world(tmp_path)
    rows = [
        ("ev-t1", "2027-01-01T00:00:00.000Z", "job.state", {"to": "RUNNING"}),
        ("ev-t2", "2027-01-01T00:00:01.000Z", "foreman.wake", {"note": "token=abc123 leaked"}),
        ("ev-t3", "2027-01-01T00:00:02.000Z", "foreman.session", {"state": "ok"}),
        ("ev-t4", "2027-01-01T00:00:03.000Z", "serve.tick", {"tick": 1}),
    ]
    for event_id, at, kind, data in rows:
        w.ctx.db.query(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, NULL, ?)",
            (event_id, at, kind, json.dumps(data)),
        )
    picked = tail_events(w.ctx, since="2027-01-01T00:00:01.000Z", kind="foreman.")
    assert [e["id"] for e in picked] == ["ev-t2", "ev-t3"]
    assert "abc123" not in json.dumps(picked[0]["data"])
    assert "[REDACTED]" in picked[0]["data"]["note"]
    assert [e["id"] for e in tail_events(w.ctx, since="2027-01-01T00:00:00.000Z", limit=2)] == [
        "ev-t3",
        "ev-t4",
    ]
    assert [
        e["id"] for e in tail_events(w.ctx, since="2027-01-01T00:00:01.000Z", kind="foreman.", limit=1)
    ] == ["ev-t3"]
    for bad in ({"since": "yesterday"}, {"limit": 0}, {"limit": 10001}):
        with pytest.raises(FactoryError) as err:
            tail_events(w.ctx, **bad)
        assert err.value.code == "EVENTS_QUERY_INVALID"
