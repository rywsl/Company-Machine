"""工厂 profile：示例 Registry、内核规则的静态性质、覆盖率、转换解析的确定性。"""

from __future__ import annotations

import itertools

from mrl.factory.check import check_registry, kernel_program
from mrl.factory.conformance import coverage
from mrl.factory.functions import resolve
from mrl.factory.paths import kernel_sources, reference_constitution_dir, spec_root
from mrl.values import from_py, to_py

EXAMPLE = spec_root() / "examples" / "registry"


def test_example_registry_is_clean() -> None:
    r = check_registry(EXAMPLE)
    assert [d.format() for d in r.diags] == []
    assert r.exit_code(strict=True) == 0
    assert r.stats["edges"] == 9


def test_example_constitution_matches_reference() -> None:
    ref = (reference_constitution_dir() / "invariants.mrl").read_text(encoding="utf-8")
    copy = (EXAMPLE / "constitution" / "invariants.mrl").read_text(encoding="utf-8")
    assert copy == ref, "示例 Registry 中的宪法副本必须与参考宪法一致"


def test_kernel_compiles_without_warnings() -> None:
    program = kernel_program()
    assert [w.format() for w in program.warnings] == []


def test_every_diag_code_in_kernel_is_registered() -> None:
    program = kernel_program()
    table = {c for c, _s, _t in program.evaluate({}).rows("code")}
    used = set()
    for cr in program.rules:
        if cr.head_rel == "diag" and cr.rule.head.terms and hasattr(cr.rule.head.terms[0], "value"):
            used.add(cr.rule.head.terms[0].value)
    assert used - table == set()


def test_kernel_sources_have_module_headers() -> None:
    for name, text in kernel_sources():
        assert f"module kernel.{name.split('/')[-1].removesuffix('.mrl')}." in text, name


def test_conformance_covers_every_code_and_rule() -> None:
    c = coverage([str(spec_root() / "mrl" / "conformance" / "factory")])
    assert c.unexercised_codes == []
    assert c.unfired_rules == []


def _conv(cid: str, src: str, dst: str, lossless: bool = True, cost: float = 0.0, p50: float = 0.0) -> dict:
    return {
        "id": cid,
        "from": src,
        "to": dst,
        "lossless": lossless,
        "requires": [],
        "guarantees": [],
        "preserves": [],
        "cost": cost,
        "p50": p50,
    }


def test_resolution_is_independent_of_library_order() -> None:
    lib = [
        _conv("a@1", "x@1", "z@1", lossless=False),
        _conv("b@1", "x@1", "y@1"),
        _conv("c@1", "y@1", "z@1"),
        _conv("d@1", "x@1", "z@1", lossless=True, cost=1.0),
        _conv("e@1", "x@1", "z@1", lossless=True, cost=1.0),
    ]
    subs = [[t, t] for t in ("x@1", "y@1", "z@1")]
    results = set()
    for perm in itertools.permutations(lib):
        res = to_py(
            resolve(from_py(list(perm)), from_py(subs), from_py([]), "x@1", from_py([]), "z@1", from_py([]))
        )
        results.add(tuple(res["chain"]))
    # 无损优先 → 步数最少：d 与 e 都是一步无损，成本相同、耗时相同，按 id 取 d
    assert results == {("d@1",)}


def _check_json_for(files: dict[str, str]) -> str:
    import json

    from mrl.factory.check import check_loaded
    from mrl.factory.registry import registry_from_texts

    result = check_loaded(registry_from_texts("<determinism>", files))
    data = result.to_json()
    data.pop("root", None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def test_check_output_is_independent_of_file_order() -> None:
    """文件读入顺序不同（例如不同文件系统的目录遍历顺序），检查输出必须逐字节相同（确定性）。"""
    import random

    from mrl.factory.registry import read_tree

    files = read_tree(EXAMPLE)
    # 制造几个诊断，让输出不只是“全部通过”
    files = dict(files)
    files["types/broken.yaml"] = (
        "apiVersion: mao/v1\nkind: Type\nmetadata: {name: broken, version: '1.0'}\nspec: {extends: nothing@1}\n"
    )
    files["stations/vertical.yaml"] = files["stations/vertical.yaml"].replace("has_audio, not_silent]", "]")
    baseline = _check_json_for(files)
    assert '"E-' in baseline, "用例应当产生诊断"
    items = list(files.items())
    rng = random.Random(20261109)
    for _ in range(3):
        rng.shuffle(items)
        assert _check_json_for(dict(items)) == baseline
