"""契约测试：validator、黄金样例、等价、差分与 Sink 干跑。"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from mao.base import canonical_json, digest_file, utc_iso
from mao.egress import SecretStore
from mao.errors import FactoryError
from mao.jobs import JobState
from mao.sandbox import Mount, SandboxSpec
from mao.values import bean_value, params_value

_COMPUTABLE = {"cost_usd", "gate_reject_rate"}


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True)
class ContractResult:
    id: str
    kind: str
    subject: str
    verdict: Verdict
    detail: dict[str, Any]
    at: str


class ContractRunner:
    def __init__(self, ctx: Any, snapshot: Any, runner: Any) -> None:
        self.ctx = ctx
        self.snapshot = snapshot
        self.runner = runner
        self._secrets = SecretStore(ctx.root)
        self._seq = 0

    def run_validator(self, type_id: str, validator: str, *, timeout_s: float) -> ContractResult:
        self._need_timeout(timeout_s, None, type_id)
        found = self._validator(type_id, validator)
        if found is None:
            self._cannot("CONTRACT_NOT_FOUND", f"找不到 validator {validator}", subject=validator)
        accept_paths = list((found.get("fixtures") or {}).get("accept") or [])
        reject_paths = list((found.get("fixtures") or {}).get("reject") or [])
        for rel in accept_paths + reject_paths:
            self._fixture_file(rel)
        accept = [
            self._one_validator(type_id, found, rel, timeout_s, want_accept=True) for rel in accept_paths
        ]
        reject = [
            self._one_validator(type_id, found, rel, timeout_s, want_accept=False) for rel in reject_paths
        ]
        code = _first_code(accept + reject)
        verdict = Verdict.FAIL if code else Verdict.PASS
        detail: dict[str, Any] = {"accept": accept, "reject": reject}
        if code:
            detail["code"] = code
        return self._store("validator", validator, verdict, detail)

    def run_golden(
        self, golden_key: str, baseline_key: str | None, *, timeout_s: float | None = None
    ) -> ContractResult:
        doc = self._record("GoldenSet", golden_key)
        if doc is None:
            self._cannot("CONTRACT_NOT_FOUND", f"找不到 GoldenSet {golden_key}", subject=golden_key)
        spec = doc["spec"]
        if "budget_usd" not in spec:
            self._cannot("CONTRACT_BUDGET_REQUIRED", "GoldenSet 没有 budget_usd", golden=golden_key)
        station_key = self.snapshot.key("Station", spec["station"])
        try:
            station = self.snapshot.spec(station_key)
        except FactoryError as exc:
            self._cannot("CONTRACT_NOT_FOUND", exc.message, subject=station_key)
        timeout = self._need_timeout(timeout_s, station, golden_key)
        baseline = None
        if baseline_key is not None:
            baseline = self._record("Baseline", baseline_key)
            if baseline is None:
                self._cannot("CONTRACT_NOT_FOUND", f"找不到 Baseline {baseline_key}", subject=baseline_key)
            missing = self._missing_metric(baseline["spec"].get("metrics") or {})
            if missing:
                return self._store(
                    "golden",
                    golden_key,
                    Verdict.FAIL,
                    {"code": "CONTRACT_METRIC_MISSING", "metric": missing},
                )
        budget = float(spec["budget_usd"])
        cases: list[dict[str, Any]] = []
        spent = 0.0
        stopped = False
        for case in spec.get("cases") or []:
            if spent > budget:
                stopped = True
                break
            item, cost = self._golden_case(station_key, case, timeout)
            cases.append(item)
            spent = round(spent + cost, 6)
        if not stopped and spent > budget:
            stopped = True
        observed = {
            "cost_usd": round(sum(float(item.get("cost_usd") or 0) for item in cases), 6),
            "gate_reject_rate": (sum(1 for item in cases if not item["sealed"]) / len(cases))
            if cases
            else 0.0,
        }
        gates: list[dict[str, Any]] = []
        code = "CONTRACT_BUDGET_EXCEEDED" if stopped else None
        if baseline is not None:
            gates, gate_code = self._gates(baseline["spec"].get("metrics") or {}, observed)
            code = code or gate_code
        elif cases and all(item["sealed"] for item in cases):
            code = code
        elif cases and not all(item["sealed"] for item in cases):
            code = code or "CONTRACT_FIXTURE_REJECTED"
        verdict = Verdict.FAIL if code else Verdict.PASS
        detail: dict[str, Any] = {"cases": _public_cases(cases), "observed": observed, "gates": gates}
        if code:
            detail["code"] = code
            if code == "CONTRACT_BUDGET_EXCEEDED":
                detail["golden"] = golden_key
                detail["spent"] = spent
                detail["cap"] = budget
        return self._store("golden", golden_key, verdict, detail)

    def run_equivalence(self, station_key: str, *, timeout_s: float | None = None) -> ContractResult:
        key, spec = self._station(station_key)
        if spec.get("role") != "converter":
            self._cannot("CONTRACT_NOT_FOUND", "工位不是 converter", subject=key)
        self._need_timeout(timeout_s, spec, key)
        accept_rows = []
        reject_rows = []
        sealed_outputs: list[str] = []
        code = None
        for rel in (spec.get("fixtures") or {}).get("accept") or []:
            row, out_id, row_code = self._equivalence_one(key, spec, rel, positive=True)
            accept_rows.append(row)
            code = code or row_code
            if out_id:
                sealed_outputs.append(out_id)
        for rel in (spec.get("fixtures") or {}).get("reject") or []:
            row, out_id, row_code = self._equivalence_one(key, spec, rel, positive=False)
            reject_rows.append(row)
            code = code or row_code
            if out_id:
                sealed_outputs.append(out_id)
        detail: dict[str, Any] = {
            "accept": accept_rows,
            "reject": reject_rows,
            "sealed_outputs": sealed_outputs,
        }
        if code:
            detail["code"] = code
        return self._store("equivalence", key, Verdict.FAIL if code else Verdict.PASS, detail)

    def run_diff(self, station_key: str, *, timeout_s: float | None = None) -> ContractResult:
        key, spec = self._station(station_key)
        previous = self._previous(key, spec)
        if previous is None:
            return self._store("diff", key, Verdict.PASS, {"reason": "no_previous", "cases": []})
        prev_key, prev_spec = previous
        self._need_timeout(timeout_s, spec, key)
        beans = self._diff_inputs(spec, prev_key, prev_spec)
        cases = []
        code = None
        extra: dict[str, Any] = {}
        for bean_id in beans:
            old_job = self._run_station(prev_key, prev_spec, bean_id)
            new_job = self._run_station(key, spec, bean_id)
            old_sealed = old_job is not None and old_job.state == JobState.SEALED
            new_sealed = new_job is not None and new_job.state == JobState.SEALED
            facts_equal = None
            if old_sealed and new_sealed:
                facts_equal = self._facts_equal(old_job, new_job)
                if facts_equal is False and code is None:
                    code = "CONTRACT_DIFF_FACTS"
                    extra = {"input": bean_id, "fact": self._first_fact_diff(old_job, new_job)}
            elif old_sealed and not new_sealed:
                if code != "CONTRACT_DIFF_REGRESSION":
                    code = "CONTRACT_DIFF_REGRESSION"
                    extra = {"input": bean_id}
            cases.append(
                {
                    "input": bean_id,
                    "old_sealed": old_sealed,
                    "new_sealed": new_sealed,
                    "facts_equal": facts_equal,
                }
            )
        if code == "CONTRACT_DIFF_REGRESSION":
            extra = {
                "input": next(
                    item["input"] for item in cases if item["old_sealed"] and not item["new_sealed"]
                )
            }
        detail: dict[str, Any] = {"reason": None, "cases": cases}
        if code:
            detail["code"] = code
            detail.update(extra)
        return self._store("diff", key, Verdict.FAIL if code else Verdict.PASS, detail)

    def run_sink_dry_run(
        self,
        station_key: str,
        *,
        inputs: Mapping[str, str],
        params: Mapping[str, Any],
        timeout_s: float | None = None,
    ) -> ContractResult:
        try:
            key = self.snapshot.key("Station", station_key)
            spec = self.snapshot.spec(key)
        except FactoryError:
            self._cannot("CONTRACT_SINK_NO_PROPOSE", "不是两阶段 Sink", station=station_key)
        if spec.get("role") != "sink":
            self._cannot("CONTRACT_SINK_NO_PROPOSE", "不是两阶段 Sink", station=key)
        self._need_timeout(timeout_s, spec, key)
        phases = list(spec.get("phases") or [])
        if "propose" not in phases:
            self._cannot("CONTRACT_SINK_NO_PROPOSE", "phases 不含 propose", station=key)
        job = self.runner.submit(key, dict(inputs), dict(params))
        approval = self._propose_only(key, spec, job)
        rows = self.ctx.db.query("SELECT id, status FROM receipts WHERE job_id = ?", (job.id,))
        receipt = None if not rows else {"id": rows[0]["id"], "status": rows[0]["status"]}
        code = None
        verdict = Verdict.PASS
        if approval is None or approval.get("status") != "approved":
            verdict = Verdict.FAIL
        elif receipt is not None:
            verdict = Verdict.FAIL
            code = "CONTRACT_SINK_PUBLISHED"
        detail: dict[str, Any] = {"approval": approval, "receipt": receipt, "executed_run": False}
        if code:
            detail["code"] = code
            detail["job"] = job.id
        return self._store("sink_dry_run", key, verdict, detail)

    def check_fixture_set(self, old: Any, new: Any, type_id: str, validator: str) -> ContractResult:
        old_paths = set(self._reject_paths(old, type_id, validator))
        new_paths = set(self._reject_paths(new, type_id, validator))
        removed = sorted(old_paths - new_paths)
        detail: dict[str, Any] = {"removed": removed}
        verdict = Verdict.PASS
        if removed:
            verdict = Verdict.FAIL
            detail["code"] = "CONTRACT_FIXTURE_REMOVED"
        return self._store("fixture_set", validator, verdict, detail)

    def get(self, result_id: str) -> ContractResult:
        rows = self.ctx.db.query("SELECT * FROM contract_runs WHERE id = ?", (result_id,))
        if not rows:
            self._cannot("CONTRACT_NOT_FOUND", f"找不到契约结果 {result_id}", subject=result_id)
        return _row_result(rows[0])

    def list(self, *, kind: str | None = None, subject: str | None = None) -> list[ContractResult]:
        sql = "SELECT * FROM contract_runs WHERE 1 = 1"
        args: list[Any] = []
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        if subject is not None:
            sql += " AND subject = ?"
            args.append(subject)
        sql += " ORDER BY id"
        return [_row_result(row) for row in self.ctx.db.query(sql, tuple(args))]

    def canary(self, station_key: str) -> ContractResult:
        key = self.snapshot.key("Station", station_key)
        name = key.split(":", 1)[1].rsplit("@", 1)[0]
        goldens = [
            doc
            for doc in self._all_records("GoldenSet")
            if str((doc.get("spec") or {}).get("station", "")).split("@")[0] == name
            or str((doc.get("spec") or {}).get("station", "")).endswith(name)
        ]
        if not goldens:
            self._cannot("CONTRACT_NOT_FOUND", "没有引用该工位的 GoldenSet", subject=key)
        goldens.sort(key=lambda doc: _doc_key("GoldenSet", doc))
        chosen = goldens[0]
        baselines = [
            doc
            for doc in self._all_records("Baseline")
            if (doc.get("spec") or {}).get("station") == (chosen.get("spec") or {}).get("station")
        ]
        baselines.sort(key=lambda doc: _doc_key("Baseline", doc))
        baseline_key = _doc_key("Baseline", baselines[0]) if baselines else None
        return self.run_golden(_doc_key("GoldenSet", chosen), baseline_key, timeout_s=None)

    def _validator(self, type_id: str, name: str) -> dict[str, Any] | None:
        try:
            found = self.snapshot.validators(type_id)
        except FactoryError:
            return None
        for item in found:
            if item.get("name") == name:
                return item
        return None

    def _one_validator(
        self, type_id: str, validator: dict[str, Any], rel: str, timeout_s: float, *, want_accept: bool
    ) -> dict[str, Any]:
        path = self._fixture_file(rel)
        accepted = self._validator_accepts(type_id, validator, path, timeout_s)
        ok = accepted if want_accept else not accepted
        row: dict[str, Any] = {"path": rel, "ok": ok}
        if not ok:
            row["code"] = "CONTRACT_FIXTURE_REJECTED" if want_accept else "CONTRACT_FIXTURE_ACCEPTED"
        return row

    def _validator_accepts(
        self, type_id: str, validator: dict[str, Any], path: Path, timeout_s: float
    ) -> bool:
        self._seq += 1
        host = self.ctx.root / "tmp" / "contract" / f"v{self._seq}"
        payload = host / "payload"
        payload.mkdir(parents=True, exist_ok=True)
        names = [
            Path(str(arg)).name
            for arg in (validator.get("run") or [])
            if str(arg).startswith("/bean/payload/")
        ]
        if not names:
            names = [path.name]
        for name in names:
            shutil.copyfile(path, payload / name)
        body = {
            "type": type_id,
            "facts": {},
            "meta": {},
            "payload": [{"path": path.name, "digest": digest_file(path), "size": path.stat().st_size}],
        }
        (host / "bean.json").write_text(canonical_json(body), encoding="utf-8")
        name = f"ctv{self._seq}"
        spec = SandboxSpec(
            name=name,
            image=str(validator.get("image") or "local@sha256:" + "0" * 64),
            mounts=(Mount(source=str(host.resolve()), target="/bean", readonly=True),),
            network="none",
        )
        stdout = host / "stdout"
        stderr = host / "stderr"
        sandbox = self.ctx.provider.open(spec)
        try:
            result = sandbox.exec(
                list(validator.get("run") or []), timeout_s=timeout_s, stdout=stdout, stderr=stderr
            )
        finally:
            sandbox.close()
        if result.timed_out or result.exit_code != 0:
            return False
        try:
            parsed = json.loads(stdout.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(parsed, dict):
            return False
        declared = {}
        try:
            declared = self.snapshot.facts_decl(type_id)
        except FactoryError:
            declared = {}
        for emit in validator.get("emits") or []:
            if emit not in parsed or not _type_ok(declared.get(emit, ""), parsed[emit]):
                return False
        return True

    def _golden_case(
        self, station_key: str, case: Mapping[str, Any], timeout_s: float
    ) -> tuple[dict[str, Any], float]:
        del timeout_s
        inputs: dict[str, str] = {}
        failed = False
        for port, raw in (case.get("inputs") or {}).items():
            bean_id = self._case_input(station_key, port, str(raw))
            if bean_id is None:
                failed = True
                break
            inputs[port] = bean_id
        if failed:
            return {"name": case.get("name"), "job": None, "sealed": False, "cost_usd": 0.0}, 0.0
        try:
            job = self.runner.submit(station_key, inputs, dict(case.get("params") or {}))
            job = self.runner.run(job.id)
        except FactoryError:
            return {"name": case.get("name"), "job": None, "sealed": False, "cost_usd": 0.0}, 0.0
        return (
            {
                "name": case.get("name"),
                "job": job.id,
                "sealed": job.state == JobState.SEALED,
                "cost_usd": float(job.cost_usd),
            },
            float(job.cost_usd),
        )

    def _case_input(self, station_key: str, port: str, raw: str) -> str | None:
        if raw.startswith("sha256:"):
            return raw
        path = self._fixture_file(raw)
        station = self.snapshot.spec(station_key)
        decl = (station.get("inputs") or {}).get(port) or {}
        type_expr = decl.get("type") if isinstance(decl, dict) else decl
        return self._admit(str(type_expr), path)

    def _equivalence_one(
        self, station_key: str, spec: Mapping[str, Any], rel: str, *, positive: bool
    ) -> tuple[dict[str, Any], str | None, str | None]:
        port = next(iter(spec.get("inputs") or {"in": {}}))
        type_expr = (spec.get("inputs") or {}).get(port) or {}
        type_id = type_expr.get("type") if isinstance(type_expr, dict) else type_expr
        try:
            path = self._fixture_file(rel)
        except FactoryError as exc:
            if exc.code == "CONTRACT_FIXTURE_PATH":
                raise
            path = None
        bean_id = None if path is None else self._admit(str(type_id), path)
        if bean_id is None:
            if positive:
                return {"path": rel, "sealed": False, "failed_expr": []}, None, "CONTRACT_FIXTURE_REJECTED"
            return {"path": rel, "sealed": False}, None, None
        job = self._run_station(station_key, spec, bean_id)
        sealed = job is not None and job.state == JobState.SEALED
        out_id = _first_output(job) if sealed else None
        if not positive:
            if sealed and out_id:
                return {"path": rel, "sealed": True}, out_id, "CONTRACT_FIXTURE_ACCEPTED"
            return {"path": rel, "sealed": False}, None, None
        if not sealed:
            return {"path": rel, "sealed": False, "failed_expr": []}, None, "CONTRACT_FIXTURE_REJECTED"
        failed: list[int] = []
        src = self.ctx.warehouse.get(bean_id)
        dst = self.ctx.warehouse.get(out_id)
        params = params_value(self.snapshot, station_key, {})
        env = {"src": bean_value(src), "dst": bean_value(dst), "params": params}
        for index, _expr in enumerate(spec.get("equivalence") or []):
            subject = f"{station_key}/equivalence[{index}]"
            try:
                ok = self.snapshot.compiled(subject).evaluate(env) is True
            except Exception:
                ok = False
            if not ok:
                failed.append(index)
        code = "CONTRACT_EQUIVALENCE_FAILED" if failed else None
        return {"path": rel, "sealed": True, "failed_expr": failed}, None, code

    def _run_station(self, station_key: str, spec: Mapping[str, Any], bean_id: str) -> Any:
        port = next(iter(spec.get("inputs") or {"in": {}}))
        try:
            job = self.runner.submit(station_key, {port: bean_id}, {})
            return self.runner.run(job.id)
        except FactoryError:
            return None

    def _diff_inputs(self, spec: Mapping[str, Any], prev_key: str, prev_spec: Mapping[str, Any]) -> list[str]:
        found: list[str] = []
        port = next(iter(spec.get("inputs") or {"in": {}}))
        type_expr = (spec.get("inputs") or {}).get(port) or {}
        type_id = type_expr.get("type") if isinstance(type_expr, dict) else type_expr
        for rel in (spec.get("fixtures") or {}).get("accept") or []:
            bean_id = self._admit(str(type_id), self._fixture_file(rel))
            if bean_id:
                found.append(bean_id)
        prev_port = next(iter(prev_spec.get("inputs") or {"in": {}}))
        prev_decl = (prev_spec.get("inputs") or {}).get(prev_port) or {}
        prev_type = prev_decl.get("type") if isinstance(prev_decl, dict) else prev_decl
        for rel in (prev_spec.get("fixtures") or {}).get("accept") or []:
            bean_id = self._admit(str(prev_type), self._fixture_file(rel))
            if bean_id is None:
                continue
            job = self._run_station(prev_key, prev_spec, bean_id)
            if job is not None and job.state == JobState.SEALED:
                found.append(bean_id)
        rows = self.ctx.db.query(
            "SELECT inputs FROM jobs WHERE station = ? AND state = 'SEALED'",
            (prev_key,),
        )
        for row in rows:
            body = json.loads(row["inputs"])
            for ids in body.values():
                if isinstance(ids, list):
                    found.extend(str(item) for item in ids)
                elif isinstance(ids, str):
                    found.append(ids)
        return sorted(set(found))

    def _facts_equal(self, old_job: Any, new_job: Any) -> bool:
        return self._first_fact_diff(old_job, new_job) is None

    def _first_fact_diff(self, old_job: Any, new_job: Any) -> str | None:
        old_facts = self._job_facts(old_job)
        new_facts = self._job_facts(new_job)
        for name in sorted(set(old_facts) & set(new_facts)):
            if canonical_json(old_facts[name]) != canonical_json(new_facts[name]):
                return name
        return None

    def _job_facts(self, job: Any) -> dict[str, Any]:
        bean_id = _first_output(job)
        if not bean_id:
            return {}
        return dict(self.ctx.warehouse.get(bean_id).facts)

    def _propose_only(self, station_key: str, spec: Mapping[str, Any], job: Any) -> dict[str, Any] | None:
        from mao.approvals import Approvals

        machine = self.snapshot.spec(self.snapshot.key("Machine", spec["machine"]))
        recipe = (machine.get("recipes") or {}).get(spec.get("recipe"), {})
        propose = recipe.get("propose")
        if not propose:
            return None
        self._seq += 1
        host = self.ctx.root / "tmp" / "contract" / f"p{self._seq}"
        for name in ("in", "out", "ctl"):
            (host / name).mkdir(parents=True, exist_ok=True)
        spec_box = SandboxSpec(
            name=f"ctp{self._seq}",
            image=str((machine.get("runtime") or {}).get("image") or "local@sha256:" + "0" * 64),
            mounts=(
                Mount(source=str((host / "in").resolve()), target="/factory/in", readonly=True),
                Mount(source=str((host / "out").resolve()), target="/factory/out", readonly=False),
                Mount(source=str((host / "ctl").resolve()), target="/factory/ctl", readonly=False),
            ),
            network="none",
        )
        sandbox = self.ctx.provider.open(spec_box)
        try:
            sandbox.exec(
                list(propose),
                timeout_s=30,
                stdout=host / "stdout",
                stderr=host / "stderr",
            )
        finally:
            sandbox.close()
        proposal_path = host / "ctl" / "proposal.json"
        if not proposal_path.is_file():
            return None
        try:
            payload = json.loads(proposal_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        approvals = Approvals(self.ctx, self.snapshot)
        try:
            opened = approvals.open_proposal(
                job_id=job.id,
                attempt=job.attempt,
                station_key=station_key,
                payload=payload,
                inputs={},
                params=job.params,
            )
        except FactoryError as exc:
            return {"id": None, "status": "error", "code": exc.code}
        return {"id": opened.id, "status": opened.status.value}

    def _admit(self, type_id: str, path: Path) -> str | None:
        try:
            type_spec = self.snapshot.spec(self.snapshot.type_key(type_id))
        except FactoryError:
            return None
        rules = type_spec.get("payload") or [{"path": path.name}]
        rel = str(rules[0].get("path", path.name)).replace("*", path.name)
        report, bean = self.runner.gate.admit_external(type_id, {rel: path}, {})
        if report.verdict != "pass" or bean is None:
            return None
        self.ctx.warehouse.seal_many([(bean.id, report.facts.get(bean.id, {}))], None, report.digest())
        return bean.id

    def _fixture_file(self, rel: str) -> Path:
        root = Path(self.snapshot.root).resolve()
        path = (root / rel).resolve()
        if not _inside(path, root):
            self._cannot("CONTRACT_FIXTURE_PATH", "样例路径越出 Registry", path=rel)
        return path

    def _record(self, kind: str, ref: str) -> dict[str, Any] | None:
        key = self.snapshot.key(kind, ref)
        if key not in self.snapshot.keys(kind):
            return None
        return self.snapshot.doc(key)

    def _all_records(self, kind: str) -> list[dict[str, Any]]:
        return [self.snapshot.doc(key) for key in self.snapshot.keys(kind)]

    def _station(self, ref: str) -> tuple[str, dict[str, Any]]:
        key = self.snapshot.key("Station", ref)
        try:
            return key, self.snapshot.spec(key)
        except FactoryError as exc:
            self._cannot("CONTRACT_NOT_FOUND", exc.message, subject=key)

    def _previous(self, key: str, spec: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        del spec
        name = key.split(":", 1)[1].rsplit("@", 1)[0]
        current = _version_tuple(key.rsplit("@", 1)[1])
        best: tuple[tuple[int, ...], str] | None = None
        for doc_key in self.snapshot.keys("Station"):
            doc = self.snapshot.doc(doc_key)
            meta = doc.get("metadata") or {}
            if meta.get("name") != name:
                continue
            version = _version_tuple(meta.get("version"))
            if version < current and (best is None or version > best[0]):
                best = (version, doc_key)
        if best is None:
            return None
        return best[1], self.snapshot.spec(best[1])

    def _reject_paths(self, snapshot: Any, type_id: str, validator: str) -> list[str]:
        try:
            found = snapshot.validators(type_id)
        except FactoryError as exc:
            self._cannot("CONTRACT_NOT_FOUND", exc.message, subject=validator)
        for item in found:
            if item.get("name") == validator:
                return [str(path) for path in (item.get("fixtures") or {}).get("reject") or []]
        self._cannot("CONTRACT_NOT_FOUND", f"找不到 validator {validator}", subject=validator)

    def _missing_metric(self, metrics: Mapping[str, Any]) -> str | None:
        for name in metrics:
            if name not in _COMPUTABLE:
                return str(name)
        return None

    def _unknown_metrics(self, metrics: Mapping[str, Any]) -> None:
        missing = self._missing_metric(metrics)
        if missing:
            self._cannot("CONTRACT_METRIC_MISSING", f"不会计算指标 {missing}", metric=missing)
        for name, body in metrics.items():
            if not isinstance(body, Mapping):
                self._cannot("CONTRACT_METRIC_UNKNOWN", f"指标 {name} 不是对象", metric=name)
            for key, value in body.items():
                if (
                    _is_gate(str(key))
                    and isinstance(value, bool)
                    or (_is_gate(str(key)) and not isinstance(value, (int, float)))
                ):
                    self._cannot("CONTRACT_METRIC_UNKNOWN", f"门槛 {key} 不是数", metric=name, key=key)

    def _gates(
        self, metrics: Mapping[str, Any], observed: Mapping[str, float]
    ) -> tuple[list[dict[str, Any]], str | None]:
        gates = []
        code = None
        for metric, body in metrics.items():
            if metric not in observed or not isinstance(body, Mapping):
                continue
            for key, limit in body.items():
                if not _is_gate(str(key)):
                    continue
                if isinstance(limit, bool) or not isinstance(limit, (int, float)):
                    self._cannot("CONTRACT_METRIC_UNKNOWN", f"门槛 {key} 不是数", metric=metric, key=key)
                value = float(observed[metric])
                limit_f = float(limit)
                ok = not _over(str(key), value, limit_f)
                gates.append({"metric": metric, "key": key, "limit": limit_f, "observed": value, "ok": ok})
                if not ok and code is None:
                    code = "CONTRACT_BASELINE_EXCEEDED"
        return gates, code

    def _need_timeout(
        self, timeout_s: float | None, station: Mapping[str, Any] | None, subject: str
    ) -> float:
        if timeout_s is not None and float(timeout_s) > 0:
            return float(timeout_s)
        raw = None if station is None else (station.get("limits") or {}).get("timeout")
        if raw:
            from mao.base import parse_duration

            return parse_duration(str(raw))
        self._cannot("CONTRACT_TIMEOUT_REQUIRED", "没有超时", subject=subject)

    def _store(self, kind: str, subject: str, verdict: Verdict, detail: dict[str, Any]) -> ContractResult:
        clean = self._secrets.redact_obj(detail)
        result_id = self.ctx.ids.new("ct")
        at = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                """INSERT INTO contract_runs (id, kind, subject, verdict, detail, at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (result_id, kind, subject, verdict.value, canonical_json(clean), at),
            )
        return ContractResult(result_id, kind, subject, verdict, clean, at)

    def _cannot(self, code: str, message: str, **detail: Any) -> None:
        raise FactoryError(code, message, **detail)


def _over(key: str, observed: float, limit: float) -> bool:
    if key == "max" or key.startswith("max_"):
        return observed >= limit if limit == 0 else observed > limit
    if key == "min" or key.startswith("min_"):
        return observed < limit
    return False


def _is_gate(key: str) -> bool:
    return key in {"max", "min"} or key.startswith("max_") or key.startswith("min_")


def _type_ok(declared: str, value: Any) -> bool:
    if declared in {"", "string"} and declared == "string":
        return isinstance(value, str)
    if declared == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared in {"float", "num"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared == "bool":
        return isinstance(value, bool)
    if declared == "":
        return True
    return isinstance(value, (str, int, float, bool)) and not (declared == "int" and isinstance(value, bool))


def _first_code(rows: list[dict[str, Any]]) -> str | None:
    for row in rows:
        if row.get("code"):
            return str(row["code"])
    return None


def _public_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": item["name"], "job": item["job"], "sealed": item["sealed"]} for item in cases]


def _first_output(job: Any) -> str | None:
    if job is None:
        return None
    for ids in job.outputs.values():
        if ids:
            return str(ids[0])
    return None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _name_version(key: str) -> tuple[str, str]:
    ref = key.split(":", 1)[1]
    name, version = ref.rsplit("@", 1)
    return name, version


def _version_tuple(value: Any) -> tuple[int, ...]:
    text = str(value)
    parts = []
    for part in text.split("."):
        if part.isdigit():
            parts.append(int(part))
        else:
            parts.append(0)
    return tuple(parts) or (0,)


def _doc_key(kind: str, doc: Mapping[str, Any]) -> str:
    meta = doc.get("metadata") or {}
    return f"{kind}:{meta.get('name')}@{meta.get('version')}"


def _row_result(row: Mapping[str, Any]) -> ContractResult:
    return ContractResult(
        str(row["id"]),
        str(row["kind"]),
        str(row["subject"]),
        Verdict(str(row["verdict"])),
        json.loads(row["detail"]),
        str(row["at"]),
    )
