#!/usr/bin/env python3
"""Validate M17 live evidence integrity without starting or modifying a factory.

Success verifies declared evidence completeness and database references only. It
cannot establish that a model made a valuable decision or that a report is genuine.
For a live database with an active WAL, supply a consistent, checkpointed snapshot
as --factory-root; immutable SQLite reads deliberately do not replay a WAL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

HASH = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
IMAGE_REF = re.compile(r"^[a-z0-9][a-z0-9._/:@-]*@sha256:[0-9a-f]{64}$")
UTC_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$")
SENSITIVE = re.compile(
    rb"(?i)(?:\bsk-[a-z0-9_-]{16,}|\bbearer\s+[a-z0-9._~+/=-]{16,}|"
    rb"(?:api[_-]?key|access[_-]?token|secret[_-]?key)\s*[=:]\s*[\"']?[a-z0-9._~+/=-]{20,})"
)
TABLES = {
    "cycle": "objective_cycles",
    "run": "runs",
    "job": "jobs",
    "turn": "agent_turns",
    "event": "events",
    "receipt": "receipts",
}


class Check:
    def __init__(self) -> None:
        self.issues: list[dict[str, str]] = []

    def fail(self, path: str, code: str) -> None:
        self.issues.append({"path": path, "code": code})

    def obj(self, value: Any, path: str, fields: tuple[str, ...]) -> dict[str, Any]:
        if not isinstance(value, dict):
            self.fail(path, "EXPECTED_OBJECT")
            return {}
        for field in fields:
            if field not in value:
                self.fail(f"{path}.{field}", "MISSING")
        return value

    def text(self, value: Any, path: str) -> str:
        if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 4096:
            self.fail(path, "EXPECTED_NONEMPTY_STRING")
            return ""
        return value

    def integer(self, value: Any, path: str, minimum: int = 0) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            self.fail(path, "EXPECTED_NONNEGATIVE_INTEGER")
            return -1
        return value

    def number(self, value: Any, path: str, *, nonnegative: bool = True) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.fail(path, "EXPECTED_FINITE_NUMBER")
            return float("nan")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            self.fail(path, "EXPECTED_FINITE_NUMBER")
            return float("nan")
        if nonnegative and value < 0:
            self.fail(path, "EXPECTED_NONNEGATIVE_NUMBER")
        return float(value)

    def boolean(self, value: Any, path: str) -> bool:
        if not isinstance(value, bool):
            self.fail(path, "EXPECTED_BOOLEAN")
            return False
        return value

    def array(self, value: Any, path: str, *, nonempty: bool = False) -> list[Any]:
        if not isinstance(value, list):
            self.fail(path, "EXPECTED_ARRAY")
            return []
        if nonempty and not value:
            self.fail(path, "EMPTY_ARRAY")
        return value

    def strings(self, value: Any, path: str, *, nonempty: bool = False) -> list[str]:
        items = self.array(value, path, nonempty=nonempty)
        result = [self.text(item, f"{path}[{i}]") for i, item in enumerate(items)]
        if len(set(result)) != len(result):
            self.fail(path, "DUPLICATE_VALUE")
        return [item for item in result if item]

    def digest(self, value: Any, path: str) -> str:
        text = self.text(value, path)
        if text and not HASH.fullmatch(text):
            self.fail(path, "INVALID_SHA256")
            return ""
        return text.removeprefix("sha256:")

    def instant(self, value: Any, path: str) -> datetime | None:
        text = self.text(value, path)
        if not text:
            return None
        try:
            if not UTC_STAMP.fullmatch(text):
                raise ValueError("not UTC timestamp")
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.utcoffset() != timedelta(0):
                raise ValueError("not UTC")
            return parsed
        except ValueError:
            self.fail(path, "INVALID_UTC_TIMESTAMP")
            return None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _windows_evidence_file(base: Path, parts: tuple[str, ...]) -> bytes:
    """Pin the entire path with handles that deny write/delete sharing.

    OPEN_REPARSE_POINT inspects each component itself; holding every ancestor
    prevents a checked directory from being renamed or changed into a junction
    before its child is opened. Never fall back to a check-then-read path.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("created", wintypes.FILETIME),
            ("accessed", wintypes.FILETIME),
            ("written", wintypes.FILETIME),
            ("volume", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create.restype = wintypes.HANDLE
    information = kernel.GetFileInformationByHandle
    information.argtypes = (wintypes.HANDLE, ctypes.POINTER(FileInfo))
    information.restype = wintypes.BOOL
    file_type = kernel.GetFileType
    file_type.argtypes = (wintypes.HANDLE,)
    file_type.restype = wintypes.DWORD
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    invalid = ctypes.c_void_p(-1).value
    target = base.absolute().joinpath(*parts)
    cursor = Path(target.anchor)
    components = [cursor]
    for part in target.parts[1:]:
        cursor = cursor / part
        components.append(cursor)
    handles: list[int] = []
    try:
        for index, component in enumerate(components):
            # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, BACKUP_SEMANTICS |
            # OPEN_REPARSE_POINT. No write/delete sharing, including ancestors.
            native = str(component)
            native = "\\\\?\\UNC\\" + native[2:] if native.startswith("\\\\") else "\\\\?\\" + native
            handle = create(native, 0x80000000, 0x1, None, 3, 0x02200000, None)
            if handle == invalid:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            info = FileInfo()
            if not information(handle, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            directory = index < len(components) - 1
            if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory or file_type(handle) != 1:
                raise ValueError("reparse point or unexpected file type")
        descriptor = msvcrt.open_osfhandle(handles[-1], os.O_RDONLY | os.O_BINARY)
        handles.pop()  # Ownership moves to the CRT descriptor.
        with os.fdopen(descriptor, "rb") as stream:
            return stream.read()
    finally:
        for handle in reversed(handles):
            close(handle)


def _evidence_file(base: Path, value: str) -> bytes:
    """Open only descendants using no-follow at every directory component."""
    if "\\" in value or "\x00" in value or ":" in value:
        raise ValueError("invalid path")
    rel = PurePosixPath(value)
    if rel.is_absolute() or not rel.parts or any(part in {".", ".."} for part in rel.parts):
        raise ValueError("invalid path")
    if os.name == "nt":
        return _windows_evidence_file(base, rel.parts)
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("no safe no-follow support")
    flags_dir = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    opened: list[int] = []
    try:
        fd = os.open(base, flags_dir)
        opened.append(fd)
        for part in rel.parts[:-1]:
            fd = os.open(part, flags_dir, dir_fd=fd)
            opened.append(fd)
        file_fd = os.open(rel.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        opened.append(file_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ValueError("not a regular file")
        with os.fdopen(os.dup(file_fd), "rb") as handle:
            return handle.read()
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _read_database(root: Path, c: Check) -> sqlite3.Connection | None:
    db = root / "factory.db"
    wal = root / "factory.db-wal"
    try:
        if db.is_symlink() or not db.is_file():
            c.fail("factory.db", "DATABASE_MISSING_OR_SYMLINK")
            return None
        if wal.is_symlink() or (wal.exists() and wal.stat().st_size > 0):
            c.fail("factory.db", "NONEMPTY_WAL_REQUIRES_CONSISTENT_SNAPSHOT")
            return None
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        for table in (*TABLES.values(), "run_nodes"):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            required = {"id"} if table != "run_nodes" else {"run_id", "job_id"}
            if table == "objective_cycles":
                required |= {"number", "state", "watermark"}
            if table == "receipts":
                required |= {"job_id", "run_id"}
            if not required <= columns:
                c.fail(f"factory.db.{table}", "TABLE_OR_COLUMNS_MISSING")
        return conn
    except (OSError, sqlite3.Error, ValueError):
        c.fail("factory.db", "DATABASE_READ_FAILED")
        return None


def _exists(conn: sqlite3.Connection | None, table: str, item: str, path: str, c: Check) -> None:
    if conn is None or not item:
        return
    try:
        if conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (item,)).fetchone() is None:
            c.fail(path, "DATABASE_ID_NOT_FOUND")
    except sqlite3.Error:
        c.fail(path, "DATABASE_QUERY_FAILED")


def validate(report_path: Path, factory_root: Path) -> dict[str, Any]:
    c = Check()
    report: dict[str, Any] = {}
    raw = b""
    try:
        if report_path.is_symlink() or not report_path.is_file():
            raise ValueError("not regular report")
        raw = report_path.read_bytes()
        if SENSITIVE.search(raw):
            c.fail("report", "POSSIBLE_SECRET")
        report = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        c.fail("report", "REPORT_READ_OR_JSON_FAILED")
    contract_path = Path(__file__).with_name("cases.yaml")
    try:
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        case_ids = [case["id"] for case in contract["cases"]]
        case_assertions = {case["id"]: len(case["assert"]) for case in contract["cases"]}
        if any(not isinstance(case["assert"], list) or not case["assert"] for case in contract["cases"]):
            raise ValueError("invalid assertions")
        report_fields = tuple(contract["live_report_contract"]["required_fields"])
        if case_ids != [f"C{i:02}" for i in range(1, 11)]:
            raise ValueError("unexpected cases")
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
        c.fail("cases.yaml", "CONTRACT_READ_FAILED")
        case_ids = []
        case_assertions = {}
        report_fields = ()
    report = c.obj(report, "report", report_fields + ("deterministic_suite",))
    if report.get("schema_version") != "mao-m17-live-1":
        c.fail("report.schema_version", "UNSUPPORTED_SCHEMA")
    source_status = report.get("status")
    if source_status != "passed":
        c.fail("report.status", "REPORT_NOT_PASSED")
    started = c.instant(report.get("started_at"), "report.started_at")
    ended = c.instant(report.get("ended_at"), "report.ended_at")
    if started and ended and ended <= started:
        c.fail("report.ended_at", "INVALID_WINDOW")
    if c.strings(report.get("blocked_reasons"), "report.blocked_reasons"):
        c.fail("report.blocked_reasons", "REPORT_HAS_BLOCKERS")

    env_fields = (
        "os",
        "architecture",
        "docker_engine_version",
        "image_digests",
        "cli_name",
        "cli_version",
        "runtime_ref",
        "model_id",
        "upstream_identity_redacted",
        "real_docker",
        "real_cli",
        "real_model_upstream",
    )
    env = c.obj(report.get("environment"), "report.environment", env_fields)
    for key in (
        "os",
        "architecture",
        "docker_engine_version",
        "cli_name",
        "cli_version",
        "runtime_ref",
        "model_id",
        "upstream_identity_redacted",
    ):
        c.text(env.get(key), f"report.environment.{key}")
    for key in ("real_docker", "real_cli", "real_model_upstream"):
        if env.get(key) is not True:
            c.fail(f"report.environment.{key}", "REAL_COMPONENT_REQUIRED")
    for i, item in enumerate(
        c.strings(env.get("image_digests"), "report.environment.image_digests", nonempty=True)
    ):
        if not (HASH.fullmatch(item) or IMAGE_REF.fullmatch(item)):
            c.fail(f"report.environment.image_digests[{i}]", "INVALID_IMAGE_DIGEST")
    base_fields = (
        "platform_commit",
        "platform_source_digest",
        "dirty_paths",
        "M16_gate_evidence",
        "M17_task_evidence",
        "active_epoch",
        "registry_digest",
    )
    base = c.obj(report.get("baseline"), "report.baseline", base_fields)
    commit = c.text(base.get("platform_commit"), "report.baseline.platform_commit")
    if commit and not COMMIT.fullmatch(commit):
        c.fail("report.baseline.platform_commit", "INVALID_COMMIT")
    for key in ("platform_source_digest", "registry_digest"):
        c.digest(base.get(key), f"report.baseline.{key}")
    c.text(base.get("active_epoch"), "report.baseline.active_epoch")
    dirty = c.strings(base.get("dirty_paths"), "report.baseline.dirty_paths")
    if any(path.startswith(("src/", "spec/", "plan/", "tests/")) for path in dirty):
        c.fail("report.baseline.dirty_paths", "PLATFORM_OR_STANDARD_DIRTY")
    baseline_refs: list[str] = []
    for key in ("M16_gate_evidence", "M17_task_evidence"):
        baseline_refs += c.strings(base.get(key), f"report.baseline.{key}", nonempty=True)
    auth_fields = (
        "objective_asset_id",
        "objective_version_digest",
        "grant_digest",
        "contract_id",
        "contract_digest",
        "source_scope_digest",
        "total_usd",
        "daily_usd",
        "expires_at",
    )
    auth = c.obj(report.get("authorization"), "report.authorization", auth_fields)
    for key in ("objective_asset_id", "contract_id"):
        c.text(auth.get(key), f"report.authorization.{key}")
    for key in ("objective_version_digest", "grant_digest", "contract_digest", "source_scope_digest"):
        c.digest(auth.get(key), f"report.authorization.{key}")
    for key in ("total_usd", "daily_usd"):
        if c.number(auth.get(key), f"report.authorization.{key}") <= 0:
            c.fail(f"report.authorization.{key}", "MUST_BE_POSITIVE")
    expiry = c.instant(auth.get("expires_at"), "report.authorization.expires_at")
    if started and expiry and expiry <= started:
        c.fail("report.authorization.expires_at", "EXPIRED_AT_START")

    suite = c.obj(
        report.get("deterministic_suite"),
        "report.deterministic_suite",
        ("passed", "failed", "errors", "skipped"),
    )
    if c.integer(suite.get("passed"), "report.deterministic_suite.passed") < 1:
        c.fail("report.deterministic_suite.passed", "NO_PASSING_TESTS")
    for key in ("failed", "errors", "skipped"):
        if c.integer(suite.get(key), f"report.deterministic_suite.{key}") != 0:
            c.fail(f"report.deterministic_suite.{key}", "SUITE_NOT_CLEAN")
    intervention = c.obj(
        report.get("intervention"),
        "report.intervention",
        ("unplanned_count", "planned_actions", "unplanned_actions", "audit_refs"),
    )
    if c.integer(intervention.get("unplanned_count"), "report.intervention.unplanned_count") != 0:
        c.fail("report.intervention.unplanned_count", "UNPLANNED_INTERVENTION")
    if c.array(intervention.get("unplanned_actions"), "report.intervention.unplanned_actions"):
        c.fail("report.intervention.unplanned_actions", "UNPLANNED_INTERVENTION")
    for i, action in enumerate(
        c.array(intervention.get("planned_actions"), "report.intervention.planned_actions")
    ):
        path = f"report.intervention.planned_actions[{i}]"
        action = c.obj(action, path, ("type", "at", "actor", "reason"))
        for key in ("type", "actor", "reason"):
            c.text(action.get(key), f"{path}.{key}")
        c.instant(action.get("at"), f"{path}.at")
    audit_refs = c.strings(intervention.get("audit_refs"), "report.intervention.audit_refs", nonempty=True)

    conn = _read_database(factory_root, c)
    evidence: dict[str, tuple[str, str]] = {}
    indexed: set[tuple[str, str]] = set()
    evidence_items = c.array(report.get("evidence_index"), "report.evidence_index", nonempty=True)
    for i, entry in enumerate(evidence_items):
        path = f"report.evidence_index[{i}]"
        entry = c.obj(
            entry, path, ("evidence_id", "kind", "source_id", "sanitized_location", "sha256", "captured_at")
        )
        eid = c.text(entry.get("evidence_id"), f"{path}.evidence_id")
        kind = c.text(entry.get("kind"), f"{path}.kind")
        source = c.text(entry.get("source_id"), f"{path}.source_id")
        location = c.text(entry.get("sanitized_location"), f"{path}.sanitized_location")
        digest = c.digest(entry.get("sha256"), f"{path}.sha256")
        c.instant(entry.get("captured_at"), f"{path}.captured_at")
        if eid in evidence:
            c.fail(f"{path}.evidence_id", "DUPLICATE_EVIDENCE_ID")
        evidence[eid] = (kind, source)
        indexed.add((kind, source))
        if kind in TABLES:
            _exists(conn, TABLES[kind], source, f"{path}.source_id", c)
        if location:
            try:
                content = _evidence_file(report_path.parent, location)
                if digest and hashlib.sha256(content).hexdigest() != digest:
                    c.fail(f"{path}.sha256", "EVIDENCE_HASH_MISMATCH")
                if SENSITIVE.search(content):
                    c.fail(f"{path}.sanitized_location", "POSSIBLE_SECRET")
            except (OSError, ValueError):
                c.fail(f"{path}.sanitized_location", "UNSAFE_OR_MISSING_EVIDENCE_FILE")
    for kind in ("docker", "cli", "model", "audit"):
        if not any(entry[0] == kind for entry in evidence.values()):
            c.fail("report.evidence_index", f"MISSING_{kind.upper()}_EVIDENCE")
    for ref in baseline_refs + audit_refs:
        if ref not in evidence:
            c.fail("report.evidence_refs", "UNKNOWN_EVIDENCE_ID")

    win_fields = (
        "actual_duration_s",
        "clock_mode",
        "cycle_count",
        "work_cycle_count",
        "distinct_work_count",
        "idle_cycle_count",
        "failed_cycle_count",
    )
    window = c.obj(report.get("observation_window"), "report.observation_window", win_fields)
    duration = c.number(window.get("actual_duration_s"), "report.observation_window.actual_duration_s")
    if duration <= 0:
        c.fail("report.observation_window.actual_duration_s", "MUST_BE_POSITIVE")
    if (
        started
        and ended
        and math.isfinite(duration)
        and abs((ended - started).total_seconds() - duration) > 1
    ):
        c.fail("report.observation_window.actual_duration_s", "WINDOW_DURATION_MISMATCH")
    if window.get("clock_mode") != "real":
        c.fail("report.observation_window.clock_mode", "REAL_CLOCK_REQUIRED")
    counts = {key: c.integer(window.get(key), f"report.observation_window.{key}") for key in win_fields[2:]}
    cycles = c.array(report.get("cycles"), "report.cycles", nonempty=True)
    if len(cycles) < 4 or counts["cycle_count"] != len(cycles):
        c.fail("report.cycles", "CYCLE_COUNT_INVALID")
    seen_cycles: set[str] = set()
    seen_numbers: set[int] = set()
    observations: set[str] = set()
    tasks: set[str] = set()
    completed_observations: set[str] = set()
    completed_tasks: set[str] = set()
    ordered_states: list[tuple[int, str]] = []
    work = idle = failed = completed_work = 0
    cycle_fields = (
        "cycle_id",
        "number",
        "state",
        "watermark",
        "plan_asset_id",
        "observation_asset_ids",
        "candidate_ids",
        "selected_ids",
        "task_ids",
        "turn_ids",
        "event_ids",
        "run_ids",
        "job_ids",
        "output_bean_ids",
        "receipt_ids",
        "receipt_not_applicable_reason",
        "domain_case_evidence",
        "costs",
    )
    for i, cycle in enumerate(cycles):
        path = f"report.cycles[{i}]"
        cycle = c.obj(cycle, path, cycle_fields)
        cid = c.text(cycle.get("cycle_id"), f"{path}.cycle_id")
        number = c.integer(cycle.get("number"), f"{path}.number", 1)
        state = c.text(cycle.get("state"), f"{path}.state")
        watermark = c.integer(cycle.get("watermark"), f"{path}.watermark")
        if cid in seen_cycles or number in seen_numbers:
            c.fail(path, "DUPLICATE_CYCLE")
        seen_cycles.add(cid)
        seen_numbers.add(number)
        c.text(cycle.get("plan_asset_id"), f"{path}.plan_asset_id")
        obs = c.strings(cycle.get("observation_asset_ids"), f"{path}.observation_asset_ids", nonempty=True)
        candidate_ids = c.strings(cycle.get("candidate_ids"), f"{path}.candidate_ids")
        selected_ids = c.strings(cycle.get("selected_ids"), f"{path}.selected_ids")
        task_ids = c.strings(cycle.get("task_ids"), f"{path}.task_ids")
        if not set(selected_ids) <= set(candidate_ids):
            c.fail(f"{path}.selected_ids", "CANDIDATE_NOT_FOUND")
        if state == "idle":
            idle += 1
            if selected_ids or task_ids:
                c.fail(path, "IDLE_HAS_TASKS")
        elif state == "failed":
            failed += 1
        elif state != "completed":
            c.fail(f"{path}.state", "CYCLE_NOT_TERMINAL")
        ordered_states.append((number, state))
        if task_ids:
            work += 1
            observations.update(obs)
            tasks.update(task_ids)
            if state == "completed":
                completed_work += 1
                completed_observations.update(obs)
                completed_tasks.update(task_ids)
        kinds = {
            "turn_ids": "turn",
            "event_ids": "event",
            "run_ids": "run",
            "job_ids": "job",
            "receipt_ids": "receipt",
        }
        collected: dict[str, list[str]] = {}
        for key, kind in kinds.items():
            collected[key] = c.strings(
                cycle.get(key), f"{path}.{key}", nonempty=key in {"turn_ids", "event_ids"}
            )
            for j, item in enumerate(collected[key]):
                _exists(conn, TABLES[kind], item, f"{path}.{key}[{j}]", c)
                if (kind, item) not in indexed:
                    c.fail(f"{path}.{key}[{j}]", "DATABASE_ID_MISSING_EVIDENCE")
        c.strings(cycle.get("output_bean_ids"), f"{path}.output_bean_ids", nonempty=state == "completed")
        if not collected["receipt_ids"]:
            c.text(cycle.get("receipt_not_applicable_reason"), f"{path}.receipt_not_applicable_reason")
        elif cycle.get("receipt_not_applicable_reason") not in (None, ""):
            c.fail(f"{path}.receipt_not_applicable_reason", "RECEIPT_REASON_CONFLICT")
        if task_ids and (not collected["run_ids"] or not collected["job_ids"]):
            c.fail(path, "WORK_MISSING_RUN_OR_JOB")
        domain = cycle.get("domain_case_evidence")
        if not isinstance(domain, dict) or (state == "completed" and not domain):
            c.fail(f"{path}.domain_case_evidence", "DOMAIN_EVIDENCE_MISSING")
        else:
            for key, item in domain.items():
                dpath = f"{path}.domain_case_evidence"
                c.text(key, dpath)
                item = c.obj(item, dpath, ("kind", "id"))
                if item.get("kind") not in ("job", "turn"):
                    c.fail(dpath, "INVALID_DOMAIN_EVIDENCE_KIND")
                elif item.get("id") not in collected["job_ids" if item["kind"] == "job" else "turn_ids"]:
                    c.fail(dpath, "DOMAIN_EVIDENCE_OUTSIDE_CYCLE")
        costs = c.obj(cycle.get("costs"), f"{path}.costs", ("spent_usd", "reserved_usd", "unknown_cost"))
        c.number(costs.get("spent_usd"), f"{path}.costs.spent_usd")
        c.number(costs.get("reserved_usd"), f"{path}.costs.reserved_usd")
        c.boolean(costs.get("unknown_cost"), f"{path}.costs.unknown_cost")
        if conn and cid:
            try:
                row = conn.execute(
                    "SELECT number, state, watermark FROM objective_cycles WHERE id = ?", (cid,)
                ).fetchone()
                if row is None:
                    c.fail(f"{path}.cycle_id", "DATABASE_ID_NOT_FOUND")
                elif row != (number, state, watermark):
                    c.fail(path, "CYCLE_DATABASE_MISMATCH")
                for run in collected["run_ids"]:
                    for job in collected["job_ids"]:
                        if conn.execute(
                            "SELECT 1 FROM run_nodes WHERE run_id = ? AND job_id = ?", (run, job)
                        ).fetchone():
                            break
                    else:
                        c.fail(f"{path}.run_ids", "RUN_JOB_LINK_MISSING")
                for job in collected["job_ids"]:
                    if not any(
                        conn.execute(
                            "SELECT 1 FROM run_nodes WHERE run_id = ? AND job_id = ?", (run, job)
                        ).fetchone()
                        for run in collected["run_ids"]
                    ):
                        c.fail(f"{path}.job_ids", "JOB_RUN_LINK_MISSING")
                for receipt in collected["receipt_ids"]:
                    row = conn.execute(
                        "SELECT job_id, run_id FROM receipts WHERE id = ?", (receipt,)
                    ).fetchone()
                    if row and (
                        row[0] not in collected["job_ids"] or (row[1] and row[1] not in collected["run_ids"])
                    ):
                        c.fail(f"{path}.receipt_ids", "RECEIPT_LINK_MISMATCH")
            except sqlite3.Error:
                c.fail(path, "DATABASE_QUERY_FAILED")
    ordered_states.sort()
    has_failure_then_success = any(
        left_state == "failed" and right_state == "completed"
        for left_number, left_state in ordered_states
        for right_number, right_state in ordered_states
        if left_number < right_number
    )
    if (
        work < 3
        or idle < 1
        or failed < 1
        or len(tasks) < 2
        or len(observations) < 2
        or completed_work < 2
        or len(completed_tasks) < 2
        or len(completed_observations) < 2
        or not ordered_states
        or ordered_states[0][1] != "idle"
        or not has_failure_then_success
    ):
        c.fail("report.cycles", "INSUFFICIENT_CYCLE_EVIDENCE")
    if (
        counts["work_cycle_count"] != work
        or counts["idle_cycle_count"] != idle
        or counts["failed_cycle_count"] != failed
        or counts["distinct_work_count"] < 2
        or counts["distinct_work_count"] > len(tasks)
    ):
        c.fail("report.observation_window", "CYCLE_COUNTS_MISMATCH")

    operation_fields = (
        "job_id",
        "operation_key",
        "provider_digest",
        "request_digest",
        "idempotency_key_digest",
        "state",
        "task_id_redacted_or_digest",
        "submit_count",
        "poll_count",
        "fetch_count",
        "output_digests",
        "error_code",
    )
    operations = c.array(report.get("external_operations"), "report.external_operations", nonempty=True)
    operation_states: set[str] = set()
    for i, operation in enumerate(operations):
        path = f"report.external_operations[{i}]"
        operation = c.obj(operation, path, operation_fields)
        job = c.text(operation.get("job_id"), f"{path}.job_id")
        _exists(conn, "jobs", job, f"{path}.job_id", c)
        c.text(operation.get("operation_key"), f"{path}.operation_key")
        for key in ("provider_digest", "request_digest", "idempotency_key_digest"):
            c.digest(operation.get(key), f"{path}.{key}")
        state = c.text(operation.get("state"), f"{path}.state")
        if state not in {
            "prepared",
            "submitting",
            "submitted",
            "polling",
            "fetching",
            "completed",
            "failed",
            "outcome_unknown",
        }:
            c.fail(f"{path}.state", "INVALID_OPERATION_STATE")
        operation_states.add(state)
        task_id = operation.get("task_id_redacted_or_digest")
        if state == "completed":
            c.text(task_id, f"{path}.task_id_redacted_or_digest")
        elif task_id is not None:
            c.text(task_id, f"{path}.task_id_redacted_or_digest")
        for key in ("submit_count", "poll_count", "fetch_count"):
            c.integer(operation.get(key), f"{path}.{key}")
        for j, digest in enumerate(c.strings(operation.get("output_digests"), f"{path}.output_digests")):
            c.digest(digest, f"{path}.output_digests[{j}]")
        if operation.get("error_code") is not None:
            c.text(operation.get("error_code"), f"{path}.error_code")
    if not {"completed", "outcome_unknown"} <= operation_states:
        c.fail("report.external_operations", "RECOVERY_EVIDENCE_INCOMPLETE")

    budget_fields = (
        "scopes",
        "cap_usd",
        "spent_usd",
        "reserved_usd",
        "remaining_usd",
        "unknown_cost",
        "as_of_seq",
        "model_usage_refs",
        "budget_event_ids",
    )
    budget = c.obj(report.get("budgets"), "report.budgets", budget_fields)
    c.strings(budget.get("scopes"), "report.budgets.scopes", nonempty=True)
    for key in ("cap_usd", "spent_usd", "reserved_usd", "remaining_usd"):
        c.number(budget.get(key), f"report.budgets.{key}", nonnegative=key != "remaining_usd")
    c.boolean(budget.get("unknown_cost"), "report.budgets.unknown_cost")
    c.integer(budget.get("as_of_seq"), "report.budgets.as_of_seq")
    c.strings(budget.get("model_usage_refs"), "report.budgets.model_usage_refs", nonempty=True)
    for item in c.strings(budget.get("budget_event_ids"), "report.budgets.budget_event_ids", nonempty=True):
        _exists(conn, "events", item, "report.budgets.budget_event_ids", c)
    gov_fields = (
        "changeset_ids",
        "risk_tiers",
        "validation_refs",
        "shadow_refs",
        "canary_refs",
        "signoff_evidence",
        "safe_mode_events",
        "active_epoch_before",
        "active_epoch_after",
        "outbox_refs",
    )
    gov = c.obj(report.get("governance"), "report.governance", gov_fields)
    for key in gov_fields[:-3]:
        c.strings(
            gov.get(key),
            f"report.governance.{key}",
            nonempty=key in {"changeset_ids", "validation_refs", "shadow_refs", "canary_refs"},
        )
    for key in ("active_epoch_before", "active_epoch_after"):
        c.text(gov.get(key), f"report.governance.{key}")
    c.strings(gov.get("outbox_refs"), "report.governance.outbox_refs")
    for i, exclusion in enumerate(c.array(report.get("exclusions"), "report.exclusions")):
        path = f"report.exclusions[{i}]"
        exclusion = c.obj(exclusion, path, ("component", "reason", "does_not_prove"))
        for key in ("component", "reason", "does_not_prove"):
            c.text(exclusion.get(key), f"{path}.{key}")

    cases = c.array(report.get("cases"), "report.cases", nonempty=True)
    found_cases: list[str] = []
    for i, case in enumerate(cases):
        path = f"report.cases[{i}]"
        case = c.obj(
            case,
            path,
            (
                "case_id",
                "status",
                "stimuli_refs",
                "observation_refs",
                "assertions",
                "failed_reasons",
                "started_at",
                "ended_at",
            ),
        )
        found_cases.append(c.text(case.get("case_id"), f"{path}.case_id"))
        if case.get("status") != "passed":
            c.fail(f"{path}.status", "CASE_NOT_PASSED")
        for key in ("stimuli_refs", "observation_refs"):
            for ref in c.strings(case.get(key), f"{path}.{key}", nonempty=True):
                if ref not in evidence:
                    c.fail(f"{path}.{key}", "UNKNOWN_EVIDENCE_ID")
        if c.strings(case.get("failed_reasons"), f"{path}.failed_reasons"):
            c.fail(f"{path}.failed_reasons", "CASE_HAS_FAILURES")
        begin = c.instant(case.get("started_at"), f"{path}.started_at")
        finish = c.instant(case.get("ended_at"), f"{path}.ended_at")
        if (
            begin
            and finish
            and (finish < begin or (started and begin < started) or (ended and finish > ended))
        ):
            c.fail(path, "CASE_WINDOW_INVALID")
        assertions = c.array(case.get("assertions"), f"{path}.assertions", nonempty=True)
        assertion_ids: set[str] = set()
        for j, assertion in enumerate(assertions):
            apath = f"{path}.assertions[{j}]"
            assertion = c.obj(assertion, apath, ("id", "passed", "evidence_refs"))
            aid = c.text(assertion.get("id"), f"{apath}.id")
            if aid in assertion_ids:
                c.fail(f"{apath}.id", "DUPLICATE_ASSERTION")
            assertion_ids.add(aid)
            if assertion.get("passed") is not True:
                c.fail(f"{apath}.passed", "ASSERTION_NOT_PASSED")
            for ref in c.strings(assertion.get("evidence_refs"), f"{apath}.evidence_refs", nonempty=True):
                if ref not in evidence:
                    c.fail(f"{apath}.evidence_refs", "UNKNOWN_EVIDENCE_ID")
        expected_count = case_assertions.get(case.get("case_id"), 0)
        if assertion_ids != {f"A{number:02}" for number in range(1, expected_count + 1)}:
            c.fail(f"{path}.assertions", "ASSERTION_SET_MISMATCH")
    if sorted(found_cases) != sorted(case_ids) or len(found_cases) != len(case_ids):
        c.fail("report.cases", "CASE_SET_MISMATCH")
    if conn is not None:
        conn.close()
    return {
        "ok": not c.issues,
        "status": "passed" if not c.issues else ("blocked" if source_status == "blocked" else "failed"),
        "issues": c.issues,
        "scope": "evidence integrity only; model quality and provenance are not established; redaction detection is incomplete",
    }


class JsonParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "failed",
                    "issues": [{"path": "arguments", "code": "INVALID_ARGUMENTS"}],
                    "scope": "evidence integrity only; model quality and provenance are not established; redaction detection is incomplete",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    parser = JsonParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--factory-root", required=True, type=Path)
    args = parser.parse_args(argv)
    result = validate(args.report, args.factory_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
