#!/usr/bin/env python3
"""Validate M17 design coverage or supplemental live evidence integrity.

Neither mode proves model judgement or witness authenticity. Acceptance must also
run the independent, frozen live witness against actual resources.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
EXPECTED_IDS = frozenset(
    [f"H{i:02}" for i in range(1, 10)]
    + [f"G{i:02}" for i in range(1, 9)]
    + [f"E{i:02}" for i in range(1, 7)]
    + [f"F{i:02}" for i in range(1, 3)]
    + [f"HC{i:02}" for i in range(1, 15)]
    + [f"U{i:02}" for i in range(1, 7)]
    + ["R01"]
)
TASK_IDS = frozenset(f"M17-{i:02}" for i in range(1, 13))
GROUP_MINIMA = {
    "M17-01": 5,
    "M17-02": 4,
    "M17-03": 7,
    "M17-04": 8,
    "M17-05": 2,
    "M17-06": 3,
    "M17-07": 13,
    "M17-08": 9,
    "M17-09": 6,
    "M17-10": 7,
    "M17-11": 7,
    "M17-12": 6,
    "core-live": 1,
    "operability-live": 1,
}
NODE = re.compile(
    r"(?:plan/evidence/m17|tests/system|tests/acceptance/m4)/[A-Za-z0-9_/]+\.py::test_[A-Za-z0-9_]+$"
)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(label + ": expected object")
    return value


def _file(root: Path, relative: str) -> Path:
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise ValueError("invalid relative path")
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("missing or unsafe file: " + relative)
    return path


def _json_unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def load_manifest(root: Path = ROOT) -> dict[str, Any]:
    return _mapping(yaml.safe_load(_file(root, "plan/evidence/m17/closure.yaml").read_text()), "closure")


def selected_groups(manifest: dict[str, Any], task: str | None = None) -> list[dict[str, Any]]:
    groups = [
        g for g in manifest["test_groups"] if g["mode"] == "offline" and (task is None or g["task"] == task)
    ]
    if not groups:
        raise ValueError("no exact offline group for task")
    return groups


def validate_design(root: Path = ROOT) -> dict[str, Any]:
    issues: list[str] = []
    manifest: dict[str, Any] = {}
    count = 0
    try:
        manifest = load_manifest(root)
        if manifest["schema_version"] != "mao-m17-closure-1":
            raise ValueError("unexpected closure schema")
        rows = manifest["issues"]
        count = len(rows)
        ids = [row["id"] for row in rows]
        if set(ids) != EXPECTED_IDS or len(ids) != len(EXPECTED_IDS):
            issues.append("ISSUE_SET_INCOMPLETE_OR_DUPLICATED")
        if set(manifest["mandatory_issue_ids"]) != EXPECTED_IDS or len(
            manifest["mandatory_issue_ids"]
        ) != len(EXPECTED_IDS):
            issues.append("MANDATORY_ISSUE_SET_CHANGED")
        tasks = _mapping(yaml.safe_load(_file(root, "plan/tasks.yaml").read_text()), "tasks")
        by_task = {t["id"]: t for t in tasks["tasks"]}
        if not TASK_IDS.issubset(by_task):
            issues.append("M17_TASK_MISSING")
        groups = manifest["test_groups"]
        by_group = {g["id"]: g for g in groups}
        if len(by_group) != len(groups) or set(by_group) != set(GROUP_MINIMA):
            issues.append("TEST_GROUP_SET_CHANGED")
        expected_products = {
            "Machine:openmontage@1",
            "Machine:minimax-h3@1",
            "Machine:minimax-dance@1",
            "Machine:grok-dance@1",
        }
        products = manifest["required_live_products"]
        if (
            len(products) != 4
            or {p["machine_key"] for p in products} != expected_products
            or any(p["required_status"] != "passed" for p in products)
        ):
            issues.append("REQUIRED_LIVE_PRODUCTS_CHANGED")
        all_nodes: set[str] = set()
        for group in groups:
            name = group["id"]
            task = group["task"]
            if task not in TASK_IDS or task != (
                "M17-08" if name in {"core-live", "operability-live"} else name
            ):
                issues.append(name + ":UNKNOWN_OR_WRONG_OWNER")
            minimum = group["minimum_cases"]
            if (
                isinstance(minimum, bool)
                or not isinstance(minimum, int)
                or minimum < GROUP_MINIMA.get(name, 1)
            ):
                issues.append(name + ":MINIMUM_WEAKENED")
            if group["mode"] != ("live" if name in {"core-live", "operability-live"} else "offline"):
                issues.append(name + ":MODE_CHANGED")
            nodes = group["nodes"]
            if (
                not nodes
                or len(nodes) != len(set(nodes))
                or any(not isinstance(n, str) or not NODE.fullmatch(n) for n in nodes)
            ):
                issues.append(name + ":EXACT_NODES_REQUIRED")
            if all_nodes.intersection(nodes):
                issues.append(name + ":NODE_IN_MULTIPLE_GROUPS")
            if name in {"M17-09", "M17-10", "M17-11", "M17-12"} and len(nodes) != GROUP_MINIMA[name]:
                issues.append(name + ":NORMATIVE_NODE_COUNT_CHANGED")
            all_nodes.update(nodes)
            if task in by_task:
                card = _file(root, by_task[task]["card"]).read_text()
                if (
                    task not in card
                    or "operability.md" not in card
                    and task in {"M17-09", "M17-10", "M17-11", "M17-12"}
                ):
                    issues.append(name + ":CARD_NOT_LINKED")
        for row in rows:
            key = row["id"]
            if not row["tasks"] or any(t not in TASK_IDS for t in row["tasks"]):
                issues.append(key + ":NO_TASK")
            if not row["tests"] or any(n not in all_nodes for n in row["tests"]):
                issues.append(key + ":UNREGISTERED_EXACT_TEST")
            if not row["spec_refs"] or not row["evidence"]:
                issues.append(key + ":MISSING_CONTRACT_OR_EVIDENCE")
            for ref in row["spec_refs"]:
                text = _file(root, ref["path"]).read_text()
                if not re.search(r"^#{2,3}\s+" + re.escape(ref["section"]) + r"(?:\s|$)", text, re.M):
                    issues.append(key + ":SPEC_SECTION_MISSING")
            for evidence in row["evidence"]:
                _file(root, evidence)
            if row.get("design_baseline_status") != "not_implemented":
                issues.append(key + ":DESIGN_IS_NOT_IMPLEMENTATION_PROOF")
        # Every new frozen node must appear in the normative acceptance chapter.
        spec = _file(root, "spec/system/operability.md").read_text()
        acceptance = spec.split("## 7. 验收测试", 1)[1].split("## 8.", 1)[0]
        for name in ("M17-09", "M17-10", "M17-11", "M17-12", "operability-live"):
            for node in by_group.get(name, {}).get("nodes", []):
                path, function = node.split("::")
                if path not in acceptance or function not in acceptance:
                    issues.append(node + ":NORMATIVE_ACCEPTANCE_MISSING")
        dependencies: set[str] = set()
        visiting: set[str] = set()

        def visit(task: str) -> None:
            if task in visiting:
                raise ValueError("task dependency cycle")
            if task in dependencies:
                return
            visiting.add(task)
            for dep in by_task[task].get("deps", []):
                if dep not in by_task:
                    raise ValueError("missing dependency")
                visit(dep)
            visiting.remove(task)
            dependencies.add(task)

        visit("M17-08")
        if not TASK_IDS.issubset(dependencies) or "M16-06" not in dependencies:
            issues.append("FINAL_TASK_DOES_NOT_REQUIRE_ALL_WORK")
        if "M17-07" not in by_task["M17-09"].get("deps", []):
            issues.append("OPERABILITY_MIGRATION_PRECEDES_OBJECTIVES")
        final = by_task["M17-08"]
        expected_checks = {
            "all-known-issues",
            "live-witness",
            "live-evidence-integrity",
            "operability-live-witness",
            "operability-live-integrity",
            "design-coverage",
        }
        if not expected_checks.issubset({c["id"] for c in final["accept"]}):
            issues.append("FINAL_ACCEPTANCE_MISSING_CHECK")
        for name in ("M17-09", "M17-10", "M17-11", "M17-12"):
            checks = by_task[name]["accept"]
            if not any(
                "run_contracts.py --task " + name in c["run"] and "out.skipped == 0" in c.get("assert", "")
                for c in checks
            ):
                issues.append(name + ":TASK_CONTRACT_NOT_ENFORCED")
        gate = _mapping(yaml.safe_load(_file(root, "plan/gates.yaml").read_text()), "gates")["gates"]["M17"]
        if "M16" not in gate.get("requires_gates", []):
            issues.append("M16_GATE_REMOVED")
        required_gate = {
            "m17-all-known-issues",
            "m17-design-coverage",
            "m17-operability-live",
            "m17-operability-integrity",
            "m17-live-witness",
            "m17-live-evidence-integrity",
            "m17-contracts",
            "m17-e2e",
            "m17-repro",
            "m17-deployment-paths",
        }
        if not required_gate.issubset({c["id"] for c in gate["checks"]}):
            issues.append("MILESTONE_GATE_MISSING_CHECK")
        for check in gate["checks"] + final["accept"]:
            if check["id"] in {"m17-all-known-issues", "all-known-issues"}:
                if "run_contracts.py --all" not in check["run"] or any(
                    clause not in check.get("assert", "")
                    for clause in (
                        "out.failed == 0",
                        "out.errors == 0",
                        "out.skipped == 0",
                        "out.passed >= 77",
                    )
                ):
                    issues.append("EXACT_CLOSURE_GATE_WEAKENED")
            if check["id"] in {"m17-operability-live", "operability-live-witness"}:
                if (
                    "tests/system/test_m17_08_operability_live.py::test_live_cross_domain_onboarding_and_recovery"
                    not in check["run"]
                    or any(
                        clause not in check.get("assert", "")
                        for clause in ("out.failed == 0", "out.errors == 0", "out.skipped == 0")
                    )
                ):
                    issues.append("LIVE_WITNESS_GATE_WEAKENED")
        contracts = _mapping(
            yaml.safe_load(_file(root, "plan/evidence/m17/operability-cases.yaml").read_text()), "cases"
        )
        contract_groups = contracts["groups"]
        if len(contract_groups) != 4 or {g["id"] for g in contract_groups} != {
            "M17-09",
            "M17-10",
            "M17-11",
            "M17-12",
        }:
            issues.append("CONTRACT_GROUP_SET_CHANGED")
        for group in contract_groups:
            if group != by_group.get(group["id"]):
                issues.append(group["id"] + ":CONTRACT_GROUP_DRIFT")
        witness_contract = contracts["live_witness"]
        if (
            witness_contract["node"] != by_group["operability-live"]["nodes"][0]
            or witness_contract["output_index"] != ".factory/m17-repro/operability-live/index.json"
        ):
            issues.append("SUPPLEMENTAL_WITNESS_CONTRACT_CHANGED")
        if (
            set(witness_contract["requires"]) != TASK_IDS - {"M17-08"}
            or type(witness_contract["minimum_cycles_per_cell"]) is not int
            or witness_contract["minimum_cycles_per_cell"] < 4
        ):
            issues.append("SUPPLEMENTAL_WITNESS_REQUIREMENTS_WEAKENED")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError) as exc:
        issues.append("DESIGN_READ_OR_SHAPE_FAILED:" + type(exc).__name__)
    return {"ok": not issues, "scope": "design_coverage", "issue_count": count, "issues": issues}


def validate_live_index(index_path: Path, root: Path = ROOT) -> dict[str, Any]:
    problems: list[str] = []
    try:
        design = validate_design(root)
        if not design["ok"]:
            raise ValueError("design invalid")
        manifest = load_manifest(root)
        if index_path.is_symlink() or not index_path.is_file():
            raise ValueError("index missing/unsafe")
        directory = index_path.resolve().parent
        index = _mapping(json.loads(index_path.read_text(), object_pairs_hook=_json_unique), "index")
        if index["schema_version"] != "mao-operability-live-1" or index["status"] != "passed":
            raise ValueError("index not passed or wrong version")
        if (
            type(index["unplanned_business_interventions"]) is not int
            or index["unplanned_business_interventions"] != 0
            or index["unplanned_actions"] != []
        ):
            raise ValueError("external business intervention")
        cells = index["cells"]
        if len(cells) != 4:
            raise ValueError("four distinct cells required")
        witness = index["witness_run_id"]
        if not isinstance(witness, str) or not witness.strip():
            raise ValueError("witness id missing")
        source = index["platform_source_digest"]
        if not isinstance(source, str) or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", source):
            raise ValueError("platform digest invalid")
        module_spec = importlib.util.spec_from_file_location(
            "m17_core_live_integrity", root / "plan/evidence/m17/verify_live.py"
        )
        if module_spec is None or module_spec.loader is None:
            raise ValueError("core validator unavailable")
        validator = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(validator)
        pairs: set[tuple[str, str]] = set()
        cell_roots: set[str] = set()
        domain_contracts: dict[str, str] = {}
        entry_modes: dict[tuple[str, str], str] = {}
        for cell in cells:
            pair = (cell["domain_id"], cell["runtime_ref"])
            if pair in pairs or any(not isinstance(v, str) or not v.strip() for v in pair):
                raise ValueError("duplicate/empty cell")
            pairs.add(pair)
            mode = cell["entry_mode"]
            if mode not in {"help_only", "skill"}:
                raise ValueError("invalid onboarding entry mode")
            entry_modes[pair] = mode
            report_path = _file(directory, cell["report"])
            if hashlib.sha256(report_path.read_bytes()).hexdigest() != cell["report_sha256"]:
                raise ValueError("cell report digest mismatch")
            factory = directory / cell["factory_root"]
            if (
                Path(cell["factory_root"]).is_absolute()
                or ".." in Path(cell["factory_root"]).parts
                or factory.is_symlink()
                or not factory.resolve().is_relative_to(directory)
            ):
                raise ValueError("unsafe factory snapshot")
            if str(factory.resolve()) in cell_roots:
                raise ValueError("cell factories reused")
            cell_roots.add(str(factory.resolve()))
            checked = validator.validate(report_path, factory)
            if not checked["ok"]:
                problems.append(cell["domain_id"] + ":CORE_LIVE_EVIDENCE_INVALID")
            report = json.loads(report_path.read_text(), object_pairs_hook=_json_unique)
            if (
                report.get("witness_run_id") != witness
                or report.get("entry_mode") != mode
                or report["baseline"]["platform_source_digest"] != source
                or report["environment"]["runtime_ref"] != cell["runtime_ref"]
            ):
                raise ValueError("cell identity/source/witness mismatch")
            contract = report["authorization"]["contract_digest"]
            if cell["domain_id"] in domain_contracts and domain_contracts[cell["domain_id"]] != contract:
                raise ValueError("same domain contract changes with runtime")
            domain_contracts[cell["domain_id"]] = contract
            if report.get("domain_id") != cell["domain_id"]:
                raise ValueError("cell domain mismatch")
        domains = {a for a, _ in pairs}
        runtimes = {b for _, b in pairs}
        if len(domains) != 2 or len(runtimes) != 2 or pairs != {(d, r) for d in domains for r in runtimes}:
            raise ValueError("full two by two matrix required")
        expected_modes = {"help_only", "skill"}
        if any({entry_modes[(d, r)] for r in runtimes} != expected_modes for d in domains) or any(
            {entry_modes[(d, r)] for d in domains} != expected_modes for r in runtimes
        ):
            raise ValueError("both entry modes required for each domain and runtime")
        if len(set(domain_contracts.values())) != 2:
            raise ValueError("distinct domain contracts required")
        receipts = index["product_receipts"]
        expected = {p["machine_key"] for p in manifest["required_live_products"]}
        if {r["machine_key"] for r in receipts} != expected or len(receipts) != len(expected):
            raise ValueError("required products omitted or substituted")
        for receipt_ref in receipts:
            receipt_path = _file(directory, receipt_ref["path"])
            if hashlib.sha256(receipt_path.read_bytes()).hexdigest() != receipt_ref["sha256"]:
                raise ValueError("product digest mismatch")
            receipt = json.loads(receipt_path.read_text(), object_pairs_hook=_json_unique)
            if (
                receipt["schema_version"] != "mao-qualification-1"
                or receipt["kind"] != "machine"
                or receipt["subject_key"] != receipt_ref["machine_key"]
                or receipt["status"] != "passed"
                or receipt["profile"] != "live"
                or receipt["platform_digest"] != source
                or receipt["blocked_reasons"]
                or receipt.get("witness_run_id") != witness
            ):
                raise ValueError("product not qualified live")
            checks = receipt["checks"]
            if {c["id"] for c in checks} != {
                "identity",
                "health",
                "invoke",
                "outputs",
                "recovery",
                "network",
            } or not all(c.get("passed") is True and c.get("evidence_refs") for c in checks):
                raise ValueError("product checks incomplete")
            evidence_ids = _verify_artifacts(directory, receipt["evidence_index"])
            if len(checks) != 6 or any(not set(c["evidence_refs"]).issubset(evidence_ids) for c in checks):
                raise ValueError("unresolved product evidence reference")
        artifacts = index["artifacts"]
        if not {
            "deployment",
            "operator_permissions",
            "transport",
            "runtime_qualification",
            "help_only",
            "skill_usage",
        }.issubset({a["kind"] for a in artifacts}):
            raise ValueError("supplemental evidence category missing")
        _verify_artifacts(directory, artifacts)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError) as exc:
        problems.append("LIVE_INDEX_INVALID:" + type(exc).__name__)
    return {"ok": not problems, "scope": "evidence_integrity", "issues": problems}


def _verify_artifacts(directory: Path, artifacts: Any) -> set[str]:
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("artifact evidence missing")
    seen: set[str] = set()
    ids: set[str] = set()
    for item in artifacts:
        evidence_id = item["evidence_id"]
        if not isinstance(evidence_id, str) or not evidence_id.strip() or evidence_id in ids:
            raise ValueError("invalid/duplicate evidence id")
        ids.add(evidence_id)
        path = _file(directory, item["path"])
        if item["path"] in seen:
            raise ValueError("duplicate artifact")
        seen.add(item["path"])
        raw = path.read_bytes()
        if not raw or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("artifact empty or digest mismatch")
        if re.search(rb"(?i)(?:bearer\s+[a-z0-9._-]{16,}|\bsk-[a-z0-9_-]{16,}|-----BEGIN.*PRIVATE KEY)", raw):
            raise ValueError("possible credential in artifact")
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-index", type=Path)
    args = parser.parse_args()
    result = validate_live_index(args.live_index) if args.live_index else validate_design()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
