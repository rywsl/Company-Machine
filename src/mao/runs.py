"""产线 run 的存储与推进。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from mao.base import canonical_json, utc_iso
from mao.errors import FactoryError
from mao.jobs import JobState, load_job
from mao.runplan import PlanNode, RunPlan, compile_run_plan
from mao.values import inputs_value, property_env


def _compile_line(snapshot: Any, line_ref: str) -> RunPlan:
    try:
        return compile_run_plan(snapshot, line_ref)
    except FactoryError as exc:
        if exc.code != "LINE_UNSUPPORTED":
            raise
        from mao.dynamic import compile_dynamic

        return compile_dynamic(snapshot, line_ref)


class RunState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    FAILING = "FAILING"
    CANCELLING = "CANCELLING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


RUN_TERMINAL = {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}


class NodeState(StrEnum):
    WAITING = "WAITING"
    SUBMITTED = "SUBMITTED"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class RunNode:
    run_id: str
    node_id: str
    state: NodeState
    job_id: str | None
    cache_hit: bool
    outputs: dict[str, list[str]]
    params: dict[str, Any] | None
    reserved_usd: float
    error: dict[str, Any] | None
    updated_at: str


@dataclass(frozen=True)
class Run:
    id: str
    line: str
    epoch: str
    state: RunState
    plan_digest: str
    inputs: dict[str, list[str]]
    variants: dict[str, str]
    budget_cap_usd: float
    spent_usd: float
    reserved_usd: float
    outputs: dict[str, list[str]]
    error: dict[str, Any] | None
    trigger: dict[str, Any]
    rebuilt_from: str | None
    use_cache: bool
    created_at: str
    updated_at: str
    sink_mode: str = "execute"

    def value(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "epoch": self.epoch,
            "line": self.line,
            "budget": {
                "cap_usd": self.budget_cap_usd,
                "spent_usd": self.spent_usd,
                "remaining_usd": self.budget_cap_usd - self.spent_usd,
            },
        }


class InlineExecutor:
    def __init__(self, runner: Any) -> None:
        self.runner = runner

    def dispatch(self, job_ids: Sequence[str]) -> None:
        for job_id in sorted(job_ids):
            self.runner.run(job_id)

    def wait(self, timeout_s: float) -> None:
        del timeout_s


class LineRunner:
    def __init__(self, ctx: Any, snapshot: Any, runner: Any, *, cache: Any = None) -> None:
        self.ctx = ctx
        self.snapshot = snapshot
        self.runner = runner
        self.cache = cache
        from mao.dynamic import bind_context

        bind_context(snapshot, ctx)

    def start_run(
        self,
        line_ref: str,
        inputs: Mapping[str, str | Sequence[str]],
        *,
        budget_usd: float | None = None,
        variants: Mapping[str, str] | None = None,
        trigger: Mapping[str, Any] | None = None,
        use_cache: bool = True,
        rebuilt_from: str | None = None,
        sink_mode: str = "execute",
    ) -> Run:
        if sink_mode not in ("execute", "dry-run"):
            raise FactoryError(
                "RUN_INPUT_INVALID", "sink_mode 只能是 execute 或 dry-run", sink_mode=sink_mode
            )
        plan = _compile_line(self.snapshot, line_ref)
        cap = _budget_cap(plan, budget_usd)
        stored = _normalize_inputs(inputs)
        from mao.warehouse import reject_expired

        reject_expired(self.ctx.warehouse, stored)
        _check_inputs(self.snapshot, self.ctx.warehouse, plan, stored)
        variant_map = _check_variants(plan, variants or {})
        trig = dict(trigger or {"on": "manual", "by": ""})
        now = utc_iso(self.ctx.clock.now())
        run_id = self.ctx.ids.new("r")
        body = canonical_json(plan.to_json())
        with self.ctx.db.tx() as conn:
            conn.execute(
                """INSERT INTO runs
                   (id, line, epoch, state, plan, plan_digest, inputs, variants, budget_cap_usd,
                    spent_usd, reserved_usd, outputs, error, trigger, rebuilt_from, use_cache,
                    created_at, updated_at, sink_mode)
                   VALUES (?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, 0, 0, '{}', NULL, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    plan.line,
                    plan.epoch,
                    body,
                    plan.digest(),
                    canonical_json(stored),
                    canonical_json(variant_map),
                    cap,
                    canonical_json(trig),
                    rebuilt_from,
                    1 if use_cache else 0,
                    now,
                    now,
                    sink_mode,
                ),
            )
            for ordinal, node in enumerate(plan.nodes):
                conn.execute(
                    """INSERT INTO run_nodes
                       (run_id, node_id, ord, state, outputs, reserved_usd, updated_at)
                       VALUES (?, ?, ?, 'WAITING', '{}', 0, ?)""",
                    (run_id, node.id, ordinal, now),
                )
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data, run_id) VALUES (?, ?, 'run.created', NULL, ?, ?)",
                (
                    self.ctx.ids.new("ev"),
                    now,
                    canonical_json(
                        {
                            "line": plan.line,
                            "epoch": plan.epoch,
                            "plan_digest": plan.digest(),
                            "inputs": stored,
                            "budget_cap_usd": cap,
                            "trigger": trig,
                        }
                    ),
                    run_id,
                ),
            )
        return self.get(run_id)

    def get(self, run_id: str) -> Run:
        rows = self.ctx.db.query("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not rows:
            raise FactoryError("RUN_NOT_FOUND", f"找不到 run {run_id}", run_id=run_id)
        return _run_from_row(rows[0])

    def nodes(self, run_id: str) -> list[RunNode]:
        self.get(run_id)
        rows = self.ctx.db.query(
            "SELECT * FROM run_nodes WHERE run_id = ? ORDER BY ord",
            (run_id,),
        )
        return [_node_from_row(row) for row in rows]

    def list(self, *, state: str | None = None) -> list[Run]:
        if state is None:
            rows = self.ctx.db.query("SELECT * FROM runs ORDER BY id")
        else:
            rows = self.ctx.db.query("SELECT * FROM runs WHERE state = ? ORDER BY id", (state,))
        return [_run_from_row(row) for row in rows]

    def _subline_children(self, run_id: str) -> list[Run]:
        found = []
        for run in self.list():
            trig = run.trigger
            if trig.get("on") == "subline" and trig.get("parent") == run_id and run.id != run_id:
                found.append(run)
        return found

    def plan(self, run_id: str) -> RunPlan:
        row = self.ctx.db.query("SELECT plan FROM runs WHERE id = ?", (run_id,))
        if not row:
            raise FactoryError("RUN_NOT_FOUND", f"找不到 run {run_id}", run_id=run_id)
        return _compile_line(self.snapshot, json.loads(row[0]["plan"])["line"])

    def tick(self, run_id: str) -> list[str]:
        run = self.get(run_id)
        if run.epoch != self.snapshot.epoch:
            raise FactoryError(
                "EPOCH_MISMATCH",
                "run 的 epoch 与当前快照不一致",
                run=run_id,
                run_epoch=run.epoch,
                snapshot_epoch=self.snapshot.epoch,
            )
        if run.state in RUN_TERMINAL:
            return []
        plan = _compile_line(self.snapshot, run.line)
        by_id = {node.id: node for node in plan.nodes}
        submitted: list[str] = []
        with self.ctx.db.tx() as conn:
            run = self.get(run_id)
            nodes = self.nodes(run_id)
            if run.state == RunState.PENDING:
                run = self._set_run(conn, run, RunState.RUNNING, "start")
            for stored in nodes:
                if stored.state != NodeState.SUBMITTED or not stored.job_id:
                    continue
                job = load_job(self.ctx.db, stored.job_id)
                if job.state == JobState.SEALED:
                    self._finish_node(
                        conn, run, stored, NodeState.DONE, job.outputs, None, job.cost_usd, False
                    )
                elif job.state == JobState.DEAD:
                    self._finish_node(conn, run, stored, NodeState.FAILED, {}, job.error, job.cost_usd, False)
            from mao.dynamic import DynamicRunner, node_spec, parallel_limit

            self._attach_plans(conn, run)
            DynamicRunner(self.ctx, self.snapshot, self.runner, self).expand(run_id)
            run = self.get(run_id)
            nodes = self.nodes(run_id)
            if run.state == RunState.RUNNING and any(node.state == NodeState.FAILED for node in nodes):
                failed = next(node for node in nodes if node.state == NodeState.FAILED)
                run = self._set_run(
                    conn,
                    run,
                    RunState.FAILING,
                    "node-failed",
                    error={"code": "NODE_FAILED", "message": "节点失败", "node": failed.node_id},
                )
            ready = {node.node_id: node.state for node in nodes}
            opened: dict[str, int] = {}
            if run.state == RunState.RUNNING:
                for stored in nodes:
                    if stored.state != NodeState.WAITING:
                        continue
                    from mao.dynamic import attached_spec

                    spec = (
                        by_id.get(stored.node_id)
                        or node_spec(self.snapshot, run.line, stored.node_id)
                        or attached_spec(self.snapshot, run, stored.node_id, nodes)
                    )
                    if spec is None or spec.kind not in ("station", "converter"):
                        continue
                    if any(ready.get(dep) not in (NodeState.DONE, NodeState.SKIPPED) for dep in spec.deps):
                        continue
                    limit = parallel_limit(self.snapshot, run.line, spec.id)
                    if limit is not None and opened.get(spec.step, 0) >= limit:
                        continue
                    if self.ctx.db.query(
                        "SELECT station FROM station_holds WHERE station = ?", (spec.station,)
                    ):
                        continue
                    if run.sink_mode == "dry-run" and spec.role == "sink":
                        self._dry_sink(conn, run, stored, spec)
                        continue
                    job_id = self._submit_node(conn, run, stored, spec, nodes)
                    if job_id is None:
                        run = self.get(run_id)
                        if run.state != RunState.RUNNING:
                            break
                        continue
                    submitted.append(job_id)
                    if limit is not None:
                        opened[spec.step] = opened.get(spec.step, 0) + 1
            run = self.get(run_id)
            nodes = self.nodes(run_id)
            if run.state in (RunState.FAILING, RunState.CANCELLING) and not any(
                node.state == NodeState.SUBMITTED for node in nodes
            ):
                now = utc_iso(self.ctx.clock.now())
                for stored in nodes:
                    if stored.state == NodeState.WAITING:
                        conn.execute(
                            "UPDATE run_nodes SET state = 'SKIPPED', updated_at = ? WHERE run_id = ? AND node_id = ?",
                            (now, run.id, stored.node_id),
                        )
                target = RunState.CANCELLED if run.state == RunState.CANCELLING else RunState.FAILED
                self._set_run(conn, run, target, "finish")
            elif (
                run.state == RunState.RUNNING
                and nodes
                and not any(node.state in (NodeState.WAITING, NodeState.SUBMITTED) for node in nodes)
                and not any(node.state == NodeState.FAILED for node in nodes)
            ):
                outputs = _collect_outputs(plan, nodes)
                self._set_run(conn, run, RunState.SUCCEEDED, "done", outputs=outputs)
            for child in self._subline_children(run_id):
                if child.state not in RUN_TERMINAL:
                    submitted.extend(self.tick(child.id))
        return sorted(set(submitted))

    def run_to_completion(
        self, run_id: str, executor: Any, *, max_ticks: int = 10_000, wait_s: float = 1.0
    ) -> Run:
        for _ in range(max_ticks):
            before = (self.get(run_id).state, [node.state for node in self.nodes(run_id)])
            new = self.tick(run_id)
            outstanding = [
                node.job_id
                for node in self.nodes(run_id)
                if node.state == NodeState.SUBMITTED and node.job_id
            ]
            to_run = sorted(set(new) | set(outstanding))
            if to_run:
                executor.dispatch(to_run)
            run = self.get(run_id)
            if run.state in RUN_TERMINAL:
                return run
            after = (run.state, [node.state for node in self.nodes(run_id)])
            if not new and after == before:
                executor.wait(wait_s)
        raise FactoryError(
            "RUN_STUCK", "run 没有在限定步数内结束", run=run_id, state=self.get(run_id).state.value
        )

    def cancel(self, run_id: str, *, by: str) -> Run:
        run = self.get(run_id)
        if run.state not in (RunState.PENDING, RunState.RUNNING):
            raise FactoryError(
                "RUN_STATE_INVALID", "当前状态不能取消", run=run_id, state=run.state.value, op="cancel"
            )
        with self.ctx.db.tx() as conn:
            return self._set_run(
                conn,
                run,
                RunState.CANCELLING,
                f"cancel by={by}",
                error={"code": "RUN_CANCELLED", "message": "run 已取消", "by": by},
            )

    def retry_node(self, run_id: str, node_id: str, *, by: str) -> Run:
        run = self.get(run_id)
        nodes = self.nodes(run_id)
        target = next((node for node in nodes if node.node_id == node_id), None)
        if (
            run.state not in (RunState.FAILED, RunState.FAILING)
            or target is None
            or target.state != NodeState.FAILED
        ):
            raise FactoryError(
                "RUN_STATE_INVALID",
                "当前状态不能重试节点",
                run=run_id,
                state=run.state.value,
                op="retry_node",
            )
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                """UPDATE run_nodes SET state = 'WAITING', error = NULL, job_id = NULL, updated_at = ?
                   WHERE run_id = ? AND node_id = ?""",
                (now, run_id, node_id),
            )
            conn.execute(
                """UPDATE run_nodes SET state = 'WAITING', updated_at = ?
                   WHERE run_id = ? AND state = 'SKIPPED'""",
                (now, run_id),
            )
            return self._set_run(conn, run, RunState.RUNNING, f"retry_node by={by}", error=None)

    def rebuild(self, run_id: str, *, by: str) -> Run:
        run = self.get(run_id)
        if run.state not in RUN_TERMINAL:
            raise FactoryError(
                "RUN_STATE_INVALID", "只有终态 run 可以重建", run=run_id, state=run.state.value, op="rebuild"
            )
        ref = run.line.split(":", 1)[1]
        return self.start_run(
            ref,
            run.inputs,
            budget_usd=run.budget_cap_usd,
            variants=run.variants,
            trigger={"on": "rebuild", "by": by},
            use_cache=run.use_cache,
            rebuilt_from=run.id,
        )

    def _attach_plans(self, conn: Any, run: Run) -> None:
        from mao.dynamic import compile_plan, plan_document_from_job, prefix_nodes

        compiled = {node.id: node for node in _compile_line(self.snapshot, run.line).nodes}
        existing = {node.node_id for node in self.nodes(run.id)}
        for stored in self.nodes(run.id):
            if stored.state != NodeState.DONE or not stored.job_id or stored.node_id not in compiled:
                continue
            spec = compiled[stored.node_id]
            if spec.kind != "station":
                continue
            station = self.snapshot.spec(spec.station)
            if station.get("role") != "planner":
                continue
            prefix = stored.node_id + "/"
            if any(name.startswith(prefix) for name in existing):
                continue
            job = load_job(self.ctx.db, stored.job_id)
            document = plan_document_from_job(self.ctx, self.snapshot, job)
            check = compile_plan(self.snapshot, document or {}, planner_key=job.station, run=self.get(run.id))
            if not check.ok or check.plan is None:
                now = utc_iso(self.ctx.clock.now())
                conn.execute(
                    """UPDATE run_nodes SET state = 'FAILED', error = ?, updated_at = ?
                       WHERE run_id = ? AND node_id = ?""",
                    (
                        canonical_json(
                            {
                                "code": "RUN_PLAN_INVALID",
                                "message": check.feedback or "Plan 不能挂接",
                                "detail": {"reason": "plan"},
                            }
                        ),
                        now,
                        run.id,
                        stored.node_id,
                    ),
                )
                continue
            ordinal = conn.execute(
                "SELECT COALESCE(MAX(ord), -1) + 1 AS n FROM run_nodes WHERE run_id = ?",
                (run.id,),
            ).fetchone()
            now = utc_iso(self.ctx.clock.now())
            next_ord = int(ordinal["n"])
            for node in prefix_nodes(check.plan.nodes, stored.node_id):
                if node.id in existing:
                    continue
                conn.execute(
                    """INSERT INTO run_nodes
                       (run_id, node_id, ord, state, outputs, reserved_usd, updated_at)
                       VALUES (?, ?, ?, 'WAITING', '{}', 0, ?)""",
                    (run.id, node.id, next_ord, now),
                )
                next_ord += 1
                existing.add(node.id)
            outputs = job.outputs or {}
            bean_ids = next(iter(outputs.values()), [])
            steps = sorted((document or {}).get("spec", {}).get("steps", {}))
            self._event(
                conn,
                run.id,
                "run.plan",
                {"plan": bean_ids[0] if bean_ids else "", "node": stored.node_id, "steps": steps},
            )

    def _submit_node(
        self, conn: Any, run: Run, stored: RunNode, spec: PlanNode, nodes: list[RunNode]
    ) -> str | None:
        by_node = {node.node_id: node for node in nodes}
        submit_inputs: dict[str, Any] = {}
        for binding in spec.inputs:
            if binding.source == "item":
                from mao.dynamic import item_bean_id

                bean_id = item_bean_id(self.snapshot, run, spec.id, nodes)
                ids = [] if bean_id is None else [bean_id]
            elif binding.source == "input":
                ids = list(run.inputs.get(binding.out_port) or [])
            else:
                ids = list((by_node[binding.node].outputs or {}).get(binding.out_port) or [])
            if len(ids) == 1:
                submit_inputs[binding.port] = ids[0]
            elif ids:
                submit_inputs[binding.port] = ids
            else:
                self._fail_input(conn, run, stored, binding.port, binding.node or binding.out_port)
                return None
        params = dict(spec.params)
        for name in spec.templated:
            subject = f"{run.line}/steps.{spec.step}.params.{name}"
            from mao.dynamic import template_extra

            env = {"run": run.value(), "inputs": inputs_value(self._input_beans(run))}
            env.update(template_extra(self.snapshot, self.ctx.warehouse, run, spec.id, nodes))
            params[name] = self.snapshot.compiled(subject).render(env)
        from mao.dynamic import until_upfront

        upfront = until_upfront(self.snapshot, run.line, spec.id, nodes)
        need = spec.budget_usd
        if upfront is not None and run.spent_usd + run.reserved_usd + upfront > run.budget_cap_usd:
            self._set_run(
                conn,
                run,
                RunState.FAILING,
                "budget",
                error={
                    "code": "RUN_BUDGET_EXHAUSTED",
                    "message": "迭代预留会超出 run 预算",
                    "node": spec.id,
                },
            )
            return None
        if run.spent_usd + run.reserved_usd + need > run.budget_cap_usd:
            self._set_run(
                conn,
                run,
                RunState.FAILING,
                "budget",
                error={
                    "code": "RUN_BUDGET_EXHAUSTED",
                    "message": "预留会超出 run 预算",
                    "node": spec.id,
                },
            )
            return None
        material = None
        if self.cache is not None and spec.role in ("transform", "converter"):
            from mao.cache import CACHEABLE_ROLES

            if spec.role in CACHEABLE_ROLES:
                material = self.cache.material(
                    self.snapshot,
                    spec.station,
                    submit_inputs,
                    params,
                    variant=run.variants.get(spec.step),
                )
                if run.use_cache:
                    hit = self.cache.lookup(material.key())
                    if hit is not None:
                        now = utc_iso(self.ctx.clock.now())
                        conn.execute(
                            """UPDATE run_nodes
                               SET state = 'DONE', job_id = ?, outputs = ?, cache_hit = 1, params = ?, updated_at = ?
                               WHERE run_id = ? AND node_id = ?""",
                            (
                                hit.id,
                                canonical_json(dict(hit.outputs)),
                                canonical_json(params),
                                now,
                                run.id,
                                spec.id,
                            ),
                        )
                        self._event(
                            conn,
                            run.id,
                            "run.node",
                            {
                                "node": spec.id,
                                "from": "WAITING",
                                "to": "DONE",
                                "job": hit.id,
                                "cache_hit": True,
                            },
                        )
                        return None
        try:
            job = self.runner.submit(spec.station, submit_inputs, params)
        except FactoryError as exc:
            now = utc_iso(self.ctx.clock.now())
            conn.execute(
                """UPDATE run_nodes SET state = 'FAILED', error = ?, updated_at = ?
                   WHERE run_id = ? AND node_id = ?""",
                (
                    canonical_json({"code": exc.code, "message": exc.message, "detail": exc.detail}),
                    now,
                    run.id,
                    spec.id,
                ),
            )
            return None
        now = utc_iso(self.ctx.clock.now())
        conn.execute(
            """UPDATE run_nodes SET state = 'SUBMITTED', job_id = ?, params = ?, reserved_usd = ?, updated_at = ?
               WHERE run_id = ? AND node_id = ?""",
            (job.id, canonical_json(params), need, now, run.id, spec.id),
        )
        conn.execute(
            "INSERT INTO run_node_jobs (run_id, node_id, job_id, created_at) VALUES (?, ?, ?, ?)",
            (run.id, spec.id, job.id, now),
        )
        conn.execute(
            "UPDATE runs SET reserved_usd = reserved_usd + ?, updated_at = ? WHERE id = ?",
            (need, now, run.id),
        )
        self._event(
            conn,
            run.id,
            "run.node",
            {"node": spec.id, "from": "WAITING", "to": "SUBMITTED", "job": job.id, "cache_hit": False},
        )
        self._event(
            conn,
            run.id,
            "run.budget",
            {"node": spec.id, "reserved_usd": need, "spent_usd": 0.0, "run_spent_usd": run.spent_usd},
        )
        if material is not None:
            self.cache.bind(job.id, material)
        return job.id

    def _dry_sink(self, conn: Any, run: Run, stored: RunNode, spec: Any) -> None:
        station = self.snapshot.spec(spec.station)
        phases = list(station.get("phases") or [])
        now = utc_iso(self.ctx.clock.now())
        if "propose" in phases:
            from mao.contract import ContractRunner

            result = ContractRunner(self.ctx, self.snapshot, self.runner).run_sink_dry_run(
                spec.station, inputs={}, params=spec.params, timeout_s=None
            )
            self._event(
                conn,
                run.id,
                "run.sink_dry_run",
                {"run": run.id, "node": spec.id, "station": spec.station, "result": result.id},
            )
            if result.verdict != "pass":
                conn.execute(
                    """UPDATE run_nodes SET state = 'FAILED', error = ?, updated_at = ?
                       WHERE run_id = ? AND node_id = ?""",
                    (
                        canonical_json(
                            {
                                "code": "SHADOW_SINK_REJECTED",
                                "message": "影子运行拒绝了 Sink",
                                "detail": {"run": run.id, "node": spec.id, "result": result.id},
                            }
                        ),
                        now,
                        run.id,
                        spec.id,
                    ),
                )
                return
        else:
            self._event(
                conn,
                run.id,
                "run.sink_skipped",
                {"run": run.id, "node": spec.id, "station": spec.station},
            )
        conn.execute(
            """UPDATE run_nodes SET state = 'DONE', outputs = '{}', updated_at = ?
               WHERE run_id = ? AND node_id = ?""",
            (now, run.id, spec.id),
        )

    def _finish_node(
        self,
        conn: Any,
        run: Run,
        stored: RunNode,
        state: NodeState,
        outputs: Mapping[str, list[str]],
        error: dict[str, Any] | None,
        cost: float,
        cache_hit: bool,
    ) -> None:
        now = utc_iso(self.ctx.clock.now())
        conn.execute(
            """UPDATE run_nodes SET state = ?, outputs = ?, error = ?, reserved_usd = 0, cache_hit = ?, updated_at = ?
               WHERE run_id = ? AND node_id = ?""",
            (
                state.value,
                canonical_json(dict(outputs)),
                None if error is None else canonical_json(error),
                1 if cache_hit else 0,
                now,
                run.id,
                stored.node_id,
            ),
        )
        spent = 0.0 if cache_hit else float(cost)
        conn.execute(
            """UPDATE runs SET reserved_usd = reserved_usd - ?, spent_usd = spent_usd + ?, updated_at = ? WHERE id = ?""",
            (stored.reserved_usd, spent, now, run.id),
        )
        self._event(
            conn,
            run.id,
            "run.node",
            {
                "node": stored.node_id,
                "from": stored.state.value,
                "to": state.value,
                "job": stored.job_id,
                "cache_hit": cache_hit,
            },
        )
        self._event(
            conn,
            run.id,
            "run.budget",
            {
                "node": stored.node_id,
                "reserved_usd": 0.0,
                "spent_usd": spent,
                "run_spent_usd": run.spent_usd + spent,
            },
        )

    def _fail_input(self, conn: Any, run: Run, stored: RunNode, port: str, source: str) -> None:
        now = utc_iso(self.ctx.clock.now())
        conn.execute(
            """UPDATE run_nodes SET state = 'FAILED', error = ?, updated_at = ? WHERE run_id = ? AND node_id = ?""",
            (
                canonical_json(
                    {
                        "code": "INPUT_MISSING",
                        "message": "上游没有产出",
                        "node": stored.node_id,
                        "port": port,
                        "source": source,
                    }
                ),
                now,
                run.id,
                stored.node_id,
            ),
        )

    def _set_run(
        self,
        conn: Any,
        run: Run,
        state: RunState,
        reason: str,
        *,
        error: Any = ...,
        outputs: Mapping[str, list[str]] | None = None,
    ) -> Run:
        now = utc_iso(self.ctx.clock.now())
        new_error = run.error if error is ... else error
        new_outputs = run.outputs if outputs is None else outputs
        conn.execute(
            "UPDATE runs SET state = ?, error = ?, outputs = ?, updated_at = ? WHERE id = ?",
            (
                state.value,
                None if new_error is None else canonical_json(new_error),
                canonical_json(dict(new_outputs)),
                now,
                run.id,
            ),
        )
        self._event(conn, run.id, "run.state", {"from": run.state.value, "to": state.value, "reason": reason})
        return self.get(run.id)

    def _event(self, conn: Any, run_id: str, kind: str, data: Mapping[str, Any]) -> None:
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data, run_id) VALUES (?, ?, ?, NULL, ?, ?)",
            (self.ctx.ids.new("ev"), utc_iso(self.ctx.clock.now()), kind, canonical_json(dict(data)), run_id),
        )

    def _input_beans(self, run: Run) -> dict[str, Any]:
        beans: dict[str, Any] = {}
        for port, ids in run.inputs.items():
            loaded = [self.ctx.warehouse.get(item) for item in ids]
            beans[port] = loaded if len(loaded) != 1 else loaded[0]
        return beans


class TriggerEngine:
    def __init__(self, ctx: Any, snapshot: Any, lines: LineRunner) -> None:
        self.ctx = ctx
        self.snapshot = snapshot
        self.lines = lines

    def process(self, *, route: Any | None = None) -> list[str]:
        created: list[str] = []
        with self.ctx.db.tx() as conn:
            cursor = conn.execute("SELECT seq FROM cursors WHERE name = 'trigger.bean'").fetchone()
            last = 0 if cursor is None else int(cursor[0])
            rows = conn.execute(
                "SELECT seq, data FROM events WHERE kind = 'bean.sealed' AND seq > ? ORDER BY seq",
                (last,),
            ).fetchall()
            seen: list[str] = []
            newest = last
            for row in rows:
                newest = int(row["seq"])
                data = json.loads(row["data"])
                bean_id = data.get("bean")
                if isinstance(bean_id, str):
                    seen.append(bean_id)
            for bean in self.ctx.warehouse.list(sealed_only=True):
                if bean.id not in seen:
                    seen.append(bean.id)
            for bean_id in seen:
                created.extend(self._fire(conn, bean_id, route=route))
            max_seq = conn.execute("SELECT COALESCE(MAX(seq), 0) AS seq FROM events").fetchone()
            newest = max(newest, int(max_seq["seq"]) if max_seq else 0)
            if cursor is None:
                conn.execute("INSERT INTO cursors (name, seq) VALUES ('trigger.bean', ?)", (newest,))
            else:
                conn.execute("UPDATE cursors SET seq = ? WHERE name = 'trigger.bean'", (newest,))
        return sorted(created)

    def _fire(self, conn: Any, bean_id: str, *, route: Any | None = None) -> list[str]:
        try:
            bean = self.ctx.warehouse.get(bean_id)
        except FactoryError:
            return []
        if bean.expired_at is not None:
            return []
        labels = self.ctx.warehouse.labels(bean_id)
        created: list[str] = []
        for key in self._line_keys():
            doc = self.snapshot.doc(key)
            if (doc.get("metadata") or {}).get("lifecycle", "active") != "active":
                continue
            spec = self.snapshot.spec(key)
            for rule in spec.get("trigger") or []:
                if not isinstance(rule, dict) or rule.get("on") != "bean":
                    continue
                label = rule.get("label")
                if label is not None and (not isinstance(label, str) or label.count("=") != 1):
                    self._invalid(conn, key, "label 不是 k=v")
                    continue
                if label:
                    name, value = label.split("=", 1)
                    if labels.get(name) != value:
                        continue
                want = _minor(str(rule.get("type", "")))
                bean_tid = _minor(bean.type)
                if bean_tid != want and not self.snapshot.is_subtype(bean_tid, want):
                    continue
                iface = (spec.get("interface") or {}).get("inputs") or {}
                if len(iface) != 1:
                    self._invalid(conn, key, "接口不是恰好一个输入")
                    continue
                port, decl = next(iter(iface.items()))
                type_expr = decl.get("type") if isinstance(decl, dict) else str(decl)
                inner = type_expr[5:-1] if str(type_expr).startswith("list<") else str(type_expr)
                if _minor(inner) != want and not self.snapshot.is_subtype(bean_tid, _minor(inner)):
                    self._invalid(conn, key, "接口输入不接受这个 Bean")
                    continue
                if conn.execute(
                    "SELECT 1 FROM trigger_fires WHERE line = ? AND bean_id = ?",
                    (key, bean_id),
                ).fetchone():
                    continue
                ref = key.split(":", 1)[1]
                lines = self.lines if route is None else route(key + ":" + canonical_json({port: bean_id}))
                run = lines.start_run(
                    ref,
                    {port: bean_id},
                    trigger={"on": "bean", "bean": bean_id, "label": label},
                )
                now = utc_iso(self.ctx.clock.now())
                conn.execute(
                    "INSERT INTO trigger_fires (line, bean_id, run_id, at) VALUES (?, ?, ?, ?)",
                    (key, bean_id, run.id, now),
                )
                self.lines._event(
                    conn,
                    run.id,
                    "run.trigger",
                    {"line": key, "bean": bean_id, "label": label},
                )
                created.append(run.id)
        return created

    def _line_keys(self) -> list[str]:
        return self.snapshot.keys("Line")

    def _invalid(self, conn: Any, line: str, reason: str) -> None:
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'run.trigger', NULL, ?)",
            (
                self.ctx.ids.new("ev"),
                utc_iso(self.ctx.clock.now()),
                canonical_json({"line": line, "code": "TRIGGER_INVALID", "reason": reason}),
            ),
        )


def _budget_cap(plan: RunPlan, given: float | None) -> float:
    if given is not None and (isinstance(given, bool) or not isinstance(given, (int, float)) or given <= 0):
        raise FactoryError("RUN_BUDGET_INVALID", "run 预算必须大于 0", budget_usd=given)
    if plan.budget_cap_usd is None:
        if given is None:
            raise FactoryError("RUN_BUDGET_REQUIRED", "产线没有预算上限，创建 run 时必须给出", line=plan.line)
        return float(given)
    if given is None:
        return float(plan.budget_cap_usd)
    if float(given) > float(plan.budget_cap_usd):
        raise FactoryError(
            "RUN_BUDGET_INVALID",
            "run 预算不能超过产线上限",
            budget_usd=float(given),
            cap_usd=float(plan.budget_cap_usd),
        )
    return float(given)


def _normalize_inputs(inputs: Mapping[str, Any]) -> dict[str, list[str]]:
    stored: dict[str, list[str]] = {}
    for port, value in inputs.items():
        if isinstance(value, str):
            stored[port] = [value]
        else:
            stored[port] = [str(item) for item in value]
    return stored


def _check_inputs(snapshot: Any, warehouse: Any, plan: RunPlan, inputs: Mapping[str, list[str]]) -> None:
    problems: list[dict[str, Any]] = []
    for name in plan.inputs:
        if name not in inputs:
            problems.append({"port": name, "reason": "missing", "detail": "缺少接口输入"})
    for name in inputs:
        if name not in plan.inputs:
            problems.append({"port": name, "reason": "extra", "detail": "接口没有这个输入"})
    for name, ids in inputs.items():
        if name not in plan.inputs:
            continue
        type_expr = plan.inputs[name]
        listed = type_expr.startswith("list<") and type_expr.endswith(">")
        inner = type_expr[5:-1] if listed else type_expr
        if listed and not ids:
            problems.append({"port": name, "reason": "cardinality", "detail": "列表输入至少要一个 Bean"})
            continue
        if not listed and len(ids) != 1:
            problems.append({"port": name, "reason": "cardinality", "detail": "标量输入恰好一个 Bean"})
            continue
        requires = plan.input_requires.get(name) or ()
        for bean_id in ids:
            problems.extend(_check_bean(snapshot, warehouse, name, bean_id, inner, requires))
    if problems:
        problems.sort(key=lambda item: (item["port"], item["reason"]))
        raise FactoryError("RUN_INPUT_INVALID", "产线输入不合法", problems=problems)


def _check_bean(
    snapshot: Any, warehouse: Any, port: str, bean_id: str, type_expr: str, requires: Sequence[str]
) -> list[dict[str, Any]]:
    try:
        bean = warehouse.get(bean_id)
    except FactoryError:
        return [{"port": port, "reason": "not_found", "detail": bean_id}]
    if not bean.sealed:
        return [{"port": port, "reason": "unsealed", "detail": bean_id}]
    bean_tid = _minor(bean.type)
    want = _minor(type_expr)
    if bean_tid != want and not snapshot.is_subtype(bean_tid, want):
        return [{"port": port, "reason": "type", "detail": bean.type}]
    problems = []
    props = snapshot.properties(bean_tid)
    for name in requires:
        compiled = props.get(name)
        ok = False
        if compiled is not None:
            try:
                ok = compiled.evaluate(property_env(bean)) is True
            except Exception:
                ok = False
        if not ok:
            problems.append({"port": port, "reason": "requires", "detail": name})
    return problems


def _check_variants(plan: RunPlan, variants: Mapping[str, str]) -> dict[str, str]:
    steps = {node.step for node in plan.nodes if not node.implicit}
    problems = []
    for name, value in variants.items():
        if name not in steps or not isinstance(value, str) or not value:
            problems.append({"port": name, "reason": "variant", "detail": "variant 不合法"})
    if problems:
        raise FactoryError("RUN_INPUT_INVALID", "variants 不合法", problems=problems)
    return {str(key): str(value) for key, value in variants.items()}


def _collect_outputs(plan: RunPlan, nodes: list[RunNode]) -> dict[str, list[str]]:
    by_id = {node.node_id: node for node in nodes}
    if plan.outputs:
        found: dict[str, list[str]] = {}
        for name, (node_id, port) in plan.outputs.items():
            source = by_id.get(node_id)
            if source is None or source.state == NodeState.SKIPPED:
                continue
            found[name] = list((source.outputs or {}).get(port) or [])
        return found
    found: dict[str, list[str]] = {}
    for node in nodes:
        for port, ids in node.outputs.items():
            if ids:
                found[port] = list(ids)
    return found


def _minor(type_name: str) -> str:
    if "@" not in type_name:
        return type_name
    name, version = type_name.rsplit("@", 1)
    return f"{name}@{version.split('.', 1)[0]}"


def _run_from_row(row: Any) -> Run:
    return Run(
        id=row["id"],
        line=row["line"],
        epoch=row["epoch"],
        state=RunState(row["state"]),
        plan_digest=row["plan_digest"],
        inputs={port: list(ids) for port, ids in json.loads(row["inputs"]).items()},
        variants=dict(json.loads(row["variants"])),
        budget_cap_usd=float(row["budget_cap_usd"]),
        spent_usd=float(row["spent_usd"]),
        reserved_usd=float(row["reserved_usd"]),
        outputs={port: list(ids) for port, ids in json.loads(row["outputs"]).items()},
        error=None if row["error"] is None else json.loads(row["error"]),
        trigger=dict(json.loads(row["trigger"])),
        rebuilt_from=row["rebuilt_from"],
        use_cache=bool(row["use_cache"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        sink_mode=str(row["sink_mode"] if "sink_mode" in row.keys() else "execute"),
    )


def _node_from_row(row: Any) -> RunNode:
    return RunNode(
        run_id=row["run_id"],
        node_id=row["node_id"],
        state=NodeState(row["state"]),
        job_id=row["job_id"],
        cache_hit=bool(row["cache_hit"]),
        outputs={port: list(ids) for port, ids in json.loads(row["outputs"]).items()},
        params=None if row["params"] is None else json.loads(row["params"]),
        reserved_usd=float(row["reserved_usd"]),
        error=None if row["error"] is None else json.loads(row["error"]),
        updated_at=row["updated_at"],
    )
