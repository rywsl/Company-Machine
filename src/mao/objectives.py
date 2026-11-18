"""宏观目标授权与周期（spec/system/continuity.md 第 3.4、5.6、5.7 节）。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from mao.base import canonical_json, utc_iso
from mao.budget import BudgetLedger
from mao.errors import FactoryError

_TERMINAL = ("idle", "blocked", "completed", "failed")
_GRANT_FIELDS = (
    "agents",
    "lines",
    "total_usd",
    "daily_usd",
    "every_s",
    "expires_at",
    "max_cycle_s",
)


def continuity_enabled(ctx: Any) -> bool:
    body = _factory(ctx)
    section = body.get("continuity") if isinstance(body, dict) else None
    return isinstance(section, dict) and section.get("enabled") is True


def require_continuity(ctx: Any) -> None:
    from mao.agents import resident_foreman
    from mao.assets import assets_config

    body = _factory(ctx)
    section = body.get("continuity") if isinstance(body, dict) else None
    if not isinstance(section, dict) or section.get("enabled") is not True:
        raise FactoryError("CONTINUITY_CONFIG_INVALID", "连续性未启用", reason="enabled")
    if assets_config(ctx) is None:
        raise FactoryError("CONTINUITY_CONFIG_INVALID", "资产目录未启用", reason="assets")
    if not isinstance(body.get("agents"), dict):
        raise FactoryError("CONTINUITY_CONFIG_INVALID", "缺少 Agents", reason="agents")
    if not isinstance(body.get("governance"), dict):
        raise FactoryError("CONTINUITY_CONFIG_INVALID", "缺少 governance", reason="governance")
    if resident_foreman(ctx.root) is None:
        raise FactoryError("CONTINUITY_CONFIG_INVALID", "厂长不是常驻", reason="foreman")


class ObjectiveStore:
    def __init__(self, ctx: Any, epochs: Any) -> None:
        self.ctx = ctx
        self.epochs = epochs

    def authorize(self, objective: str, grant: Mapping[str, Any], *, by: str) -> dict[str, Any]:
        require_continuity(self.ctx)
        _actor(by)
        asset = self._objective_asset(objective)
        normalized = _validate_grant(self, grant)
        text = canonical_json(normalized)
        standards = _standards_text(self.ctx, str(asset["document"]["data"]["contract"]))
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM objectives WHERE asset_id = ?",
                (asset["id"],),
            ).fetchone()
            if row is not None:
                if str(row["grant"]) != text or str(row["standards"]) != standards:
                    raise FactoryError(
                        "OBJECTIVE_STATE_INVALID",
                        "同一资产不能更换授权",
                        id=str(row["id"]),
                        state=str(row["state"]),
                        operation="authorize",
                    )
                return self._view(row)
            objective_id = self.ctx.ids.new("ob")
            conn.execute(
                """INSERT INTO objectives (
                       id, asset_id, grant, standards, state, next_due_at, active_cycle, reason, updated_at
                   ) VALUES (?, ?, ?, ?, 'active', ?, NULL, '', ?)""",
                (objective_id, asset["id"], text, standards, now, now),
            )
            row = conn.execute("SELECT * FROM objectives WHERE id = ?", (objective_id,)).fetchone()
        return self._view(row)

    def get(self, objective_id: str) -> dict[str, Any]:
        require_continuity(self.ctx)
        return self._view(self._refresh(self._row(objective_id)))

    def list(self) -> list[dict[str, Any]]:
        require_continuity(self.ctx)
        found = []
        for row in self.ctx.db.query("SELECT * FROM objectives ORDER BY id"):
            found.append(self._view(self._refresh(row)))
        return found

    def pause(self, objective_id: str, *, reason: str, by: str) -> dict[str, Any]:
        require_continuity(self.ctx)
        _actor(by)
        if not isinstance(reason, str) or not reason:
            raise FactoryError("OBJECTIVE_INVALID", "暂停原因不能为空", field="reason", reason="empty")
        row = self._refresh(self._row(objective_id))
        if str(row["state"]) != "active":
            raise FactoryError(
                "OBJECTIVE_STATE_INVALID",
                "当前状态不能暂停",
                id=objective_id,
                state=str(row["state"]),
                operation="pause",
            )
        return self._set_state(objective_id, "paused", reason)

    def resume(self, objective_id: str, *, by: str) -> dict[str, Any]:
        require_continuity(self.ctx)
        _actor(by)
        row = self._refresh(self._row(objective_id))
        if str(row["state"]) != "paused":
            raise FactoryError(
                "OBJECTIVE_STATE_INVALID",
                "当前状态不能恢复",
                id=objective_id,
                state=str(row["state"]),
                operation="resume",
            )
        return self._set_state(objective_id, "active", "")

    def revoke(self, objective_id: str, *, reason: str, by: str) -> dict[str, Any]:
        require_continuity(self.ctx)
        _actor(by)
        if not isinstance(reason, str) or not reason:
            raise FactoryError("OBJECTIVE_INVALID", "撤销原因不能为空", field="reason", reason="empty")
        self._row(objective_id)
        return self._set_state(objective_id, "revoked", reason)

    def collect(self) -> list[str]:
        require_continuity(self.ctx)
        now = utc_iso(self.ctx.clock.now())
        created: list[str] = []
        for row in self.ctx.db.query("SELECT * FROM objectives ORDER BY id"):
            current = self._refresh(row)
            if str(current["state"]) != "active" or current["active_cycle"]:
                continue
            if str(current["next_due_at"]) > now:
                continue
            cycle_id = self._open_cycle(current)
            if cycle_id:
                created.append(cycle_id)
        return created

    def commit_plan(self, cycle_id: str, plan: str, *, by: str, turn_id: str | None = None) -> dict[str, Any]:
        require_continuity(self.ctx)
        _actor(by)
        self._require_foreman_turn(cycle_id, turn_id)
        cycle = self._cycle(cycle_id)
        objective = self._refresh(self._row(str(cycle["objective_id"])))
        if str(objective["state"]) not in ("active", "paused"):
            raise FactoryError(
                "OBJECTIVE_STATE_INVALID",
                "目标不能提交计划",
                id=str(objective["id"]),
                state=str(objective["state"]),
                operation="commit_plan",
            )
        if cycle["plan_asset"]:
            if str(cycle["plan_asset"]) == plan:
                return self._cycle_view(cycle_id)
            raise FactoryError(
                "OBJECTIVE_STATE_INVALID",
                "周期已有另一份计划",
                id=cycle_id,
                state=str(cycle["state"]),
                operation="commit_plan",
            )
        if str(cycle["state"]) != "discovering":
            raise FactoryError(
                "OBJECTIVE_STATE_INVALID",
                "周期不能提交计划",
                id=cycle_id,
                state=str(cycle["state"]),
                operation="commit_plan",
            )
        data = self._validate_plan(objective, cycle, plan)
        disposition = str(data["disposition"])
        if disposition == "work":
            self._reserve_closeout(objective, cycle_id)
        now = utc_iso(self.ctx.clock.now())
        state = "executing" if disposition == "work" else disposition
        review = int(data["review_after_s"])
        context = {
            "epoch": self.epochs.current(),
            "contract": str(
                self._objective_asset(str(objective["asset_id"]))["document"]["data"]["contract"]
            ),
            "plan": plan,
            "standards": str(objective["standards"]),
        }
        with self.ctx.db.tx() as conn:
            current = conn.execute(
                "SELECT state, plan_asset FROM objective_cycles WHERE id = ?",
                (cycle_id,),
            ).fetchone()
            if current is None or current["plan_asset"]:
                raise FactoryError(
                    "OBJECTIVE_STATE_INVALID",
                    "周期不能提交计划",
                    id=cycle_id,
                    state="" if current is None else str(current["state"]),
                    operation="commit_plan",
                )
            if state in _TERMINAL:
                conn.execute(
                    """UPDATE objective_cycles
                       SET state = ?, plan_asset = ?, context = ?, finished_at = ?
                       WHERE id = ?""",
                    (state, plan, canonical_json(context), now, cycle_id),
                )
                due = utc_iso(self.ctx.clock.now() + _seconds(review))
                conn.execute(
                    """UPDATE objectives
                       SET active_cycle = NULL, next_due_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (due, now, objective["id"]),
                )
            else:
                conn.execute(
                    "UPDATE objective_cycles SET state = ?, plan_asset = ?, context = ? WHERE id = ?",
                    (state, plan, canonical_json(context), cycle_id),
                )
        return self._cycle_view(cycle_id)

    def dispatch(self, cycle_id: str, task_id: str, *, by: str, turn_id: str | None = None) -> dict[str, Any]:
        require_continuity(self.ctx)
        _actor(by)
        self._require_foreman_turn(cycle_id, turn_id)
        cycle = self._cycle(cycle_id)
        objective = self._refresh(self._row(str(cycle["objective_id"])))
        if str(objective["state"]) != "active":
            raise FactoryError(
                "OBJECTIVE_STATE_INVALID",
                "目标不能派工",
                id=str(objective["id"]),
                state=str(objective["state"]),
                operation="dispatch",
            )
        existing = self.ctx.db.query(
            "SELECT message_id, state FROM objective_dispatches WHERE cycle_id = ? AND task_id = ?",
            (cycle_id, task_id),
        )
        if existing:
            return {
                "cycle_id": cycle_id,
                "task_id": task_id,
                "message_id": str(existing[0]["message_id"]),
                "state": str(existing[0]["state"]),
            }
        if str(cycle["state"]) != "executing" or not cycle["plan_asset"]:
            raise FactoryError(
                "OBJECTIVE_STATE_INVALID",
                "周期不能派工",
                id=cycle_id,
                state=str(cycle["state"]),
                operation="dispatch",
            )
        task = _task(self._plan_data(str(cycle["plan_asset"])), task_id)
        from mao.agents import Mailbox

        with self.ctx.db.tx() as conn:
            again = conn.execute(
                "SELECT message_id, state FROM objective_dispatches WHERE cycle_id = ? AND task_id = ?",
                (cycle_id, task_id),
            ).fetchone()
            if again is not None:
                return {
                    "cycle_id": cycle_id,
                    "task_id": task_id,
                    "message_id": str(again["message_id"]),
                    "state": str(again["state"]),
                }
            message = Mailbox(self.ctx).post(
                str(task["agent"]),
                "objective_task",
                f"objective:{cycle_id}/{task_id}",
                {
                    "objective_id": objective["id"],
                    "cycle_id": cycle_id,
                    "task_id": task_id,
                    "line": task["line"],
                    "inputs": task["inputs"],
                    "plan": cycle["plan_asset"],
                },
                delivery_kind="objective_dispatch",
                delivery_source=f"{cycle_id}/{task_id}",
                origin={
                    "objective_id": objective["id"],
                    "cycle_id": cycle_id,
                    "task_id": task_id,
                    "parent_kind": "task",
                    "parent_id": f"{cycle_id}/{task_id}",
                },
                conn=conn,
            )
            conn.execute(
                """INSERT INTO objective_dispatches (cycle_id, task_id, message_id, state)
                   VALUES (?, ?, ?, 'pending')""",
                (cycle_id, task_id, message.id),
            )
        return {"cycle_id": cycle_id, "task_id": task_id, "message_id": message.id, "state": "pending"}

    def evaluate(
        self,
        cycle_id: str,
        evidence: Mapping[str, Mapping[str, str]],
        *,
        by: str,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        require_continuity(self.ctx)
        _actor(by)
        self._require_foreman_turn(cycle_id, turn_id)
        cycle = self._cycle(cycle_id)
        proof = _evidence_shape(cycle_id, evidence)
        stored = json.loads(cycle["evidence"] or "{}")
        if str(cycle["state"]) in _TERMINAL:
            if stored == proof:
                return self._cycle_view(cycle_id)
            raise FactoryError(
                "OBJECTIVE_STATE_INVALID",
                "终态周期不能改写证据",
                id=cycle_id,
                state=str(cycle["state"]),
                operation="evaluate",
            )
        if str(cycle["state"]) != "executing":
            raise FactoryError(
                "OBJECTIVE_STATE_INVALID",
                "周期不能评价",
                id=cycle_id,
                state=str(cycle["state"]),
                operation="evaluate",
            )
        for item in proof.values():
            if item["kind"] == "job":
                self._require_cycle_job(cycle_id, item["id"])
        objective = self._row(str(cycle["objective_id"]))
        asset = self._objective_asset(str(objective["asset_id"]))
        contract = str(asset["document"]["data"]["contract"])
        context = json.loads(cycle["context"] or "{}")
        from mao.learning import evaluate_objective_contract

        try:
            result = evaluate_objective_contract(
                self.ctx,
                self.epochs,
                cycle_id=cycle_id,
                contract=contract,
                plan=str(cycle["plan_asset"]),
                watermark=int(cycle["watermark"]),
                context=context,
                evidence=proof,
            )
        except FactoryError as exc:
            if exc.code == "OBJECTIVE_EVIDENCE_INVALID":
                raise
            raise FactoryError(
                "OBJECTIVE_EVIDENCE_INVALID",
                "证据不能用于本周期",
                cycle_id=cycle_id,
                case="",
                reason=exc.code,
            ) from exc
        state = "completed" if result["passed"] else "failed"
        now = utc_iso(self.ctx.clock.now())
        plan = self._plan_data(str(cycle["plan_asset"]))
        review = int(plan["review_after_s"])
        due = utc_iso(self.ctx.clock.now() + _seconds(review))
        with self.ctx.db.tx() as conn:
            current = conn.execute(
                "SELECT state FROM objective_cycles WHERE id = ?",
                (cycle_id,),
            ).fetchone()
            if current is None or str(current["state"]) != "executing":
                fresh = self._cycle(cycle_id)
                if json.loads(fresh["evidence"] or "{}") == proof:
                    return self._cycle_view(cycle_id)
                raise FactoryError(
                    "OBJECTIVE_STATE_INVALID",
                    "终态周期不能改写证据",
                    id=cycle_id,
                    state="" if current is None else str(current["state"]),
                    operation="evaluate",
                )
            conn.execute(
                """UPDATE objective_cycles
                   SET state = ?, checks = ?, evidence = ?, finished_at = ?
                   WHERE id = ?""",
                (
                    state,
                    canonical_json(result["checks"]),
                    canonical_json(result["evidence"]),
                    now,
                    cycle_id,
                ),
            )
            conn.execute(
                """UPDATE objectives
                   SET active_cycle = NULL, next_due_at = ?, updated_at = ?
                   WHERE id = ?""",
                (due, now, objective["id"]),
            )
        return self._cycle_view(cycle_id)

    def start_task(self, session: Any, arguments: Mapping[str, Any]) -> dict[str, Any]:
        require_continuity(self.ctx)
        cycle_id = arguments.get("cycle_id") or session.cycle_id
        task_id = arguments.get("task_id") or session.task_id
        if not cycle_id or not task_id:
            raise FactoryError("OBJECTIVE_FORBIDDEN", "周期任务不完整", id="", reason="identity")
        if session.cycle_id and session.cycle_id != cycle_id:
            raise FactoryError("OBJECTIVE_FORBIDDEN", "会话周期不符", id=str(cycle_id), reason="session")
        if session.task_id and session.task_id != task_id:
            raise FactoryError("OBJECTIVE_FORBIDDEN", "会话任务不符", id=str(cycle_id), reason="session")
        agent = str(session.agent or "")
        cycle = self._cycle(str(cycle_id))
        objective = self._refresh(self._row(str(cycle["objective_id"])))
        if str(objective["state"]) != "active":
            raise FactoryError(
                "OBJECTIVE_FORBIDDEN",
                "目标不能开工",
                id=str(objective["id"]),
                reason=str(objective["state"]),
            )
        if not cycle["plan_asset"] or str(cycle["state"]) != "executing":
            raise FactoryError("OBJECTIVE_FORBIDDEN", "周期没有可执行计划", id=str(cycle_id), reason="plan")
        task = _task(self._plan_data(str(cycle["plan_asset"])), str(task_id))
        if agent != str(task["agent"]):
            raise FactoryError("OBJECTIVE_FORBIDDEN", "不是该任务的岗位", id=str(cycle_id), reason="agent")
        grant = json.loads(objective["grant"])
        if agent not in grant["agents"] or str(task["line"]) not in grant["lines"]:
            raise FactoryError("OBJECTIVE_FORBIDDEN", "超出授权范围", id=str(cycle_id), reason="scope")
        line = str(arguments.get("line") or "")
        inputs = arguments.get("inputs")
        if (
            line != str(task["line"])
            or not isinstance(inputs, Mapping)
            or dict(inputs) != dict(task["inputs"])
        ):
            raise FactoryError("OBJECTIVE_FORBIDDEN", "开工参数与计划不符", id=str(cycle_id), reason="inputs")
        request_key = arguments.get("request_key")
        if not isinstance(request_key, str) or not request_key:
            raise FactoryError("OBJECTIVE_FORBIDDEN", "缺少请求键", id=str(cycle_id), reason="request_key")
        from mao.results import ResultStore

        started = ResultStore(self.ctx, self.epochs).start(
            agent,
            line,
            dict(inputs),
            request_key=request_key,
            cycle_id=str(cycle_id),
            task_id=str(task_id),
            turn_id=session.turn_id,
        )
        self._origin_run(str(objective["id"]), str(cycle_id), str(task_id), str(started["id"]))
        return started

    def _open_cycle(self, objective: Any) -> str:
        from mao.agents import Mailbox

        objective_id = str(objective["id"])
        number_row = self.ctx.db.query(
            "SELECT COALESCE(MAX(number), 0) AS n FROM objective_cycles WHERE objective_id = ?",
            (objective_id,),
        )
        number = int(number_row[0]["n"]) + 1
        cycle_id = self.ctx.ids.new("oc")
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            current = conn.execute(
                "SELECT active_cycle, state FROM objectives WHERE id = ?",
                (objective_id,),
            ).fetchone()
            if current is None or current["active_cycle"] or str(current["state"]) != "active":
                return str(current["active_cycle"]) if current is not None and current["active_cycle"] else ""
            watermark = conn.execute("SELECT COALESCE(MAX(seq), 0) AS n FROM events").fetchone()
            conn.execute(
                """INSERT INTO objective_cycles (
                       id, objective_id, number, state, plan_asset, checks, evidence, watermark,
                       context, created_at, finished_at
                   ) VALUES (?, ?, ?, 'discovering', NULL, '[]', '{}', ?, '{}', ?, NULL)""",
                (cycle_id, objective_id, number, int(watermark["n"]), now),
            )
            conn.execute(
                "UPDATE objectives SET active_cycle = ?, updated_at = ? WHERE id = ?",
                (cycle_id, now, objective_id),
            )
            Mailbox(self.ctx).post(
                "foreman",
                "objective_cycle",
                f"objective:{objective_id}:{number}",
                {
                    "objective_id": objective_id,
                    "cycle_id": cycle_id,
                    "number": number,
                    "asset_id": objective["asset_id"],
                },
                delivery_kind="objective_cycle",
                delivery_source=cycle_id,
                origin={
                    "objective_id": objective_id,
                    "cycle_id": cycle_id,
                    "task_id": None,
                    "parent_kind": "cycle",
                    "parent_id": cycle_id,
                },
                conn=conn,
            )
        return cycle_id

    def _require_foreman_turn(self, cycle_id: str, turn_id: str | None) -> None:
        if not turn_id:
            raise FactoryError("OBJECTIVE_FORBIDDEN", "缺少本周期厂长轮次", id=cycle_id, reason="turn")
        rows = self.ctx.db.query("SELECT * FROM agent_turns WHERE id = ?", (turn_id,))
        if not rows:
            raise FactoryError("OBJECTIVE_FORBIDDEN", "轮次不存在", id=cycle_id, reason="turn")
        turn = rows[0]
        cycle = self._cycle(cycle_id)
        if (
            str(turn["state"]) != "running"
            or str(turn["agent"]) != "foreman"
            or turn["owner"] is None
            or turn["lease_generation"] is None
            or str(turn["cycle_id"] or "") != cycle_id
            or str(turn["objective_id"] or "") != str(cycle["objective_id"])
        ):
            raise FactoryError("OBJECTIVE_FORBIDDEN", "轮次没有绑定本周期", id=cycle_id, reason="turn")
        from mao.durable import LeaseStore

        LeaseStore(self.ctx).guard(
            "agents",
            owner=str(turn["owner"]),
            generation=int(turn["lease_generation"]),
        )

    def _require_cycle_job(self, cycle_id: str, job_id: str) -> None:
        rows = self.ctx.db.query(
            """SELECT n.cache_hit FROM run_nodes n
               JOIN run_requests q ON q.run_id = n.run_id
               WHERE q.cycle_id = ? AND n.job_id = ?""",
            (cycle_id, job_id),
        )
        if not rows or int(rows[0]["cache_hit"]):
            raise FactoryError(
                "OBJECTIVE_EVIDENCE_INVALID",
                "job 不属于本周期任务",
                cycle_id=cycle_id,
                case="",
                reason="binding",
            )

    def _validate_plan(self, objective: Any, cycle: Any, plan: str) -> dict[str, Any]:
        from mao.assets import AssetStore

        record = AssetStore(self.ctx).get(plan)
        if record.get("released"):
            raise FactoryError("OBJECTIVE_INVALID", "计划已释放", field="plan", reason="released")
        document = record["document"]
        if document.get("kind") != "objective_plan":
            raise FactoryError("OBJECTIVE_INVALID", "不是目标计划", field="kind", reason="kind")
        data = document.get("data")
        if not isinstance(data, dict) or data.get("cycle_id") != cycle["id"]:
            raise FactoryError("OBJECTIVE_INVALID", "计划不属于本周期", field="cycle_id", reason="cycle")
        observations = data.get("observations")
        if not isinstance(observations, list) or not observations:
            raise FactoryError("OBJECTIVE_INVALID", "观察不能为空", field="observations", reason="empty")
        evidence_ids: list[str] = []
        for item in observations:
            if not isinstance(item, dict) or not item.get("asset_id"):
                raise FactoryError("OBJECTIVE_INVALID", "观察不合法", field="observations", reason="shape")
            observed = AssetStore(self.ctx).get(str(item["asset_id"]))
            if observed.get("released"):
                raise FactoryError("OBJECTIVE_INVALID", "观察已释放", field="observations", reason="released")
        for candidate in data.get("candidates") or []:
            if isinstance(candidate, dict):
                evidence_ids.extend(str(item) for item in candidate.get("evidence") or [])
        requires = list(document.get("requires") or [])
        needed = [str(objective["asset_id"])]
        needed.extend(str(item["asset_id"]) for item in observations)
        needed.extend(evidence_ids)
        if any(item not in requires for item in needed):
            raise FactoryError(
                "OBJECTIVE_INVALID", "计划引用未进入 requires", field="requires", reason="requires"
            )
        disposition = data.get("disposition")
        if disposition not in ("work", "idle", "blocked"):
            raise FactoryError("OBJECTIVE_INVALID", "处置不合法", field="disposition", reason="disposition")
        grant = json.loads(objective["grant"])
        review = data.get("review_after_s")
        if (
            isinstance(review, bool)
            or not isinstance(review, int)
            or review < int(grant["every_s"])
            or review > 86400
        ):
            raise FactoryError("OBJECTIVE_INVALID", "复查间隔不合法", field="review_after_s", reason="review")
        if not isinstance(data.get("reason"), str) or not data["reason"]:
            raise FactoryError("OBJECTIVE_INVALID", "理由不能为空", field="reason", reason="empty")
        selected = data.get("selected") or []
        tasks = data.get("tasks") or []
        if disposition == "work":
            if not data.get("candidates") or not selected or not tasks:
                raise FactoryError(
                    "OBJECTIVE_INVALID", "工作计划缺少候选或任务", field="tasks", reason="empty"
                )
            self._check_tasks(grant, tasks, selected)
        elif selected or tasks:
            raise FactoryError("OBJECTIVE_INVALID", "空闲或阻塞计划不能带任务", field="tasks", reason="tasks")
        return data

    def _check_tasks(self, grant: Mapping[str, Any], tasks: Any, selected: Any) -> None:
        snapshot = self.epochs.materialize(self.epochs.current())
        seen: set[str] = set()
        chosen = {str(item) for item in selected}
        for task in tasks:
            if not isinstance(task, dict) or not task.get("id") or not task.get("candidate"):
                raise FactoryError("OBJECTIVE_INVALID", "任务不合法", field="tasks", reason="shape")
            task_id = str(task["id"])
            if task_id in seen or str(task["candidate"]) not in chosen:
                raise FactoryError("OBJECTIVE_INVALID", "任务候选不合法", field="tasks", reason="candidate")
            seen.add(task_id)
            agent = str(task.get("agent") or "")
            line = str(task.get("line") or "")
            if agent not in grant["agents"] or line not in grant["lines"]:
                raise FactoryError("OBJECTIVE_FORBIDDEN", "任务超出授权", id=agent or line, reason="scope")
            from mao.agents import agent_config

            if line not in agent_config(snapshot, agent).lines:
                raise FactoryError("OBJECTIVE_FORBIDDEN", "岗位不能跑该产线", id=agent, reason="line")
            inputs = task.get("inputs")
            if not isinstance(inputs, dict):
                raise FactoryError("OBJECTIVE_INVALID", "任务输入不合法", field="inputs", reason="inputs")

    def _reserve_closeout(self, objective: Any, cycle_id: str) -> None:
        amount = _foreman_turn_usd(self.ctx, self.epochs)
        self._reserve(str(objective["id"]), f"closeout:{cycle_id}", amount, json.loads(objective["grant"]))

    def _reserve(self, objective_id: str, operation_id: str, amount: float, grant: Mapping[str, Any]) -> None:
        if amount <= 0:
            raise FactoryError(
                "OBJECTIVE_BUDGET_EXCEEDED",
                "没有可保留的费用上界",
                id=objective_id,
                scope="total",
                remaining_usd=0,
            )
        spent, reserved = _sums(self.ctx, f"objective:{objective_id}")
        day_spent, day_reserved = _sums(self.ctx, _day_scope(self.ctx, objective_id))
        total_left = float(grant["total_usd"]) - spent - reserved
        day_left = float(grant["daily_usd"]) - day_spent - day_reserved
        if amount > total_left + 1e-9 or amount > day_left + 1e-9:
            raise FactoryError(
                "OBJECTIVE_BUDGET_EXCEEDED",
                "目标预算不足",
                id=objective_id,
                scope="total" if amount > total_left + 1e-9 else "daily",
                remaining_usd=min(total_left, day_left),
            )
        ledger = BudgetLedger(self.ctx)
        ledger.reserve(operation_id, f"objective:{objective_id}", amount)
        ledger.reserve(operation_id, _day_scope(self.ctx, objective_id), amount)

    def _plan_data(self, plan: str) -> dict[str, Any]:
        from mao.assets import AssetStore

        return dict(AssetStore(self.ctx).get(plan)["document"]["data"])

    def _objective_asset(self, asset_id: str) -> dict[str, Any]:
        from mao.assets import AssetStore

        record = AssetStore(self.ctx).get(asset_id)
        document = record["document"]
        if document.get("kind") != "objective":
            raise FactoryError("OBJECTIVE_INVALID", "不是目标资产", field="kind", reason="kind")
        data = document.get("data")
        if (
            not isinstance(data, dict)
            or not data.get("intent")
            or not data.get("observation_scope")
            or not data.get("contract")
        ):
            raise FactoryError("OBJECTIVE_INVALID", "目标内容不完整", field="data", reason="data")
        if str(data["contract"]) not in list(document.get("requires") or []):
            raise FactoryError("OBJECTIVE_INVALID", "目标未引用契约", field="requires", reason="contract")
        return record

    def _origin_run(self, objective_id: str, cycle_id: str, task_id: str, run_id: str) -> None:
        with self.ctx.db.tx() as conn:
            existing = conn.execute(
                "SELECT 1 FROM execution_origins WHERE kind = 'run' AND execution_id = ?",
                (run_id,),
            ).fetchone()
            if existing is not None:
                return
            conn.execute(
                """INSERT INTO execution_origins
                   (kind, execution_id, objective_id, cycle_id, task_id, parent_kind, parent_id)
                   VALUES ('run', ?, ?, ?, ?, 'task', ?)""",
                (run_id, objective_id, cycle_id, task_id, f"{cycle_id}/{task_id}"),
            )

    def _refresh(self, row: Any) -> Any:
        grant = json.loads(row["grant"])
        now = utc_iso(self.ctx.clock.now())
        state = str(row["state"])
        reason = str(row["reason"] or "")
        if state in ("active", "paused") and str(grant["expires_at"]) <= now:
            return self._set_state(str(row["id"]), "expired", "expired")
        if state == "paused" and reason == "daily_budget":
            spent, reserved = _sums(self.ctx, f"objective:{row['id']}")
            day_spent, day_reserved = _sums(self.ctx, _day_scope(self.ctx, str(row["id"])))
            if spent + reserved + 1e-9 < float(
                grant["total_usd"]
            ) and day_spent + day_reserved + 1e-9 < float(grant["daily_usd"]):
                return self._set_state(str(row["id"]), "active", "")
        return row

    def _set_state(self, objective_id: str, state: str, reason: str) -> Any:
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE objectives SET state = ?, reason = ?, updated_at = ? WHERE id = ?",
                (state, reason, now, objective_id),
            )
            row = conn.execute("SELECT * FROM objectives WHERE id = ?", (objective_id,)).fetchone()
        return row

    def _row(self, objective_id: str) -> Any:
        rows = self.ctx.db.query("SELECT * FROM objectives WHERE id = ?", (objective_id,))
        if not rows:
            raise FactoryError("OBJECTIVE_NOT_FOUND", "目标不存在", id=objective_id)
        return rows[0]

    def _cycle(self, cycle_id: str) -> Any:
        rows = self.ctx.db.query("SELECT * FROM objective_cycles WHERE id = ?", (cycle_id,))
        if not rows:
            raise FactoryError("OBJECTIVE_NOT_FOUND", "周期不存在", id=cycle_id)
        return rows[0]

    def _view(self, row: Any) -> dict[str, Any]:
        spent, reserved = _sums(self.ctx, f"objective:{row['id']}")
        return {
            "id": str(row["id"]),
            "asset_id": str(row["asset_id"]),
            "grant": json.loads(row["grant"]),
            "state": str(row["state"]),
            "next_due_at": str(row["next_due_at"]),
            "active_cycle": None if row["active_cycle"] is None else str(row["active_cycle"]),
            "spent_usd": spent,
            "reserved_usd": reserved,
            "reason": str(row["reason"] or ""),
        }

    def _cycle_view(self, cycle_id: str) -> dict[str, Any]:
        cycle = self._cycle(cycle_id)
        objective = self._row(str(cycle["objective_id"]))
        return {
            "id": str(cycle["id"]),
            "objective_id": str(cycle["objective_id"]),
            "number": int(cycle["number"]),
            "state": str(cycle["state"]),
            "plan_asset": None if cycle["plan_asset"] is None else str(cycle["plan_asset"]),
            "checks": json.loads(cycle["checks"] or "[]"),
            "evidence": json.loads(cycle["evidence"] or "{}"),
            "next_due_at": str(objective["next_due_at"]),
        }


def note_turn_reserved(ctx: Any, epochs: Any, turn_id: str, amount: float) -> None:
    if not continuity_enabled(ctx):
        return
    rows = ctx.db.query("SELECT objective_id FROM agent_turns WHERE id = ?", (turn_id,))
    if not rows or not rows[0]["objective_id"]:
        return
    store = ObjectiveStore(ctx, epochs)
    objective = store._row(str(rows[0]["objective_id"]))
    store._reserve(str(objective["id"]), f"turn:{turn_id}", float(amount), json.loads(objective["grant"]))


def note_turn_settled(ctx: Any, turn_id: str, cost: float) -> None:
    if not continuity_enabled(ctx):
        return
    rows = ctx.db.query("SELECT objective_id FROM agent_turns WHERE id = ?", (turn_id,))
    if not rows or not rows[0]["objective_id"]:
        return
    objective_id = str(rows[0]["objective_id"])
    operation_id = f"turn:{turn_id}"
    scopes = [
        str(row["scope"])
        for row in ctx.db.query(
            "SELECT scope FROM budget_entries WHERE operation_id = ? AND kind = 'reserve'",
            (operation_id,),
        )
    ]
    ledger = BudgetLedger(ctx)
    for scope in scopes:
        reserved = _kind_amount(ctx, operation_id, scope, "reserve")
        ledger.settle(operation_id, scope, float(cost))
        unused = reserved - float(cost)
        if unused > 1e-9:
            ledger.release(operation_id, scope, unused)
    _pause_if_exhausted(ctx, objective_id)


def _pause_if_exhausted(ctx: Any, objective_id: str) -> None:
    rows = ctx.db.query("SELECT * FROM objectives WHERE id = ?", (objective_id,))
    if not rows or str(rows[0]["state"]) != "active":
        return
    grant = json.loads(rows[0]["grant"])
    spent, reserved = _sums(ctx, f"objective:{objective_id}")
    day_spent, day_reserved = _sums(ctx, _day_scope(ctx, objective_id))
    reason = ""
    if spent + reserved + 1e-9 >= float(grant["total_usd"]):
        reason = "total_budget"
    elif day_spent + day_reserved + 1e-9 >= float(grant["daily_usd"]):
        reason = "daily_budget"
    if not reason:
        return
    now = utc_iso(ctx.clock.now())
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE objectives SET state = 'paused', reason = ?, updated_at = ? WHERE id = ? AND state = 'active'",
            (reason, now, objective_id),
        )


def _sums(ctx: Any, scope: str) -> tuple[float, float]:
    rows = ctx.db.query(
        "SELECT kind, COALESCE(SUM(amount_usd), 0) AS amount FROM budget_entries WHERE scope = ? GROUP BY kind",
        (scope,),
    )
    amounts = {str(row["kind"]): float(row["amount"]) for row in rows}
    spent = amounts.get("settle", 0.0)
    reserved = amounts.get("reserve", 0.0) - amounts.get("release", 0.0) - spent
    return spent, max(0.0, reserved)


def _kind_amount(ctx: Any, operation_id: str, scope: str, kind: str) -> float:
    rows = ctx.db.query(
        "SELECT amount_usd FROM budget_entries WHERE operation_id = ? AND scope = ? AND kind = ?",
        (operation_id, scope, kind),
    )
    return 0.0 if not rows else float(rows[0]["amount_usd"])


def _day_scope(ctx: Any, objective_id: str) -> str:
    return f"objective:{objective_id}:day:{utc_iso(ctx.clock.now())[:10]}"


def _validate_grant(store: ObjectiveStore, grant: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(grant, Mapping) or any(field not in grant for field in _GRANT_FIELDS):
        raise FactoryError("OBJECTIVE_INVALID", "授权字段不完整", field="grant", reason="fields")
    unknown = [key for key in grant if key not in _GRANT_FIELDS and key != "extensions"]
    if unknown:
        raise FactoryError("OBJECTIVE_INVALID", "授权有未知字段", field=str(unknown[0]), reason="fields")
    agents = _names(grant.get("agents"), "agents")
    lines = _names(grant.get("lines"), "lines")
    _money(grant.get("total_usd"), "total_usd")
    _money(grant.get("daily_usd"), "daily_usd")
    every = grant.get("every_s")
    cycle = grant.get("max_cycle_s")
    if isinstance(every, bool) or not isinstance(every, int) or every < 60 or every > 86400:
        raise FactoryError("OBJECTIVE_INVALID", "周期间隔不合法", field="every_s", reason="every_s")
    if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 60:
        raise FactoryError("OBJECTIVE_INVALID", "周期时限不合法", field="max_cycle_s", reason="max_cycle_s")
    expires = grant.get("expires_at")
    if not isinstance(expires, str) or expires <= utc_iso(store.ctx.clock.now()):
        raise FactoryError("OBJECTIVE_INVALID", "到期时间不在将来", field="expires_at", reason="expires_at")
    snapshot = store.epochs.materialize(store.epochs.current())
    from mao.agents import agent_config, agent_names

    known = set(agent_names(snapshot, store.ctx.root))
    if any(name not in known for name in agents):
        raise FactoryError("OBJECTIVE_INVALID", "授权岗位不存在", field="agents", reason="agents")
    for line in lines:
        try:
            snapshot.doc(line)
        except FactoryError as exc:
            raise FactoryError("OBJECTIVE_INVALID", "授权产线不存在", field="lines", reason="lines") from exc
    if not any(line in agent_config(snapshot, name).lines for name in agents for line in lines):
        raise FactoryError("OBJECTIVE_INVALID", "没有岗位能跑授权产线", field="lines", reason="lines")
    if "extensions" in grant:
        _extensions(grant.get("extensions"))
    return dict(grant)


def _extensions(value: Any) -> None:
    if not isinstance(value, dict):
        raise FactoryError("OBJECTIVE_INVALID", "扩展范围不合法", field="extensions", reason="extensions")
    allowed = {"agent_prefixes", "line_prefixes", "max_risk_tier", "policies"}
    if any(key not in allowed for key in value):
        raise FactoryError("OBJECTIVE_INVALID", "扩展范围不合法", field="extensions", reason="extensions")


def _names(value: Any, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise FactoryError("OBJECTIVE_INVALID", "授权名单不合法", field=field, reason=field)
    if len(set(value)) != len(value):
        raise FactoryError("OBJECTIVE_INVALID", "授权名单有重复", field=field, reason=field)
    return [str(item) for item in value]


def _money(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0:
        raise FactoryError("OBJECTIVE_INVALID", "金额必须为正", field=field, reason=field)
    return float(value)


def _standards_text(ctx: Any, contract_id: str) -> str:
    from mao.assets import AssetStore
    from mao.learning import _require_authorized, _standards

    record = AssetStore(ctx).get(contract_id)
    _require_authorized(ctx, record)
    return canonical_json(_standards(ctx, record))


def _foreman_turn_usd(ctx: Any, epochs: Any) -> float:
    from mao.agents import foreman_agent, resident_foreman

    foreman = resident_foreman(ctx.root)
    if foreman is None:
        raise FactoryError("CONTINUITY_CONFIG_INVALID", "厂长不是常驻", reason="foreman")
    snapshot = epochs.materialize(epochs.current())
    return float(foreman_agent(snapshot, foreman).turn_usd)


def _task(data: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    for task in data.get("tasks") or []:
        if isinstance(task, dict) and str(task.get("id")) == task_id:
            return task
    raise FactoryError("OBJECTIVE_NOT_FOUND", "任务不存在", id=task_id)


def _evidence_shape(cycle_id: str, evidence: Mapping[str, Mapping[str, str]]) -> dict[str, dict[str, str]]:
    if not isinstance(evidence, Mapping):
        raise FactoryError(
            "OBJECTIVE_EVIDENCE_INVALID", "证据格式不合法", cycle_id=cycle_id, case="", reason="shape"
        )
    found: dict[str, dict[str, str]] = {}
    for name, item in evidence.items():
        if not isinstance(item, Mapping) or set(item) != {"kind", "id"}:
            raise FactoryError(
                "OBJECTIVE_EVIDENCE_INVALID",
                "证据项不合法",
                cycle_id=cycle_id,
                case=str(name),
                reason="shape",
            )
        if item.get("kind") not in ("job", "turn") or not isinstance(item.get("id"), str):
            raise FactoryError(
                "OBJECTIVE_EVIDENCE_INVALID",
                "证据项不合法",
                cycle_id=cycle_id,
                case=str(name),
                reason="shape",
            )
        found[str(name)] = {"kind": str(item["kind"]), "id": str(item["id"])}
    return found


def _actor(by: str) -> None:
    if not isinstance(by, str) or not by:
        raise FactoryError("OBJECTIVE_INVALID", "操作者不能为空", field="by", reason="by")


def _factory(ctx: Any) -> dict[str, Any]:
    path = ctx.root / "factory.json"
    if not path.is_file():
        return {}
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def _seconds(value: int) -> Any:
    from datetime import timedelta

    return timedelta(seconds=value)
