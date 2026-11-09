"""规范对照后仍须由实现保证的行为。

每条测试都调用已交付的语言入口（compile_expr / evaluate / Program / parse_type / explain），
不重写语言，也不改已有一致性用例。语义以 spec/mrl/mrl-1.0.md 为准。
"""

from __future__ import annotations

import pytest

from mrl.cli import main
from mrl.errors import CODES, EvalError, StaticError
from mrl.expr import compile_expr, compile_template, evaluate
from mrl.lexer import MAX_SOURCE
from mrl.rules import MAX_TUPLES, Program
from mrl.types import DYN, FLOAT, NUM, parse_type


def test_in_limit_chains_do_not_escape_as_host_exceptions() -> None:
    """左结合链不增加嵌套深度，源长度以内必须得到 MRL 结果而不是宿主异常。"""
    assert evaluate("+".join(["1"] * 800)) == 800
    assert evaluate("true" + " && true" * 1000) is True
    compiled = compile_expr("x" + ".a" * 1000, env={"x": parse_type("dyn")})
    assert compiled.type == DYN
    nested = None
    for _ in range(20):
        nested = {"a": nested}
    assert evaluate("x" + ".a" * 20, {"x": nested}) is None


def test_numeric_conversion_rejects_trailing_newline() -> None:
    with pytest.raises(EvalError) as ei:
        evaluate('int("12\\n")')
    assert ei.value.code == "MRL-CONVERT"
    with pytest.raises(EvalError) as ei:
        evaluate('float("1.5\\n")')
    assert ei.value.code == "MRL-CONVERT"
    assert evaluate('int("12")') == 12
    assert evaluate('float("1.5")') == 1.5


def test_round_uses_exact_half_away_from_zero() -> None:
    assert evaluate("round(-0.49999999999999994)") == 0
    assert evaluate("round(0.49999999999999994)") == 0
    assert evaluate("round(2.5)") == 3
    assert evaluate("round(-2.5)") == -3
    assert evaluate("round(-0.5)") == -1


def test_minmax_order_follows_spec_including_unspecified_tie() -> None:
    src = """
module t.
@input rel e(k: string, v: any).
rel lo(k: string, v: any).
lo(K, min(V)) :- e(K, V).
"""
    program = Program.from_sources([("t.mrl", src)])
    forward = program.evaluate({"e": [["a", 1], ["a", 1.0]]}).rows("lo")
    reverse = program.evaluate({"e": [["a", 1.0], ["a", 1]]}).rows("lo")
    for rows in (forward, reverse):
        assert len(rows) == 1
        assert rows[0][0] == "a"
        assert rows[0][1] == 1
        assert type(rows[0][1]) in (int, float)
    ordered = program.evaluate({"e": [["a", 3], ["a", 1]]}).rows("lo")
    flipped = program.evaluate({"e": [["a", 1], ["a", 3]]}).rows("lo")
    assert ordered == flipped == [("a", 1)]


def test_equal_numbers_unify_across_columns_as_specified() -> None:
    src = """
module t.
@input rel a(v: int).
@input rel b(v: float).
rel both(v: float).
both(X) :- a(X), b(X).
@input rel e(p: int, q: float).
rel paired(p: int).
paired(X) :- e(X, X).
"""
    model = Program.from_sources([("t.mrl", src)]).evaluate({"a": [[1]], "b": [[1.0]], "e": [[1, 1.0]]})
    assert model.rows("both") == [(1.0,)]
    assert model.rows("paired") == []


def test_index_and_field_definedness_follow_spec() -> None:
    env = {"r": parse_type("{opt?: int}")}
    field = compile_expr("r.opt", env=env)
    assert any(w.code == "MRL-MAYBE-ABSENT" for w in field.warnings)
    index = compile_expr('r["opt"]', env=env)
    assert all(w.code != "MRL-MAYBE-ABSENT" for w in index.warnings)
    with pytest.raises(EvalError) as ei:
        index.evaluate({"r": {}})
    assert ei.value.code == "MRL-NO-FIELD"


def test_negative_float_constant_in_recursive_rule_follows_spec() -> None:
    with pytest.raises(StaticError) as ei:
        Program.from_sources(
            [
                (
                    "bad.mrl",
                    "module t.\nrel n(v: float).\nn(-1.5) :- n(X).\n",
                )
            ]
        )
    assert ei.value.code == "MRL-INVENTION"
    program = Program.from_sources(
        [
            (
                "ok.mrl",
                "module t.\nrel n(v: int).\nn(0).\nn(-1) :- n(X).\n",
            )
        ]
    )
    assert (-1,) in program.evaluate({}).rows("n")
    helper = Program.from_sources(
        [
            (
                "via.mrl",
                "module t.\nrel c(v: float).\nc(-1.5).\nrel n(v: float).\nn(V) :- c(V).\n",
            )
        ]
    )
    assert helper.evaluate({}).rows("n") == [(-1.5,)]


def test_error_code_phase_metadata_matches_chapter_9(capsys: pytest.CaptureFixture[str]) -> None:
    assert CODES["MRL-DUP-KEY"][0] == "both"
    assert CODES["MRL-UNDEFINED"][0] == "both"
    unused = CODES["MRL-UNUSED"][1]
    assert "永远为空" in unused and "没有使用" in unused
    assert main(["explain", "MRL-DUP-KEY"]) == 0
    assert "both" in capsys.readouterr().out
    assert main(["explain", "MRL-UNDEFINED"]) == 0
    assert "both" in capsys.readouterr().out


def test_template_diagnostic_position_and_per_interpolation_length() -> None:
    with pytest.raises(StaticError) as ei:
        compile_template("line1\nline2 {{ nope }}")
    diag = ei.value.diagnostics[0]
    assert diag.code == "MRL-UNDEFINED"
    assert (diag.span.line, diag.span.col) == (2, 10)
    long_inner = "1+" * (MAX_SOURCE // 2 + 8) + "1"
    with pytest.raises(StaticError) as ei:
        compile_template("{{ " + long_inner + " }}")
    assert ei.value.code == "MRL-LIMIT"
    rendered = compile_template(("x" * (MAX_SOURCE + 8)) + "{{ 1 }}").render({})
    assert rendered.endswith("1")
    assert len(rendered) > MAX_SOURCE


def test_string_fact_row_is_rejected() -> None:
    src = """
module t.
@input rel e(a: string, b: string, c: string).
rel o(a: string, b: string, c: string).
o(A, B, C) :- e(A, B, C).
"""
    program = Program.from_sources([("t.mrl", src)])
    with pytest.raises(EvalError) as ei:
        program.evaluate({"e": ["abc"]})
    assert ei.value.code == "MRL-FACT"
    assert program.evaluate({"e": [["a", "b", "c"]]}).rows("o") == [("a", "b", "c")]


def test_step_budget_limits_steps_not_value_size() -> None:
    expr = '"x"'
    for _ in range(10):
        expr = f"[{expr}].map(y, y + y)[0]"
    assert evaluate(expr) == "x" * 1024
    compiled = compile_expr("1 + 1")
    with pytest.raises(EvalError) as ei:
        compiled.evaluate({}, budget=1)
    assert ei.value.code == "MRL-COST"


def test_tuple_budget_includes_host_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_TUPLES == 5_000_000
    src = """
module t.
@input rel e(v: int).
rel o(v: int).
o(V) :- e(V).
"""
    program = Program.from_sources([("t.mrl", src)])
    monkeypatch.setattr("mrl.rules.MAX_TUPLES", 2)
    with pytest.raises(EvalError) as ei:
        program.evaluate({"e": [[1], [2], [3]]})
    assert ei.value.code == "MRL-COST"
    assert "宿主事实" in ei.value.message


def test_static_typing_of_dyn_follows_spec() -> None:
    env = {"d": parse_type("dyn"), "xs": parse_type("list<int>")}
    assert str(compile_expr("d + xs", env=env).type) == "list<int>"
    assert compile_expr("d + 1.0", env={"d": DYN}).type == NUM
    assert compile_expr("d * 1.0", env={"d": DYN}).type == FLOAT


def test_aggregate_compare_error_has_location() -> None:
    src = """
module t.
@input rel e(v: any).
rel m(v: any).
m(min(V)) :- e(V).
"""
    program = Program.from_sources([("t.mrl", src)])
    with pytest.raises(EvalError) as ei:
        program.evaluate({"e": [[1], ["a"]]})
    assert ei.value.code == "MRL-TYPE"
    assert ei.value.span.line >= 1
    assert ei.value.span.col >= 1
    assert ei.value.span.offset != 0 or ei.value.span.end != 0


def test_parse_type_follows_type_string_grammar() -> None:
    assert str(parse_type("{a: int, b?: string, ...}")) == "{a: int, b?: string, ...}"
    with pytest.raises(ValueError):
        parse_type("{a: int,}")
    with pytest.raises(ValueError):
        parse_type("{a: int, }")
    with pytest.raises(ValueError):
        parse_type("{a:\tint}")
    with pytest.raises(ValueError):
        parse_type("{名字: int}")
    assert str(parse_type("list<{path: string, size: int}>")) == "list<{path: string, size: int}>"
