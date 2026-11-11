"""不变量审计与声明态对账。"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from mao.base import canonical_json, digest_file, utc_iso
from mao.errors import FactoryError
from mao.governance.git import git
from mrl.factory.check import check_registry
from mrl.yamlio import YamlError, load_documents


class FindingState(StrEnum):
    OPEN = "open"
    RESTORED = "restored"
    HELD = "held"
    WAKE = "wake"


@dataclass(frozen=True)
class Finding:
    id: str
    invariant: str
    subject: str
    state: FindingState
    detail: dict[str, Any]
    at: str


class Reconciler:
    def __init__(self, ctx: Any, epochs: Any) -> None:
        self.ctx = ctx
        self.epochs = epochs

    def audit_once(self, *, timeout_s: float) -> list[Finding]:
        self._cas()
        self._lineage()
        self._unsealed_inputs()
        self._git_refs(timeout_s)
        self._images(timeout_s)
        self._drift()
        self._resolutions()
        self._missing_contract()
        return self.findings()

    def findings(self, *, state: str | None = None) -> list[Finding]:
        if state is None:
            rows = self.ctx.db.query("SELECT * FROM audit_findings ORDER BY id")
        else:
            rows = self.ctx.db.query("SELECT * FROM audit_findings WHERE state = ? ORDER BY id", (state,))
        return [_finding(row) for row in rows]

    def holds(self) -> list[str]:
        return [
            str(row["station"])
            for row in self.ctx.db.query("SELECT station FROM station_holds ORDER BY station")
        ]

    def restore_refs(self, *, timeout_s: float) -> None:
        self._git_refs(timeout_s)

    def _cas(self) -> None:
        problems = self.ctx.warehouse.verify()
        cas = self.ctx.warehouse.root / "cas" / "sha256"
        if cas.is_dir():
            for path in sorted(cas.rglob("*")):
                if not path.is_file():
                    continue
                expected = "sha256:" + path.parent.name + path.name
                actual = digest_file(path)
                if actual != expected:
                    problems.append(
                        {"kind": "digest_mismatch", "path": str(path), "digest": expected, "actual": actual}
                    )
        for item in problems:
            raw_path = str(item.get("path", ""))
            path = _relative_path(self.ctx.warehouse.root, raw_path)
            expected = str(item.get("digest", ""))
            actual = (
                digest_file(Path(raw_path))
                if raw_path and Path(raw_path).is_file()
                else str(item.get("actual", ""))
            )
            finding = self._record(
                "I1",
                path or expected,
                FindingState.WAKE,
                {"digest": expected, "path": path, "expected": expected, "actual": actual},
            )
            self._wake(finding, "invariant")

    def _lineage(self) -> None:
        rows = self.ctx.db.query("SELECT job_id, bean_id FROM lineage WHERE direction = 'in'")
        known = {str(row["id"]) for row in self.ctx.db.query("SELECT id FROM beans")}
        for row in rows:
            bean_id = str(row["bean_id"])
            if bean_id in known:
                continue
            finding = self._record(
                "I2",
                bean_id,
                FindingState.WAKE,
                {"bean": bean_id, "missing_input": bean_id},
            )
            self._wake(finding, "invariant")

    def _unsealed_inputs(self) -> None:
        for row in self.ctx.db.query("SELECT id, station, state, inputs FROM jobs"):
            try:
                inputs = json.loads(row["inputs"])
            except json.JSONDecodeError:
                continue
            for ids in inputs.values():
                values = ids if isinstance(ids, list) else [ids]
                for bean_id in values:
                    try:
                        bean = self.ctx.warehouse.get(str(bean_id))
                    except FactoryError:
                        continue
                    if bean.sealed:
                        continue
                    finding = self._record(
                        "I3",
                        str(bean_id),
                        FindingState.HELD,
                        {"job": row["id"], "bean": str(bean_id)},
                    )
                    if row["state"] not in {"SEALED", "DEAD"}:
                        self._hold(str(row["station"]), finding.id)

    def _git_refs(self, timeout_s: float) -> None:
        declared = self.epochs.pointers().get("active")
        if not declared:
            return
        try:
            actual = git(
                self.epochs.registry, "rev-parse", "refs/factory/active", timeout_s=timeout_s
            ).strip()
        except FactoryError:
            actual = ""
        if actual == declared:
            return
        try:
            git(self.epochs.registry, "update-ref", "refs/factory/active", declared, timeout_s=timeout_s)
        except FactoryError as exc:
            self._record(
                "I9",
                "refs/factory/active",
                FindingState.WAKE,
                {"kind": "git-ref", "declared": declared, "actual": actual, "stderr": "", "code": exc.code},
            )
            return
        self._record(
            "I9",
            "refs/factory/active",
            FindingState.RESTORED,
            {"kind": "git-ref", "declared": declared, "actual": actual},
        )

    def _images(self, timeout_s: float) -> None:
        snapshot = self.epochs.materialize(self.epochs.current())
        images = []
        for key, doc in snapshot._docs.items():
            if not str(key).startswith("Machine:"):
                continue
            image = str(((doc.get("spec") or {}).get("runtime") or {}).get("image") or "")
            if image:
                images.append(image)
        for image in sorted(set(images)):
            docker = shutil.which("docker") or "docker"
            proc = subprocess.run(
                [docker, "image", "inspect", image],
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
            if proc.returncode == 0:
                continue
            finding = self._record(
                "I9",
                image,
                FindingState.WAKE,
                {"kind": "image", "declared": image, "actual": ""},
            )
            self._wake(finding, "invariant")

    def _drift(self) -> None:
        snapshot = self.epochs.materialize(self.epochs.current())
        baselines = _baselines(Path(self.epochs.registry))
        seen: set[str] = set()
        for baseline in baselines:
            spec = baseline.get("spec") or {}
            station_ref = str(spec.get("station") or "")
            if not station_ref:
                continue
            station = snapshot.key("Station", station_ref)
            seen.add(station)
            metrics = spec.get("metrics") or {}
            breached = _breach(self.ctx, station, metrics)
            if breached:
                finding = self._record("drift", station, FindingState.HELD, breached)
                self._hold(station, finding.id)
                self._wake(finding, "drift")
            else:
                self._release(station)
        for station in self.holds():
            if station not in seen:
                continue

    def _resolutions(self) -> None:
        epoch = self.epochs.current()
        path = self.ctx.root / "epochs" / epoch / "resolutions.json"
        if not path.is_file():
            return
        stored = json.loads(path.read_text(encoding="utf-8"))
        result = check_registry(self.epochs.registry)
        recomputed = list(getattr(result, "resolutions", []) or [])
        stored_keys = {_resolution_key(item) for item in stored if isinstance(item, dict)}
        fresh_keys = {_resolution_key(item) for item in recomputed if isinstance(item, dict)}
        if stored_keys != fresh_keys:
            self._record(
                "I14",
                epoch,
                FindingState.WAKE,
                {
                    "epoch": epoch,
                    "line": "",
                    "step": "",
                    "port": "",
                    "stored": sorted(stored_keys)[:1],
                    "recomputed": sorted(fresh_keys)[:1],
                },
            )

    def _missing_contract(self) -> None:
        self._record(
            "I8",
            "contract-runner",
            FindingState.WAKE,
            {"validator": "", "polarity": "", "path": "", "missing": "contract-runner"},
        )

    def _record(self, invariant: str, subject: str, state: FindingState, detail: dict[str, Any]) -> Finding:
        now = utc_iso(self.ctx.clock.now())
        rows = self.ctx.db.query(
            """SELECT id FROM audit_findings
               WHERE invariant = ? AND subject = ? AND state IN ('open', 'held', 'wake')""",
            (invariant, subject),
        )
        if rows:
            finding_id = str(rows[0]["id"])
            with self.ctx.db.tx() as conn:
                conn.execute(
                    "UPDATE audit_findings SET state = ?, detail = ?, updated_at = ? WHERE id = ?",
                    (state.value, canonical_json(detail), now, finding_id),
                )
        else:
            finding_id = self.ctx.ids.new("au")
            with self.ctx.db.tx() as conn:
                conn.execute(
                    """INSERT INTO audit_findings (id, invariant, subject, state, detail, at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (finding_id, invariant, subject, state.value, canonical_json(detail), now, now),
                )
                conn.execute(
                    "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'audit.finding', NULL, ?)",
                    (
                        self.ctx.ids.new("ev"),
                        now,
                        canonical_json(
                            {
                                "id": finding_id,
                                "invariant": invariant,
                                "subject": subject,
                                "state": state.value,
                            }
                        ),
                    ),
                )
        return _finding(self.ctx.db.query("SELECT * FROM audit_findings WHERE id = ?", (finding_id,))[0])

    def _hold(self, station: str, finding_id: str) -> None:
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                """INSERT INTO station_holds (station, finding_id, at) VALUES (?, ?, ?)
                   ON CONFLICT(station) DO UPDATE SET finding_id = excluded.finding_id, at = excluded.at""",
                (station, finding_id, now),
            )

    def _release(self, station: str) -> None:
        rows = self.ctx.db.query(
            """SELECT h.finding_id FROM station_holds h
               JOIN audit_findings f ON f.id = h.finding_id
               WHERE h.station = ? AND f.invariant = 'drift'""",
            (station,),
        )
        if not rows:
            return
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute("DELETE FROM station_holds WHERE station = ?", (station,))
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'audit.release', NULL, ?)",
                (self.ctx.ids.new("ev"), now, canonical_json({"station": station})),
            )
            conn.execute(
                "UPDATE audit_findings SET state = 'restored', updated_at = ? WHERE id = ?",
                (now, rows[0]["finding_id"]),
            )

    def _wake(self, finding: Finding, signal: str) -> None:
        from mao.foreman import WakeQueue

        WakeQueue(self.ctx).file(signal, f"{finding.invariant}:{finding.subject}", {"id": finding.id})


def _finding(row: Any) -> Finding:
    return Finding(
        id=str(row["id"]),
        invariant=str(row["invariant"]),
        subject=str(row["subject"]),
        state=FindingState(str(row["state"])),
        detail=json.loads(row["detail"]),
        at=str(row["at"]),
    )


def _baselines(root: Path) -> list[dict[str, Any]]:
    found = []
    if not root.is_dir():
        return found
    for path in sorted(root.rglob("*.yaml")):
        try:
            docs = load_documents(path.read_text(encoding="utf-8"))
        except (OSError, YamlError, UnicodeError):
            continue
        for doc in docs:
            if isinstance(doc.data, dict) and doc.data.get("kind") == "Baseline":
                found.append(doc.data)
    return found


def _breach(ctx: Any, station: str, metrics: dict[str, Any]) -> dict[str, Any] | None:
    rows = ctx.db.query("SELECT cost_usd FROM jobs WHERE station = ? AND state = 'SEALED'", (station,))
    if not rows or "cost_usd" not in metrics:
        return None
    observed = sum(float(row["cost_usd"] or 0) for row in rows)
    body = metrics["cost_usd"]
    if not isinstance(body, dict):
        return None
    for key, limit in body.items():
        if not str(key).startswith("max") and key != "max":
            continue
        if observed > float(limit):
            return {"station": station, "metric": "cost_usd", "bound": float(limit), "actual": observed}
    return None


def _relative_path(root: Path, path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return Path(path).name


def _resolution_key(item: dict[str, Any]) -> str:
    chain = item.get("chain") or []
    return "|".join(
        [
            str(item.get("line", "")),
            str(item.get("step", "")),
            str(item.get("port", "")),
            str(item.get("status", "")),
            ",".join(str(part) for part in chain),
        ]
    )
