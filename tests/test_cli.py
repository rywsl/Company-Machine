"""mrl 命令行：子命令与退出码。"""

from __future__ import annotations

import json

import pytest

from mrl.cli import EXIT_FAIL, EXIT_OK, EXIT_RUNTIME, EXIT_STATIC, EXIT_USAGE, main
from mrl.factory.paths import spec_root

EXAMPLE = str(spec_root() / "examples" / "registry")


def run(capsys, *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def test_eval_value_and_type(capsys) -> None:
    code, out = run(capsys, "eval", "1 + 2.5", "--json")
    assert code == EXIT_OK
    data = json.loads(out)
    assert data["value"] == 3.5 and data["type"] == "float"


def test_eval_with_env(capsys) -> None:
    code, out = run(capsys, "eval", "x * 2", "--types", "{x: int}", "--env", "{x: 21}")
    assert (code, out.strip()) == (EXIT_OK, "42")


def test_eval_static_error(capsys) -> None:
    code, out = run(capsys, "eval", "1 +")
    assert code == EXIT_STATIC and "MRL-SYNTAX" in out


def test_eval_runtime_error(capsys) -> None:
    code, out = run(capsys, "eval", "1 / 0")
    assert code == EXIT_RUNTIME and "MRL-DIV-ZERO" in out


def test_template(capsys) -> None:
    code, out = run(capsys, "template", "hi {{ name }}", "--env", "{name: mrl}")
    assert (code, out.strip()) == (EXIT_OK, "hi mrl")


def test_check_and_run_rules(capsys, tmp_path) -> None:
    f = tmp_path / "m.mrl"
    f.write_text(
        "module t.\nrel e(a: int, b: int).\ne(1, 2).\ne(2, 3).\nrel p(a: int, b: int).\n"
        "p(A, B) :- e(A, B).\np(A, C) :- e(A, B), p(B, C).\n",
        encoding="utf-8",
    )
    assert run(capsys, "check", str(f))[0] == EXIT_OK
    code, out = run(capsys, "run", str(f), "--rel", "p")
    assert code == EXIT_OK
    assert out.split() == ["p(1,", "2).", "p(1,", "3).", "p(2,", "3)."]


def test_registry_check_example(capsys) -> None:
    code, out = run(capsys, "registry", "check", EXAMPLE, "--json")
    assert code == EXIT_OK
    assert json.loads(out)["ok"] is True


def test_registry_check_missing_dir(capsys) -> None:
    assert main(["registry", "check", "definitely/not/here"]) == EXIT_USAGE


def test_resolve(capsys) -> None:
    code, out = run(
        capsys, "resolve", EXAMPLE, "--from", "subtitle.vtt@1", "--to", "subtitle.srt@1", "--json"
    )
    assert code == EXIT_OK
    assert json.loads(out)["chain"] == ["subs-convert@1"]
    code, _ = run(
        capsys,
        "resolve",
        EXAMPLE,
        "--from",
        "subtitle.vtt@1",
        "--to",
        "subtitle.srt@1",
        "--requires",
        "non_empty",
    )
    assert code == EXIT_FAIL


@pytest.mark.parametrize("code", ["E-CONTRACT", "W-EXPR", "MRL-COST"])
def test_explain(capsys, code: str) -> None:
    rc, out = run(capsys, "explain", code)
    assert rc == EXIT_OK and code in out


def test_explain_unknown(capsys) -> None:
    assert main(["explain", "E-NOPE"]) == EXIT_USAGE


def test_usage_error(capsys) -> None:
    assert main(["no-such-command"]) == EXIT_USAGE
