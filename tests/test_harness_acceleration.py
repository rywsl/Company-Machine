"""harness 的恢复、缓存、并行和越界审查契约。"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import harness  # noqa: E402


def _check(name: str, code: str, **kwargs) -> harness.Check:
    return harness.Check(
        id=name,
        run=f'{sys.executable.replace(chr(92), "/")} -c "{code}"',
        timeout=10,
        **kwargs,
    )


@pytest.fixture()
def isolated_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(harness, "HARNESS_RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(harness, "HARNESS_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(harness, "HARNESS_REVIEWS_DIR", tmp_path / "reviews")
    monkeypatch.setattr(harness, "_fingerprint_base", lambda: {"test": "base"})
    monkeypatch.setattr(
        harness,
        "_cache_fingerprint",
        lambda check, base=None: hashlib.sha256(check.run.encode("utf-8")).hexdigest(),
    )
    return tmp_path


def test_only_and_from_select_checks(isolated_harness: Path):
    checks = [_check("one", "print('one')"), _check("two", "print('two')"), _check("three", "print('three')")]
    only = harness.run_checks(checks, "selection-only", only=["two"])
    assert [x.check.id for x in only] == ["two"]
    resumed = harness.run_checks(checks, "selection-from", from_id="two")
    assert [x.check.id for x in resumed] == ["two", "three"]


def test_check_metadata_is_optional_and_conservative():
    check = harness.Check.parse(
        {
            "id": "offline",
            "run": "echo ok",
            "parallel_group": "p",
            "exclusive_resources": [],
            "cacheable": True,
            "cache_inputs": ["tests", "env:MAO_TEST_MODE"],
            "live": False,
            "requires": ["previous"],
        },
        "test",
        0,
    )
    assert check.parallel_group == "p"
    assert check.exclusive_resources == ()
    assert check.cacheable is True
    assert check.cache_inputs == ("tests", "env:MAO_TEST_MODE")
    assert check.requires == ("previous",)
    with pytest.raises(harness.UsageError):
        harness.Check.parse({"run": "echo", "cacheable": True, "live": True}, "test", 0)


def test_fail_fast_stops_after_first_failure(isolated_harness: Path):
    checks = [_check("bad", "raise SystemExit(4)"), _check("later", "raise SystemExit(5)")]
    results = harness.run_checks(checks, "fail-fast", fail_fast=True)
    assert [x.check.id for x in results] == ["bad"]
    assert results[0].ok is False


def test_cache_hit_and_input_change_invalidate(isolated_harness: Path):
    first = _check("offline", "print('ok')", cacheable=True, parallel_group="offline", exclusive_resources=())
    assert harness.run_checks([first], "cache", jobs=2)[0].executed is True
    reused = harness.run_checks([first], "cache", resume=True)[0]
    assert reused.reused is True and reused.executed is False
    changed = _check(
        "offline", "print('changed')", cacheable=True, parallel_group="offline", exclusive_resources=()
    )
    invalidated = harness.run_checks([changed], "cache", resume=True)[0]
    assert invalidated.reused is False and invalidated.executed is True


def test_fingerprint_excludes_harness_state_output(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(harness, "_changed_files", lambda: ["plan/state.json", "src/feature.py"])
    monkeypatch.setattr(harness, "_file_digest", lambda rel: {"src/feature.py": "source"}[rel])
    monkeypatch.setattr(harness, "_git", lambda *args: "commit" if args == ("rev-parse", "HEAD") else "")
    monkeypatch.setattr(harness, "_input_files_for_fingerprint", lambda *args, **kwargs: {})
    monkeypatch.setattr(harness, "_command_version", lambda argv: "version")
    monkeypatch.setattr(harness.platform, "system", lambda: "test")
    monkeypatch.setattr(harness.platform, "release", lambda: "test")
    monkeypatch.setattr(harness.platform, "machine", lambda: "test")

    base = harness._fingerprint_base()

    assert "plan/state.json" not in base["source"]["changed_digests"]
    assert base["source"]["changed_digests"] == {"src/feature.py": "source"}


def test_live_never_reuses_cache(isolated_harness: Path):
    live = _check("live", "print('live')", cacheable=False, live=True)
    assert harness.run_checks([live], "live", resume=True)[0].executed is True
    assert harness.run_checks([live], "live", resume=True)[0].executed is True


def test_parallel_checks_get_independent_temp_dirs(isolated_harness: Path):
    checks = [
        _check(
            "a",
            "import os; print(os.environ['MAO_HARNESS_TEMP_DIR'])",
            parallel_group="p",
            exclusive_resources=(),
        ),
        _check(
            "b",
            "import os; print(os.environ['MAO_HARNESS_TEMP_DIR'])",
            parallel_group="p",
            exclusive_resources=(),
        ),
    ]
    results = harness.run_checks(checks, "parallel", jobs=2)
    assert len({r.temp_dir for r in results}) == 2
    assert all(r.ok for r in results)


def test_failure_then_resume_reuses_only_previous_success(isolated_harness: Path):
    good = _check("good", "print('good')", cacheable=True, parallel_group="p", exclusive_resources=())
    bad = _check("bad", "raise SystemExit(3)")
    first = harness.run_checks([good, bad], "recover")
    assert [r.ok for r in first] == [True, False]
    fixed = _check("bad", "print('fixed')")
    second = harness.run_checks([good, fixed], "recover", resume=True)
    assert second[0].reused is True
    assert second[1].executed is True and second[1].ok is True


def test_windows_atomic_replace_retries_permission_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    attempts = {"n": 0}

    class LockedError(OSError):
        winerror = 5

    original = harness.os.replace

    def flaky(source, target):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise LockedError("locked")
        return original(source, target)

    monkeypatch.setattr(harness.os, "name", "nt")
    monkeypatch.setattr(harness.os, "replace", flaky)
    target = tmp_path / "state.json"
    harness._atomic_write_text(target, "{}\n")
    assert target.read_text(encoding="utf-8") == "{}\n"
    assert attempts["n"] == 3


def test_process_cleanup_is_recorded(isolated_harness: Path):
    check = _check("timeout", "import time; time.sleep(2)")
    check.timeout = 0
    result = harness.run_checks([check], "cleanup")[0]
    assert result.ok is False
    assert result.cleanup.get("attempted") is True
    assert result.blocker in {"timeout", "process_cleanup"}


def test_formal_m17_checks_stay_complete_and_review_audit_is_read_only():
    tasks = harness.load_tasks()
    state = harness.load_state()
    m17 = tasks["M17-08"]
    ids = {c.id for c in m17.accept}
    assert {"live-witness", "all-known-issues", "operability-live-witness", "lock"} <= ids
    assert "regress" not in ids
    m17_checks = {c.id: c for c in m17.accept}
    assert m17_checks["all-known-issues"].cacheable is True
    assert m17_checks["live-witness"].cacheable is False
    assert m17_checks["operability-live-witness"].cacheable is False
    gate_ids = {
        item.get("id")
        for item in harness.load_gates()["gates"]["M17"].get("checks", [])
        if isinstance(item, dict)
    }
    assert "pytest" in gate_ids
    for task_id in ("M17-09", "M17-10", "M17-11", "M17-12"):
        checks = {c.id: c for c in tasks[task_id].accept}
        assert "regress" not in checks
        assert checks["contract"].parallel_group == "m17-offline-checks"
        assert checks["contract"].cacheable is True
        assert checks["contract"].exclusive_resources == ()
    before = json.dumps(state, sort_keys=True)
    report = harness._review_audit(tasks["M17-01"], state)
    assert report["files"][0]["path"] == "lan/state.json"
    assert report["files"][0]["exists"] is False
    assert json.dumps(harness.load_state(), sort_keys=True) == before


def test_review_artifact_requires_independent_identity_and_matching_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    task = harness.Task("T", "M", "title", "plan/tasks/T.md", paths=["tests/**"])
    record = {
        "commit": harness._git("rev-parse", "--short", "HEAD"),
        "outside_paths": ["src/mao/agents.py"],
    }
    monkeypatch.setattr(harness, "HARNESS_REVIEWS_DIR", tmp_path / "reviews")
    monkeypatch.setenv("MAO_IMPLEMENTER_ID", "agent-a")
    path = tmp_path / "src" / "mao" / "agents.py"
    path.parent.mkdir(parents=True)
    path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(harness, "ROOT", tmp_path)
    monkeypatch.setattr(harness, "_changed_files", lambda: ["src/mao/agents.py"])
    artifact_dir = harness.HARNESS_REVIEWS_DIR / "T"
    artifact_dir.mkdir(parents=True)
    payload = {
        "task_id": "T",
        "source_commit": record["commit"],
        "working_tree_digest": harness._working_tree_digest(),
        "changed_files": {"src/mao/agents.py": harness._file_digest("src/mao/agents.py")},
        "outside_paths": ["src/mao/agents.py"],
        "file_ownership": {"src/mao/agents.py": {"task_id": "T", "reason": "shared implementation"}},
        "tests": [{"command": "pytest", "passed": True}],
        "reviewer_id": "agent-b",
        "reviewer_model": "reviewer",
        "reviewer_context_id": "ctx-b",
        "reviewer_input_digest": "input",
        "decision": "accepted",
    }
    artifact = artifact_dir / "r.json"
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    assert harness._validate_review_artifact(task, record, artifact)["decision"] == "accepted"
    payload["reviewer_id"] = "agent-a"
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    assert harness._validate_review_artifact(task, record, artifact)["decision"] == "needs_human"
    payload["reviewer_id"] = "agent-b"
    payload["working_tree_digest"] = "stale"
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    assert (
        "working_tree_digest_mismatch" in harness._validate_review_artifact(task, record, artifact)["reason"]
    )


def test_high_risk_review_is_escalated_even_with_ai_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    task = harness.Task("T", "M", "title", "plan/tasks/T.md", paths=[])
    record = {"commit": "abc", "outside_paths": ["plan/state.json"]}
    monkeypatch.setattr(harness, "ROOT", tmp_path)
    monkeypatch.setattr(harness, "HARNESS_REVIEWS_DIR", tmp_path / "reviews")
    monkeypatch.setattr(harness, "_working_tree_digest", lambda: "tree")
    (tmp_path / "plan").mkdir()
    (tmp_path / "plan" / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(harness, "_file_digest", lambda rel: "digest")
    artifact_dir = harness.HARNESS_REVIEWS_DIR / "T"
    artifact_dir.mkdir(parents=True)
    payload = {
        "task_id": "T",
        "source_commit": "abc",
        "working_tree_digest": "tree",
        "changed_files": {"plan/state.json": "digest"},
        "outside_paths": ["plan/state.json"],
        "file_ownership": {"plan/state.json": {"task_id": "T"}},
        "tests": [{"passed": True}],
        "reviewer_id": "agent-b",
        "reviewer_model": "reviewer",
        "reviewer_context_id": "ctx",
        "reviewer_input_digest": "input",
        "decision": "accepted",
    }
    artifact = artifact_dir / "high-risk.json"
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    result = harness._validate_review_artifact(task, record, artifact)
    assert result["decision"] == "needs_human"
    assert "high_risk_requires_human" in result["reason"]
