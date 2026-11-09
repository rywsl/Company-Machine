"""M1-06 验收：来料检验与出厂检验。

模块还不存在时整个文件跳过。实现之后不得在测试体内 skip。
测试自带 Provider，不连接 Docker。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("mao.gate")
pytest.importorskip("mao.base")

_base = pytest.importorskip("mao.base")
_db = pytest.importorskip("mao.db")
_errors = pytest.importorskip("mao.errors")
_gate = pytest.importorskip("mao.gate")
_snapshot = pytest.importorskip("mao.snapshot")
_warehouse = pytest.importorskip("mao.warehouse")

Gate = _gate.Gate
Snapshot = _snapshot.Snapshot
BeanDraft = _warehouse.BeanDraft
Warehouse = _warehouse.Warehouse
Database = _db.Database
FactoryError = _errors.FactoryError
FakeClock = _base.FakeClock
CounterIds = _base.CounterIds
canonical_bytes = _base.canonical_bytes
digest_bytes = _base.digest_bytes

_REPORT = "sha256:" + "ab" * 32


def _helpers():
    path = Path(__file__).resolve().with_name("m1_data_helpers.py")
    spec = importlib.util.spec_from_file_location("m1_data_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


H = _helpers()


class _Sandbox:
    def __init__(self, spec: object, script: Path) -> None:
        self.spec = spec
        self.script = script
        self.name = spec.name
        self.closed = False

    def exec(self, argv, *, timeout_s, stdout, stderr, env=None, workdir=None):
        mount = next(item for item in self.spec.mounts if item.target == "/bean")
        source = Path(mount.source)
        args = list(argv)
        if args and args[0] == "count-text":
            args = args[1:]
        translated = []
        for arg in args:
            if isinstance(arg, str) and arg.startswith("/bean"):
                translated.append(str(source / arg[len("/bean") :].lstrip("/")))
            else:
                translated.append(arg)
        proc = subprocess.run(
            [sys.executable, str(self.script), *translated],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        Path(stdout).write_bytes(proc.stdout)
        Path(stderr).write_bytes(proc.stderr)
        return SimpleNamespace(exit_code=proc.returncode, timed_out=False, duration_s=0.0)

    def close(self) -> None:
        self.closed = True


class _Provider:
    def __init__(self, script: Path) -> None:
        self.script = script
        self.name = "local"
        self.opened: list = []
        self.sandboxes: list[_Sandbox] = []

    def open(self, spec: object) -> _Sandbox:
        sandbox = _Sandbox(spec, self.script)
        self.opened.append(spec)
        self.sandboxes.append(sandbox)
        return sandbox

    def remove_volume(self, name: str) -> None:
        return None


def _stack(tmp_path: Path):
    root = tmp_path / "factory"
    db = Database(root / "factory.db")
    db.migrate()
    clock = FakeClock()
    warehouse = Warehouse(root, db, clock)
    snap = Snapshot.load(H.write_notes_registry(tmp_path / "reg"))
    script = H.write_count_text(tmp_path / "tools")
    provider = _Provider(script)
    gate = Gate(snap, warehouse, provider, clock, CounterIds(), workdir=tmp_path / "work")
    return db, warehouse, gate, provider


def _seal_note(warehouse: Warehouse, tmp_path: Path, name: str, text: bytes, facts: dict) -> object:
    path = tmp_path / name
    path.write_bytes(text)
    bean = warehouse.add(BeanDraft(type="note.text@1.1", meta={}, files={"note.txt": path}))
    warehouse.seal_many([(bean.id, facts)], job_id="j-0001", report_digest=_REPORT)
    return warehouse.get(bean.id)


def _write_out(out: Path, port: str, text: bytes, meta: dict | None = None, index: int | None = None) -> None:
    root = out / port if index is None else out / port / str(index)
    root.mkdir(parents=True)
    (root / "note.txt").write_bytes(text)
    if meta is not None:
        (root / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def test_check_inputs_rejects_unsealed(tmp_path: Path) -> None:
    db, warehouse, gate, provider = _stack(tmp_path)
    try:
        path = tmp_path / "note.txt"
        path.write_bytes(b"hi\n")
        bean = warehouse.add(BeanDraft(type="note.text@1.1", meta={}, files={"note.txt": path}))
        assert bean.sealed is False
        with pytest.raises(FactoryError) as caught:
            gate.check_inputs("Station:shout@1", {"note": bean.id})
        assert caught.value.code == "INTAKE_REJECTED"
        assert any(
            item["reason"] == "unsealed" and item["bean_id"] == bean.id
            for item in caught.value.detail["problems"]
        )
        missing = "sha256:" + "ff" * 32
        with pytest.raises(FactoryError) as caught:
            gate.check_inputs("shout@1", {"note": missing})
        assert caught.value.code == "INTAKE_REJECTED"
        assert any(item["reason"] == "not_found" for item in caught.value.detail["problems"])
        assert provider.opened == []
    finally:
        db.close()


def test_check_inputs_type_requires_cardinality(tmp_path: Path) -> None:
    db, warehouse, gate, _provider = _stack(tmp_path)
    try:
        card_path = tmp_path / "card.json"
        card_path.write_bytes(b"{}")
        card = warehouse.add(BeanDraft(type="card@1.0", meta={"title": "c"}, files={"card.json": card_path}))
        warehouse.seal_many([(card.id, {})], job_id="j-0001", report_digest=_REPORT)
        with pytest.raises(FactoryError) as caught:
            gate.check_inputs("shout@1", {"note": card.id})
        assert caught.value.code == "INTAKE_REJECTED"
        assert any(item["reason"] == "type" for item in caught.value.detail["problems"])
        empty = _seal_note(warehouse, tmp_path, "empty.txt", b"   \n", {"chars": 0, "lines": 1})
        with pytest.raises(FactoryError) as caught:
            gate.check_inputs("shout@1", {"note": empty.id})
        assert caught.value.code == "INTAKE_REJECTED"
        assert any(item["reason"] == "requires" for item in caught.value.detail["problems"])
        ok = _seal_note(warehouse, tmp_path, "ok.txt", b"hi\n", {"chars": 2, "lines": 1})
        with pytest.raises(FactoryError) as caught:
            gate.check_inputs("shout@1", {"note": [ok.id]})
        assert caught.value.code == "INTAKE_REJECTED"
        assert any(item["reason"] == "cardinality" for item in caught.value.detail["problems"])
    finally:
        db.close()


def test_check_inputs_ok_and_list(tmp_path: Path) -> None:
    db, warehouse, gate, provider = _stack(tmp_path)
    try:
        one = _seal_note(warehouse, tmp_path, "a.txt", b"hi\n", {"chars": 2, "lines": 1})
        two = _seal_note(warehouse, tmp_path, "b.txt", b"yo\n", {"chars": 2, "lines": 1})
        got = gate.check_inputs("shout@1", {"note": one.id})
        assert got["note"].id == one.id
        assert got["note"].sealed is True
        listed = gate.check_inputs("merge@1", {"notes": [one.id, two.id]})
        assert [item.id for item in listed["notes"]] == [one.id, two.id]
        assert provider.opened == []
    finally:
        db.close()


def test_inspect_pass_does_not_seal(tmp_path: Path) -> None:
    db, warehouse, gate, provider = _stack(tmp_path)
    try:
        incoming = _seal_note(warehouse, tmp_path, "in.txt", b"hi\n", {"chars": 2, "lines": 1})
        out = tmp_path / "out"
        _write_out(out, "note", b"hi!\n")
        report = gate.inspect(
            "shout@1",
            job_id="j-0007",
            attempt=1,
            out_dir=out,
            inputs={"note": incoming},
            params={"weight": 1},
        )
        assert report.verdict == "pass"
        assert report.feedback == ""
        assert report.station == "Station:shout@1"
        assert report.attempt == 1
        ids = report.outputs["note"]
        assert len(ids) == 1
        assert "extra" not in report.outputs
        stored = warehouse.get(ids[0])
        assert stored.sealed is False
        assert report.facts[ids[0]] == {"chars": 3, "lines": 1}
        assert any(
            item.stage == "validator" and item.name == "count-text" and item.ok for item in report.checks
        )
        assert any(
            item.stage == "property" and item.name == "non_empty" and item.ok for item in report.checks
        )
        assert any(item.stage == "accept" and item.name == "accept[0]" and item.ok for item in report.checks)
        assert any(
            item.stage == "consistency" and item.name == "meta_schema" and item.ok for item in report.checks
        )
        opened = provider.opened[0]
        assert opened.image == H.COUNT_TEXT_IMAGE
        assert opened.network == "none"
        assert opened.name.startswith("mao-gate-")
        assert len(opened.mounts) == 1
        assert opened.mounts[0].target == "/bean"
        assert opened.mounts[0].readonly is True
        source = Path(opened.mounts[0].source)
        assert source.is_absolute()
        assert (source / "payload" / "note.txt").read_bytes() == b"hi!\n"
        assert (source / "bean.json").is_file()
        assert all(sandbox.closed for sandbox in provider.sandboxes)
    finally:
        db.close()


def test_inspect_validator_reject_and_infra(tmp_path: Path) -> None:
    db, warehouse, gate, _provider = _stack(tmp_path)
    try:
        incoming = _seal_note(warehouse, tmp_path, "in.txt", b"hi\n", {"chars": 2, "lines": 1})
        rejected = tmp_path / "rejected"
        _write_out(rejected, "note", b"see REJECT\n")
        report = gate.inspect(
            "shout@1",
            job_id="j-0008",
            attempt=1,
            out_dir=rejected,
            inputs={"note": incoming},
            params={"weight": 1},
        )
        assert report.verdict == "reject"
        assert any(
            item.stage == "validator" and not item.ok and "内容包含 REJECT" in item.detail
            for item in report.checks
        )
        assert "内容包含 REJECT" in report.feedback
        assert "validator" in report.feedback
        crashed = tmp_path / "crashed"
        _write_out(crashed, "note", b"CRASH\n")
        report = gate.inspect(
            "shout@1",
            job_id="j-0008",
            attempt=2,
            out_dir=crashed,
            inputs={"note": incoming},
            params={"weight": 1},
        )
        assert report.verdict == "error"
        assert any(
            item.stage == "validator" and not item.ok and item.detail.startswith("设施故障：")
            for item in report.checks
        )
        assert "设施故障：" in report.feedback
        bad = tmp_path / "badjson"
        _write_out(bad, "note", b"BADJSON\n")
        report = gate.inspect(
            "shout@1",
            job_id="j-0008",
            attempt=3,
            out_dir=bad,
            inputs={"note": incoming},
            params={"weight": 1},
        )
        assert report.verdict == "error"
        assert any(item.detail.startswith("设施故障：") for item in report.checks)
    finally:
        db.close()


def test_inspect_property_accept_consistency(tmp_path: Path) -> None:
    db, warehouse, gate, _provider = _stack(tmp_path)
    try:
        incoming = _seal_note(warehouse, tmp_path, "in.txt", b"hi\n", {"chars": 2, "lines": 1})
        blank = tmp_path / "blank"
        _write_out(blank, "note", b"   \n")
        report = gate.inspect(
            "shout@1",
            job_id="j-0009",
            attempt=1,
            out_dir=blank,
            inputs={"note": incoming},
            params={"weight": 1},
        )
        assert report.verdict == "reject"
        assert any(
            item.stage == "property" and item.name == "non_empty" and not item.ok for item in report.checks
        )
        long_in = _seal_note(warehouse, tmp_path, "long.txt", b"hello\n", {"chars": 5, "lines": 1})
        short = tmp_path / "short"
        _write_out(short, "note", b"x")
        report = gate.inspect(
            "shout@1",
            job_id="j-0009",
            attempt=2,
            out_dir=short,
            inputs={"note": long_in},
            params={"weight": 1},
        )
        assert report.verdict == "reject"
        assert any(
            item.stage == "accept" and item.name == "accept[0]" and not item.ok for item in report.checks
        )
        assert any(
            item.stage == "property" and item.name == "non_empty" and item.ok for item in report.checks
        )
        bad_meta = tmp_path / "meta"
        _write_out(bad_meta, "note", b"hi!\n", meta={"nope": 1})
        report = gate.inspect(
            "shout@1",
            job_id="j-0009",
            attempt=3,
            out_dir=bad_meta,
            inputs={"note": incoming},
            params={"weight": 1},
        )
        assert report.verdict == "reject"
        assert any(
            item.stage == "consistency" and item.name == "meta_schema" and not item.ok
            for item in report.checks
        )
        assert "consistency" in report.feedback
        assert "meta_schema" in report.feedback
    finally:
        db.close()


def test_inspect_list_port(tmp_path: Path) -> None:
    db, warehouse, gate, _provider = _stack(tmp_path)
    try:
        incoming = _seal_note(warehouse, tmp_path, "in.txt", b"hi\n", {"chars": 2, "lines": 1})
        good = tmp_path / "good"
        _write_out(good, "parts", b"ab", index=0)
        _write_out(good, "parts", b"cd", index=1)
        report = gate.inspect(
            "split@1",
            job_id="j-0010",
            attempt=1,
            out_dir=good,
            inputs={"note": incoming},
            params={},
        )
        assert report.verdict == "pass"
        assert len(report.outputs["parts"]) == 2
        assert all(not warehouse.get(item).sealed for item in report.outputs["parts"])
        assert all(report.facts[item]["chars"] == 2 for item in report.outputs["parts"])
        one = tmp_path / "one"
        _write_out(one, "parts", b"ab", index=0)
        report = gate.inspect(
            "split@1",
            job_id="j-0010",
            attempt=2,
            out_dir=one,
            inputs={"note": incoming},
            params={},
        )
        assert report.verdict == "reject"
        assert any(
            item.stage == "accept" and item.name == "accept[0]" and not item.ok for item in report.checks
        )
        gap = tmp_path / "gap"
        _write_out(gap, "parts", b"ab", index=0)
        _write_out(gap, "parts", b"cd", index=2)
        report = gate.inspect(
            "split@1",
            job_id="j-0010",
            attempt=3,
            out_dir=gap,
            inputs={"note": incoming},
            params={},
        )
        assert report.verdict == "reject"
        assert any(item.stage == "structure" and not item.ok for item in report.checks)
    finally:
        db.close()


def test_admit_external(tmp_path: Path) -> None:
    db, warehouse, gate, provider = _stack(tmp_path)
    try:
        card = tmp_path / "card.json"
        card.write_bytes(b"{}")
        report, bean = gate.admit_external("card@1", {"card.json": card}, {"title": "ok"})
        assert report.verdict == "pass"
        assert bean is not None
        assert bean.sealed is False
        assert warehouse.get(bean.id).sealed is False
        assert warehouse.get(bean.id).type == "card@1.0"
        assert provider.opened == []
        rejected, missing = gate.admit_external("card@1", {"card.json": card}, {})
        assert rejected.verdict == "reject"
        assert missing is None
        assert any(
            item.stage == "consistency" and item.name == "meta_schema" and not item.ok
            for item in rejected.checks
        )
    finally:
        db.close()


def test_report_json_and_digest(tmp_path: Path) -> None:
    db, warehouse, gate, _provider = _stack(tmp_path)
    try:
        incoming = _seal_note(warehouse, tmp_path, "in.txt", b"hi\n", {"chars": 2, "lines": 1})
        out = tmp_path / "out"
        _write_out(out, "note", b"hi!\n")
        report = gate.inspect(
            "shout@1",
            job_id="j-0011",
            attempt=4,
            out_dir=out,
            inputs={"note": incoming},
            params={"weight": 1},
        )
        body = report.to_json()
        assert set(body) >= {
            "job_id",
            "station",
            "attempt",
            "verdict",
            "outputs",
            "facts",
            "checks",
            "feedback",
        }
        assert body["job_id"] == "j-0011"
        assert body["attempt"] == 4
        assert body["verdict"] == report.verdict
        assert body["checks"][0]["stage"] in {
            "structure",
            "validator",
            "property",
            "accept",
            "consistency",
        }
        assert report.digest() == digest_bytes(canonical_bytes(body))
        assert report.digest() == report.digest()
    finally:
        db.close()
