"""M7-03 验收：保留期与回收（spec/system/retention.md 第 7 章）。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("mao.retention")

from tests.acceptance.m3.gov_world import open_world  # noqa: E402

DAY = 86400
RET = {"beans": {"note.text@1": 7}, "workspaces_days": 7, "logs_days": None, "sessions_days": 7}


def _configure(w, retention: dict) -> None:
    path = w.ctx.root / "factory.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["retention"] = retention
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")


def _world(tmp: Path, retention: dict | None = None):
    w = open_world(tmp)
    _configure(w, retention or RET)
    return w


def _gc(w, **kwargs):
    from mao.retention import RetentionConfig, collect

    return collect(w.ctx, RetentionConfig.load(w.ctx.root), **kwargs)


def _bean(w, type_ref: str, content: bytes, meta: dict | None = None) -> str:
    from mao.warehouse import BeanDraft

    type_key = w.snapshot.type_key(type_ref)
    type_id = type_key.split(":", 1)[1]
    spec = w.snapshot.spec(type_key)
    item = (spec.get("payload") or [{"path": "payload.bin"}])[0]
    rel = str(item.get("path", "payload.bin")).replace("*", "item.txt")
    src = w.ctx.root / "incoming" / hashlib.sha256(content + type_id.encode()).hexdigest()[:16] / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(content)
    facts = {
        name: {"int": 1, "float": 1.0, "string": "x", "bool": True}.get(typ, 1)
        for name, typ in w.snapshot.facts_decl(type_id).items()
    }
    bean = w.ctx.warehouse.add(BeanDraft(type=type_id, meta=meta or {}, files={rel: src}))
    w.ctx.warehouse.seal_many([(bean.id, facts)], job_id=None, report_digest="sha256:" + "0" * 64)
    return bean.id


def _blob(w, bean_id: str) -> Path:
    [ref] = w.ctx.warehouse.get(bean_id).payload
    return w.ctx.warehouse.blob_path(ref.digest)


def _upper(w):
    from mao.runner import Runner

    runner = Runner(w.ctx, w.snapshot)
    return runner, w.snapshot.key("Station", "upper@1")


def test_config_validation(tmp_path: Path, capsys) -> None:
    from mao.cli import main
    from mao.errors import FactoryError
    from mao.retention import RetentionConfig

    w = open_world(tmp_path)
    assert main(["--root", str(w.ctx.root), "gc"]) == 4
    _configure(w, RET)
    config = RetentionConfig.load(w.ctx.root)
    assert config.beans == {"note.text@1": 7}
    assert (config.workspaces_days, config.logs_days, config.sessions_days) == (7, None, 7)
    for bad in ({"beans": {"note.text@1": -1}}, {"beans": {}, "bogus": 1}, {"beans": {}, "logs_days": 1.5}):
        _configure(w, bad)
        with pytest.raises(FactoryError) as err:
            RetentionConfig.load(w.ctx.root)
        assert err.value.code == "RETENTION_CONFIG_INVALID"


def test_expire_by_type_age(tmp_path: Path) -> None:
    w = _world(tmp_path)
    old_text = _bean(w, "note.text@1", b"old text\n")
    old_raw = _bean(w, "note.raw@1", b"old raw\n")
    old_blob = _blob(w, old_text)
    digest = w.ctx.warehouse.get(old_text).payload[0].digest
    w.clock.advance(10 * DAY)
    new_text = _bean(w, "note.text@1", b"new text\n")
    report = _gc(w)
    assert report.expired == [old_text]
    assert (report.blobs_deleted, report.bytes_freed) == (1, len(b"old text\n"))
    tomb = w.ctx.warehouse.get(old_text)
    assert tomb.expired_at is not None and tomb.sealed
    assert tomb.payload[0].digest == digest
    assert not old_blob.exists()
    assert _blob(w, new_text).exists() and _blob(w, old_raw).exists()
    assert w.ctx.warehouse.get(old_raw).expired_at is None
    rows = w.ctx.db.query("SELECT data FROM events WHERE kind = 'bean.expired'")
    assert [json.loads(r["data"])["bean_id"] for r in rows] == [old_text]
    assert old_text not in [b.id for b in w.ctx.warehouse.list()]
    assert old_text in [b.id for b in w.ctx.warehouse.list(include_expired=True)]


def test_pin_quarantine_in_use_kept(tmp_path: Path) -> None:
    w = _world(tmp_path)
    pinned = _bean(w, "note.text@1", b"pinned\n")
    w.ctx.warehouse.label(pinned, "pin", "yes", by="human:acceptance")
    quarantined = _bean(w, "note.text@1", b"quarantined\n")
    w.ctx.db.query(
        "INSERT INTO quarantines (bean_id, reason, by, at, source) VALUES (?, 'acceptance', 'human', ?, 'manual')",
        (quarantined, "2026-11-10T00:00:00.000Z"),
    )
    used = _bean(w, "note.text@1", b"used\n")
    free = _bean(w, "note.text@1", b"free\n")
    runner, station = _upper(w)
    job = runner.submit(station, {"note": used}, {})
    w.clock.advance(10 * DAY)
    report = _gc(w)
    assert report.expired == [free]
    assert report.kept == {"pinned": 1, "in_use": 1, "quarantined": 1}
    done = runner.run(job.id)
    assert done.state.value == "SEALED", done.error
    again = _gc(w)
    assert again.expired == [used]
    [out] = done.outputs["note"]
    assert w.ctx.warehouse.get(out).expired_at is None


def test_shared_blob_survives(tmp_path: Path) -> None:
    w = _world(tmp_path)
    first = _bean(w, "note.text@1", b"same\n")
    w.clock.advance(10 * DAY)
    # Bean id 是 type、meta、payload 的摘要（warehouse.md 第 3 章）：meta 不同才是另一个 Bean，payload 摘要相同
    second = _bean(w, "note.text@1", b"same\n", meta={"copy": 2})
    assert first != second
    assert _blob(w, first) == _blob(w, second)
    report = _gc(w)
    assert report.expired == [first] and report.blobs_deleted == 0
    assert _blob(w, second).exists()
    w.clock.advance(10 * DAY)
    report = _gc(w)
    assert report.expired == [second] and report.blobs_deleted == 1
    assert not _blob(w, second).exists()


def test_tombstone_rejected(tmp_path: Path) -> None:
    from mao.errors import FactoryError

    w = _world(tmp_path)
    old = _bean(w, "note.text@1", b"old\n")
    w.clock.advance(10 * DAY)
    assert _gc(w).expired == [old]
    runner, station = _upper(w)
    before = w.ctx.db.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"]
    with pytest.raises(FactoryError) as err:
        runner.submit(station, {"note": old}, {})
    assert err.value.code == "BEAN_EXPIRED"
    assert w.ctx.db.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"] == before
    with pytest.raises(FactoryError) as err:
        w.ctx.warehouse.materialize(old, tmp_path / "out", layout="bean")
    assert err.value.code == "BEAN_EXPIRED"
    assert w.ctx.warehouse.verify() == []
    assert _bean(w, "note.text@1", b"old\n") == old
    assert w.ctx.warehouse.get(old).expired_at is not None


def test_add_restores_deleted_blob(tmp_path: Path) -> None:
    w = _world(tmp_path)
    old = _bean(w, "note.text@1", b"again\n")
    blob = _blob(w, old)
    w.clock.advance(10 * DAY)
    assert _gc(w).blobs_deleted == 1
    assert not blob.exists()
    fresh = _bean(w, "note.text@1", b"again\n", meta={"copy": 2})
    assert fresh != old
    assert w.ctx.warehouse.get(fresh).expired_at is None
    assert _blob(w, fresh) == blob
    assert blob.read_bytes() == b"again\n"
    assert w.ctx.warehouse.verify() == []


def test_dry_run(tmp_path: Path) -> None:
    w = _world(tmp_path)
    _bean(w, "note.text@1", b"one\n")
    _bean(w, "note.text@1", b"two\n")
    w.clock.advance(10 * DAY)
    _bean(w, "note.text@1", b"three\n")

    def state():
        beans = w.ctx.db.query("SELECT id, expired_at FROM beans ORDER BY id")
        events = w.ctx.db.query("SELECT COUNT(*) AS n FROM events")[0]["n"]
        blobs = sorted(p.name for p in (w.ctx.root / "cas").rglob("*") if p.is_file())
        return [dict(r) for r in beans], events, blobs

    before = state()
    dry = _gc(w, dry_run=True).to_json()
    assert state() == before
    assert dry["dry_run"] is True and len(dry["expired"]) == 2 and dry["blobs_deleted"] == 2
    real = _gc(w).to_json()
    assert real.pop("dry_run") is False
    dry.pop("dry_run")
    assert real == dry


def test_directories(tmp_path: Path) -> None:
    w = _world(tmp_path)
    runner, station = _upper(w)
    note = _bean(w, "note.text@1", b"dirs\n")
    done = runner.run(runner.submit(station, {"note": note}, {}).id)
    assert done.state.value == "SEALED", done.error
    attempt = w.ctx.root / "jobs" / done.id / str(done.attempt)
    (attempt / "workspace").mkdir(parents=True, exist_ok=True)
    (attempt / "workspace" / "draft.txt").write_text("x", encoding="utf-8")
    (attempt / "logs").mkdir(parents=True, exist_ok=True)
    (attempt / "logs" / "keep.txt").write_text("x", encoding="utf-8")
    pending = runner.submit(station, {"note": note}, {})
    pending_ws = w.ctx.root / "jobs" / pending.id / "1" / "workspace"
    pending_ws.mkdir(parents=True, exist_ok=True)
    for sid, state, ended in (("fs-0001", "ok", "2026-11-10T00:00:00.000Z"), ("fs-0002", "running", None)):
        w.ctx.db.query(
            "INSERT INTO foreman_sessions (id, wake_id, role, runtime, actor, model, epoch, state, cost_usd, "
            "started_at, ended_at) VALUES (?, 'wk-0001', 'foreman', 'OperatorRuntime:fake-agent@1', "
            "'fake-agent@1', 'm', 'x', ?, 0, '2026-11-10T00:00:00.000Z', ?)",
            (sid, state, ended),
        )
        logs = w.ctx.root / "foreman" / "sessions" / sid / "logs"
        logs.mkdir(parents=True)
        (logs / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
    w.clock.advance(10 * DAY)
    report = _gc(w, by="acceptance")
    assert report.workspaces == [done.id]
    assert report.sessions == ["fs-0001"]
    assert report.logs == []
    assert not (attempt / "workspace").exists()
    assert (attempt / "logs" / "keep.txt").exists()
    assert pending_ws.is_dir()
    assert not (w.ctx.root / "foreman" / "sessions" / "fs-0001").exists()
    assert (w.ctx.root / "foreman" / "sessions" / "fs-0002" / "logs" / "transcript.jsonl").exists()
    assert w.ctx.db.query("SELECT COUNT(*) AS n FROM foreman_sessions")[0]["n"] == 2
