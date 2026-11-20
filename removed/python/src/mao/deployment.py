"""只读预检与幂等部署（spec/system/operability.md 第 3.3、5.5 节）。"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from mao.base import canonical_json
from mao.errors import FactoryError

_DEFAULT_MINIMUMS = {
    "T1": {"shadow_runs": 1, "observe_s": 1},
    "T2": {"shadow_runs": 1, "observe_s": 1},
    "T3": {"shadow_runs": 1, "observe_s": 1},
}


def plan_deployment(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """只计算计划。不创建 root、数据库或租约。"""
    root = Path(root)
    document = _manifest(manifest)
    before = _snapshot_paths(root)
    state = _state_digest(root)
    plan = _plan_body(root, document, state)
    after = _snapshot_paths(root)
    if before != after:
        raise FactoryError("DEPLOY_MANIFEST_INVALID", "plan 改动了文件系统", field="plan")
    return plan


def apply_deployment(root: Path, manifest: dict[str, Any], *, plan_digest: str) -> dict[str, Any]:
    root = Path(root)
    document = _manifest(manifest)
    journal = _load_journal(root, plan_digest)
    if journal.get("stage") == "committed":
        if _state_digest(root) == journal.get("after_state_digest"):
            return {**journal, "stage": "committed", "noop": True}
        raise FactoryError("DEPLOY_CONFLICT", "提交后的根目录又变了", field="state")
    current = _state_digest(root)
    planned = plan_deployment(root, document)
    if planned["plan_digest"] != plan_digest:
        raise FactoryError("DEPLOY_CONFLICT", "计划摘要已变化", field="plan_digest")
    if planned["initial_state_digest"] != current:
        raise FactoryError("DEPLOY_CONFLICT", "根目录在计划之后变了", field="state")
    if _serve_busy(root):
        raise FactoryError("DEPLOY_BUSY", "serve 仍在运行", field="owner")
    journal = _load_journal(root, plan_digest)
    if journal.get("stage") == "committed" and journal.get("manifest_digest") == planned["manifest_digest"]:
        return {**planned, "stage": "committed", "noop": True}
    root.mkdir(parents=True, exist_ok=True)
    journal = {**planned, "stage": "applying", "steps": list(journal.get("steps") or [])}
    _save_journal(root, journal)
    config_path = root / "factory.json"
    existing = {}
    if config_path.is_file():
        existing = json.loads(config_path.read_text(encoding="utf-8"))
    existing["deployment"] = {
        "profile": document["profile"],
        "root_id": document["root_id"],
        "manifest_digest": planned["manifest_digest"],
        "plan_digest": plan_digest,
        "version": 1,
    }
    if "operations" not in existing:
        existing["operations"] = {"mailbox": "operations", "rollout_minimums": _DEFAULT_MINIMUMS}
    config_path.write_text(canonical_json(existing), encoding="utf-8")
    journal["stage"] = "committed"
    journal["steps"] = [*journal["steps"], "write-deployment"]
    journal["after_state_digest"] = _state_digest(root)
    _save_journal(root, journal)
    _save_db_journal(root, journal)
    return {**planned, "stage": "committed", "noop": False}


def diagnose(root: Path, *, profile: str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """只读诊断。不创建 root 或数据库。"""
    root = Path(root)
    before = _snapshot_paths(root)
    document = (
        _manifest(manifest) if manifest is not None else {"profile": profile, "qualification_receipts": []}
    )
    facts = {
        "docker_ok": docker_available(),
        "qualification_receipts": list(document.get("qualification_receipts") or []),
        "governance": _has_governance(root),
        "profile": profile,
    }
    blockers = deployment_blockers(profile, facts)
    status = "blocked" if blockers else ("degraded" if profile == "local-test" else "ready")
    report = {
        "profile": profile,
        "status": status,
        "facts": facts,
        "blockers": blockers,
        "limitations": ["local-test 没有隔离，也不能代替 live 资格"] if profile == "local-test" else [],
        "next": blockers[0]["next"] if blockers else "factory init --manifest FILE --plan",
    }
    if _snapshot_paths(root) != before:
        raise FactoryError("DEPLOY_MANIFEST_INVALID", "doctor 改动了文件系统", field="doctor")
    return report


def deployment_blockers(profile: str, facts: dict[str, Any]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    if profile == "resident" and not facts.get("docker_ok"):
        found.append({"code": "DOCKER_UNAVAILABLE", "reason": "没有 Docker", "next": "安装并启动 Docker"})
    if profile == "resident" and not facts.get("qualification_receipts"):
        found.append(
            {
                "code": "RUNTIME_UNQUALIFIED",
                "reason": "没有 live 资格回执",
                "next": "factory runtime qualify --profile live",
            }
        )
    if profile == "resident" and not facts.get("governance"):
        found.append(
            {
                "code": "DEPLOY_MANIFEST_INVALID",
                "reason": "没有治理配置",
                "next": "在 manifest 中固定 Registry",
            }
        )
    return found


def docker_available() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except OSError:
        return False
    return proc.returncode == 0


def effective_rollout(policy: dict[str, Any] | None, configured: dict[str, Any] | None) -> dict[str, Any]:
    """取治理策略和 operations.rollout_minimums 中更严的值。"""
    result = {}
    for tier, default in _DEFAULT_MINIMUMS.items():
        left = (policy or {}).get(tier) or default
        right = (configured or {}).get(tier) or default
        result[tier] = {
            "shadow_runs": max(int(left.get("shadow_runs", 0)), int(right.get("shadow_runs", 0))),
            "observe_s": max(int(left.get("observe_s", 0)), int(right.get("observe_s", 0))),
        }
    return result


def rollout_evidence(actual_runs: int, actual_observe_s: int, required: dict[str, int]) -> dict[str, Any]:
    runs_ok = actual_runs >= int(required["shadow_runs"])
    observe_ok = actual_observe_s >= int(required["observe_s"])
    if int(required["shadow_runs"]) == 0 and int(required["observe_s"]) == 0:
        return {"passed": True, "observed": False, "not_performed": True, "reason": "最低要求为零"}
    if not runs_ok or not observe_ok:
        raise FactoryError(
            "ROLLOUT_EVIDENCE_REQUIRED",
            "低于生效最低样本或观察",
            runs=actual_runs,
            observe_s=actual_observe_s,
        )
    return {"passed": True, "observed": True, "not_performed": False, "reason": ""}


def _plan_body(root: Path, document: dict[str, Any], state: str) -> dict[str, Any]:
    manifest_digest = hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
    plan_digest = hashlib.sha256(
        canonical_json({"manifest": manifest_digest, "state": state}).encode("utf-8")
    ).hexdigest()
    profile = str(document.get("profile") or "local-test")
    return {
        "schema_version": "mao-deployment-plan-1",
        "profile": profile,
        "root_id": document.get("root_id"),
        "manifest_digest": manifest_digest,
        "initial_state_digest": state,
        "plan_digest": plan_digest,
        "status": diagnose(root, profile=profile, manifest=document)["status"],
        "preserves": ["registry", "secrets", "cas", "factory.db"],
    }


def _manifest(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FactoryError("DEPLOY_MANIFEST_INVALID", "manifest 必须是对象", field="manifest")
    required = ("schema_version", "profile", "root_id")
    if any(key not in value for key in required):
        raise FactoryError("DEPLOY_MANIFEST_INVALID", "manifest 缺少字段", field="manifest")
    if value.get("schema_version") != "mao-deployment-1":
        raise FactoryError("DEPLOY_MANIFEST_INVALID", "manifest 版本不对", field="schema_version")
    if value.get("profile") not in ("resident", "local-test"):
        raise FactoryError("DEPLOY_MANIFEST_INVALID", "profile 不合法", field="profile")
    return dict(value)


def _state_digest(root: Path) -> str:
    if not root.exists():
        return "empty"
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and "qualification" not in path.parts and "deployment" not in path.parts:
            rows.append(f"{path.relative_to(root).as_posix()}:{path.stat().st_size}")
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _snapshot_paths(root: Path) -> tuple[str, ...]:
    if not root.exists():
        return ()
    return tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*")))


def _serve_busy(root: Path) -> bool:
    db = root / "factory.db"
    if not db.is_file():
        return False
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT owner FROM leases WHERE resource = 'serve'").fetchone()
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()
    return row is not None and bool(row[0])


def _journal_path(root: Path, plan_digest: str) -> Path:
    return root / "deployment" / f"{plan_digest}.json"


def _load_journal(root: Path, plan_digest: str) -> dict[str, Any]:
    path = _journal_path(root, plan_digest)
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_journal(root: Path, journal: dict[str, Any]) -> None:
    path = _journal_path(root, str(journal["plan_digest"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(journal), encoding="utf-8")


def _save_db_journal(root: Path, journal: dict[str, Any]) -> None:
    db = root / "factory.db"
    if not db.is_file():
        return
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """INSERT INTO deployment_operations (
                   plan_digest, manifest_digest, initial_state_digest, owner, lease_generation,
                   stage, journal, created_at, updated_at
               ) VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?)
               ON CONFLICT(plan_digest) DO UPDATE SET stage = excluded.stage, journal = excluded.journal,
                   updated_at = excluded.updated_at""",
            (
                journal["plan_digest"],
                journal["manifest_digest"],
                journal["initial_state_digest"],
                journal["stage"],
                canonical_json(journal),
                journal.get("created_at", ""),
                journal.get("updated_at", ""),
            ),
        )
        conn.commit()
    except sqlite3.OperationalError:
        return
    finally:
        conn.close()


def _has_governance(root: Path) -> bool:
    path = root / "factory.json"
    if not path.is_file():
        return False
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return isinstance(body, dict) and isinstance(body.get("governance"), dict)
