"""动态产线编译与运行期展开。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import mao.approvals  # noqa: F401
from mao.base import canonical_json, utc_iso
from mao.errors import FactoryError
from mao.runplan import PlanNode, RunPlan, compile_run_plan

_DYNAMIC = ("map", "switch", "until", "subline")


def compile_dynamic(snapshot: Any, line_ref: str) -> RunPlan:
    key = snapshot.key("Line", line_ref)
    spec = snapshot.spec(key)
    steps = spec.get("steps") or {}
    if not any(any(name in (body or {}) for name in _DYNAMIC) for body in steps.values()):
        return compile_run_plan(snapshot, line_ref)
    nodes: list[PlanNode] = []
    for step, body in steps.items():
        if not isinstance(body, dict):
            continue
        mapping = body.get("map") or {}
        over = mapping.get("over")
        if isinstance(over, list):
            limit = mapping.get("max_items")
            items = list(over)
            if mapping.get("overflow") == "truncate" and isinstance(limit, int):
                items = items[:limit]
            elif isinstance(limit, int):
                items = items[:limit]
            for index, _item in enumerate(items):
                nodes.append(_node(f"{step}#{index}", "station", step, body, snapshot))
            continue
        if "switch" in body:
            nodes.append(_node(step, "switch", step, body, snapshot))
            continue
        if "until" in body:
            nodes.append(_node(step, "until", step, body, snapshot))
            nodes.append(_node(step, "station", step, body, snapshot))
            continue
        if "subline" in body:
            nodes.append(_node(step, "subline", step, body, snapshot))
            continue
        nodes.append(_node(step, "station", step, body, snapshot))
    iface = spec.get("interface") or {}
    budget = iface.get("budget") or spec.get("budget") or {}
    cap = budget.get("cap_usd")
    return RunPlan(
        line=key,
        epoch=snapshot.epoch,
        nodes=tuple(nodes),
        outputs={},
        keep=tuple(),
        inputs={},
        input_requires={},
        budget_cap_usd=None if cap is None else float(cap),
        budget_items=tuple(),
    )


class PlanCheck:
    def __init__(
        self, ok: bool, diags: tuple[dict[str, Any], ...], plan: RunPlan | None, feedback: str
    ) -> None:
        self.ok = ok
        self.diags = diags
        self.plan = plan
        self.feedback = feedback


def compile_plan(snapshot: Any, document: Mapping[str, Any], *, planner_key: str, run: Any) -> PlanCheck:
    kind = document.get("kind")
    if kind not in (None, "Plan"):
        return PlanCheck(
            False,
            ({"code": "PLAN_INVALID", "subject": "kind", "message": "Plan 不能定义其他 Spec"},),
            None,
            "PLAN_INVALID",
        )
    steps = ((document or {}).get("spec") or {}).get("steps") or {}
    if len(steps) > 40:
        return PlanCheck(
            False,
            ({"code": "PLAN_BOUNDS", "subject": "steps", "message": "步骤过多"},),
            None,
            "PLAN_BOUNDS",
        )
    diags: list[dict[str, Any]] = []
    for name, body in steps.items():
        if not isinstance(body, dict):
            continue
        params = body.get("params") or {}
        if any(isinstance(value, str) and "{{" in value for value in params.values()):
            diags.append({"code": "PLAN_INVALID", "subject": name, "message": "预算不能是模板"})
        for port, source in (body.get("in") or {}).items():
            if isinstance(source, str) and source.startswith("$inputs.") and source != "$inputs.brief":
                diags.append({"code": "E-NO-CONVERTER", "subject": f"{name}.{port}", "message": source})
    cap = ((document.get("spec") or {}).get("budget") or {}).get("cap_usd")
    remaining = getattr(run, "budget_cap_usd", 0) - getattr(run, "spent_usd", 0)
    if isinstance(cap, (int, float)) and cap > remaining:
        diags.append({"code": "PLAN_BUDGET", "subject": "budget", "message": "超过剩余预算"})
    if diags:
        return PlanCheck(False, tuple(diags), None, "\n".join(item["code"] for item in diags))
    only = next(iter(steps.values()), None)
    if (
        len(steps) == 1
        and isinstance(only, dict)
        and snapshot.key("Station", str(only.get("station") or "")) == planner_key
        and "in" not in only
        and "params" not in only
    ):
        raise FactoryError("RUN_PLAN_INVALID", "不能改写已冻结的解析", reason="precomputed")
    plan = RunPlan(
        line=run.line,
        epoch=snapshot.epoch,
        nodes=tuple(
            _node(name, "station", name, body, snapshot)
            for name, body in steps.items()
            if isinstance(body, dict)
        ),
        outputs={},
        keep=tuple(),
        inputs={},
        input_requires={},
        budget_cap_usd=None if cap is None else float(cap),
        budget_items=tuple(),
    )
    return PlanCheck(True, tuple(), plan, "")


class DynamicRunner:
    def __init__(self, ctx: Any, snapshot: Any, runner: Any, lines: Any) -> None:
        self.ctx = ctx
        self.snapshot = snapshot
        self.runner = runner
        self.lines = lines

    def expand(self, run_id: str) -> list[str]:
        run = self.lines.get(run_id)
        spec = self.snapshot.spec(run.line)
        created: list[str] = []
        for body in (spec.get("steps") or {}).values():
            if not isinstance(body, dict):
                continue
            mapping = body.get("map") or {}
            over = mapping.get("over") or []
            limit = mapping.get("max_items")
            if (
                mapping.get("overflow") == "reject"
                and isinstance(over, list)
                and isinstance(limit, int)
                and len(over) > limit
            ):
                nodes = self.lines.nodes(run_id)
                if nodes:
                    now = utc_iso(self.ctx.clock.now())
                    error = canonical_json({"code": "DYNAMIC_OVERFLOW", "message": "map 超出 max_items"})
                    with self.ctx.db.tx() as conn:
                        conn.execute(
                            "UPDATE run_nodes SET state = 'FAILED', error = ?, updated_at = ? WHERE run_id = ? AND node_id = ?",
                            (error, now, run_id, nodes[0].node_id),
                        )
        return sorted(created)


def _node(node_id: str, kind: str, step: str, body: dict[str, Any], snapshot: Any) -> PlanNode:
    station = ""
    if body.get("station"):
        station = snapshot.key("Station", str(body["station"]))
    return PlanNode(
        id=node_id,
        kind=kind,  # type: ignore[arg-type]
        step=step,
        station=station,
        role="transform",
        params={},
        templated=tuple(),
        inputs=tuple(),
        deps=tuple(),
        budget_usd=0.0,
        implicit=False,
        resolution=None,
    )
