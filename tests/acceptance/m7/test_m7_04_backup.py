"""M7-04 验收：备份、校验与恢复（spec/system/backup.md 第 7 章）。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("mao.backup")

from tests.acceptance.m2.world import seal_input  # noqa: E402
from tests.acceptance.m3.gov_world import START, git, open_world  # noqa: E402

SECRET = "BACKUP-SECRET-VALUE-7f3a"
DAY = 86400


def _world(tmp: Path):
    w = open_world(tmp)
    secrets = w.ctx.root / "secrets"
    (secrets / "values").mkdir(parents=True)
    (secrets / "manifest.json").write_text('{"ANTHROPIC_API_KEY": "model"}', encoding="utf-8")
    (secrets / "values" / "ANTHROPIC_API_KEY").write_text(SECRET, encoding="utf-8")
    (w.ctx.root / "console.token").write_text("c" * 40, encoding="utf-8")
    egress = w.ctx.root / "egress"
    egress.mkdir()
    (egress / "prices.json").write_text(
        '{"m": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}', encoding="utf-8"
    )
    return w


def _open(root: Path):
    from mao.base import FakeClock
    from mao.context import Context
    from mao.sandbox import LocalProvider
    from tests.fakes.fakelib import local_commands

    clock = FakeClock(START)
    return Context.open(
        root, provider=LocalProvider(root, commands=local_commands(), clock=clock), clock=clock
    )


def _files(dest: Path) -> list[str]:
    return sorted(
        p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file() and p.name != "manifest.json"
    )


def _problems(report: dict) -> set[tuple[str, str]]:
    return {(item["kind"], item["path"]) for item in report["problems"]}


def _code(fn, *args, **kwargs) -> str:
    from mao.errors import FactoryError

    with pytest.raises(FactoryError) as err:
        fn(*args, **kwargs)
    return err.value.code


def test_create_layout(tmp_path: Path) -> None:
    from mao.backup import create_backup
    from mao.base import digest_file

    w = _world(tmp_path)
    bean = seal_input(w.ctx, w.snapshot, "note.text@1")
    [ref] = w.ctx.warehouse.get(bean).payload
    dest = tmp_path / "bk"
    manifest = create_backup(w.ctx, dest)
    assert manifest["format"] == "mao-backup/1"
    assert json.loads((dest / "manifest.json").read_text(encoding="utf-8")) == manifest
    listed = [item["path"] for item in manifest["files"]]
    assert listed == _files(dest)
    for item in manifest["files"]:
        path = dest / item["path"]
        assert digest_file(path) == item["sha256"] and path.stat().st_size == item["size"], item["path"]
    hexd = ref.digest.split(":", 1)[1]
    for expected in (
        "factory.db",
        f"cas/sha256/{hexd[:2]}/{hexd[2:]}",
        "config/factory.json",
        "config/egress/prices.json",
        "config/secrets/manifest.json",
        f"epochs/{w.base}/resolutions.json",
        "registry.bundle",
    ):
        assert expected in listed, expected
    assert not any("secrets/values" in p or p.endswith("console.token") for p in listed)
    assert manifest["secrets"] == ["ANTHROPIC_API_KEY"]
    assert manifest["registry"]["refs"]["refs/factory/active"] == w.base
    for path in dest.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path
    kinds = [r["kind"] for r in w.ctx.db.query("SELECT kind FROM events WHERE kind LIKE 'backup.%'")]
    assert kinds == ["backup.created"]


def test_verify_detects_damage(tmp_path: Path, capsys) -> None:
    from mao.backup import create_backup, verify_backup
    from mao.cli import main

    w = _world(tmp_path)
    seal_input(w.ctx, w.snapshot, "note.text@1")
    dest = tmp_path / "bk"
    create_backup(w.ctx, dest)
    assert verify_backup(dest) == {"ok": True, "problems": []}

    flipped = tmp_path / "flipped"
    shutil.copytree(dest, flipped)
    blob = next(p for p in sorted((flipped / "cas").rglob("*")) if p.is_file())
    data = bytearray(blob.read_bytes())
    data[0] ^= 0x01
    blob.write_bytes(bytes(data))
    report = verify_backup(flipped)
    assert report["ok"] is False
    assert ("mismatch", blob.relative_to(flipped).as_posix()) in _problems(report)

    missing = tmp_path / "missing"
    shutil.copytree(dest, missing)
    (missing / "config" / "factory.json").unlink()
    assert ("missing", "config/factory.json") in _problems(verify_backup(missing))

    extra = tmp_path / "extra"
    shutil.copytree(dest, extra)
    (extra / "extra.txt").write_text("x", encoding="utf-8")
    assert _problems(verify_backup(extra)) == {("extra", "extra.txt")}

    capsys.readouterr()
    assert main(["--json", "backup", "verify", str(extra)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_restore_roundtrip(tmp_path: Path) -> None:
    from mao.backup import create_backup, restore_backup
    from mao.durable import LeaseStore
    from mao.governance import EpochStore

    w = _world(tmp_path)
    bean = seal_input(w.ctx, w.snapshot, "note.text@1")
    assert LeaseStore(w.ctx).acquire("serve", owner="old-host:1:o-0001", ttl_s=600)
    dest = tmp_path / "bk"
    create_backup(w.ctx, dest)
    root = tmp_path / "restored"
    report = restore_backup(dest, root)
    assert report["secrets_missing"] == ["ANTHROPIC_API_KEY"]
    assert report["epochs"] == [w.base]
    registry = Path(json.loads((root / "factory.json").read_text(encoding="utf-8"))["governance"]["registry"])
    assert registry.resolve() == (root / "registry").resolve()
    assert Path(report["registry"]).resolve() == registry.resolve()
    assert git(registry, "rev-parse", "refs/factory/active") == w.base
    assert not (root / "secrets" / "values").exists()
    assert not (root / "console.token").exists()
    ctx = _open(root)
    try:
        assert ctx.warehouse.get(bean).sealed
        ctx.warehouse.materialize(bean, tmp_path / "out", layout="bean")
        assert ctx.warehouse.verify() == []
        assert EpochStore(ctx).current() == w.base
        assert (root / "epochs" / w.base / "registry").is_dir()
        assert ctx.db.query("SELECT COUNT(*) AS n FROM leases")[0]["n"] == 0
        kinds = [r["kind"] for r in ctx.db.query("SELECT kind FROM events WHERE kind = 'backup.restored'")]
        assert kinds == ["backup.restored"]
    finally:
        ctx.close()


def test_restore_refuses(tmp_path: Path) -> None:
    from mao.backup import create_backup, restore_backup

    w = _world(tmp_path)
    dest = tmp_path / "bk"
    create_backup(w.ctx, dest)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("x", encoding="utf-8")
    assert _code(restore_backup, dest, occupied) == "BACKUP_TARGET_NOT_EMPTY"
    assert [p.name for p in occupied.iterdir()] == ["keep.txt"]
    bad = tmp_path / "bad"
    shutil.copytree(dest, bad)
    (bad / "config" / "factory.json").write_text("{}", encoding="utf-8")
    fresh = tmp_path / "fresh"
    assert _code(restore_backup, bad, fresh) == "BACKUP_CORRUPT"
    assert not fresh.exists()


def test_busy_and_unsupported(tmp_path: Path) -> None:
    from mao.backup import create_backup
    from mao.durable import LeaseStore

    w = _world(tmp_path)
    leases = LeaseStore(w.ctx)
    assert leases.acquire("gc", owner="other:1:o-0009", ttl_s=600)
    dest = tmp_path / "bk"
    assert _code(create_backup, w.ctx, dest) == "BACKUP_BUSY"
    assert not dest.exists()
    assert not (tmp_path / "bk.part").exists()
    assert leases.release("gc", owner="other:1:o-0009")
    (w.ctx.root / "backend-active.json").write_text(
        '{"digest": "sha256:0", "switched_at": "x"}', encoding="utf-8"
    )
    assert _code(create_backup, w.ctx, dest) == "BACKUP_UNSUPPORTED"
    assert not dest.exists()


def test_tombstones_backed_up(tmp_path: Path) -> None:
    from mao.backup import create_backup, restore_backup, verify_backup
    from mao.retention import RetentionConfig, collect

    w = _world(tmp_path)
    path = w.ctx.root / "factory.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["retention"] = {
        "beans": {"note.text@1": 7},
        "workspaces_days": None,
        "logs_days": None,
        "sessions_days": None,
    }
    path.write_text(json.dumps(config), encoding="utf-8")
    bean = seal_input(w.ctx, w.snapshot, "note.text@1")
    w.clock.advance(10 * DAY)
    report = collect(w.ctx, RetentionConfig.load(w.ctx.root))
    assert report.expired == [bean] and report.blobs_deleted == 1
    dest = tmp_path / "bk"
    create_backup(w.ctx, dest)
    assert verify_backup(dest)["ok"] is True
    root = tmp_path / "restored"
    restore_backup(dest, root)
    ctx = _open(root)
    try:
        assert ctx.warehouse.get(bean).expired_at is not None
        assert ctx.warehouse.verify() == []
    finally:
        ctx.close()


def test_cli_roundtrip(tmp_path: Path, capsys) -> None:
    from mao.cli import main

    w = _world(tmp_path)
    seal_input(w.ctx, w.snapshot, "note.text@1")
    dest = tmp_path / "bk"
    root = tmp_path / "restored"
    capsys.readouterr()
    assert main(["--root", str(w.ctx.root), "--json", "backup", "create", str(dest)]) == 0
    assert json.loads(capsys.readouterr().out)["format"] == "mao-backup/1"
    assert main(["--json", "backup", "verify", str(dest)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "problems": []}
    assert main(["--json", "backup", "restore", str(dest), str(root)]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["secrets_missing"] == ["ANTHROPIC_API_KEY"]
    assert (root / "factory.db").is_file()
