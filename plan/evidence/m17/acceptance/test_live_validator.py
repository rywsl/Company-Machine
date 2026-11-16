"""Synthetic-only tests of evidence validation; never evidence of real autonomy."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "verify_live.py"
DIGEST = "a" * 64
START = "2026-11-16T00:00:00Z"
END = "2026-11-16T00:05:00Z"


@pytest.fixture
def synthetic(tmp_path: Path):
    """Build a fake, internally consistent report and isolated SQLite snapshot."""
    root = tmp_path / "factory"
    root.mkdir()
    conn = sqlite3.connect(root / "factory.db")
    conn.executescript("""
        CREATE TABLE objective_cycles(id TEXT PRIMARY KEY, number INTEGER, state TEXT, watermark INTEGER);
        CREATE TABLE runs(id TEXT PRIMARY KEY);
        CREATE TABLE jobs(id TEXT PRIMARY KEY);
        CREATE TABLE agent_turns(id TEXT PRIMARY KEY);
        CREATE TABLE events(id TEXT PRIMARY KEY);
        CREATE TABLE receipts(id TEXT PRIMARY KEY, job_id TEXT, run_id TEXT);
        CREATE TABLE run_nodes(run_id TEXT, job_id TEXT);
    """)
    artifact = tmp_path / "evidence.txt"
    artifact.write_text("sanitized synthetic fixture only\n", encoding="utf-8")
    sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    evidence = []

    def add(kind: str, source: str) -> str:
        eid = f"proof-{kind}-{source}"
        evidence.append(
            {
                "evidence_id": eid,
                "kind": kind,
                "source_id": source,
                "sanitized_location": artifact.name,
                "sha256": sha,
                "captured_at": START,
            }
        )
        return eid

    for kind in ("docker", "cli", "model", "audit"):
        add(kind, f"synthetic-{kind}")
    cycles = []
    for i, state in enumerate(("idle", "completed", "failed", "completed"), 1):
        cid, turn, event = f"cycle-{i}", f"turn-{i}", f"event-{i}"
        conn.execute("INSERT INTO objective_cycles VALUES (?, ?, ?, ?)", (cid, i, state, i * 10))
        conn.execute("INSERT INTO agent_turns VALUES (?)", (turn,))
        conn.execute("INSERT INTO events VALUES (?)", (event,))
        add("turn", turn)
        add("event", event)
        works = state != "idle"
        job, run = f"job-{i}", f"run-{i}"
        receipt = f"receipt-{i}"
        if works:
            conn.execute("INSERT INTO jobs VALUES (?)", (job,))
            conn.execute("INSERT INTO runs VALUES (?)", (run,))
            conn.execute("INSERT INTO run_nodes VALUES (?, ?)", (run, job))
            add("job", job)
            add("run", run)
        has_receipt = i == 2
        if has_receipt:
            conn.execute("INSERT INTO receipts VALUES (?, ?, ?)", (receipt, job, run))
            add("receipt", receipt)
        cycles.append(
            {
                "cycle_id": cid,
                "number": i,
                "state": state,
                "watermark": i * 10,
                "plan_asset_id": f"plan-{i}",
                "observation_asset_ids": [f"observation-{i}"],
                "candidate_ids": [f"candidate-{i}"] if works else [],
                "selected_ids": [f"candidate-{i}"] if works else [],
                "task_ids": [f"task-{i}"] if works else [],
                "turn_ids": [turn],
                "event_ids": [event],
                "run_ids": [run] if works else [],
                "job_ids": [job] if works else [],
                "output_bean_ids": [f"bean-{i}"] if state == "completed" else [],
                "receipt_ids": [receipt] if has_receipt else [],
                "receipt_not_applicable_reason": "" if has_receipt else "no sink in this synthetic case",
                "domain_case_evidence": {"quality": {"kind": "job", "id": job}}
                if state == "completed"
                else {},
                "costs": {"spent_usd": 0.1, "reserved_usd": 0, "unknown_cost": False},
            }
        )
    conn.commit()
    conn.close()
    operations = []
    for i, state in ((2, "completed"), (3, "outcome_unknown")):
        operations.append(
            {
                "job_id": f"job-{i}",
                "operation_key": "recipe",
                "provider_digest": DIGEST,
                "request_digest": DIGEST,
                "idempotency_key_digest": DIGEST,
                "state": state,
                "task_id_redacted_or_digest": DIGEST if state == "completed" else None,
                "submit_count": 1,
                "poll_count": 1,
                "fetch_count": 1,
                "output_digests": [DIGEST] if state == "completed" else [],
                "error_code": None,
            }
        )
    ref = "proof-audit-synthetic-audit"
    case_source = yaml.safe_load((SCRIPT.parent / "cases.yaml").read_text(encoding="utf-8"))["cases"]
    cases = [
        {
            "case_id": case["id"],
            "status": "passed",
            "stimuli_refs": [ref],
            "observation_refs": [ref],
            "assertions": [
                {"id": f"A{number:02}", "passed": True, "evidence_refs": [ref]}
                for number in range(1, len(case["assert"]) + 1)
            ],
            "failed_reasons": [],
            "started_at": START,
            "ended_at": END,
        }
        for case in case_source
    ]
    report = {
        "schema_version": "mao-m17-live-1",
        "status": "passed",
        "started_at": START,
        "ended_at": END,
        "environment": {
            "os": "linux",
            "architecture": "x86_64",
            "docker_engine_version": "1.0",
            "image_digests": ["example/image@sha256:" + DIGEST],
            "cli_name": "synthetic-cli",
            "cli_version": "1.0",
            "runtime_ref": "operator@1",
            "model_id": "synthetic-model",
            "upstream_identity_redacted": "synthetic-upstream",
            "real_docker": True,
            "real_cli": True,
            "real_model_upstream": True,
        },
        "baseline": {
            "platform_commit": "b" * 40,
            "platform_source_digest": DIGEST,
            "dirty_paths": [],
            "M16_gate_evidence": [ref],
            "M17_task_evidence": [ref],
            "active_epoch": "epoch-1",
            "registry_digest": DIGEST,
        },
        "authorization": {
            "objective_asset_id": "objective-1",
            "objective_version_digest": DIGEST,
            "grant_digest": DIGEST,
            "contract_id": "contract-1",
            "contract_digest": DIGEST,
            "source_scope_digest": DIGEST,
            "total_usd": 10,
            "daily_usd": 5,
            "expires_at": "2026-12-01T00:00:00Z",
        },
        "observation_window": {
            "actual_duration_s": 300,
            "clock_mode": "real",
            "cycle_count": 4,
            "work_cycle_count": 3,
            "distinct_work_count": 3,
            "idle_cycle_count": 1,
            "failed_cycle_count": 1,
        },
        "intervention": {
            "unplanned_count": 0,
            "planned_actions": [],
            "unplanned_actions": [],
            "audit_refs": [ref],
        },
        "cycles": cycles,
        "external_operations": operations,
        "budgets": {
            "scopes": ["objective:1"],
            "cap_usd": 5,
            "spent_usd": 0.3,
            "reserved_usd": 0,
            "remaining_usd": 4.7,
            "unknown_cost": False,
            "as_of_seq": 3,
            "model_usage_refs": ["usage-1"],
            "budget_event_ids": ["event-2"],
        },
        "governance": {
            "changeset_ids": ["cs-1"],
            "risk_tiers": ["T2"],
            "validation_refs": [ref],
            "shadow_refs": [ref],
            "canary_refs": [ref],
            "signoff_evidence": [],
            "safe_mode_events": [],
            "active_epoch_before": "e-1",
            "active_epoch_after": "e-2",
            "outbox_refs": [],
        },
        "cases": cases,
        "evidence_index": evidence,
        "exclusions": [],
        "blocked_reasons": [],
        "deterministic_suite": {"passed": 9, "failed": 0, "errors": 0, "skipped": 0},
    }
    return root, tmp_path / "report.json", report, artifact


def invoke(root: Path, path: Path, report: dict) -> tuple[int, dict]:
    path.write_text(json.dumps(report), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--report", str(path), "--factory-root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.stderr == ""
    return proc.returncode, json.loads(proc.stdout)


def issue_codes(result: dict) -> set[str]:
    return {issue["code"] for issue in result["issues"]}


def test_synthetic_valid_report_checks_integrity_only(synthetic) -> None:
    root, path, report, artifact = synthetic
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    code, result = invoke(root, path, report)
    assert (code, result["ok"], result["status"]) == (0, True, "passed"), result
    assert "model quality and provenance are not established" in result["scope"]
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    assert artifact.is_file()


@pytest.mark.parametrize(
    "kind", ["missing", "wrong_hash", "escape", "absolute", "symlink", "directory_symlink"]
)
def test_rejects_unverifiable_or_unsafe_evidence(synthetic, kind: str) -> None:
    root, path, report, artifact = synthetic
    report = copy.deepcopy(report)
    entry = report["evidence_index"][0]
    if kind == "missing":
        entry["sanitized_location"] = "missing.txt"
    elif kind == "wrong_hash":
        entry["sha256"] = "0" * 64
    elif kind == "escape":
        entry["sanitized_location"] = "../outside.txt"
    elif kind == "absolute":
        entry["sanitized_location"] = str(artifact.resolve())
    elif kind == "symlink":
        (path.parent / "link.txt").symlink_to(artifact)
        entry["sanitized_location"] = "link.txt"
    else:
        (path.parent / "linked").symlink_to(path.parent, target_is_directory=True)
        entry["sanitized_location"] = "linked/evidence.txt"
    code, result = invoke(root, path, report)
    assert code == 1 and not result["ok"]
    assert issue_codes(result) & {"UNSAFE_OR_MISSING_EVIDENCE_FILE", "EVIDENCE_HASH_MISMATCH"}


def test_missing_database_id_cannot_pass(synthetic) -> None:
    root, path, report, _artifact = synthetic
    conn = sqlite3.connect(root / "factory.db")
    conn.execute("DELETE FROM jobs WHERE id = ?", ("job-2",))
    conn.commit()
    conn.close()
    code, result = invoke(root, path, report)
    assert code == 1 and "DATABASE_ID_NOT_FOUND" in issue_codes(result)


def test_report_missing_cases_or_fabricated_intervention_count_fails(synthetic) -> None:
    root, path, report, _artifact = synthetic
    report["cases"].pop()
    report["intervention"]["unplanned_count"] = 1
    report["intervention"]["unplanned_actions"] = [{"type": "repair"}]
    code, result = invoke(root, path, report)
    assert code == 1
    assert {"CASE_SET_MISMATCH", "UNPLANNED_INTERVENTION"} <= issue_codes(result)


def test_blocked_report_exits_one_and_cannot_be_live_pass(synthetic) -> None:
    root, path, report, _artifact = synthetic
    report["status"] = "blocked"
    report["blocked_reasons"] = ["no real Docker"]
    code, result = invoke(root, path, report)
    assert code == 1 and result["status"] == "blocked" and not result["ok"]
    assert "REPORT_NOT_PASSED" in issue_codes(result)


def test_real_components_and_valid_schema_required(synthetic) -> None:
    root, path, report, _artifact = synthetic
    report["environment"]["real_cli"] = False
    report["environment"]["model_id"] = ""
    report["deterministic_suite"]["skipped"] = 1
    report["authorization"]["contract_digest"] = "bad-hash"
    code, result = invoke(root, path, report)
    assert code == 1
    assert {
        "REAL_COMPONENT_REQUIRED",
        "EXPECTED_NONEMPTY_STRING",
        "SUITE_NOT_CLEAN",
        "INVALID_SHA256",
    } <= issue_codes(result)


def test_missing_audit_and_broken_evidence_refs_fail(synthetic) -> None:
    root, path, report, _artifact = synthetic
    report["intervention"]["audit_refs"] = []
    report["cases"][0]["assertions"][0]["evidence_refs"] = ["absent-proof"]
    code, result = invoke(root, path, report)
    assert code == 1 and {"EMPTY_ARRAY", "UNKNOWN_EVIDENCE_ID"} <= issue_codes(result)


def test_nonempty_wal_requires_snapshot(synthetic) -> None:
    root, path, report, _artifact = synthetic
    (root / "factory.db-wal").write_bytes(b"active wal")
    code, result = invoke(root, path, report)
    assert code == 1 and "NONEMPTY_WAL_REQUIRES_CONSISTENT_SNAPSHOT" in issue_codes(result)


def test_unredacted_evidence_rejected_even_with_matching_digest(synthetic) -> None:
    root, path, report, artifact = synthetic
    artifact.write_text("Bearer " + "x" * 24, encoding="utf-8")
    sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    for entry in report["evidence_index"]:
        entry["sha256"] = sha
    code, result = invoke(root, path, report)
    assert code == 1 and "POSSIBLE_SECRET" in issue_codes(result)


def test_nonexistent_factory_database_cannot_pass(synthetic) -> None:
    root, path, report, _artifact = synthetic
    (root / "factory.db").unlink()
    code, result = invoke(root, path, report)
    assert code == 1 and "DATABASE_MISSING_OR_SYMLINK" in issue_codes(result)


def test_invalid_utc_timestamp_is_rejected(synthetic) -> None:
    root, path, report, _artifact = synthetic
    report["started_at"] = "2026-11-16Z"
    code, result = invoke(root, path, report)
    assert code == 1 and "INVALID_UTC_TIMESTAMP" in issue_codes(result)


def test_two_completed_different_works_are_required(synthetic) -> None:
    root, path, report, _artifact = synthetic
    report["cycles"][3]["state"] = "failed"
    report["cycles"][3]["output_bean_ids"] = []
    report["cycles"][3]["domain_case_evidence"] = {}
    report["observation_window"]["failed_cycle_count"] = 2
    conn = sqlite3.connect(root / "factory.db")
    conn.execute("UPDATE objective_cycles SET state = ? WHERE id = ?", ("failed", "cycle-4"))
    conn.commit()
    conn.close()
    code, result = invoke(root, path, report)
    assert code == 1 and "INSUFFICIENT_CYCLE_EVIDENCE" in issue_codes(result)


def test_duplicate_json_keys_are_rejected(synthetic) -> None:
    root, path, report, _artifact = synthetic
    body = json.dumps(report)
    path.write_text(body[:-1] + ', "status": "passed"}', encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--report", str(path), "--factory-root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    result = json.loads(proc.stdout)
    assert proc.returncode == 1 and "REPORT_READ_OR_JSON_FAILED" in issue_codes(result)


def test_every_frozen_assertion_is_required(synthetic) -> None:
    root, path, report, _artifact = synthetic
    report["cases"][0]["assertions"].pop()
    code, result = invoke(root, path, report)
    assert code == 1 and "ASSERTION_SET_MISMATCH" in issue_codes(result)


def test_missing_cli_arguments_are_json_and_exit_one() -> None:
    proc = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False)
    result = json.loads(proc.stdout)
    assert proc.returncode == 1 and "INVALID_ARGUMENTS" in issue_codes(result)


@pytest.mark.parametrize(
    ("table", "item"),
    [("runs", "run-2"), ("agent_turns", "turn-2"), ("events", "event-2"), ("receipts", "receipt-2")],
)
def test_all_declared_database_record_kinds_must_exist(synthetic, table: str, item: str) -> None:
    root, path, report, _artifact = synthetic
    conn = sqlite3.connect(root / "factory.db")
    conn.execute(f"DELETE FROM {table} WHERE id = ?", (item,))
    conn.commit()
    conn.close()
    code, result = invoke(root, path, report)
    assert code == 1 and "DATABASE_ID_NOT_FOUND" in issue_codes(result)


def test_database_symlink_is_not_a_snapshot(synthetic) -> None:
    root, path, report, _artifact = synthetic
    db = root / "factory.db"
    moved = root / "real-snapshot.db"
    db.rename(moved)
    db.symlink_to(moved)
    code, result = invoke(root, path, report)
    assert code == 1 and "DATABASE_MISSING_OR_SYMLINK" in issue_codes(result)
