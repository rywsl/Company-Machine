"""稳定运行身份与结果查询（spec/system/continuity.md 第 3.3、5.5 节）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from mao.base import canonical_json
from mao.errors import FactoryError


class ResultStore:
    def __init__(self, ctx: Any, epochs: Any) -> None:
        self.ctx = ctx
        self.epochs = epochs

    def get(self, run_id: str) -> dict[str, Any]:
        from mao.runs import open_line_runner

        snapshot = self.epochs.materialize(self.epochs.current())
        lines = open_line_runner(self.ctx, snapshot)
        run = lines.get(run_id)
        nodes = []
        for node in lines.nodes(run_id):
            nodes.append(
                {
                    "node_id": node.node_id,
                    "job_id": node.job_id,
                    "state": node.state.value,
                    "outputs": dict(node.outputs),
                    "error": node.error,
                    "cache_hit": node.cache_hit,
                }
            )
        seq = self.ctx.db.query("SELECT COALESCE(MAX(seq), 0) AS n FROM events")
        receipts: list[str] = []
        return {
            "id": run.id,
            "line": run.line,
            "epoch": run.epoch,
            "state": run.state.value,
            "trigger": dict(run.trigger),
            "nodes": nodes,
            "outputs": dict(run.outputs),
            "error": run.error,
            "budget": {
                "cap_usd": run.budget_cap_usd,
                "spent_usd": run.spent_usd,
                "reserved_usd": run.reserved_usd,
            },
            "receipts": receipts,
            "as_of_seq": int(seq[0]["n"]) if seq else 0,
        }

    def start(
        self,
        agent: str,
        line: str,
        inputs: Mapping[str, str | Sequence[str]],
        *,
        request_key: str,
        cycle_id: str | None = None,
        task_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        del turn_id
        if (cycle_id is None) != (task_id is None):
            raise FactoryError(
                "RUN_REQUEST_CONFLICT", "周期和任务必须同时提供", agent=agent, request_key=request_key
            )
        digest = canonical_json({"line": line, "inputs": dict(inputs)})
        existing = self.ctx.db.query(
            "SELECT run_id, args_digest FROM run_requests WHERE agent = ? AND request_key = ?",
            (agent, request_key),
        )
        if existing:
            if str(existing[0]["args_digest"]) != digest:
                raise FactoryError(
                    "RUN_REQUEST_CONFLICT",
                    "同一请求键参数已改变",
                    agent=agent,
                    request_key=request_key,
                )
            run = self.get(str(existing[0]["run_id"]))
            return {"id": run["id"], "state": run["state"]}
        from mao.agents import _run_start

        snapshot = self.epochs.materialize(self.epochs.current())
        payload: dict[str, Any] = {"line": line, "inputs": dict(inputs)}
        if cycle_id is not None and task_id is not None:
            payload["cycle_id"] = cycle_id
            payload["task_id"] = task_id
        started = _run_start(self.ctx, snapshot, agent, payload)
        with self.ctx.db.tx() as conn:
            conn.execute(
                """INSERT INTO run_requests (agent, request_key, args_digest, run_id, cycle_id, task_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (agent, request_key, digest, started["id"], cycle_id, task_id),
            )
        return started
