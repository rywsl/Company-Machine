"""Job 状态、持久化与合法转换。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from mao.errors import FactoryError


class JobState(StrEnum):
    PENDING = "PENDING"
    PROVISIONING = "PROVISIONING"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    GATING = "GATING"
    SEALED = "SEALED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    DEAD = "DEAD"


TERMINAL = {JobState.SEALED, JobState.DEAD}

EDGES: dict[JobState, frozenset[JobState]] = {
    JobState.PENDING: frozenset({JobState.PROVISIONING, JobState.SEALED, JobState.DEAD}),
    JobState.PROVISIONING: frozenset({JobState.RUNNING, JobState.FAILED, JobState.DEAD}),
    JobState.RUNNING: frozenset(
        {JobState.AWAITING_APPROVAL, JobState.GATING, JobState.FAILED, JobState.DEAD}
    ),
    JobState.AWAITING_APPROVAL: frozenset({JobState.RUNNING, JobState.FAILED}),
    JobState.GATING: frozenset({JobState.SEALED, JobState.REJECTED, JobState.FAILED}),
    JobState.REJECTED: frozenset({JobState.PENDING, JobState.DEAD}),
    JobState.FAILED: frozenset({JobState.PENDING, JobState.DEAD}),
}


@dataclass(frozen=True)
class Job:
    id: str
    station: str
    epoch: str
    state: JobState
    attempt: int
    inputs: dict[str, list[str]]
    params: dict[str, Any]
    outputs: dict[str, list[str]]
    cost_usd: float
    error: dict[str, Any] | None
    feedback: str | None
    created_at: str
    updated_at: str


def load_job(db: Any, job_id: str) -> Job:
    rows = db.query("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not rows:
        raise FactoryError("JOB_NOT_FOUND", f"找不到 job {job_id}", job_id=job_id)
    return job_from_row(rows[0])


def dump_job(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "station": job.station,
        "epoch": job.epoch,
        "state": job.state.value,
        "attempt": job.attempt,
        "inputs": {port: list(ids) for port, ids in job.inputs.items()},
        "params": dict(job.params),
        "outputs": {port: list(ids) for port, ids in job.outputs.items()},
        "cost_usd": job.cost_usd,
        "error": None if job.error is None else dict(job.error),
        "feedback": job.feedback,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def job_from_row(row: Any) -> Job:
    try:
        state = JobState(row["state"])
    except ValueError as exc:
        raise FactoryError(
            "JOB_STATE_INVALID", "不认识的 job 状态", job_id=row["id"], state=row["state"]
        ) from exc
    return Job(
        id=row["id"],
        station=row["station"],
        epoch=row["epoch"],
        state=state,
        attempt=int(row["attempt"]),
        inputs=_load_ports(row["inputs"]),
        params=json.loads(row["params"]),
        outputs=_load_ports(row["outputs"]),
        cost_usd=float(row["cost_usd"]),
        error=None if row["error"] is None else json.loads(row["error"]),
        feedback=row["feedback"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def ensure_edge(job: Job, to: JobState) -> None:
    if to not in EDGES.get(job.state, frozenset()):
        raise FactoryError(
            "JOB_STATE_INVALID",
            f"不能从 {job.state.value} 转到 {to.value}",
            job_id=job.id,
            state=job.state.value,
        )


def _load_ports(text: str) -> dict[str, list[str]]:
    raw = json.loads(text)
    return {str(port): [str(item) for item in value] for port, value in raw.items()}
