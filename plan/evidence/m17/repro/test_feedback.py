"""Reproduced autonomy contract gaps, not violations of M13's original API.

The run and its terminal result exist; the initiating agent has no correlated
terminal notification. These assertions freeze M17's proposed feedback contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mao.foreman import ForemanQuery
from mao.runs import InlineExecutor, open_line_runner
from tests.acceptance.m13.agent_world import World, tool_result


@pytest.mark.parametrize("fail", [False, True], ids=["success", "failure"])
def test_terminal_run_returns_to_initiating_agent(tmp_path: Path, fail: bool) -> None:
    w = World(tmp_path)
    try:
        if fail:
            w.ctx.provider.commands["upper-text"] = [sys.executable, "-c", "raise SystemExit(1)"]
        started, error = tool_result(
            w.mcp("run_start", {"line": "note-trigger@1", "inputs": {"note": w.note()}})
        )
        assert error is None, error
        assert set(started) == {"id", "state"}, started  # Original M13 contract, positive control.
        lines = open_line_runner(w.ctx, w.snapshot)
        done = lines.run_to_completion(started["id"], InlineExecutor(lines.runner), max_ticks=100, wait_s=0)
        assert done.state.value == ("FAILED" if fail else "SUCCEEDED"), done.error
        nodes = lines.nodes(done.id)
        assert nodes and all(n.job_id for n in nodes)
        events = w.rows("SELECT data FROM events WHERE run_id = ? AND kind = 'run.state'", (done.id,))
        assert json.loads(events[-1]["data"])["to"] == done.state.value
        host = w.host()
        host.collect()
        host.collect()  # Repeated polling must not produce duplicate notifications.
        mail = [m for m in w.messages("scout") if m.kind == "run_result" and m.body.get("run_id") == done.id]
        assert len(mail) == 1, {
            "run_id": done.id,
            "terminal_state": done.state.value,
            "owner_mail": [(m.kind, m.source) for m in w.messages("scout")],
            "original_reply": started,
        }
    finally:
        w.ctx.close()


def test_metrics_query_returns_evidence_not_only_echoed_arguments(tmp_path: Path) -> None:
    """A successful real run still yields no metric value, count or source window."""
    w = World(tmp_path)
    try:
        started, error = tool_result(
            w.mcp("run_start", {"line": "note-trigger@1", "inputs": {"note": w.note()}})
        )
        assert error is None, error
        lines = open_line_runner(w.ctx, w.snapshot)
        done = lines.run_to_completion(started["id"], InlineExecutor(lines.runner), max_ticks=100, wait_s=0)
        assert done.state.value == "SUCCEEDED", done.error
        station = w.snapshot.key("Station", "upper@1")
        result = ForemanQuery(w.ctx, w.epochs).metrics_query(station, "success_rate")
        assert result.get("sample_count") == 1 and result.get("value") == 1.0, result
    finally:
        w.ctx.close()
