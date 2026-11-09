"""M2-09 验收：回执配方与两阶段静态检查（spec/system/connectors.md 5.7、approvals.md 5.7）。

诊断码还没登记时整个文件跳过。登记之后，测试调用已交付的 mrl 检查。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mrl.factory.check import kernel_program

ROOT = Path(__file__).resolve().parents[3]
CASE_IDS = (
    "receipt-recipe-missing",
    "receipt-recipe-no-run",
    "receipt-recipe-has-outputs",
    "receipt-template-typo",
    "phases-propose-without-rule",
    "approval-propose-without-phases",
)


def _codes() -> set[str]:
    return {row[0] for row in kernel_program().evaluate({}).rows("code")}


if "E-RECEIPT" not in _codes() or "E-APPROVAL" not in _codes():
    pytest.skip("M2-09 的诊断码尚未登记", allow_module_level=True)


def _conformance() -> dict:
    proc = subprocess.run(
        ["uv", "run", "mrl", "conformance", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_conformance_cases_pass() -> None:
    from mrl.conformance import load_cases, run
    from mrl.factory.paths import spec_root

    paths = [str(spec_root() / "mrl" / "conformance")]
    present = {case.id for case in load_cases(paths)}
    for case_id in CASE_IDS:
        assert case_id in present
    by_id = {item.case: item for item in run(paths)}
    for case_id in CASE_IDS:
        assert by_id[case_id].ok is True
    payload = _conformance()
    failed = {item["case"].split("/")[-1] for item in payload["failures"]}
    assert failed.isdisjoint(CASE_IDS)
    assert payload["total"] >= 702


def test_example_registry_still_clean() -> None:
    proc = subprocess.run(
        ["uv", "run", "mrl", "registry", "check", "spec/examples/registry", "--strict", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    body = json.loads(proc.stdout)
    assert body["ok"] is True
    assert body["counts"]["error"] == 0
    assert body["counts"]["warning"] == 0


def test_coverage_complete() -> None:
    proc = subprocess.run(
        ["uv", "run", "mrl", "coverage", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    body = json.loads(proc.stdout)
    assert body["unexercised_codes"] == []
    assert body["unfired_rules"] == []
    assert "E-RECEIPT" in _codes()
