"""Design/validator regression only; synthetic data never proves factory capability."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

AUDIT = Path(__file__).resolve().parents[1]
ROOT = AUDIT.parents[2]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = load("m17_plan_check", AUDIT / "verify_plan.py")
SYNTHETIC = load("m17_original_synthetic_fixture", AUDIT / "acceptance/test_live_validator.py")


def write_yaml(path: Path, value) -> None:
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")


def write_json(path: Path, value) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def design_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "design"
    manifest = CHECK.load_manifest()
    files = {
        "plan/tasks.yaml",
        "plan/gates.yaml",
        "plan/evidence/m17/closure.yaml",
        "plan/evidence/m17/operability-cases.yaml",
        "spec/system/operability.md",
    }
    files.update(f"plan/tasks/M17-{i:02}.md" for i in range(1, 13))
    for row in manifest["issues"]:
        files.update(row["evidence"])
        files.update(ref["path"] for ref in row["spec_refs"])
    for rel in files:
        target = tree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, target)
    return tree


def test_registered_design_is_complete_but_not_behavior(design_tree: Path) -> None:
    assert CHECK.validate_design(design_tree) == {
        "ok": True,
        "scope": "design_coverage",
        "issue_count": 46,
        "issues": [],
    }
    assert not (design_tree / "tests/system/test_m17_09_transport.py").exists()


@pytest.mark.parametrize(
    "change",
    [
        "omit_issue",
        "duplicate_issue",
        "empty_tests",
        "unknown_test",
        "empty_spec",
        "bad_section",
        "missing_evidence",
        "unsafe_evidence",
        "pretend_fixed",
        "drop_product",
        "weaken_group",
        "broad_selector",
    ],
)
def test_incomplete_or_misleading_mapping_is_rejected(design_tree: Path, change: str) -> None:
    path = design_tree / "plan/evidence/m17/closure.yaml"
    document = yaml.safe_load(path.read_text())
    if change == "omit_issue":
        document["issues"].pop()
    elif change == "duplicate_issue":
        document["issues"][-1] = copy.deepcopy(document["issues"][0])
    elif change == "empty_tests":
        document["issues"][0]["tests"] = []
    elif change == "unknown_test":
        document["issues"][0]["tests"] = ["tests/system/test_missing.py::test_unregistered"]
    elif change == "empty_spec":
        document["issues"][0]["spec_refs"] = []
    elif change == "bad_section":
        document["issues"][0]["spec_refs"][0]["section"] = "99.99"
    elif change == "missing_evidence":
        document["issues"][0]["evidence"] = ["missing.md"]
    elif change == "unsafe_evidence":
        document["issues"][0]["evidence"] = ["../outside.md"]
    elif change == "pretend_fixed":
        document["issues"][0]["design_baseline_status"] = "fixed"
    elif change == "drop_product":
        document["required_live_products"].pop()
    elif change == "weaken_group":
        document["test_groups"][0]["minimum_cases"] = 0
    elif change == "broad_selector":
        document["test_groups"][0]["nodes"] = ["tests/system -k success"]
    write_yaml(path, document)
    assert CHECK.validate_design(design_tree)["ok"] is False


@pytest.mark.parametrize(
    "change",
    [
        "omit_final_dependency",
        "migration_order",
        "cycle",
        "drop_task_check",
        "drop_core_integrity",
        "drop_gate",
        "drop_core_witness_gate",
        "weaken_gate",
    ],
)
def test_task_and_gate_cannot_bypass_closure(design_tree: Path, change: str) -> None:
    path = design_tree / (
        "plan/gates.yaml"
        if change in {"drop_gate", "drop_core_witness_gate", "weaken_gate"}
        else "plan/tasks.yaml"
    )
    data = yaml.safe_load(path.read_text())
    if change in {"drop_gate", "drop_core_witness_gate", "weaken_gate"}:
        checks = data["gates"]["M17"]["checks"]
        if change in {"drop_gate", "drop_core_witness_gate"}:
            removed = "m17-operability-live" if change == "drop_gate" else "m17-live-witness"
            checks[:] = [c for c in checks if c["id"] != removed]
        else:
            next(c for c in checks if c["id"] == "m17-all-known-issues")["assert"] = "out.passed >= 1"
    elif change == "migration_order":
        task = next(t for t in data["tasks"] if t["id"] == "M17-09")
        task["deps"].remove("M17-07")
    else:
        task = next(t for t in data["tasks"] if t["id"] == "M17-08")
        if change == "omit_final_dependency":
            task["deps"] = ["M17-07"]
        elif change == "cycle":
            task["deps"].append("M17-08")
        else:
            removed = "live-evidence-integrity" if change == "drop_core_integrity" else "all-known-issues"
            task["accept"] = [c for c in task["accept"] if c["id"] != removed]
    write_yaml(path, data)
    assert CHECK.validate_design(design_tree)["ok"] is False


@pytest.mark.parametrize(
    "change", ["omit_group", "duplicate_group", "omit_witness", "wrong_witness_node", "weaken_cycles"]
)
def test_supplemental_contract_cannot_lose_requirements(design_tree: Path, change: str) -> None:
    path = design_tree / "plan/evidence/m17/operability-cases.yaml"
    document = yaml.safe_load(path.read_text())
    if change == "omit_group":
        document["groups"].pop()
    elif change == "duplicate_group":
        document["groups"][-1] = copy.deepcopy(document["groups"][0])
    elif change == "omit_witness":
        del document["live_witness"]
    elif change == "wrong_witness_node":
        document["live_witness"]["node"] = "tests/system/test_missing.py::test_not_a_witness"
    else:
        document["live_witness"]["minimum_cycles_per_cell"] = 1
    write_yaml(path, document)
    assert CHECK.validate_design(design_tree)["ok"] is False


def test_missing_behavior_nodes_fail_execution(design_tree: Path) -> None:
    # The isolated tree deliberately omits behavior nodes even after implementation.
    # Run the real runner and harness, with their actual mrl dependency, not a stub.
    for relative in (
        "plan/evidence/m17/run_contracts.py",
        "plan/evidence/m17/verify_plan.py",
        "tools/harness.py",
    ):
        target = design_tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    shutil.copytree(
        ROOT / "src/mrl", design_tree / "src/mrl", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    result = subprocess.run(
        [sys.executable, str(design_tree / "plan/evidence/m17/run_contracts.py"), "--task", "M17-09"],
        cwd=design_tree,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    report = json.loads(result.stdout)
    assert result.returncode != 0 and report["exit"] != 0
    assert report["passed"] == 0 and report["scope"] == "offline_contract_execution"


@pytest.fixture
def live_index(tmp_path: Path):
    directory = tmp_path / "live"
    directory.mkdir()
    source = SYNTHETIC.DIGEST
    index = {
        "schema_version": "mao-operability-live-1",
        "status": "passed",
        "witness_run_id": "synthetic-only-witness",
        "platform_source_digest": source,
        "unplanned_business_interventions": 0,
        "unplanned_actions": [],
        "cells": [],
        "product_receipts": [],
        "artifacts": [],
    }
    for domain in ("media", "analysis"):
        for runtime in ("adapter-a@1", "adapter-b@1"):
            folder = directory / (domain + "-" + runtime.split("@")[0])
            folder.mkdir()
            factory, report_path, report, _ = SYNTHETIC.synthetic.__wrapped__(folder)
            report["witness_run_id"] = index["witness_run_id"]
            report["domain_id"] = domain
            mode = "help_only" if (domain == "media") == (runtime == "adapter-a@1") else "skill"
            report["entry_mode"] = mode
            report["environment"]["runtime_ref"] = runtime
            report["authorization"]["contract_digest"] = ("c" if domain == "analysis" else "a") * 64
            digest = write_json(report_path, report)
            index["cells"].append(
                {
                    "domain_id": domain,
                    "entry_mode": mode,
                    "runtime_ref": runtime,
                    "report": report_path.relative_to(directory).as_posix(),
                    "report_sha256": digest,
                    "factory_root": factory.relative_to(directory).as_posix(),
                }
            )
    for n, product in enumerate(CHECK.load_manifest()["required_live_products"]):
        rel = f"product-evidence/{n}.json"
        digest = write_json(
            directory / rel, {"synthetic_only": True, "witness_run_id": index["witness_run_id"]}
        )
        receipt = {
            "schema_version": "mao-qualification-1",
            "kind": "machine",
            "subject_key": product["machine_key"],
            "status": "passed",
            "profile": "live",
            "platform_digest": source,
            "witness_run_id": index["witness_run_id"],
            "blocked_reasons": [],
            "checks": [
                {"id": key, "passed": True, "evidence_refs": ["product-invocation"]}
                for key in ("identity", "health", "invoke", "outputs", "recovery", "network")
            ],
            "evidence_index": [
                {"evidence_id": "product-invocation", "kind": "invocation", "path": rel, "sha256": digest}
            ],
        }
        receipt_rel = f"products/{n}.json"
        sha = write_json(directory / receipt_rel, receipt)
        index["product_receipts"].append(
            {"machine_key": product["machine_key"], "path": receipt_rel, "sha256": sha}
        )
    for kind in (
        "deployment",
        "operator_permissions",
        "transport",
        "runtime_qualification",
        "help_only",
        "skill_usage",
    ):
        rel = "audit/" + kind + ".json"
        digest = write_json(directory / rel, {"synthetic_only": True})
        index["artifacts"].append({"evidence_id": kind, "kind": kind, "path": rel, "sha256": digest})
    path = directory / "index.json"
    write_json(path, index)
    return path, index


def test_synthetic_matrix_checks_integrity_only(live_index) -> None:
    path, _ = live_index
    result = CHECK.validate_live_index(path)
    assert result == {"ok": True, "scope": "evidence_integrity", "issues": []}


@pytest.mark.parametrize(
    "change",
    [
        "drop_cell",
        "repeat_pair",
        "reuse_factory",
        "missing_product",
        "offline_product",
        "unresolved_product_ref",
        "stale_product",
        "changed_source",
        "stale_cell_witness",
        "same_domain_contract",
        "skipped_core_case",
        "missing_artifact",
        "tamper_artifact",
        "intervention",
        "boolean_zero",
        "drop_entry_mode",
        "unbalanced_entry_modes",
        "entry_modes_by_domain",
        "entry_modes_by_runtime",
        "entry_mode_report_mismatch",
    ],
)
def test_live_matrix_requires_all_real_evidence_shapes(live_index, change: str) -> None:
    path, data = live_index
    if change == "drop_cell":
        data["cells"].pop()
    elif change == "repeat_pair":
        data["cells"][1] = copy.deepcopy(data["cells"][0])
    elif change == "reuse_factory":
        data["cells"][1]["factory_root"] = data["cells"][0]["factory_root"]
    elif change == "missing_product":
        data["product_receipts"].pop()
    elif change in {"offline_product", "unresolved_product_ref", "stale_product"}:
        ref = data["product_receipts"][0]
        receipt_path = path.parent / ref["path"]
        receipt = json.loads(receipt_path.read_text())
        if change == "offline_product":
            receipt["profile"] = "offline"
        elif change == "unresolved_product_ref":
            receipt["checks"][0]["evidence_refs"] = ["missing"]
        else:
            receipt["witness_run_id"] = "old-witness"
        ref["sha256"] = write_json(receipt_path, receipt)
    elif change in {"changed_source", "stale_cell_witness", "same_domain_contract", "skipped_core_case"}:
        ref = data["cells"][-1]
        report_path = path.parent / ref["report"]
        report = json.loads(report_path.read_text())
        if change == "changed_source":
            report["baseline"]["platform_source_digest"] = "d" * 64
        elif change == "stale_cell_witness":
            report["witness_run_id"] = "old-witness"
        elif change == "same_domain_contract":
            report["authorization"]["contract_digest"] = "a" * 64
        else:
            report["cases"][0]["status"] = "skipped"
        ref["report_sha256"] = write_json(report_path, report)
    elif change == "missing_artifact":
        data["artifacts"].pop()
    elif change == "tamper_artifact":
        (path.parent / data["artifacts"][0]["path"]).write_text("changed")
    elif change == "intervention":
        data["unplanned_business_interventions"] = 1
    elif change == "boolean_zero":
        data["unplanned_business_interventions"] = False
    elif change == "drop_entry_mode":
        del data["cells"][0]["entry_mode"]
    elif change in {"unbalanced_entry_modes", "entry_modes_by_domain", "entry_modes_by_runtime"}:
        for cell in data["cells"]:
            mode = "help_only"
            if change == "entry_modes_by_domain" and cell["domain_id"] != "media":
                mode = "skill"
            if change == "entry_modes_by_runtime" and cell["runtime_ref"] != "adapter-a@1":
                mode = "skill"
            cell["entry_mode"] = mode
            report_path = path.parent / cell["report"]
            report = json.loads(report_path.read_text())
            report["entry_mode"] = mode
            cell["report_sha256"] = write_json(report_path, report)
    elif change == "entry_mode_report_mismatch":
        cell = data["cells"][0]
        report_path = path.parent / cell["report"]
        report = json.loads(report_path.read_text())
        report["entry_mode"] = "skill"
        cell["report_sha256"] = write_json(report_path, report)
    write_json(path, data)
    assert CHECK.validate_live_index(path)["ok"] is False


def test_missing_live_index_never_passes(tmp_path: Path) -> None:
    assert CHECK.validate_live_index(tmp_path / "absent.json")["ok"] is False
