"""M1-03 验收：CAS、Bean、封存、血缘、完整性。

模块还不存在时整个文件跳过。实现之后不得在测试体内 skip。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.warehouse")
pytest.importorskip("mao.base")

_base = pytest.importorskip("mao.base")
_context = pytest.importorskip("mao.context")
_db = pytest.importorskip("mao.db")
_errors = pytest.importorskip("mao.errors")
_warehouse = pytest.importorskip("mao.warehouse")

BeanDraft = _warehouse.BeanDraft
Warehouse = _warehouse.Warehouse
media_type_of = _warehouse.media_type_of
install_factoryctl = _context.install_factoryctl
Context = _context.Context
Database = _db.Database
FactoryError = _errors.FactoryError
FakeClock = _base.FakeClock
CounterIds = _base.CounterIds
canonical_bytes = _base.canonical_bytes
canonical_json = _base.canonical_json
digest_bytes = _base.digest_bytes
utc_iso = _base.utc_iso

_EMPTY = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_REPORT = "sha256:" + "ab" * 32


def _open(tmp_path: Path) -> tuple[Path, object, Warehouse]:
    root = tmp_path / "factory"
    db = Database(root / "factory.db")
    db.migrate()
    clock = FakeClock()
    return root, db, Warehouse(root, db, clock)


def _identity(type_: str, meta: dict, records: list[dict]) -> str:
    obj = {"type": type_, "meta": meta, "payload": sorted(records, key=lambda item: item["path"])}
    return digest_bytes(canonical_bytes(obj))


def _bean_json(bean: object) -> dict:
    return {
        "id": bean.id,
        "type": bean.type,
        "facts": bean.facts,
        "meta": bean.meta,
        "payload": [
            {"path": item.path, "digest": item.digest, "size": item.size, "media_type": item.media_type}
            for item in bean.payload
        ],
    }


def test_put_file_cas_layout_and_idempotent(tmp_path: Path) -> None:
    root, db, wh = _open(tmp_path)
    try:
        empty = tmp_path / "empty.bin"
        empty.write_bytes(b"")
        digest = wh.put_file(empty)
        assert digest == _EMPTY
        assert digest == digest_bytes(b"")
        hexpart = digest.split(":", 1)[1]
        assert wh.blob_path(digest) == root / "cas" / "sha256" / hexpart[:2] / hexpart[2:]
        assert wh.blob_path(digest).is_file()
        assert wh.put_file(empty) == digest
        blobs = list((root / "cas" / "sha256").rglob("*"))
        assert sum(1 for path in blobs if path.is_file()) == 1
    finally:
        db.close()


def test_bean_id_and_add_get(tmp_path: Path) -> None:
    root, db, _wh = _open(tmp_path)
    db.close()
    clock = FakeClock()
    db = Database(root / "factory.db")
    wh = Warehouse(root, db, clock)
    try:
        note = tmp_path / "note.txt"
        note.write_bytes(b"hi")
        draft = BeanDraft(type="note.text@1.1", meta={"title": "t"}, files={"note.txt": note})
        bean = wh.add(draft)
        assert bean.sealed is False
        assert bean.facts == {}
        assert bean.created_at == utc_iso(clock.now())
        assert bean.created_at == "2026-01-01T00:00:00.000Z"
        assert bean.payload[0].path == "note.txt"
        assert bean.payload[0].media_type == "text/plain"
        assert media_type_of("note.txt") == "text/plain"
        assert media_type_of("card.json") == "application/json"
        record = {
            "path": "note.txt",
            "digest": bean.payload[0].digest,
            "size": note.stat().st_size,
            "media_type": "text/plain",
        }
        assert bean.id == _identity("note.text@1.1", {"title": "t"}, [record])
        bean.meta["title"] = "changed"
        assert wh.get(bean.id).meta == {"title": "t"}
        clock.advance(5)
        again = wh.add(draft)
        assert again.id == bean.id
        assert again.created_at == bean.created_at
        with pytest.raises(FactoryError) as caught:
            wh.get("sha256:" + "00" * 32)
        assert caught.value.code == "BEAN_NOT_FOUND"
    finally:
        db.close()


def test_payload_path_and_type_rejected(tmp_path: Path) -> None:
    _root, db, wh = _open(tmp_path)
    try:
        note = tmp_path / "note.txt"
        note.write_bytes(b"hi")
        with pytest.raises(FactoryError) as caught:
            wh.add(BeanDraft(type="note.text@1.1", meta={}, files={"../x": note}))
        assert caught.value.code == "PAYLOAD_PATH_INVALID"
        with pytest.raises(FactoryError) as caught:
            wh.add(BeanDraft(type="note.text@1.1", meta={}, files={"a\\b.txt": note}))
        assert caught.value.code == "PAYLOAD_PATH_INVALID"
        with pytest.raises(FactoryError) as caught:
            wh.add(BeanDraft(type="note.text@1", meta={}, files={"note.txt": note}))
        assert caught.value.code == "BEAN_TYPE_INVALID"
        with pytest.raises(FactoryError) as caught:
            wh.blob_path("sha256:ABCD")
        assert caught.value.code == "DIGEST_INVALID"
        with pytest.raises(FactoryError) as caught:
            wh.get("sha256:" + "11" * 32)
        assert caught.value.code == "BEAN_NOT_FOUND"
    finally:
        db.close()


def test_seal_many_transaction_and_conflict(tmp_path: Path) -> None:
    _root, db, wh = _open(tmp_path)
    try:
        first = tmp_path / "a.txt"
        second = tmp_path / "b.txt"
        first.write_bytes(b"a")
        second.write_bytes(b"b")
        id1 = wh.add(BeanDraft(type="note.text@1.1", meta={}, files={"note.txt": first})).id
        id2 = wh.add(BeanDraft(type="note.text@1.1", meta={"n": 2}, files={"note.txt": second})).id
        missing = "sha256:" + "00" * 32
        with pytest.raises(FactoryError) as caught:
            wh.seal_many([(id1, {"chars": 1}), (missing, {})], job_id="j-0001", report_digest=_REPORT)
        assert caught.value.code == "BEAN_NOT_FOUND"
        assert wh.get(id1).sealed is False
        assert wh.get(id2).sealed is False
        wh.seal_many(
            [(id1, {"chars": 1, "lines": 1}), (id2, {"chars": 2, "lines": 1})],
            job_id="j-0001",
            report_digest=_REPORT,
        )
        assert wh.get(id1).sealed is True
        assert wh.get(id1).facts == {"chars": 1, "lines": 1}
        assert wh.get(id1).created_at == "2026-01-01T00:00:00.000Z"
        with pytest.raises(FactoryError) as caught:
            wh.seal_many([(id1, {"chars": 9})], job_id="j-0002", report_digest="sha256:" + "cd" * 32)
        assert caught.value.code == "FACTS_CONFLICT"
        assert wh.get(id1).facts == {"chars": 1, "lines": 1}
        wh.seal_many(
            [(id1, {"lines": 1, "chars": 1})],
            job_id="j-0002",
            report_digest="sha256:" + "cd" * 32,
        )
        assert wh.get(id1).facts == {"chars": 1, "lines": 1}
        assert wh.get(id1).sealed is True
    finally:
        db.close()


def test_lineage_up_and_down(tmp_path: Path) -> None:
    _root, db, wh = _open(tmp_path)
    try:
        src = tmp_path / "in.txt"
        dst = tmp_path / "out.txt"
        src.write_bytes(b"in")
        dst.write_bytes(b"out")
        id_in = wh.add(BeanDraft(type="note.text@1.1", meta={}, files={"note.txt": src})).id
        id_out = wh.add(BeanDraft(type="note.text@1.1", meta={"n": 1}, files={"note.txt": dst})).id
        assert wh.lineage(id_out, "up") == {"id": id_out, "direction": "up", "jobs": [], "beans": []}
        wh.record_lineage("j-0001", {"note": [id_in]}, {"note": [id_out]})
        up = wh.lineage(id_out, "up")
        down = wh.lineage(id_in, "down")
        assert up["beans"] == [id_in]
        assert down["beans"] == [id_out]
        assert up["jobs"] == [
            {"job_id": "j-0001", "inputs": {"note": [id_in]}, "outputs": {"note": [id_out]}}
        ]
        assert down["jobs"][0]["job_id"] == "j-0001"
        with pytest.raises(FactoryError) as caught:
            wh.record_lineage("j-0009", {"note": [id_in]}, {"note": ["sha256:" + "22" * 32]})
        assert caught.value.code == "BEAN_NOT_FOUND"
        assert "j-0009" not in [item["job_id"] for item in wh.lineage(id_in, "down")["jobs"]]
        with pytest.raises(FactoryError) as caught:
            wh.lineage("sha256:" + "33" * 32, "up")
        assert caught.value.code == "BEAN_NOT_FOUND"
    finally:
        db.close()


def test_materialize_layouts(tmp_path: Path) -> None:
    _root, db, wh = _open(tmp_path)
    try:
        note = tmp_path / "note.txt"
        note.write_bytes(b"hello")
        bean = wh.add(BeanDraft(type="note.text@1.1", meta={"title": "t"}, files={"note.txt": note}))
        port = tmp_path / "port"
        bean_dir = tmp_path / "bean"
        wh.materialize(bean.id, port, layout="port")
        wh.materialize(bean.id, bean_dir, layout="bean")
        assert (port / "note.txt").read_bytes() == b"hello"
        assert (bean_dir / "payload" / "note.txt").read_bytes() == b"hello"
        expected = canonical_bytes(_bean_json(wh.get(bean.id)))
        assert (port / "bean.json").read_bytes() == expected
        assert (bean_dir / "bean.json").read_bytes() == expected
        loaded = json.loads((port / "bean.json").read_text(encoding="utf-8"))
        assert loaded["id"] == bean.id
        assert loaded["facts"] == {}
        assert loaded["payload"][0]["media_type"] == "text/plain"
    finally:
        db.close()


def test_list_and_verify(tmp_path: Path) -> None:
    root, db, wh = _open(tmp_path)
    try:
        note = tmp_path / "note.txt"
        card = tmp_path / "card.json"
        note.write_bytes(b"n")
        card.write_bytes(b"{}")
        note_bean = wh.add(BeanDraft(type="note.text@1.1", meta={}, files={"note.txt": note}))
        card_bean = wh.add(BeanDraft(type="card@1.0", meta={"title": "c"}, files={"card.json": card}))
        assert note_bean.id not in [item.id for item in wh.list()]
        wh.seal_many([(note_bean.id, {"chars": 1})], job_id="j-0001", report_digest=_REPORT)
        listed = [item.id for item in wh.list(type_prefix="note.text")]
        assert note_bean.id in listed
        assert card_bean.id not in listed
        assert card_bean.id in [item.id for item in wh.list(type_prefix="card", sealed_only=False)]
        assert wh.verify() == []
        blob = wh.blob_path(note_bean.payload[0].digest)
        blob.write_bytes(blob.read_bytes() + b"x")
        assert any(item["kind"] == "digest_mismatch" for item in wh.verify())
        with db.tx() as conn:
            conn.execute(
                "UPDATE beans SET meta_json = ? WHERE id = ?",
                (canonical_json({"tampered": True}), note_bean.id),
            )
        problems = wh.verify()
        assert any(item["kind"] == "id_mismatch" and item["bean_id"] == note_bean.id for item in problems)
        assert root / "cas" / "sha256"
    finally:
        db.close()


def test_context_open_layout(tmp_path: Path) -> None:
    class _Provider:
        name = "local"

        def open(self, spec: object) -> None:
            raise AssertionError("Context.open 不应打开沙箱")

        def remove_volume(self, name: str) -> None:
            return None

    ctx = Context.open(tmp_path / "factory", provider=_Provider(), clock=FakeClock(), ids=CounterIds())
    try:
        assert ctx.root == (tmp_path / "factory").resolve()
        assert (ctx.root / "factory.db").is_file()
        assert (ctx.root / "cas").is_dir()
        assert (ctx.root / "jobs").is_dir()
        assert (ctx.root / "tmp").is_dir()
        assert (ctx.root / "tools" / "factory" / "bin").is_dir()
        assert ctx.warehouse is not None
        version = ctx.db.query("PRAGMA user_version")[0][0]
        assert version >= 1
    finally:
        ctx.close()


def test_install_factoryctl_normalizes_newlines(tmp_path: Path) -> None:
    source = tmp_path / "factoryctl.py"
    source.write_bytes(b"print('a')\r\nprint('b')\r\n")
    root = tmp_path / "root"
    (root / "tmp").mkdir(parents=True)
    (root / "tools" / "factory" / "bin").mkdir(parents=True)
    target = install_factoryctl(root, source)
    assert target is not None
    assert target.read_bytes() == b"print('a')\nprint('b')\n"
    assert install_factoryctl(root, source) == target
    assert install_factoryctl(tmp_path / "root2", tmp_path / "missing.py") is None
