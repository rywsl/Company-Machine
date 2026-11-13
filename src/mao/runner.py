"""单工位执行：准备沙箱、收货、检验、封存、重试与运行中审批。"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from mao.base import canonical_json, parse_duration, utc_iso
from mao.ctl.server import CtlServer
from mao.errors import FactoryError
from mao.gate import Gate
from mao.jobs import TERMINAL, Job, JobState, dump_job, ensure_edge, load_job
from mao.operator import build_invocation, expand_control_vars, parse_transcript, render_brief
from mao.sandbox import Mount, SandboxSpec
from mao.values import approval_env, bean_value, params_value
from mrl.errors import EvalError

__all__ = ["Job", "JobState", "Runner", "TERMINAL", "dump_job", "load_job"]

_IMAGE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_MEMORY = re.compile(r"^([0-9]+)(Mi|M|MB|Gi|G|GB)$")
_LOG = logging.getLogger("mao.runner")
_KEEP = object()


class Runner:
    def __init__(self, ctx: Any, snapshot: Any, *, gateway: Any = None) -> None:
        self.ctx = ctx
        self.snapshot = snapshot
        self.gateway = gateway
        self.gate = Gate(
            snapshot,
            ctx.warehouse,
            ctx.provider,
            ctx.clock,
            ctx.ids,
            workdir=ctx.root / "tmp" / "gate",
        )

    def submit(
        self,
        station_ref: str,
        inputs: Mapping[str, str | Sequence[str]],
        params: Mapping[str, Any],
        run: Mapping[str, Any] | None = None,
    ) -> Job:
        station_key = self.snapshot.key("Station", station_ref)
        station = self.snapshot.spec(station_key)
        if station.get("role") == "source" and inputs:
            raise FactoryError(
                "INTAKE_REJECTED",
                "Source 工位不能有输入",
                problems=[
                    {
                        "port": next(iter(inputs)),
                        "index": 0,
                        "reason": "unexpected",
                        "detail": "source",
                        "bean_id": "",
                    }
                ],
            )
        self._policy_defense(station_key, station, run)
        stored, checked = _split_inputs(inputs)
        from mao.warehouse import reject_expired

        reject_expired(self.ctx.warehouse, stored)
        if inputs or ":" not in str(station_ref):
            self.gate.check_inputs(station_key, checked)
        normalized = params_value(self.snapshot, station_key, params)
        now = utc_iso(self.ctx.clock.now())
        job_id = self.ctx.ids.new("j")
        with self.ctx.db.tx() as conn:
            conn.execute(
                """INSERT INTO jobs
                   (id, station, epoch, state, attempt, inputs, params, outputs, cost_usd, error,
                    feedback, created_at, updated_at)
                   VALUES (?, ?, ?, 'PENDING', 1, ?, ?, '{}', 0, NULL, NULL, ?, ?)""",
                (
                    job_id,
                    station_key,
                    self.snapshot.epoch,
                    canonical_json(stored),
                    canonical_json(normalized),
                    now,
                    now,
                ),
            )
            _insert_event(
                conn,
                self.ctx,
                "job.created",
                job_id,
                {
                    "station": station_key,
                    "epoch": self.snapshot.epoch,
                    "inputs": stored,
                    "params": normalized,
                },
            )
        return load_job(self.ctx.db, job_id)

    def get(self, job_id: str) -> Job:
        return load_job(self.ctx.db, job_id)

    def run(self, job_id: str) -> Job:
        job = load_job(self.ctx.db, job_id)
        if job.state in TERMINAL:
            return job
        if job.state not in {
            JobState.PENDING,
            JobState.PROVISIONING,
            JobState.RUNNING,
            JobState.AWAITING_APPROVAL,
            JobState.GATING,
            JobState.REJECTED,
            JobState.FAILED,
        }:
            raise FactoryError(
                "JOB_STATE_INVALID", "run 看见不认识的状态", job_id=job.id, state=job.state.value
            )
        from mao.durable import LeaseKeeper, LeaseStore

        store = LeaseStore(self.ctx)
        resource = f"job:{job.id}"
        if not store.acquire(resource, owner=self.ctx.owner):
            holder = store.holder(resource)
            raise FactoryError(
                "JOB_LEASED",
                "job 租约被占用",
                job=job.id,
                owner="" if holder is None else holder.owner,
                expires_at="" if holder is None else holder.expires_at,
            )
        keeper = LeaseKeeper(store, resource, owner=self.ctx.owner)
        keeper.start()
        try:
            while job.state not in TERMINAL:
                if keeper.lost.is_set():
                    return load_job(self.ctx.db, job.id)
                job = self._step(job)
                if job.state == JobState.FAILED and self._budget_code(job.id):
                    return job
            return job
        finally:
            keeper.stop()
            store.release(resource, owner=self.ctx.owner)

    def decide(self, approval_id: str, decision: str, *, by: str, note: str = "") -> None:
        rows = self.ctx.db.query("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if not rows:
            raise FactoryError("APPROVAL_NOT_FOUND", f"找不到审批 {approval_id}", approval_id=approval_id)
        row = rows[0]
        if row["status"] != "pending" or decision not in ("approve", "deny"):
            raise FactoryError(
                "APPROVAL_STATE_INVALID",
                "审批不在可决定状态",
                approval_id=approval_id,
                status=row["status"],
            )
        status = "approved" if decision == "approve" else "denied"
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE approvals SET status = ?, by = ?, note = ?, decided_at = ? WHERE id = ?",
                (status, by, note, now, approval_id),
            )

    def _step(self, job: Job) -> Job:
        if job.state == JobState.PENDING:
            problem = self._static_problem(job)
            if problem is not None and problem.code == "FORM_UNSUPPORTED":
                raise problem
            claimed = self._claim_sink(job)
            if claimed == "duplicate":
                return self._transition(job, JobState.SEALED, "duplicate")
            if isinstance(claimed, FactoryError):
                return self._dead(job, claimed)
            job = self._transition(job, JobState.PROVISIONING, "start")
            if problem is not None:
                return self._dead(job, problem)
            return self._provision_execute(job, already_running=False)
        if job.state == JobState.PROVISIONING:
            return self._provision_execute(job, already_running=False)
        if job.state == JobState.AWAITING_APPROVAL:
            job = self._transition(job, JobState.RUNNING, "resume")
            return self._provision_execute(job, already_running=True)
        if job.state == JobState.RUNNING:
            return self._provision_execute(job, already_running=True)
        if job.state == JobState.GATING:
            return self._gate_recovery(job)
        if job.state in (JobState.REJECTED, JobState.FAILED):
            return self._retry_or_dead(job)
        raise FactoryError("JOB_STATE_INVALID", "run 看见不认识的状态", job_id=job.id, state=job.state.value)

    def _provision_execute(self, job: Job, *, already_running: bool) -> Job:
        sandbox = None
        try:
            prepared = self._bind_control(job, self._prepare(job, reset_out=True))
            self._register_cap(job)
            sandbox = self.ctx.provider.open(prepared.spec)
            if not already_running:
                job = self._transition(job, JobState.RUNNING, "ready")
            return self._execute(job, sandbox, prepared)
        except FactoryError as exc:
            job = load_job(self.ctx.db, job.id)
            if job.state in TERMINAL or job.state in (JobState.FAILED, JobState.REJECTED):
                return job
            if job.state in (
                JobState.PROVISIONING,
                JobState.RUNNING,
                JobState.GATING,
                JobState.AWAITING_APPROVAL,
            ):
                return self._fail(job, exc.code, exc.message, dict(exc.detail))
            raise
        finally:
            if sandbox is not None:
                try:
                    sandbox.close()
                except Exception:
                    _LOG.exception("沙箱 close 失败")

    def _gate_recovery(self, job: Job) -> Job:
        sandbox = None
        try:
            prepared = self._prepare(job, reset_out=False)
            sandbox = self.ctx.provider.open(prepared.spec)
            return self._finish(job, sandbox, prepared)
        finally:
            if sandbox is not None:
                try:
                    sandbox.close()
                except Exception:
                    _LOG.exception("沙箱 close 失败")

    def _prepare(self, job: Job, *, reset_out: bool) -> _Prepared:
        station = self.snapshot.spec(job.station)
        machine_key = self.snapshot.key("Machine", station["machine"])
        machine = self.snapshot.spec(machine_key)
        recipe_name = station["recipe"]
        recipe = (machine.get("recipes") or {})[recipe_name]
        runtime_key = self.snapshot.key("OperatorRuntime", station["operator"]["runtime"])
        runtime = self.snapshot.spec(runtime_key)
        attempt_dir = self.ctx.root / "jobs" / job.id / str(job.attempt)
        for name in ("in", "out", "ctl", "workspace", "logs", "tmp"):
            (attempt_dir / name).mkdir(parents=True, exist_ok=True)
        self._workspace(job, attempt_dir, machine)
        if reset_out:
            _clear_children(attempt_dir / "in")
            _clear_children(attempt_dir / "out")
        beans = self._materialize(job, station, attempt_dir / "in")
        limits = station.get("limits") or {}
        budget = float(limits.get("budget_usd", 0.0))
        timeout_s = parse_duration(limits["timeout"]) if "timeout" in limits else 600.0
        brief = render_brief(
            self.snapshot,
            job.station,
            inputs=beans,
            params=job.params,
            attempt=job.attempt,
            feedback=job.feedback,
            budget_usd=budget,
        )
        ctl = attempt_dir / "ctl"
        for name in ("requests", "responses", "submissions"):
            (ctl / name).mkdir(exist_ok=True)
        _write_json(
            ctl / "job.json",
            {
                "job_id": job.id,
                "attempt": job.attempt,
                "station": job.station,
                "epoch": job.epoch,
                "inputs": _bean_inputs(station, beans),
                "params": job.params,
                "budget_usd": budget,
                "timeout_s": timeout_s,
            },
        )
        _write_json(ctl / "contract.json", self._contract(job.station, station, machine))
        (ctl / "brief.md").write_bytes(brief.markdown.encode("utf-8"))
        spec = self._spec(job, station, machine, runtime, attempt_dir)
        return _Prepared(
            station, machine_key, machine, recipe_name, recipe, runtime, beans, attempt_dir, brief, spec
        )

    def _execute(self, job: Job, sandbox: Any, prepared: _Prepared) -> Job:
        if prepared.machine.get("form") == "service" and self.gateway is not None:
            return self._execute_service(job, sandbox, prepared)
        brief = prepared.brief
        invocation = build_invocation(
            self.snapshot, job.station, brief, inputs=prepared.beans, params=job.params
        )
        try:
            invocation = expand_control_vars(invocation, self._control_values(job))
        except FactoryError as exc:
            return self._fail(job, exc.code, exc.message, dict(exc.detail))
        logs = prepared.attempt_dir / "logs"
        stdout = logs / "operator.stdout"
        stderr = logs / "operator.stderr"
        handler = _CtlHandler(self, job, prepared)
        server = CtlServer(prepared.attempt_dir / "ctl", handler, self.ctx.clock)
        server.start(0.2)
        try:
            result = sandbox.exec(
                list(invocation.argv),
                timeout_s=invocation.timeout_s,
                stdout=stdout,
                stderr=stderr,
                env=invocation.env,
                workdir="/workspace",
            )
        finally:
            server.stop()
        stop = self._budget_code(job.id)
        counted, spent = self._usage_spent(job.id)
        if stop:
            if counted:
                job = self._set_cost(job, spent)
            return self._budget_fail(job, stop)
        cost = 0.0
        summary = None
        if invocation.transcript in {"stream-json", "codex-jsonl"}:
            summary = parse_transcript(stdout, invocation.transcript)
            cost = summary.cost_usd
        if counted:
            cost = spent
        job = self._set_cost(job, cost)
        budget = float((prepared.station.get("limits") or {}).get("budget_usd", 0.0))
        if counted == 0 and cost > budget:
            return self._dead(
                job,
                {
                    "code": "BUDGET_EXCEEDED",
                    "message": "成本超过工位预算",
                    "detail": {"job": job.id, "cost_usd": cost, "budget_usd": budget},
                },
            )
        if result.timed_out:
            return self._fail(job, "OPERATOR_TIMEOUT", "操作工超时", {"timeout_s": invocation.timeout_s})
        fail_path = prepared.attempt_dir / "ctl" / "fail.json"
        if fail_path.is_file():
            reason = _fail_reason(fail_path)
            return self._fail(job, "OPERATOR_FAILED", "操作工声明失败", {"reason": reason})
        if summary is not None and summary.is_error:
            return self._fail(job, "AGENT_ERROR", "agent 报告执行失败", {"result": summary.result})
        if result.exit_code != 0:
            if prepared.station.get("role") == "sink":
                return self._dead(
                    job,
                    {
                        "code": "SINK_OUTCOME_UNKNOWN",
                        "message": "Sink 操作工启动后失败",
                        "detail": {"job": job.id, "cause": {"exit_code": result.exit_code}},
                    },
                )
            return self._fail(job, "OPERATOR_FAILED", "操作工退出码不是 0", {"exit_code": result.exit_code})
        timed = self._approval_timed_out(job)
        if timed:
            return self._fail(job, "APPROVAL_TIMEOUT", "审批超时", {"approval_id": timed})
        job = self._transition(job, JobState.GATING, "inspect")
        return self._finish(job, sandbox, prepared)

    def _execute_service(self, job: Job, sandbox: Any, prepared: _Prepared) -> Job:
        timeout = (prepared.station.get("limits") or {}).get("timeout")
        timeout_s = parse_duration(str(timeout)) if timeout else 0.0
        try:
            self.gateway.run_recipe(
                station_key=job.station,
                job_id=job.id,
                attempt=job.attempt,
                sandbox=sandbox,
                inputs=prepared.beans,
                params=job.params,
                timeout_s=timeout_s,
            )
        except FactoryError as exc:
            return self._fail(job, exc.code, exc.message, dict(exc.detail))
        job = self._transition(job, JobState.GATING, "inspect")
        return self._finish(job, sandbox, prepared)

    def _finish(self, job: Job, sandbox: Any, prepared: _Prepared) -> Job:
        harvested = self._harvest(job, sandbox, prepared)
        if harvested is not None:
            return self._fail(job, "HARVEST_FAILED", "收货失败", {"job": job.id, "result": harvested})
        report = self.gate.inspect(
            job.station,
            job_id=job.id,
            attempt=job.attempt,
            out_dir=prepared.attempt_dir / "out",
            inputs=prepared.beans,
            params=job.params,
        )
        if report.verdict == "error":
            return self._fail(
                job,
                "GATE_ERROR",
                report.feedback or "出厂检验设施故障",
                {"job": job.id, "feedback": report.feedback},
            )
        if report.verdict == "reject":
            body = {"code": "GATE_REJECTED", "message": report.feedback, "detail": {"job": job.id}}
            job = self._transition(
                job, JobState.REJECTED, "GATE_REJECTED", error=body, feedback=report.feedback
            )
            return self._retry_or_dead(job)
        if report.verdict == "pass" and prepared.station.get("role") == "planner":
            decided = self._check_planner(job, prepared)
            if decided is not None:
                return decided
        sealed = self._seal(job, report)
        if prepared.station.get("role") == "source":
            with self.ctx.db.tx() as conn:
                _insert_event(
                    conn,
                    self.ctx,
                    "source.ingested",
                    sealed.id,
                    {"job": sealed.id, "station": sealed.station, "outputs": sealed.outputs},
                )
        return sealed

    def _check_planner(self, job: Job, prepared: _Prepared) -> Job | None:
        rows = self.ctx.db.query(
            "SELECT run_id, node_id FROM run_nodes WHERE job_id = ?",
            (job.id,),
        )
        if not rows:
            return self._fail(
                job,
                "RUN_PLAN_INVALID",
                "planner 工位不在 run 里",
                {"reason": "planner-outside-run"},
            )
        document, problem = self._planner_document(job, prepared)
        if problem:
            return self._reject_plan(job, problem)
        from mao.dynamic import compile_plan
        from mao.runs import LineRunner

        run = LineRunner(self.ctx, self.snapshot, self).get(str(rows[0]["run_id"]))
        check = compile_plan(self.snapshot, document or {}, planner_key=job.station, run=run)
        if not check.ok:
            return self._reject_plan(job, check.feedback or "PLAN_INVALID")
        return None

    def _planner_document(self, job: Job, prepared: _Prepared) -> tuple[dict[str, Any] | None, str]:
        station = self.snapshot.spec(job.station)
        outputs = station.get("outputs") or {}
        if len(outputs) != 1:
            return None, "PLAN_INVALID"
        port, decl = next(iter(outputs.items()))
        type_expr = decl.get("type") if isinstance(decl, dict) else str(decl)
        if str(type_expr).startswith("list<"):
            return None, "PLAN_INVALID"
        from mao.dynamic import _required_payload_paths

        paths = _required_payload_paths(self.snapshot, str(type_expr))
        if len(paths) != 1:
            return None, "PLAN_INVALID"
        file = prepared.attempt_dir / "out" / str(port) / paths[0]
        try:
            loaded = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None, "PLAN_INVALID"
        if not isinstance(loaded, dict):
            return None, "PLAN_INVALID"
        return loaded, ""

    def _reject_plan(self, job: Job, feedback: str) -> Job:
        body = {"code": "GATE_REJECTED", "message": feedback, "detail": {"job": job.id}}
        job = self._transition(job, JobState.REJECTED, "GATE_REJECTED", error=body, feedback=feedback)
        return self._retry_or_dead(job)

    def _harvest(self, job: Job, sandbox: Any, prepared: _Prepared) -> Any:
        ports = self._harvest_ports(prepared)
        if not ports:
            return None
        result_path = prepared.attempt_dir / "ctl" / "harvest-result.json"
        if result_path.is_file():
            try:
                previous = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                previous = None
            if isinstance(previous, dict) and previous.get("ok") is True:
                return None
        _write_json(prepared.attempt_dir / "ctl" / "harvest.json", {"outputs": ports})
        logs = prepared.attempt_dir / "logs"
        result = sandbox.exec(
            ["/opt/factory/bin/factoryctl", "harvest"],
            timeout_s=60,
            stdout=logs / "harvest.stdout",
            stderr=logs / "harvest.stderr",
            workdir="/workspace",
        )
        if not result_path.is_file():
            return {"ok": False, "exit_code": result.exit_code}
        try:
            body = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"ok": False, "exit_code": result.exit_code}
        if result.exit_code != 0 or not isinstance(body, dict) or body.get("ok") is not True:
            return body if isinstance(body, dict) else {"ok": False, "exit_code": result.exit_code}
        return None

    def _harvest_ports(self, prepared: _Prepared) -> dict[str, Any]:
        outputs = prepared.recipe.get("outputs") or {}
        found: dict[str, Any] = {}
        for port, decl in outputs.items():
            if not isinstance(decl, dict) or "harvest" not in decl:
                continue
            pattern = decl["harvest"]
            if isinstance(pattern, str) and "{{" in pattern:
                subject = f"{prepared.machine_key}/recipes.{prepared.recipe_name}.outputs.{port}.harvest"
                pattern = self.snapshot.compiled(subject).render(
                    {"inputs": _bean_inputs(prepared.station, prepared.beans), "params": {}}
                )
            type_expr = str(decl.get("type", ""))
            found[port] = {
                "harvest": pattern,
                "place_as": self._place_as(type_expr),
                "optional": bool(decl.get("optional", False)),
                "list": type_expr.startswith("list<"),
            }
        return found

    def _place_as(self, type_expr: str) -> str:
        expr = type_expr[5:-1] if type_expr.startswith("list<") and type_expr.endswith(">") else type_expr
        try:
            spec = self.snapshot.spec(self.snapshot.type_key(_minor_id(expr)))
        except FactoryError:
            return "file"
        required = [
            str(rule.get("path", ""))
            for rule in spec.get("payload") or []
            if rule.get("required", True) and "*" not in str(rule.get("path", ""))
        ]
        if len(required) == 1:
            return required[0].rsplit("/", 1)[-1]
        return "file"

    def _seal(self, job: Job, report: Any) -> Job:
        ensure_edge(job, JobState.SEALED)
        now = utc_iso(self.ctx.clock.now())
        digest = report.digest()
        items = [
            (bean_id, report.facts.get(bean_id, {})) for ids in report.outputs.values() for bean_id in ids
        ]
        with self.ctx.db.tx() as conn:
            self.ctx.warehouse.seal_many(items, job.id, digest)
            self.ctx.warehouse.record_lineage(job.id, job.inputs, report.outputs)
            for ids in report.outputs.values():
                for bean_id in ids:
                    if _has_bean_event(conn, job.id, bean_id):
                        continue
                    bean = self.ctx.warehouse.get(bean_id)
                    _insert_event(
                        conn,
                        self.ctx,
                        "bean.sealed",
                        job.id,
                        {"bean": bean_id, "job": job.id, "type": bean.type, "station": job.station},
                    )
            conn.execute(
                """UPDATE jobs SET state = ?, outputs = ?, error = NULL, updated_at = ? WHERE id = ?""",
                (JobState.SEALED.value, canonical_json(dict(report.outputs)), now, job.id),
            )
            _insert_event(
                conn,
                self.ctx,
                "job.state",
                job.id,
                {
                    "from": job.state.value,
                    "to": JobState.SEALED.value,
                    "attempt": job.attempt,
                    "reason": "pass",
                },
            )
            _insert_event(
                conn,
                self.ctx,
                "job.sealed",
                job.id,
                {"outputs": dict(report.outputs), "report": digest},
            )
        return load_job(self.ctx.db, job.id)

    def _spec(
        self,
        job: Job,
        station: Mapping[str, Any],
        machine: Mapping[str, Any],
        runtime: Mapping[str, Any],
        attempt_dir: Path,
    ) -> SandboxSpec:
        image = str((machine.get("runtime") or {}).get("image", ""))
        if _IMAGE.fullmatch(image) is None:
            raise FactoryError("IMAGE_REF_INVALID", "机器镜像引用不合法", image=image)
        resources = (machine.get("runtime") or {}).get("resources")
        cpus, memory_mb = _resources(resources)
        mounts = [
            Mount(source=str((attempt_dir / "in").resolve()), target="/factory/in", readonly=True),
            Mount(source=str((attempt_dir / "out").resolve()), target="/factory/out", readonly=False),
            Mount(source=str((attempt_dir / "ctl").resolve()), target="/factory/ctl", readonly=False),
            Mount(
                source=str((self.ctx.root / "tools" / "factory").resolve()),
                target="/opt/factory",
                readonly=True,
            ),
        ]
        form = machine.get("form")
        mode = ((station.get("limits") or {}).get("retries") or {}).get("mode", "fresh")
        volume = (
            f"mao-{job.id}-ws" if job.attempt == 1 or mode == "resume" else f"mao-{job.id}-ws-{job.attempt}"
        )
        workspace_volume = None
        if getattr(self.ctx.provider, "name", "") == "docker" and form == "workspace":
            workspace_volume = volume
            if job.attempt == 1 and mode != "resume":
                subprocess.run(
                    [self.ctx.provider.docker, "volume", "rm", "-f", volume],
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
        else:
            mounts.append(
                Mount(source=str((attempt_dir / "workspace").resolve()), target="/workspace", readonly=False)
            )
        if runtime.get("kind") == "agent-cli":
            inject = runtime.get("inject") or {}
            volume_ref = inject.get("volume")
            if volume_ref and getattr(self.ctx.provider, "name", "") == "docker":
                hexpart = str(volume_ref).split("sha256:", 1)[1]
                op_volume = "mao-op-" + hexpart[:12]
                self.ctx.provider.prepare_inject_volume(volume_ref, op_volume)
                mounts.append(Mount(source="volume:" + op_volume, target=inject["mount"], readonly=True))
        return SandboxSpec(
            name=f"mao-{job.id}-{job.attempt}",
            image=image,
            mounts=tuple(mounts),
            workdir=str((machine.get("runtime") or {}).get("workdir") or "/workspace"),
            env={"FACTORY_JOB_ID": job.id, "FACTORY_ATTEMPT": str(job.attempt)},
            network="none",
            cpus=cpus,
            memory_mb=memory_mb,
            workspace_volume=workspace_volume,
        )

    def _workspace(self, job: Job, attempt_dir: Path, machine: Mapping[str, Any]) -> None:
        del machine
        dest = attempt_dir / "workspace"
        station = self.snapshot.spec(job.station)
        mode = ((station.get("limits") or {}).get("retries") or {}).get("mode", "fresh")
        if mode == "resume" and job.attempt > 1:
            prev = self.ctx.root / "jobs" / job.id / str(job.attempt - 1) / "workspace"
            if dest.exists():
                shutil.rmtree(dest)
            if prev.exists():
                shutil.copytree(prev, dest)
                return
        dest.mkdir(parents=True, exist_ok=True)

    def _materialize(self, job: Job, station: Mapping[str, Any], in_dir: Path) -> dict[str, Any]:
        declared = station.get("inputs") or {}
        beans: dict[str, Any] = {}
        for port, ids in job.inputs.items():
            loaded = []
            expr = declared.get(port)
            type_expr = expr.get("type") if isinstance(expr, dict) else str(expr)
            listed = str(type_expr).startswith("list<")
            for index, bean_id in enumerate(ids):
                dest = in_dir / port / str(index) if listed else in_dir / port
                self.ctx.warehouse.materialize(bean_id, dest, layout="port")
                loaded.append(self.ctx.warehouse.get(bean_id))
            beans[port] = loaded if listed else loaded[0]
        return beans

    def _contract(
        self, station_key: str, station: Mapping[str, Any], machine: Mapping[str, Any]
    ) -> dict[str, Any]:
        del machine
        outputs: dict[str, Any] = {}
        for port, decl in (station.get("outputs") or {}).items():
            type_expr = decl.get("type") if isinstance(decl, dict) else str(decl)
            payload = self._payload_paths(str(type_expr))
            item: dict[str, Any] = {
                "type": type_expr,
                "optional": bool(decl.get("optional", False)) if isinstance(decl, dict) else False,
                "guarantees": list(decl.get("guarantees") or []) if isinstance(decl, dict) else [],
                "accept": list(decl.get("accept") or []) if isinstance(decl, dict) else [],
                "payload": payload,
            }
            outputs[port] = item
        approval_out: dict[str, Any] = {}
        approval = station.get("approval") or {}
        if approval:
            approval_out["timeout_s"] = (
                parse_duration(approval["timeout"]) if "timeout" in approval else 600.0
            )
            rule = approval.get("in_run")
            if isinstance(rule, str):
                approval_out["in_run"] = {"approver": rule}
            elif isinstance(rule, dict):
                approval_out["in_run"] = {"auto_if": rule.get("auto_if"), "else": rule.get("else")}
        limits_out: dict[str, Any] = {}
        limits = station.get("limits") or {}
        if "timeout" in limits:
            limits_out["timeout_s"] = parse_duration(limits["timeout"])
        if "budget_usd" in limits:
            limits_out["budget_usd"] = float(limits["budget_usd"])
        if "retries" in limits:
            limits_out["retries"] = limits["retries"]
        return {"station": station_key, "outputs": outputs, "approval": approval_out, "limits": limits_out}

    def _payload_paths(self, type_expr: str) -> list[str]:
        expr = type_expr[5:-1] if type_expr.startswith("list<") and type_expr.endswith(">") else type_expr
        try:
            spec = self.snapshot.spec(self.snapshot.type_key(_minor_id(expr)))
        except FactoryError:
            return []
        paths = [
            str(rule.get("path", ""))
            for rule in spec.get("payload") or []
            if rule.get("required", True) and "*" not in str(rule.get("path", ""))
        ]
        return sorted(paths)

    def _claim_sink(self, job: Job) -> str | FactoryError | None:
        station = self.snapshot.spec(job.station)
        if station.get("role") != "sink" or "idempotency_key" not in station:
            return None
        from mao.connectors import ReceiptStore, idempotency_key

        try:
            key = idempotency_key(self.snapshot, job.station, job.inputs, job.params, {})
            claim = ReceiptStore(self.ctx).claim(
                station_key=job.station,
                key=key,
                job_id=job.id,
                run_id=None,
                inputs=job.inputs,
                verify_method=str(station.get("verify_receipt") or ""),
            )
        except FactoryError as exc:
            return exc
        return claim.kind

    def _bind_control(self, job: Job, prepared: _Prepared) -> _Prepared:
        station = self.snapshot.spec(job.station)
        if not station.get("policy"):
            return prepared
        from mao.egress import SecretStore, build_egress, proxy_env

        plan = build_egress(self.snapshot, job.station, SecretStore(self.ctx.root))
        if plan.network != "egress":
            return prepared
        self._ensure_proxies()
        docker = getattr(self.ctx.provider, "name", "") == "docker"
        env = dict(prepared.spec.env)
        env.update(plan.env)
        env.update(proxy_env(self.ctx.proxies, docker=docker))
        prepared.spec = SandboxSpec(
            name=prepared.spec.name,
            image=prepared.spec.image,
            mounts=prepared.spec.mounts,
            workdir=prepared.spec.workdir,
            env=env,
            network="egress",
            cpus=prepared.spec.cpus,
            memory_mb=prepared.spec.memory_mb,
            workspace_volume=prepared.spec.workspace_volume,
        )
        return prepared

    def _ensure_proxies(self) -> None:
        if getattr(self.ctx, "proxies", None) is not None:
            return
        from mao.egress import start_owned_proxies

        start_owned_proxies(self.ctx, model_port=0, timeout_s=60.0)

    def _control_values(self, job: Job) -> dict[str, str]:
        values = {"FACTORY_JOB_ID": job.id, "FACTORY_ATTEMPT": str(job.attempt)}
        proxies = getattr(self.ctx, "proxies", None)
        if proxies is not None:
            from mao.egress import proxy_env

            urls = proxy_env(proxies, docker=getattr(self.ctx.provider, "name", "") == "docker")
            values["MODEL_PROXY"] = urls["MODEL_PROXY"]
            values["EGRESS_PROXY"] = urls["EGRESS_PROXY"]
        return values

    def _usage_spent(self, job_id: str) -> tuple[int, float]:
        rows = self.ctx.db.query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS spent FROM model_usage WHERE job_id = ?",
            (job_id,),
        )
        return int(rows[0]["n"]), float(rows[0]["spent"])

    def _budget_code(self, job_id: str) -> str | None:
        rows = self.ctx.db.query("SELECT code FROM budget_stops WHERE job_id = ?", (job_id,))
        if not rows:
            return None
        return str(rows[0]["code"])

    def _budget_fail(self, job: Job, code: str) -> Job:
        body = {"code": code, "message": "模型预算停止", "detail": {"job": job.id}}
        return self._transition(job, JobState.FAILED, code, error=body)

    def _register_cap(self, job: Job) -> None:
        from mao.egress import EgressPlan, SecretStore, build_egress, register_job_cap, register_job_egress

        limits = self.snapshot.spec(job.station).get("limits") or {}
        cap = float(limits["budget_usd"]) if "budget_usd" in limits else None
        register_job_cap(self.ctx.db, job.id, job.station, cap)
        station = self.snapshot.spec(job.station)
        if station.get("policy"):
            plan = build_egress(self.snapshot, job.station, SecretStore(self.ctx.root))
        else:
            plan = EgressPlan("none", (), (), (), {})
        register_job_egress(self.ctx.db, job.id, plan)

    def _policy_defense(
        self, station_key: str, station: Mapping[str, Any], run: Mapping[str, Any] | None
    ) -> None:
        role = str(station.get("role") or "")
        if run is not None:
            budget = run.get("budget") if isinstance(run, Mapping) else None
            complete = (
                isinstance(run, Mapping)
                and all(name in run for name in ("id", "epoch", "line"))
                and isinstance(budget, Mapping)
                and all(name in budget for name in ("cap_usd", "spent_usd", "remaining_usd"))
            )
            if not complete:
                raise FactoryError(
                    "POLICY_VIOLATION",
                    "运行值不完整，拒绝启动",
                    station=station_key,
                    role=role,
                    reason="run",
                )
        policy_ref = station.get("policy")
        if not policy_ref:
            return
        egress = self.snapshot.spec(self.snapshot.key("Policy", policy_ref)).get("egress") or {}
        if "publish" in egress and role != "sink":
            raise FactoryError(
                "POLICY_VIOLATION",
                "非 sink 工位不能使用 publish 出网",
                station=station_key,
                role=role,
                reason="publish",
            )
        if "ingress" in egress and role != "source":
            raise FactoryError(
                "POLICY_VIOLATION",
                "非 source 工位不能使用 ingress 出网",
                station=station_key,
                role=role,
                reason="ingress",
            )

    def _static_problem(self, job: Job) -> FactoryError | None:
        station = self.snapshot.spec(job.station)
        role = station.get("role")
        if role not in ("transform", "converter", "source", "sink", "planner"):
            return FactoryError("ROLE_UNSUPPORTED", "这个角色还不能运行", station=job.station, role=role)
        if "phases" in station and station["phases"] != ["execute"]:
            return FactoryError(
                "PHASE_UNSUPPORTED", "M1 不运行两阶段工位", station=job.station, phases=station.get("phases")
            )
        machine_key = self.snapshot.key("Machine", station["machine"])
        machine = self.snapshot.spec(machine_key)
        form = machine.get("form")
        if form == "service":
            if self.gateway is None:
                return FactoryError(
                    "FORM_UNSUPPORTED",
                    "M1 不运行这种机器形态",
                    station=job.station,
                    machine=machine_key,
                    form=form,
                )
        elif form not in ("tool", "workspace"):
            return FactoryError(
                "FORM_UNSUPPORTED",
                "M1 不运行这种机器形态",
                station=job.station,
                machine=machine_key,
                form=form,
            )
        runtime = machine.get("runtime") or {}
        if "compose" in runtime and "image" not in runtime:
            return FactoryError(
                "COMPOSE_UNSUPPORTED", "M1 不运行 compose 机器", station=job.station, machine=machine_key
            )
        policy = self.snapshot.spec(self.snapshot.key("Policy", station["policy"]))
        egress = policy.get("egress") or {}
        if set(egress) != {"none"}:
            return FactoryError(
                "EGRESS_UNSUPPORTED", "M1 只允许 egress none", station=job.station, egress=sorted(egress)
            )
        secrets = _secret_names(policy, machine)
        if secrets:
            return FactoryError("SECRETS_UNSUPPORTED", "M1 不注入秘密", station=job.station, secrets=secrets)
        return None

    def _fail(self, job: Job, code: str, message: str, detail: dict[str, Any]) -> Job:
        job = load_job(self.ctx.db, job.id)
        if job.state in TERMINAL:
            return job
        body = {"code": code, "message": message, "detail": detail}
        job = self._transition(job, JobState.FAILED, code, error=body)
        return self._retry_or_dead(job)

    def _dead(self, job: Job, error: Any) -> Job:
        body = _error_body(error)
        return self._transition(
            job,
            JobState.DEAD,
            body["code"],
            error=body,
            extra=[("job.failed", {"code": body["code"], "message": body["message"]})],
        )

    def _retry_or_dead(self, job: Job) -> Job:
        code = (job.error or {}).get("code", "")
        kind = job.state.value
        count = self._count_to(job.id, kind)
        limits = self._retry_limits(job)
        limit = limits["failed"] if job.state == JobState.FAILED else limits["rejected"]
        if job.state == JobState.FAILED and code == "BUDGET_EXCEEDED":
            return self._dead(job, job.error)
        if count <= limit:
            return self._transition(job, JobState.PENDING, "retry", attempt=job.attempt + 1)
        return self._dead(job, job.error)

    def _retry_limits(self, job: Job) -> dict[str, Any]:
        station = self.snapshot.spec(job.station)
        retries = (station.get("limits") or {}).get("retries") or {}
        return {
            "rejected": int(retries.get("rejected", 0)),
            "failed": int(retries.get("failed", 0)),
            "mode": retries.get("mode", "fresh"),
        }

    def _count_to(self, job_id: str, to: str) -> int:
        rows = self.ctx.db.query("SELECT data FROM events WHERE job_id = ? AND kind = 'job.state'", (job_id,))
        return sum(1 for row in rows if json.loads(row["data"]).get("to") == to)

    def _approval_timed_out(self, job: Job) -> str | None:
        rows = self.ctx.db.query(
            "SELECT id FROM approvals WHERE job_id = ? AND attempt = ? AND status = 'timed_out'",
            (job.id, job.attempt),
        )
        if not rows:
            return None
        return str(rows[0]["id"])

    def _set_cost(self, job: Job, cost: float) -> Job:
        rounded = round(float(cost), 6)
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute("UPDATE jobs SET cost_usd = ?, updated_at = ? WHERE id = ?", (rounded, now, job.id))
        return load_job(self.ctx.db, job.id)

    def _transition(
        self,
        job: Job,
        to: JobState,
        reason: str,
        *,
        attempt: int | None = None,
        outputs: dict[str, list[str]] | None = None,
        error: Any = _KEEP,
        feedback: Any = _KEEP,
        extra: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> Job:
        ensure_edge(job, to)
        now = utc_iso(self.ctx.clock.now())
        new_attempt = job.attempt if attempt is None else attempt
        new_outputs = job.outputs if outputs is None else outputs
        new_error = job.error if error is _KEEP else error
        new_feedback = job.feedback if feedback is _KEEP else feedback
        with self.ctx.db.tx() as conn:
            conn.execute(
                """UPDATE jobs
                   SET state = ?, attempt = ?, outputs = ?, error = ?, feedback = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    to.value,
                    new_attempt,
                    canonical_json(new_outputs),
                    None if new_error is None else canonical_json(new_error),
                    new_feedback,
                    now,
                    job.id,
                ),
            )
            _insert_event(
                conn,
                self.ctx,
                "job.state",
                job.id,
                {"from": job.state.value, "to": to.value, "attempt": new_attempt, "reason": reason},
            )
            for kind, data in extra or []:
                _insert_event(conn, self.ctx, kind, job.id, data)
        return load_job(self.ctx.db, job.id)


class _Prepared:
    def __init__(
        self,
        station: Mapping[str, Any],
        machine_key: str,
        machine: Mapping[str, Any],
        recipe_name: str,
        recipe: Mapping[str, Any],
        runtime: Mapping[str, Any],
        beans: Mapping[str, Any],
        attempt_dir: Path,
        brief: Any,
        spec: SandboxSpec,
    ) -> None:
        self.station = station
        self.machine_key = machine_key
        self.machine = machine
        self.recipe_name = recipe_name
        self.recipe = recipe
        self.runtime = runtime
        self.beans = beans
        self.attempt_dir = attempt_dir
        self.brief = brief
        self.spec = spec


class _CtlHandler:
    def __init__(self, runner: Runner, job: Job, prepared: _Prepared) -> None:
        self.runner = runner
        self.job_id = job.id
        self.attempt = job.attempt
        self.prepared = prepared

    def handle(self, body: Mapping[str, Any]) -> dict[str, Any] | None:
        op = body.get("op")
        if op == "check":
            return self._check(body)
        if op == "approval":
            return self._approval(body)
        return _ctl_error(str(body.get("id", "")), "CTL_REQUEST_INVALID", "不支持的控制请求")

    def _check(self, body: Mapping[str, Any]) -> dict[str, Any]:
        rid = str(body["id"])
        args = body.get("args") or {}
        port = str(args.get("port", ""))
        virtual = str(args.get("path", ""))
        host = _map_virtual(self.prepared.spec.mounts, virtual)
        dest_root = self.prepared.attempt_dir / "tmp" / "check"
        dest = dest_root / port
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        if host.is_file():
            shutil.copyfile(host, dest / host.name)
        job = load_job(self.runner.ctx.db, self.job_id)
        try:
            report = self.runner.gate.inspect(
                job.station,
                job_id=self.job_id,
                attempt=self.attempt,
                out_dir=dest_root,
                inputs=self.prepared.beans,
                params=job.params,
            )
            verdict, feedback = report.verdict, report.feedback
        except Exception as exc:
            verdict, feedback = "error", str(exc)
        return {
            "id": rid,
            "ok": True,
            "result": {"verdict": verdict, "feedback": feedback},
            "error": None,
        }

    def _approval(self, body: Mapping[str, Any]) -> dict[str, Any] | None:
        rid = str(body["id"])
        args = body.get("args") or {}
        try:
            payload = {
                "kind": str(args["kind"]),
                "summary": str(args["summary"]),
                "cost_usd": float(args.get("cost_usd") or 0),
            }
        except (KeyError, TypeError, ValueError):
            return _ctl_error(rid, "CTL_REQUEST_INVALID", "审批参数不合法")
        row = self._lookup(rid)
        if row is None:
            self._insert_pending(rid, payload)
            row = self._lookup(rid)
        if row is None:
            return _ctl_error(rid, "APPROVAL_NOT_FOUND", "审批没有写入")
        if row["status"] == "pending":
            decided = self._auto_or_timeout(row, payload)
            if decided is None:
                return None
            row = decided
        return self._respond(row)

    def _lookup(self, request_id: str) -> Any:
        rows = self.runner.ctx.db.query("SELECT * FROM approvals WHERE request_id = ?", (request_id,))
        return rows[0] if rows else None

    def _insert_pending(self, request_id: str, payload: Mapping[str, Any]) -> None:
        approval_id = self.runner.ctx.ids.new("a")
        now = utc_iso(self.runner.ctx.clock.now())
        job = load_job(self.runner.ctx.db, self.job_id)
        with self.runner.ctx.db.tx() as conn:
            conn.execute(
                """INSERT INTO approvals
                   (id, job_id, attempt, request_id, phase, kind, summary, cost_usd, status, note, created_at)
                   VALUES (?, ?, ?, ?, 'in_run', ?, ?, ?, 'pending', '', ?)""",
                (
                    approval_id,
                    self.job_id,
                    self.attempt,
                    request_id,
                    payload["kind"],
                    payload["summary"],
                    payload["cost_usd"],
                    now,
                ),
            )
        if job.state == JobState.RUNNING:
            self.runner._transition(job, JobState.AWAITING_APPROVAL, "approval")

    def _auto_or_timeout(self, row: Any, payload: Mapping[str, Any]) -> Any:
        station = self.prepared.station
        rule = (station.get("approval") or {}).get("in_run")
        timeout_s = 600.0
        approval = station.get("approval") or {}
        if "timeout" in approval:
            timeout_s = parse_duration(approval["timeout"])
        created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        if (self.runner.ctx.clock.now() - created).total_seconds() >= timeout_s:
            return self._mark(row, "timed_out", None, "")
        if rule is None:
            return self._mark(row, "denied", "auto", "")
        if rule == "auto" or (isinstance(rule, dict) and self._auto_ok(rule, payload)):
            return self._mark(row, "approved", "auto", "")
        if isinstance(rule, dict) and rule.get("else") == "auto" and not self._auto_ok(rule, payload):
            return self._mark(row, "approved", "auto", "")
        return None

    def _auto_ok(self, rule: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
        if "auto_if" not in rule:
            return False
        job = load_job(self.runner.ctx.db, self.job_id)
        subject = f"{job.station}/approval.in_run"
        try:
            return (
                self.runner.snapshot.compiled(subject).evaluate(
                    approval_env("in_run", payload, self.prepared.beans, job.params)
                )
                is True
            )
        except EvalError:
            return False

    def _mark(self, row: Any, status: str, by: str | None, note: str) -> Any:
        now = utc_iso(self.runner.ctx.clock.now())
        with self.runner.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE approvals SET status = ?, by = ?, note = ?, decided_at = ? WHERE id = ?",
                (status, by, note, now, row["id"]),
            )
        return self.runner.ctx.db.query("SELECT * FROM approvals WHERE id = ?", (row["id"],))[0]

    def _respond(self, row: Any) -> dict[str, Any]:
        job = load_job(self.runner.ctx.db, self.job_id)
        if job.state == JobState.AWAITING_APPROVAL:
            decision = "approve" if row["status"] == "approved" else row["status"]
            self.runner._transition(
                job,
                JobState.RUNNING,
                "approval",
                extra=[
                    (
                        "job.approval",
                        {"approval": row["id"], "decision": decision, "by": row["by"]},
                    )
                ],
            )
        rid = str(row["request_id"])
        if row["status"] == "approved":
            return {
                "id": rid,
                "ok": True,
                "result": {"decision": "approve", "by": row["by"] or "", "note": row["note"] or ""},
                "error": None,
            }
        code = "APPROVAL_TIMEOUT" if row["status"] == "timed_out" else "APPROVAL_DENIED"
        message = "审批超时" if row["status"] == "timed_out" else "审批被拒绝"
        return _ctl_error(rid, code, message)


def _ctl_error(rid: str, code: str, message: str) -> dict[str, Any]:
    return {"id": rid, "ok": False, "result": None, "error": {"code": code, "message": message}}


def _split_inputs(inputs: Mapping[str, Any]) -> tuple[dict[str, list[str]], dict[str, Any]]:
    stored: dict[str, list[str]] = {}
    checked: dict[str, Any] = {}
    for port, value in inputs.items():
        if isinstance(value, str):
            ids = [value]
            checked[port] = value
        else:
            ids = [str(item) for item in value]
            checked[port] = ids
        if any(item == "" for item in ids):
            raise FactoryError(
                "INTAKE_REJECTED",
                "输入 id 为空",
                problems=[{"port": port, "index": 0, "reason": "missing", "detail": "空 id", "bean_id": ""}],
            )
        stored[port] = ids
    return stored, checked


def _bean_inputs(station: Mapping[str, Any], beans: Mapping[str, Any]) -> dict[str, Any]:
    declared = station.get("inputs") or {}
    out: dict[str, Any] = {}
    for port, value in beans.items():
        expr = declared.get(port)
        type_expr = expr.get("type") if isinstance(expr, dict) else str(expr)
        if str(type_expr).startswith("list<"):
            out[port] = [bean_value(item) for item in value]
        else:
            out[port] = bean_value(value)
    return out


def _minor_id(type_name: str) -> str:
    if "@" not in type_name:
        return type_name
    name, version = type_name.rsplit("@", 1)
    return f"{name}@{version.split('.', 1)[0]}"


def _resources(resources: Any) -> tuple[float | None, int | None]:
    if not resources:
        return None, None
    cpus = resources.get("cpus")
    memory = resources.get("memory")
    memory_mb = None
    if memory is not None:
        match = _MEMORY.fullmatch(str(memory))
        if match is None:
            raise FactoryError("SANDBOX_SPEC_INVALID", "memory 无法解析", memory=str(memory))
        amount = int(match.group(1))
        memory_mb = amount if match.group(2) in ("Mi", "M", "MB") else amount * 1024
    return (None if cpus is None else float(cpus), memory_mb)


def _secret_names(policy: Mapping[str, Any], machine: Mapping[str, Any]) -> list[str]:
    secrets = policy.get("secrets")
    if isinstance(secrets, list) and secrets:
        return [str(item) for item in secrets]
    if secrets == "from-machine":
        declared = (machine.get("needs") or {}).get("secrets") or []
        if declared:
            return [str(item) for item in declared]
    return []


def _error_body(error: Any) -> dict[str, Any]:
    if isinstance(error, FactoryError):
        return {"code": error.code, "message": error.message, "detail": dict(error.detail)}
    if isinstance(error, dict) and "code" in error:
        return {
            "code": error["code"],
            "message": str(error.get("message", "")),
            "detail": dict(error.get("detail") or {}),
        }
    return {"code": "JOB_STATE_INVALID", "message": str(error), "detail": {}}


def _insert_event(conn: Any, ctx: Any, kind: str, job_id: str | None, data: Mapping[str, Any]) -> None:
    conn.execute(
        "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, ?, ?)",
        (ctx.ids.new("ev"), utc_iso(ctx.clock.now()), kind, job_id, canonical_json(dict(data))),
    )


def open_runner(ctx: Any, snapshot: Any) -> Runner:
    from mao.gateway import ServiceGateway

    return Runner(ctx, snapshot, gateway=ServiceGateway(ctx, snapshot))


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_bytes(canonical_json(dict(payload)).encode("utf-8"))


def _clear_children(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _fail_reason(path: Path) -> str:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if isinstance(body, dict) and isinstance(body.get("reason"), str):
        return body["reason"]
    return ""


def _has_bean_event(conn: Any, job_id: str, bean_id: str) -> bool:
    rows = conn.execute(
        "SELECT data FROM events WHERE job_id = ? AND kind = 'bean.sealed'",
        (job_id,),
    ).fetchall()
    for row in rows:
        if json.loads(row["data"]).get("bean") == bean_id:
            return True
    return False


def _map_virtual(mounts: tuple[Mount, ...], virtual: str) -> Path:
    pairs = sorted(
        ((mount.target, mount.source) for mount in mounts), key=lambda item: len(item[0]), reverse=True
    )
    for target, source in pairs:
        if virtual == target or virtual.startswith(target + "/"):
            rest = virtual[len(target) :].lstrip("/")
            return Path(source) / rest if rest else Path(source)
    return Path(virtual)
