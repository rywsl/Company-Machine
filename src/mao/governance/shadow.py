"""缺省影子运行：重放基线 run，Sink 不发布。"""

from __future__ import annotations

import json
from typing import Any

from mao.errors import FactoryError
from mao.governance.rollout import ShadowReport


class LineShadowRunner:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def run(
        self,
        cs: Any,
        *,
        candidate: Any,
        active: Any,
        rollout: Any,
        golden_only: bool,
    ) -> ShadowReport:
        if golden_only:
            return self._goldens(cs, candidate, active)
        return self._replay(cs, candidate, active, rollout)

    def _goldens(self, cs: Any, candidate: Any, active: Any) -> ShadowReport:
        del active
        from mao.contract import ContractRunner
        from mao.runner import open_runner

        stations = _impact_stations(cs, candidate)
        names = {key.split(":", 1)[1].split("@", 1)[0] for key in stations}
        keys = [
            key
            for key in candidate.keys("GoldenSet")
            if str((candidate.doc(key).get("spec") or {}).get("station", "")).split("@", 1)[0] in names
        ]
        runner = ContractRunner(self.ctx, candidate, open_runner(self.ctx, candidate))
        regressions = []
        spent = 0.0
        for key in keys:
            result = runner.run_golden(key, None, timeout_s=None)
            spent += float((result.detail or {}).get("observed", {}).get("cost_usd") or 0)
            if result.verdict != "pass":
                regressions.append({"kind": "golden", "subject": key, "detail": {"result": result.id}})
        regressions.sort(key=lambda item: (item["kind"], item["subject"]))
        return ShadowReport(True, len(keys), round(spent, 6), regressions)

    def _replay(self, cs: Any, candidate: Any, active: Any, rollout: Any) -> ShadowReport:
        from mao.runs import InlineExecutor, open_line_runner

        limit = int(((rollout or {}).get("shadow") or {}).get("runs") or 0)
        rows = self.ctx.db.query(
            """SELECT * FROM runs
               WHERE epoch = ? AND state = 'SUCCEEDED' AND sink_mode = 'execute'
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (active.epoch, limit),
        )
        regressions: list[dict[str, Any]] = []
        spent = 0.0
        for row in rows:
            line = str(row["line"])
            try:
                candidate.key("Line", line)
            except FactoryError:
                regressions.append(
                    {
                        "kind": "node_failed",
                        "subject": "*",
                        "detail": {"error": "LINE_NOT_FOUND", "base_run": row["id"]},
                    }
                )
                continue
            lines = open_line_runner(self.ctx, candidate, cache=False)
            started = lines.start_run(
                line,
                json.loads(row["inputs"]),
                budget_usd=float(row["budget_cap_usd"]),
                variants=json.loads(row["variants"] or "{}"),
                trigger={"on": "shadow", "changeset": cs.id, "base_run": row["id"]},
                use_cache=False,
                sink_mode="dry-run",
            )
            done = lines.run_to_completion(started.id, InlineExecutor(lines.runner))
            spent += float(done.spent_usd)
            regressions.extend(_compare(self.ctx, lines, row["id"], done.id))
        regressions.sort(key=lambda item: (item["kind"], item["subject"]))
        return ShadowReport(True, len(rows), round(spent, 6), regressions)


def _impact_stations(cs: Any, candidate: Any) -> list[str]:
    report = getattr(cs, "report", None) or {}
    impact = report.get("impact") if isinstance(report, dict) else None
    if isinstance(impact, dict) and impact.get("stations"):
        return [str(item) for item in impact["stations"]]
    return candidate.keys("Station")


def _compare(ctx: Any, lines: Any, base_id: str, candidate_id: str) -> list[dict[str, Any]]:
    base_nodes = {node.node_id: node for node in lines.nodes(base_id)}
    # nodes() uses the runner snapshot, which is the candidate. Base nodes live in the same table.
    base_rows = ctx.db.query(
        "SELECT node_id, state, outputs, error FROM run_nodes WHERE run_id = ?",
        (base_id,),
    )
    base_nodes = {str(row["node_id"]): row for row in base_rows}
    found = []
    for node in lines.nodes(candidate_id):
        base = base_nodes.get(node.node_id)
        if base is None:
            continue
        if base["state"] == "DONE" and node.state.value == "FAILED":
            error = (node.error or {}).get("code")
            found.append(
                {
                    "kind": "node_failed",
                    "subject": node.node_id,
                    "detail": {"base_run": base_id, "candidate_run": candidate_id, "error": error},
                }
            )
            continue
        if base["state"] == "DONE" and node.state.value == "DONE":
            base_types = _port_types(ctx, json.loads(base["outputs"] or "{}"))
            cand_types = _port_types(ctx, node.outputs)
            if base_types != cand_types:
                found.append(
                    {
                        "kind": "type_changed",
                        "subject": node.node_id,
                        "detail": {"base": base_types, "candidate": cand_types},
                    }
                )
    return found


def _port_types(ctx: Any, outputs: dict[str, list[str]]) -> dict[str, list[str]]:
    typed: dict[str, list[str]] = {}
    for port, ids in outputs.items():
        names = []
        for bean_id in ids:
            try:
                names.append(ctx.warehouse.get(bean_id).type)
            except FactoryError:
                names.append("")
        typed[port] = sorted(names)
    return typed
