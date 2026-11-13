"""M14-01 验收：工位 admit 的 schema、抽取、表达式位置与诊断（spec/system/admit.md 第 4、5.1、5.2 节）。

全部经 `mrl registry check ROOT --json`（mrl.cli.main）检查运行期构造的 Registry。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.acceptance.m14.admit_world import build_registry, registry_check  # noqa: E402

STAGED = ROOT / "tests" / "acceptance" / "m14" / "admit.conformance.yaml"
INSTALLED = ROOT / "spec" / "mrl" / "conformance" / "factory" / "admit.yaml"


def _set_admit(registry: Path, station: str, lines: list[str]) -> None:
    path = registry / "stations" / f"{station}.yaml"
    text = path.read_text(encoding="utf-8")
    head, rest = text.split("  admit:\n", 1)
    tail = rest[rest.index("  outputs:") :]
    body = "".join(f"    - {json.dumps(line)}\n" for line in lines)
    path.write_text(head + "  admit:\n" + body + tail, encoding="utf-8", newline="\n")


def _diags(result: dict, code: str) -> list[dict]:
    return [d for d in result["diagnostics"] if d["code"] == code]


def test_registry_with_admit_is_clean(tmp_path: Path) -> None:
    code, result = registry_check(build_registry(tmp_path), "--strict")
    assert code == 0, result["diagnostics"]
    assert result["counts"] == {"error": 0, "warning": 0, "info": result["counts"]["info"]}


def test_admit_field_errors_are_e_expr(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    _set_admit(
        registry,
        "flash",
        [
            "inputs.firmware.facts.target == inputs.car.facts.model",
            "inputs.wheel.facts.size == 17",
            "inputs.car.facts.model",
        ],
    )
    code, result = registry_check(registry)
    assert code == 1
    found = {d["subject"]: d for d in _diags(result, "E-EXPR")}
    assert set(found) == {f"Station:flash@1/admit[{i}]" for i in range(3)}, result["diagnostics"]
    assert "MRL-NO-FIELD" in found["Station:flash@1/admit[0]"]["message"]
    assert "MRL-NO-FIELD" in found["Station:flash@1/admit[1]"]["message"]
    assert found["Station:flash@1/admit[2]"]["path"] == "spec.admit[2]"
    assert found["Station:flash@1/admit[0]"]["file"] == "stations/flash.yaml"


def test_admit_sees_params_and_list_ports(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    _set_admit(registry, "flash-fixed", ["inputs.firmware.facts.target_model == params.modle"])
    _set_admit(registry, "flash-many", ["inputs.parts.all(p, p.facts.platfrom == 'A')"])
    code, result = registry_check(registry)
    assert code == 1
    subjects = sorted(d["subject"] for d in _diags(result, "E-EXPR"))
    assert subjects == ["Station:flash-fixed@1/admit[0]", "Station:flash-many@1/admit[0]"], result[
        "diagnostics"
    ]


def test_admit_on_meta_warns(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    _set_admit(
        registry,
        "flash",
        [
            "has(inputs.firmware.meta.target_model) && inputs.firmware.meta.target_model == inputs.car.facts.model",
            "inputs.firmware.facts.platform == inputs.car.facts.platform",
        ],
    )
    code, result = registry_check(registry)
    assert code == 0, result["diagnostics"]
    [warn] = _diags(result, "W-ADMIT-META")
    assert warn["severity"] == "warning" and warn["subject"] == "Station:flash@1/admit[0]"
    assert [d["code"] for d in result["diagnostics"] if d["severity"] != "info"] == ["W-ADMIT-META"]
    strict, _ = registry_check(registry, "--strict")
    assert strict == 1


def test_admit_optional_port_needs_has(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    path = registry / "stations" / "flash.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "{type: fw.image@1, requires: [signed]}", "{type: fw.image@1, requires: [signed], optional: true}"
        ),
        encoding="utf-8",
        newline="\n",
    )
    _set_admit(
        registry,
        "flash",
        [
            "inputs.firmware.facts.platform == inputs.car.facts.platform",
            "!has(inputs.firmware) || inputs.firmware.facts.target_model == inputs.car.facts.model",
        ],
    )
    code, result = registry_check(registry)
    warns = _diags(result, "W-EXPR")
    assert [d["subject"] for d in warns] == ["Station:flash@1/admit[0]"], result["diagnostics"]
    assert "MRL-MAYBE-ABSENT" in warns[0]["message"]


def test_admit_without_inputs_is_e_port(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    path = registry / "stations" / "pull-fw.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("  inputs: {}\n", "  inputs: {}\n  admit: [\"params.platform != ''\"]\n"),
        encoding="utf-8",
        newline="\n",
    )
    code, result = registry_check(registry)
    assert code == 1
    [diag] = [d for d in _diags(result, "E-PORT") if d["subject"].endswith("/admit")]
    assert diag["subject"] == "Station:pull-fw@1/admit"
    assert diag["message"] == "没有输入端口的工位不能写 admit"
    assert diag["path"] == "spec.admit"


def test_admit_schema(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    path = registry / "stations" / "flash.yaml"
    original = path.read_text(encoding="utf-8")
    for bad in ("  admit: []\n", "  admit: [1]\n", "  admit: \"inputs.car.facts.model == 'X1'\"\n"):
        head, rest = original.split("  admit:\n", 1)
        tail = rest[rest.index("  outputs:") :]
        path.write_text(head + bad + tail, encoding="utf-8", newline="\n")
        code, result = registry_check(registry)
        assert code == 1, bad
        schema = [d for d in _diags(result, "E-SCHEMA") if d["file"] == "stations/flash.yaml"]
        assert schema and all("admit" in d["path"] for d in schema), (bad, result["diagnostics"])


def test_conformance_cases_installed() -> None:
    from mrl.conformance import load_cases, run_case

    assert INSTALLED.exists(), (
        "把 tests/acceptance/m14/admit.conformance.yaml 原样复制到 spec/mrl/conformance/factory/admit.yaml"
    )
    assert INSTALLED.read_bytes().replace(b"\r\n", b"\n") == STAGED.read_bytes().replace(b"\r\n", b"\n")
    cases = list(load_cases([str(INSTALLED)]))
    assert len(cases) >= 6
    failed = [(r.case, r.detail) for r in map(run_case, cases) if not r.ok]
    assert failed == []
