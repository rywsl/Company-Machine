"""M1-02 验收：Registry 快照与运行期求值环境。

模块还不存在时整个文件跳过。实现之后不得在测试体内 skip。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

pytest.importorskip("mao.snapshot")
pytest.importorskip("mao.base")

_base = pytest.importorskip("mao.base")
_errors = pytest.importorskip("mao.errors")
_snapshot = pytest.importorskip("mao.snapshot")
_values = pytest.importorskip("mao.values")

FactoryError = _errors.FactoryError
Snapshot = _snapshot.Snapshot
canonical_bytes = _base.canonical_bytes
digest_bytes = _base.digest_bytes
digest_file = _base.digest_file


def _helpers():
    path = Path(__file__).resolve().with_name("m1_data_helpers.py")
    spec = importlib.util.spec_from_file_location("m1_data_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


H = _helpers()


def _tree_epoch(root: Path) -> str:
    """与 snapshot.md 5.2 相同的独立计算，用来核对实现没有另写一套哈希。"""
    manifest = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        manifest[path.relative_to(root).as_posix()] = digest_file(path)
    tree = digest_bytes(canonical_bytes(manifest))
    return "e-" + tree.split(":", 1)[1][:16]


class _File:
    def __init__(self, path: str, digest: str, size: int, media_type: str) -> None:
        self.path = path
        self.digest = digest
        self.size = size
        self.media_type = media_type


class _Bean:
    def __init__(self, chars: int = 1, **kw: object) -> None:
        self.id = kw.get("id", "sha256:" + "ab" * 32)
        self.type = kw.get("type", "note.text@1.1")
        self.facts = kw.get("facts", {"chars": chars, "lines": 1})
        self.meta = kw.get("meta", {})
        self.payload = kw.get("payload", ())
        self.sealed = kw.get("sealed", False)


def test_notes_registry_is_clean(tmp_path: Path) -> None:
    """夹具 Registry 零 error、零 warning，并且能作为快照加载。"""
    root = H.write_notes_registry(tmp_path / "reg")
    snap = Snapshot.load(root)
    assert snap.result.ok
    assert snap.result.count("error") == 0
    assert snap.result.count("warning") == 0
    assert snap.root == root.resolve()


def test_load_rejects_invalid_registry(tmp_path: Path) -> None:
    root = H.write_registry(tmp_path / "bad", H.notes_registry_files(), constitution=False)
    with pytest.raises(FactoryError) as caught:
        Snapshot.load(root)
    assert caught.value.code == "REGISTRY_INVALID"
    assert any(d["code"] == "E-CONSTITUTION" for d in caught.value.detail["diags"])


def test_epoch_is_tree_hash(tmp_path: Path) -> None:
    root = H.write_notes_registry(tmp_path / "reg")
    snap = Snapshot.load(root)
    assert re.fullmatch(r"e-[0-9a-f]{16}", snap.epoch)
    assert snap.epoch == _tree_epoch(root)


def test_epoch_ignores_absolute_path_and_changes_with_content(tmp_path: Path) -> None:
    a = H.write_notes_registry(tmp_path / "a")
    b = H.write_notes_registry(tmp_path / "b")
    assert Snapshot.load(a).epoch == Snapshot.load(b).epoch
    assert Snapshot.load(a).epoch == _tree_epoch(a)
    (a / "notes").mkdir()
    (a / "notes" / "extra.txt").write_bytes(b"not a spec")
    again = Snapshot.load(a)
    assert again.epoch != Snapshot.load(b).epoch
    assert again.epoch == _tree_epoch(a)


def test_key_doc_spec_and_deep_copy(tmp_path: Path) -> None:
    snap = Snapshot.load(H.write_notes_registry(tmp_path / "reg"))
    assert snap.key("Station", "shout@1") == "Station:shout@1"
    assert snap.key("Station", "Station:shout@1") == "Station:shout@1"
    assert snap.key("Type", "note.text@1.1") == "Type:note.text@1.1"
    doc = snap.doc("Station:shout@1")
    doc["spec"]["recipe"] = "mutated"
    assert snap.doc("Station:shout@1")["spec"]["recipe"] == "shout"
    spec = snap.spec("Station:shout@1")
    spec["recipe"] = "mutated"
    assert snap.spec("Station:shout@1")["recipe"] == "shout"
    assert snap.spec("Type:note.text@1.1")["extends"] == "note@1"
    with pytest.raises(FactoryError) as caught:
        snap.doc("Station:missing@1")
    assert caught.value.code == "SPEC_NOT_FOUND"


def test_type_key_latest_and_retired(tmp_path: Path) -> None:
    snap = Snapshot.load(H.write_notes_registry(tmp_path / "reg"))
    assert snap.type_key("note.text@1") == "Type:note.text@1.1"
    assert snap.type_key("note@1") == "Type:note@1.0"
    assert snap.type_key("card@1") == "Type:card@1.0"
    with pytest.raises(FactoryError) as caught:
        snap.type_key("note.text@1.1")
    assert caught.value.code == "TYPE_NOT_FOUND"
    retired = Snapshot.load(H.write_notes_registry(tmp_path / "retired", retire_note_text_1_1=True))
    assert retired.type_key("note.text@1") == "Type:note.text@1.0"
    assert retired.always("note.text@1") == frozenset()


def test_is_subtype_matches_kernel(tmp_path: Path) -> None:
    snap = Snapshot.load(H.write_notes_registry(tmp_path / "reg"))
    assert snap.is_subtype("note.text@1", "note@1")
    assert snap.is_subtype("note.text@1", "note.text@1")
    assert snap.is_subtype("note@1", "note@1")
    assert not snap.is_subtype("note@1", "note.text@1")
    assert not snap.is_subtype("card@1", "note@1")
    assert not snap.is_subtype("missing@1", "note@1")
    assert not snap.is_subtype("note.text@1.1", "note@1")
    assert not snap.is_subtype("Type:note@1.0", "note@1")


def test_facts_validators_properties_always(tmp_path: Path) -> None:
    snap = Snapshot.load(H.write_notes_registry(tmp_path / "reg"))
    assert snap.facts_decl("note.text@1") == {"chars": "int", "lines": "int"}
    assert snap.facts_decl("note@1") == {"chars": "int", "lines": "int"}
    assert snap.facts_decl("card@1") == {}
    assert snap.always("note.text@1") == frozenset({"non_empty"})
    assert snap.always("note@1") == frozenset()
    validators = snap.validators("note.text@1")
    assert [item["name"] for item in validators] == ["count-text"]
    assert validators[0]["image"] == H.COUNT_TEXT_IMAGE
    assert validators[0]["run"] == ["count-text", "/bean/payload/note.txt"]
    validators.append({"name": "extra"})
    assert snap.validators("note.text@1")[0]["name"] == "count-text"
    assert snap.validators("note@1") == []
    props = snap.properties("note.text@1")
    assert set(props) == {"non_empty", "short"}
    assert props["non_empty"].source_text == "bean.facts.chars > 0"
    assert props["short"].source_text == "bean.facts.chars <= 280"
    assert props["non_empty"].evaluate(_values.property_env(_Bean(3))) is True
    assert props["non_empty"].evaluate(_values.property_env(_Bean(0))) is False
    with pytest.raises(FactoryError) as caught:
        snap.facts_decl("missing@1")
    assert caught.value.code == "TYPE_NOT_FOUND"


def test_compiled_covers_sites_and_envs(tmp_path: Path) -> None:
    files = H.notes_registry_files()
    files["runtimes/agent.yaml"] = (
        "apiVersion: mao/v1\n"
        "kind: OperatorRuntime\n"
        "metadata: {name: agent, version: 1}\n"
        "spec:\n"
        "  kind: agent-cli\n"
        "  models_allowed: [fake]\n"
        "  invoke:\n"
        '    argv: [agent, "{{ brief.task }}", "{{ limits.max_turns }}", "{{ limits.budget_usd }}"]\n'
        "  env:\n"
        '    MODEL: "{{ operator.model }}"\n'
    )
    snap = Snapshot.load(H.write_registry(tmp_path / "reg", files))
    sites = snap.result.extraction.sites
    assert sites
    kinds = {site.kind for site in sites}
    assert {"property", "accept", "approval", "recipe-template", "runtime-template"} <= kinds
    for site in sites:
        compiled = snap.compiled(site.subject)
        assert compiled.source_text == site.text
    bean = _Bean(4)
    assert snap.compiled("Type:note@1.0/properties.non_empty").evaluate(_values.property_env(bean)) is True
    out = _Bean(5)
    inputs = {"note": _Bean(3)}
    params = {"suffix": "!", "times": 2, "ratio": 0.5, "weight": 1, "loud": False}
    accept = snap.compiled("Station:shout@1/outputs.note.accept[0]")
    assert accept.evaluate(_values.accept_env(out, inputs, params)) is True
    assert accept.evaluate(_values.accept_env(_Bean(1), inputs, params)) is False
    parts = snap.compiled("Station:split@1/outputs.parts.accept[0]")
    assert parts.evaluate(_values.accept_env([_Bean(1), _Bean(1)], {}, {})) is True
    assert parts.evaluate(_values.accept_env([_Bean(1)], {}, {})) is False
    approval = snap.compiled("Station:shout@1/approval.in_run")
    low = _values.approval_env("in_run", {"kind": "cost", "summary": "s", "cost_usd": 0.5}, {}, {})
    high = _values.approval_env("in_run", {"kind": "cost", "summary": "s", "cost_usd": 0.6}, {}, {})
    assert approval.evaluate(low) is True
    assert approval.evaluate(high) is False
    rendered = snap.compiled("Machine:toolbox@1/recipes.shout.run[3]").render(
        _values.recipe_env({}, {"suffix": "!"})
    )
    assert rendered == "!"
    runtime = _values.runtime_env(
        {"task": "hello", "factory_contract": "c"},
        {"max_turns": 4, "timeout_s": 30, "budget_usd": 1.5},
        {"model": "fake", "runtime": "agent@1"},
    )
    assert snap.compiled("OperatorRuntime:agent@1/invoke.argv[1]").render(runtime) == "hello"
    assert snap.compiled("OperatorRuntime:agent@1/invoke.argv[2]").render(runtime) == "4"
    assert snap.compiled("OperatorRuntime:agent@1/env.MODEL").render(runtime) == "fake"


def test_compiled_unknown_subject(tmp_path: Path) -> None:
    snap = Snapshot.load(H.write_notes_registry(tmp_path / "reg"))
    with pytest.raises(FactoryError) as caught:
        snap.compiled("Station:shout@1/nope")
    assert caught.value.code == "SUBJECT_UNKNOWN"


def test_bean_and_inputs_value(tmp_path: Path) -> None:
    payload = (_File("note.txt", "sha256:" + "cd" * 32, 2, "text/plain"),)
    bean = _Bean(2, meta={"title": "t", "tags": ["x"]}, payload=payload, sealed=False)
    value = _values.bean_value(bean)
    assert value["id"] == bean.id
    assert value["type"] == "note.text@1.1"
    assert value["facts"] == {"chars": 2, "lines": 1}
    assert value["payload"] == [
        {"path": "note.txt", "digest": "sha256:" + "cd" * 32, "size": 2, "media_type": "text/plain"}
    ]
    value["facts"]["chars"] = 9
    value["meta"]["tags"].append("y")
    assert bean.facts["chars"] == 2
    assert bean.meta["tags"] == ["x"]
    unsealed = _Bean(facts={"chars": 7, "lines": 1}, sealed=False)
    assert _values.bean_value(unsealed)["facts"] == {"chars": 7, "lines": 1}
    scalar = _values.inputs_value({"note": bean})
    assert scalar["note"]["facts"]["chars"] == 2
    listed = _values.inputs_value({"notes": [bean, _Bean(4)]})
    assert isinstance(listed["notes"], list)
    assert [item["facts"]["chars"] for item in listed["notes"]] == [2, 4]


def test_params_value_defaults_and_rejects(tmp_path: Path) -> None:
    snap = Snapshot.load(H.write_notes_registry(tmp_path / "reg"))
    filled = _values.params_value(snap, "shout@1", {"weight": 1})
    assert filled == {"suffix": "!", "times": 2, "ratio": 0.5, "weight": 1, "loud": False}
    again = _values.params_value(snap, "Station:shout@1", {"weight": 1.5, "loud": True})
    assert again["weight"] == 1.5
    assert again["loud"] is True
    given = {"weight": 1}
    _values.params_value(snap, "shout@1", given)
    assert given == {"weight": 1}
    cases = [
        ({}, "weight", "missing"),
        ({"weight": True}, "weight", "type"),
        ({"weight": 1, "ratio": 1}, "ratio", "type"),
        ({"weight": 1, "nope": 1}, "nope", "unknown"),
    ]
    for given, name, reason in cases:
        with pytest.raises(FactoryError) as caught:
            _values.params_value(snap, "shout@1", given)
        assert caught.value.code == "PARAM_INVALID"
        assert any(
            item["name"] == name and item["reason"] == reason for item in caught.value.detail["problems"]
        )


def test_envs_match_sites(tmp_path: Path) -> None:
    propose = _values.approval_env(
        "propose",
        {"summary": "s", "cost_estimate_usd": 1, "time_estimate_s": 2},
        {},
        {"weight": 1},
    )
    assert set(propose) == {"proposal", "inputs", "params"}
    assert propose["proposal"]["summary"] == "s"
    running = _values.approval_env("in_run", {"kind": "cost", "summary": "s", "cost_usd": 0.1}, {}, {})
    assert set(running) == {"request", "inputs", "params"}
    with pytest.raises(ValueError):
        _values.approval_env("later", {}, {}, {})
    params = {"suffix": "!"}
    recipe = _values.recipe_env({}, params)
    recipe["params"]["suffix"] = "x"
    assert params["suffix"] == "!"
    assert set(recipe) == {"inputs", "params"}

    class _Brief:
        task = "hello"
        factory_contract = "contract"

    limits = {"max_turns": 4, "timeout_s": 30, "budget_usd": 1.5}
    operator = {"model": "fake", "runtime": "agent@1"}
    via_attr = _values.runtime_env(_Brief(), limits, operator)
    via_dict = _values.runtime_env({"task": "hello", "factory_contract": "contract"}, limits, operator)
    assert via_attr == via_dict
    assert via_attr["limits"]["timeout_s"] == 30
    assert via_attr["operator"]["runtime"] == "agent@1"
    assert isinstance(via_attr["limits"]["budget_usd"], float)
