"""verify 入口的 spec、验收测试和外部检查契约。"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from tools import harness

REPO = Path(__file__).resolve().parents[1]


def _summary(**overrides: int | list[str]) -> dict[str, object]:
    summary: dict[str, object] = {
        "exit": 0,
        "total": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "failures": [],
        "skips": [],
    }
    summary.update(overrides)
    return summary


def _write_mapping_fixture(root: Path, *, spec_text: str, test_text: str | None = None) -> None:
    (root / "spec" / "system").mkdir(parents=True)
    acceptance = root / "tests" / "acceptance" / "m1"
    acceptance.mkdir(parents=True)
    (root / "spec" / "system" / "example.md").write_text(spec_text, encoding="utf-8")
    if test_text is not None:
        (acceptance / "test_example.py").write_text(test_text, encoding="utf-8")


def _mapping_spec(*names: str) -> str:
    rows = "\n".join(f"| `{name}` | `test_expected` |" for name in names)
    return f"## 7. 验收测试\n\n{rows}\n\n## 8. 错误示范与正确示范\n"


def _run_verify_with_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    acceptance: dict[str, object] | None = None,
    policy: dict[str, object] | None = None,
    mrl_problems: dict[str, list[str]] | None = None,
) -> int:
    monkeypatch.setattr(harness, "doc_problems", lambda: [])
    monkeypatch.setattr(harness, "_acceptance_files", lambda: ["tests/acceptance/m1/test_example.py"])
    summaries = {
        "spec 关联的验收测试": acceptance or _summary(),
        "代码策略测试": policy or _summary(),
    }
    monkeypatch.setattr(harness, "run_pytest", lambda _paths, label: summaries[label])
    problems = mrl_problems or {}
    monkeypatch.setattr(
        harness,
        "_verify_mrl_command",
        lambda label, _argv, _predicate: problems.get(label, []),
    )
    return harness.cmd_verify(argparse.Namespace())


def test_current_repository_verify_inputs_are_consistent() -> None:
    assert harness.doc_problems() == []
    acceptance = harness._acceptance_files()
    assert acceptance
    assert all((REPO / path).is_file() for path in acceptance)


def test_parser_exposes_only_verify() -> None:
    parser = harness.build_parser()
    subparsers = next(action for action in parser._actions if action.dest == "cmd")

    assert set(subparsers.choices) == {"verify"}
    assert parser.parse_args(["verify"]).cmd == "verify"
    assert harness.main(["status"]) == harness.EXIT_USAGE


def test_spec_missing_acceptance_file_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_mapping_fixture(tmp_path, spec_text=_mapping_spec("tests/acceptance/m1/test_example.py"))
    monkeypatch.setattr(harness, "ROOT", tmp_path)

    problems = harness._acceptance_problems()

    assert any("引用的验收文件" in problem and "不存在" in problem for problem in problems)


def test_spec_missing_test_function_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_mapping_fixture(
        tmp_path,
        spec_text=_mapping_spec("tests/acceptance/m1/test_example.py"),
        test_text="def test_actual():\n    pass\n",
    )
    monkeypatch.setattr(harness, "ROOT", tmp_path)

    problems = harness._acceptance_problems()

    assert any("test_expected 不在" in problem for problem in problems)


def test_unreferenced_acceptance_test_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_mapping_fixture(
        tmp_path,
        spec_text=_mapping_spec("tests/acceptance/m1/test_example.py").replace(
            "`test_expected`", "`test_actual`"
        ),
        test_text="def test_actual():\n    pass\n\ndef test_extra():\n    pass\n",
    )
    monkeypatch.setattr(harness, "ROOT", tmp_path)

    problems = harness._acceptance_problems()

    assert any("test_extra 没有写进" in problem for problem in problems)


@pytest.mark.parametrize(
    ("field", "value"),
    [("failed", 1), ("errors", 1), ("skipped", 1)],
)
def test_acceptance_failure_error_or_skip_fails_verify(
    monkeypatch: pytest.MonkeyPatch, field: str, value: int
) -> None:
    summary = _summary(**{field: value})
    if field == "failed":
        summary["failures"] = ["test_example"]
    if field == "skipped":
        summary["skips"] = ["test_example"]

    result = _run_verify_with_stubs(monkeypatch, acceptance=summary)

    assert result == harness.EXIT_FAIL


def test_mrl_conformance_failure_fails_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_verify_with_stubs(
        monkeypatch,
        mrl_problems={"MRL conformance": ["MRL conformance 未通过"]},
    )

    assert result == harness.EXIT_FAIL


def test_mrl_coverage_failure_fails_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_verify_with_stubs(
        monkeypatch,
        mrl_problems={"MRL coverage": ["MRL coverage 未通过"]},
    )

    assert result == harness.EXIT_FAIL


def test_code_policy_failure_fails_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_verify_with_stubs(monkeypatch, policy=_summary(failed=1, failures=["policy"]))

    assert result == harness.EXIT_FAIL


def test_verify_does_not_require_or_write_task_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "ROOT", tmp_path)
    result = _run_verify_with_stubs(monkeypatch)

    assert result == harness.EXIT_OK
    assert not (tmp_path / "plan" / "state.json").exists()
    assert not (tmp_path / "plan" / "gates.yaml").exists()
    assert not (tmp_path / "plan" / "tasks.yaml").exists()
    assert not (tmp_path / ".factory" / "harness-cache").exists()
    assert not (tmp_path / ".factory" / "harness-runs").exists()
