"""ChangeSet 受理、验证与复核。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from mao.base import canonical_bytes, canonical_json, digest_bytes, parse_duration, utc_iso
from mao.errors import FactoryError
from mao.governance.epochs import EpochStore
from mao.governance.git import git, git_bytes
from mao.governance.risk import diff_facts, governance_limits, score_risk

_SLO = re.compile(r"^(>=|<=|>|<)\s*[0-9]+(\.[0-9]+)?$")
_SLO_NAMES = {"success_rate", "gate_reject_rate", "cost_per_run_usd"}


@dataclass(frozen=True)
class ChangeSet:
    id: str
    base_epoch: str
    source_commit: str
    commit: str
    state: str
    risk_tier: str | None
    reviews_required: int
    proposer: str
    proposer_runtime: str | None
    document: dict[str, Any]
    report: dict[str, Any]
    stage_started_at: str | None
    activated_at: str | None
    created_at: str
    updated_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "base_epoch": self.base_epoch,
            "source_commit": self.source_commit,
            "commit": self.commit,
            "state": self.state,
            "risk_tier": self.risk_tier,
            "reviews_required": self.reviews_required,
            "proposer": self.proposer,
            "proposer_runtime": self.proposer_runtime,
            "document": self.document,
            "report": self.report,
            "stage_started_at": self.stage_started_at,
            "activated_at": self.activated_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ChangePipeline:
    def __init__(
        self,
        ctx: Any,
        epochs: EpochStore | None = None,
        *,
        contracts: Callable[[Any], Any] | None = None,
        shadow: Any = None,
        rebuild: Callable[[str, Any], str] | None = None,
    ) -> None:
        self.ctx = ctx
        self.epochs = epochs or EpochStore(ctx)
        if contracts is None:

            def contracts(snap: Any) -> Any:
                from mao.contract import ContractRunner
                from mao.runner import open_runner

                return ContractRunner(ctx, snap, open_runner(ctx, snap))

        if shadow is None:
            from mao.governance.shadow import LineShadowRunner

            shadow = LineShadowRunner(ctx)
        if rebuild is None:

            def rebuild(run_id: str, snap: Any) -> str:
                from mao.runs import open_line_runner

                return open_line_runner(ctx, snap).rebuild(run_id, by="governance").id

        self.contracts = contracts
        self.shadow = shadow
        self.rebuild = rebuild

    def propose(self, document: Mapping[str, Any], commit: str, *, by: str, timeout_s: float) -> ChangeSet:
        doc = json.loads(json.dumps(document))
        self._validate(doc)
        self._freeze()
        self._rate()
        source = self.epochs.verify_commit(commit)
        active = self.epochs.current()
        base = str((doc.get("spec") or {}).get("base_epoch", ""))
        if not (7 <= len(base) <= 40 and active.startswith(base)):
            raise FactoryError("CHANGESET_STALE", "基线不是当前 active", base_epoch=base, active=active)
        try:
            git(self.epochs.registry, "merge-base", "--is-ancestor", active, source, timeout_s=timeout_s)
        except FactoryError as exc:
            raise FactoryError(
                "CHANGESET_STALE", "提交不是当前 active 的后代", base_epoch=base, active=active
            ) from exc
        names = _diff_names(self.epochs.registry, active, source, timeout_s)
        blocked = [name for name in names if name.startswith("constitution/")]
        if blocked:
            raise FactoryError("CONSTITUTION_FORBIDDEN", "不能修改宪法", path=blocked[0])
        if not names:
            raise FactoryError("CHANGESET_INVALID", "变更是空的", reason="empty")
        spec = doc.setdefault("spec", {})
        spec.pop("risk_tier", None)
        spec.pop("status", None)
        cs_id = self.ctx.ids.new("cs")
        tree = git(self.epochs.registry, "rev-parse", f"{source}^{{tree}}", timeout_s=timeout_s).strip()
        message = f"changeset: {cs_id}\n\n{spec.get('intent', '')}"
        created = git(
            self.epochs.registry,
            "commit-tree",
            tree,
            "-p",
            active,
            "-m",
            message,
            timeout_s=timeout_s,
            env=_git_identity(self.ctx),
        ).strip()
        git(self.epochs.registry, "update-ref", f"refs/changesets/{cs_id}", created, timeout_s=timeout_s)
        now = utc_iso(self.ctx.clock.now())
        runtime = None
        if not by.startswith("human:"):
            runtime = self.epochs.materialize(active).key("OperatorRuntime", by)
        with self.ctx.db.tx() as conn:
            conn.execute(
                """INSERT INTO changesets
                   (id, base_epoch, source_commit, "commit", state, reviews_required, proposer,
                    proposer_runtime, document, report, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'draft', 0, ?, ?, ?, '{}', ?, ?)""",
                (cs_id, active, source, created, by, runtime, canonical_json(doc), now, now),
            )
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'changeset.proposed', NULL, ?)",
                (
                    self.ctx.ids.new("ev"),
                    now,
                    canonical_json(
                        {
                            "id": cs_id,
                            "base_epoch": active,
                            "source_commit": source,
                            "commit": created,
                            "by": by,
                        }
                    ),
                ),
            )
        return self.get(cs_id)

    def get(self, cs_id: str) -> ChangeSet:
        rows = self.ctx.db.query("SELECT * FROM changesets WHERE id = ?", (cs_id,))
        if not rows:
            raise FactoryError("CHANGESET_NOT_FOUND", f"找不到变更 {cs_id}", id=cs_id)
        return _changeset(rows[0])

    def signoff(self, cs_id: str, decision: str, *, by: str, note: str = "") -> ChangeSet:
        current = self.get(cs_id)
        if current.state != "verified":
            raise FactoryError("CHANGESET_STATE_INVALID", "当前状态不能签核", id=cs_id, state=current.state)
        if re.fullmatch(r"human:\S+", by) is None:
            raise FactoryError("SIGNOFF_INVALID", "签核身份不合法", reason="by")
        if decision not in ("approve", "deny"):
            raise FactoryError("SIGNOFF_INVALID", "签核决定不合法", reason="decision")
        if len(note) > 2000:
            raise FactoryError("SIGNOFF_INVALID", "签核备注过长", reason="note")
        if self.signoff_of(cs_id) is not None:
            raise FactoryError("CHANGESET_STATE_INVALID", "已经签核", id=cs_id, state=current.state)
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "INSERT INTO changeset_signoffs (changeset_id, decision, by, note, at) VALUES (?, ?, ?, ?, ?)",
                (cs_id, decision, by, note, now),
            )
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'changeset.signoff', NULL, ?)",
                (
                    self.ctx.ids.new("ev"),
                    now,
                    canonical_json({"id": cs_id, "decision": decision, "by": by}),
                ),
            )
        from mao.inbox import Inbox

        inbox = Inbox(self.ctx)
        item = inbox.find("signoff", cs_id)
        if item is not None:
            inbox.close(item.id, status="answered", answer={"decision": decision, "note": note}, by=by)
        return self.get(cs_id)

    def signoff_of(self, cs_id: str) -> dict[str, Any] | None:
        rows = self.ctx.db.query("SELECT * FROM changeset_signoffs WHERE changeset_id = ?", (cs_id,))
        if not rows:
            return None
        row = rows[0]
        return {"decision": row["decision"], "by": row["by"], "note": row["note"], "at": row["at"]}

    def list(self, *, state: str | None = None) -> list[ChangeSet]:
        rows = self.ctx.db.query("SELECT * FROM changesets ORDER BY id")
        found = [_changeset(row) for row in rows]
        if state is not None:
            found = [item for item in found if item.state == state]
        return found

    def withdraw(self, cs_id: str, *, by: str) -> ChangeSet:
        current = self.get(cs_id)
        if current.state not in ("draft", "verified"):
            raise FactoryError("CHANGESET_STATE_INVALID", "当前状态不能撤回", id=cs_id, state=current.state)
        self._write_state(current, "withdrawn", "WITHDRAWN", {"by": by})
        return self.get(cs_id)

    def add_review(self, cs_id: str, *, runtime: str, verdict: str, note: str, by: str) -> str:
        current = self.get(cs_id)
        if current.state not in ("verified", "shadow", "canary"):
            raise FactoryError("CHANGESET_STATE_INVALID", "当前状态不能复核", id=cs_id, state=current.state)
        if verdict not in ("approve", "deny") or len(note) > 2000:
            raise FactoryError("REVIEW_INVALID", "复核不合法", id=cs_id, runtime=runtime)
        snapshot = self.epochs.materialize(current.commit)
        runtime_key = snapshot.key("OperatorRuntime", runtime)
        try:
            snapshot.doc(runtime_key)
        except FactoryError as exc:
            raise FactoryError("REVIEW_INVALID", "复核运行时不存在", id=cs_id, runtime=runtime) from exc
        if runtime_key == current.proposer_runtime:
            raise FactoryError("REVIEW_SAME_RUNTIME", "提出者不能复核自己", id=cs_id, runtime=runtime_key)
        digest = digest_bytes(canonical_bytes(snapshot.doc(runtime_key)))
        now = utc_iso(self.ctx.clock.now())
        rows = self.ctx.db.query(
            "SELECT id FROM reviews WHERE changeset_id = ? AND runtime = ?",
            (cs_id, runtime_key),
        )
        with self.ctx.db.tx() as conn:
            if rows:
                review_id = rows[0]["id"]
                conn.execute(
                    "UPDATE reviews SET verdict = ?, note = ?, by = ?, at = ?, runtime_digest = ? WHERE id = ?",
                    (verdict, note, by, now, digest, review_id),
                )
            else:
                review_id = self.ctx.ids.new("rv")
                conn.execute(
                    """INSERT INTO reviews
                       (id, changeset_id, runtime, runtime_digest, verdict, note, by, at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (review_id, cs_id, runtime_key, digest, verdict, note, by, now),
                )
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'changeset.review', NULL, ?)",
                (
                    self.ctx.ids.new("ev"),
                    now,
                    canonical_json(
                        {"id": cs_id, "review": review_id, "runtime": runtime_key, "verdict": verdict}
                    ),
                ),
            )
        return str(review_id)

    def approvals(self, cs_id: str) -> list[str]:
        current = self.get(cs_id)
        snapshot = self.epochs.materialize(current.commit)
        rows = self.ctx.db.query(
            "SELECT runtime, runtime_digest, verdict FROM reviews WHERE changeset_id = ? ORDER BY runtime",
            (cs_id,),
        )
        found = []
        for row in rows:
            if row["verdict"] != "approve" or row["runtime"] == current.proposer_runtime:
                continue
            digest = digest_bytes(canonical_bytes(snapshot.doc(row["runtime"])))
            if row["runtime_digest"] == digest:
                found.append(str(row["runtime"]))
        return found

    def advance(self, cs_id: str) -> ChangeSet:
        current = self.get(cs_id)
        if current.state != "draft":
            from mao.governance.rollout import advance_open

            return advance_open(self, current)
        self._write_state(current, "verifying", None, {})
        current = self.get(cs_id)
        try:
            candidate = self.epochs.materialize(current.commit)
            missing = [item.to_json() for item in candidate.result.diags if item.code == "E-NO-CONVERTER"]
            if missing:
                from mao.foreman import file_conversions

                names = {
                    str(change.get("name"))
                    for change in ((current.document.get("spec") or {}).get("changes") or [])
                    if change.get("kind") == "Line"
                }
                scoped = [
                    item
                    for item in candidate.result.resolutions
                    if item.get("status") == "no-converter"
                    and str(item.get("line", "")).split(":", 1)[-1].split("@", 1)[0] in names
                ]
                file_conversions(
                    self.ctx,
                    missing,
                    scoped
                    or [
                        item for item in candidate.result.resolutions if item.get("status") == "no-converter"
                    ],
                    found_at={"changeset": current.id},
                )
                return self.reject_changeset(current, "REGISTRY_INVALID", {"diags": missing})
        except FactoryError as exc:
            if exc.code == "REGISTRY_INVALID":
                from mao.foreman import file_conversions

                file_conversions(
                    self.ctx,
                    list(exc.detail.get("diags") or []),
                    list(exc.detail.get("resolutions") or []),
                    found_at={"changeset": current.id},
                )
                return self.reject_changeset(
                    current, "REGISTRY_INVALID", {"diags": exc.detail.get("diags", [])}
                )
            raise
        runtime = self._proposer_runtime(current, candidate)
        if isinstance(runtime, str) and runtime.startswith("!"):
            return self.reject_changeset(current, "PROPOSER_UNKNOWN", {"by": current.proposer})
        problem = self._immutable(self.epochs.materialize(current.base_epoch), candidate)
        if problem:
            return self.reject_changeset(current, "V3_IMMUTABLE", problem)
        facts, changes = diff_facts(
            self.epochs.materialize(current.base_epoch),
            candidate,
            base_resolutions=_load_resolutions(self.ctx.root, current.base_epoch),
        )
        mismatch = self._mismatch(current.document, changes)
        if mismatch:
            return self.reject_changeset(current, "CHANGESET_MISMATCH", mismatch)
        incompatible = self._incompatible(self.epochs.materialize(current.base_epoch), candidate, changes)
        if incompatible:
            return self.reject_changeset(current, "V3_INCOMPATIBLE", incompatible)
        try:
            risk = score_risk(facts)
        except FactoryError as exc:
            if exc.code == "RISK_UNSCORED":
                return self.reject_changeset(current, "RISK_UNSCORED", exc.detail)
            raise
        failed = self._v4(candidate, changes)
        if failed:
            return self.reject_changeset(current, failed[0], failed[1])
        impact = {
            "lines": [],
            "runs": [],
            "stations": sorted({item["key"] for item in changes if item["kind"] == "Station"}),
            "sinks": [],
        }
        report = {
            "stage": "verifying",
            "ok": True,
            "code": None,
            "detail": {},
            "impact": impact,
            "stages": list(risk.stages),
            "multiplier": risk.multiplier,
        }
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                """UPDATE changesets
                   SET state = 'verified', risk_tier = ?, reviews_required = ?, proposer_runtime = ?,
                       report = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    risk.tier,
                    risk.reviews_required,
                    None if current.proposer.startswith("human:") else runtime,
                    canonical_json(report),
                    now,
                    cs_id,
                ),
            )
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'changeset.state', NULL, ?)",
                (
                    self.ctx.ids.new("ev"),
                    now,
                    canonical_json({"id": cs_id, "from": "verifying", "to": "verified", "code": None}),
                ),
            )
        return self.get(cs_id)

    def tick(self) -> list[ChangeSet]:
        from mao.governance.rollout import evaluate_safe_mode

        evaluate_safe_mode(self.ctx)
        changed = []
        rows = self.ctx.db.query(
            "SELECT id, state FROM changesets WHERE state NOT IN ('rejected', 'withdrawn', 'rolled-back') ORDER BY id"
        )
        for row in rows:
            before = self.get(row["id"])
            after = self.advance(row["id"])
            if after.state != before.state or after.updated_at != before.updated_at:
                changed.append(after)
        return changed

    def safe_mode(self) -> dict[str, Any]:
        from mao.governance.rollout import safe_mode_row

        row = safe_mode_row(self.ctx)
        return {"on": row["on"], "level": row["level"]}

    def _proposer_runtime(self, current: ChangeSet, snapshot: Any) -> str | None:
        if current.proposer.startswith("human:"):
            return None
        try:
            return snapshot.key("OperatorRuntime", current.proposer)
        except FactoryError:
            return "!missing"

    def _immutable(self, base: Any, candidate: Any) -> dict[str, Any] | None:
        base_keys = set(base.keys())
        cand_keys = set(candidate.keys())
        for key in candidate.keys():
            if key not in base_keys:
                continue
            if _norm(base.doc(key)) != _norm(candidate.doc(key)):
                return {"key": key, "reason": "modified"}
        for key in base.keys():
            if key not in cand_keys and _split_kind(key) in {
                "Type",
                "Machine",
                "OperatorRuntime",
                "Station",
                "Line",
                "Policy",
                "GoldenSet",
                "Baseline",
            }:
                return {"key": key, "reason": "deleted"}
        return None

    def _mismatch(self, document: Mapping[str, Any], changes: list[dict[str, Any]]) -> dict[str, Any] | None:
        declared = {
            (item["kind"], item["name"], item["op"])
            for item in (document.get("spec") or {}).get("changes") or []
        }
        actual = set()
        for item in changes:
            op = "upsert" if item["op"] in ("add", "modify", "minor", "major") else item["op"]
            actual.add((item["kind"], item["name"], op))
        if declared == actual:
            return None
        return {
            "declared_only": sorted([list(item) for item in declared - actual]),
            "actual_only": sorted([list(item) for item in actual - declared]),
        }

    def _incompatible(
        self, base: Any, candidate: Any, changes: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        for item in changes:
            if item["op"] != "minor":
                continue
            old = item.get("old") or {}
            new = item.get("doc") or {}
            old_paths = _required_paths(old)
            new_paths = _required_paths(new)
            if old_paths != new_paths or not old_paths <= new_paths:
                return {"type": item["key"], "reason": "required payload changed"}
        return None

    def _v4(self, snapshot: Any, changes: list[dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
        runner = self.contracts(snapshot)
        results = []
        names = {item["name"] for item in changes if item["kind"] == "Station"}
        timeout = float(getattr(self.epochs, "timeout_s", 60) or 60)
        try:
            for item in changes:
                spec = (item.get("doc") or {}).get("spec") or {}
                if item["kind"] == "Station" and item["op"] == "modify":
                    results.append(runner.run_diff(item["key"], timeout_s=timeout))
                if item["kind"] == "Station" and spec.get("role") == "converter":
                    results.append(runner.run_equivalence(item["key"], timeout_s=timeout))
                if item["kind"] == "Type" and item["op"] in ("add", "minor", "major"):
                    for validator in spec.get("validators") or []:
                        results.append(
                            runner.run_validator(
                                item["key"].split(":", 1)[1],
                                validator.get("name"),
                                timeout_s=timeout,
                            )
                        )
            for key in snapshot.keys("GoldenSet"):
                station = str((snapshot.doc(key).get("spec") or {}).get("station", ""))
                if station.split("@", 1)[0] in names:
                    results.append(runner.run_golden(key, None, timeout_s=timeout))
        except FactoryError as exc:
            return "V4_ERROR", {"cause": exc.code}
        failed = [result.id for result in results if getattr(result, "verdict", None) != "pass"]
        if failed:
            return "V4_FAILED", {"results": failed}
        return None

    def reject_changeset(self, current: ChangeSet, code: str, detail: Mapping[str, Any]) -> ChangeSet:
        report = {"stage": "verifying", "ok": False, "code": code, "detail": dict(detail)}
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE changesets SET state = 'rejected', report = ?, updated_at = ? WHERE id = ?",
                (canonical_json(report), now, current.id),
            )
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'changeset.state', NULL, ?)",
                (
                    self.ctx.ids.new("ev"),
                    now,
                    canonical_json({"id": current.id, "from": current.state, "to": "rejected", "code": code}),
                ),
            )
        return self.get(current.id)

    def _write_state(
        self, current: ChangeSet, state: str, code: str | None, detail: Mapping[str, Any]
    ) -> None:
        now = utc_iso(self.ctx.clock.now())
        report = {"stage": current.state, "ok": code is None, "code": code, "detail": dict(detail)}
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE changesets SET state = ?, report = ?, updated_at = ? WHERE id = ?",
                (state, canonical_json(report), now, current.id),
            )
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'changeset.state', NULL, ?)",
                (
                    self.ctx.ids.new("ev"),
                    now,
                    canonical_json({"id": current.id, "from": current.state, "to": state, "code": code}),
                ),
            )

    def _validate(self, doc: Mapping[str, Any]) -> None:
        spec = doc.get("spec") or {}
        problems = []
        rollout = spec.get("rollout") or {}
        shadow = rollout.get("shadow") or {}
        if "budget_usd" in shadow and not isinstance(shadow["budget_usd"], (int, float)):
            problems.append("shadow.budget_usd")
        observe = rollout.get("observe")
        if observe is not None:
            try:
                parse_duration(str(observe))
            except FactoryError:
                problems.append("observe")
        canary = rollout.get("canary") or {}
        percent = canary.get("percent")
        if percent is not None and (not isinstance(percent, int) or not 1 <= percent <= 100):
            problems.append("canary.percent")
        slo = rollout.get("slo") or {}
        for name, value in slo.items():
            if name not in _SLO_NAMES or not _SLO.fullmatch(str(value)):
                problems.append(f"slo.{name}")
        if problems:
            raise FactoryError("CHANGESET_INVALID", "变更文档不合法", problems=problems)

    def _freeze(self) -> None:
        path = self.ctx.root / "factory.json"
        body = json.loads(path.read_text(encoding="utf-8"))
        now = self.ctx.clock.now()
        for window in (body.get("governance") or {}).get("freeze") or []:
            start = _parse(window["start"])
            until = _parse(window["until"])
            if start <= now < until:
                raise FactoryError("FREEZE_WINDOW", "封版期内不能受理", until=window["until"])

    def _rate(self) -> None:
        limit = governance_limits()["active_per_24h"]
        now = self.ctx.clock.now()
        count = 0
        for row in self.ctx.db.query("SELECT activated_at FROM changesets WHERE activated_at IS NOT NULL"):
            if now - _parse(row["activated_at"]) <= timedelta(hours=24):
                count += 1
        if count >= limit:
            raise FactoryError("RATE_LIMITED", "推广过于频繁", count=count)


def _changeset(row: Any) -> ChangeSet:
    return ChangeSet(
        row["id"],
        row["base_epoch"],
        row["source_commit"],
        row["commit"],
        row["state"],
        row["risk_tier"],
        int(row["reviews_required"]),
        row["proposer"],
        row["proposer_runtime"],
        json.loads(row["document"]),
        json.loads(row["report"] or "{}"),
        row["stage_started_at"],
        row["activated_at"],
        row["created_at"],
        row["updated_at"],
    )


def _diff_names(repo: Any, left: str, right: str, timeout_s: float) -> list[str]:
    blob = git_bytes(repo, "diff", "--name-only", "--no-renames", "-z", left, right, timeout_s=timeout_s)
    return [item.decode("utf-8") for item in blob.split(b"\0") if item]


def _git_identity(ctx: Any) -> dict[str, str]:
    stamp = int(ctx.clock.now().timestamp())
    return {
        "GIT_AUTHOR_NAME": "mao-governance",
        "GIT_COMMITTER_NAME": "mao-governance",
        "GIT_AUTHOR_EMAIL": "governance@factory.invalid",
        "GIT_COMMITTER_EMAIL": "governance@factory.invalid",
        "GIT_AUTHOR_DATE": f"@{stamp} +0000",
        "GIT_COMMITTER_DATE": f"@{stamp} +0000",
    }


def _load_resolutions(root: Any, commit: str) -> list[dict[str, Any]]:
    path = root / "epochs" / commit / "resolutions.json"
    if not path.is_file():
        return []
    body = json.loads(path.read_text(encoding="utf-8"))
    return body if isinstance(body, list) else []


def _required_paths(doc: Mapping[str, Any]) -> set[str]:
    found = set()
    for item in (doc.get("spec") or {}).get("payload") or []:
        if item.get("required", True):
            found.add(str(item.get("path", "")))
    return found


def _norm(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _norm(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_norm(item) for item in value]
    return value


def _split_kind(key: str) -> str:
    return key.split(":", 1)[0]


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
