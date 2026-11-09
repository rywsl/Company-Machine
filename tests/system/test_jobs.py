"""M1-08 单元：mao.jobs（spec/system/runner.md 第 3 节）。模块缺失时整文件跳过。"""

from __future__ import annotations

import pytest

pytest.importorskip("mao.jobs")
pytest.importorskip("mao.db")
pytest.importorskip("mao.errors")

from mao.db import Database  # noqa: E402
from mao.errors import FactoryError  # noqa: E402
from mao.jobs import TERMINAL, JobState, load_job  # noqa: E402


def test_terminal_states() -> None:
    assert JobState.SEALED in TERMINAL
    assert JobState.DEAD in TERMINAL
    assert JobState.RUNNING not in TERMINAL
    assert JobState.FAILED not in TERMINAL


def test_state_values_match_spec() -> None:
    assert JobState.PENDING.value == "PENDING"
    assert JobState.AWAITING_APPROVAL.value == "AWAITING_APPROVAL"
    assert JobState.SEALED.value == "SEALED"
    assert JobState.REJECTED.value == "REJECTED"


def test_load_missing_job(tmp_path) -> None:
    db = Database(tmp_path / "factory.db")
    try:
        db.migrate()
        with pytest.raises(FactoryError) as ei:
            load_job(db, "j-missing")
        assert ei.value.code == "JOB_NOT_FOUND"
    finally:
        db.close()


def test_dump_roundtrip_state() -> None:
    from mao.jobs import Job, dump_job

    job = Job(
        id="j-0001",
        station="Station:upper@1",
        epoch="e",
        state=JobState.RUNNING,
        attempt=1,
        inputs={},
        params={},
        outputs={},
        cost_usd=0.0,
        error=None,
        feedback=None,
        created_at="2026-01-01T00:00:00.000Z",
        updated_at="2026-01-01T00:00:00.000Z",
    )
    body = dump_job(job)
    assert body["id"] == "j-0001"
    assert body["state"] == "RUNNING"


def test_rejected_is_not_terminal() -> None:
    assert JobState.REJECTED not in TERMINAL
    assert JobState.FAILED.value == "FAILED"
