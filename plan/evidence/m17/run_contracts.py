#!/usr/bin/env python3
"""Run frozen exact M17 nodes through harness; never substitute design validation."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from verify_plan import ROOT, load_manifest, selected_groups, validate_design


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--task", choices=[f"M17-{i:02}" for i in range(1, 14)])
    choice.add_argument("--all", action="store_true")
    choice.add_argument("--plan", action="store_true", help="show exact offline nodes without running them")
    args = parser.parse_args()
    checked = validate_design()
    if not checked["ok"]:
        print(
            json.dumps(
                {
                    "exit": 1,
                    "total": 0,
                    "passed": 0,
                    "failed": 0,
                    "errors": 1,
                    "skipped": 0,
                    "design": checked,
                }
            )
        )
        return 1
    groups = selected_groups(load_manifest(), args.task)
    nodes = [n for g in groups for n in g["nodes"]]
    minimum = sum(g["minimum_cases"] for g in groups)
    if args.plan:
        print(
            json.dumps(
                {
                    "scope": "execution_plan_only",
                    "nodes": nodes,
                    "minimum_cases": minimum,
                    "live_included": False,
                }
            )
        )
        return 0
    label = args.task or "all-known-issues"
    command = [
        sys.executable,
        str(ROOT / "tools/harness.py"),
        "pytest",
        *nodes,
        "--basetemp=" + str(ROOT / ".factory/m17-repro" / label),
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    sys.stderr.write(result.stderr)
    try:
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        report = json.loads(lines[-1])
        required = ("total", "passed", "failed", "errors", "skipped")
        if not isinstance(report, dict) or any(
            isinstance(report.get(k), bool) or not isinstance(report.get(k), int) for k in required
        ):
            raise ValueError("bad harness result")
        ok = (
            result.returncode == 0
            and report["failed"] == report["errors"] == report["skipped"] == 0
            and report["passed"] >= minimum
        )
    except (ValueError, IndexError):
        report = {
            "exit": 1,
            "total": 0,
            "passed": 0,
            "failed": 0,
            "errors": 1,
            "skipped": 0,
            "failures": ["harness report missing or invalid"],
        }
        ok = False
    report.update(
        {
            "exit": 0 if ok else 1,
            "required_minimum_cases": minimum,
            "exact_nodes_requested": len(nodes),
            "scope": "offline_contract_execution",
        }
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
