"""M3-04 验收：调和器（spec/system/reconciler.md 第 7 章）。"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("mao.reconcile")
pytest.importorskip("mao.governance")

from mao.governance import EpochStore  # noqa: E402
from mao.reconcile import Reconciler  # noqa: E402
from mao.warehouse import BeanDraft  # noqa: E402
from tests.acceptance.m2.world import FIXTURE, open_factory, seal_input  # noqa: E402

CANARY = "CANARY-SECRET-VALUE-0123456789"


def _audit(tmp_path: Path):
    ctx, snapshot, _runner, _clock = open_factory(tmp_path, FIXTURE)
    return ctx, snapshot, Reconciler(ctx, EpochStore(ctx))


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8"
    )
    return proc.stdout.strip()


def test_cas_mismatch_is_i1(tmp_path: Path) -> None:
    ctx, snapshot, reconciler = _audit(tmp_path)
    bean_id = seal_input(ctx, snapshot, "note.text@1")
    bean = ctx.warehouse.get(bean_id)
    blob = ctx.warehouse.blob_path(bean.payload[0].digest)
    original = blob.read_bytes()
    blob.write_bytes(original[:-1] + bytes([original[-1] ^ 0x01]))
    corrupted = blob.read_bytes()
    findings = reconciler.audit_once(timeout_s=5)
    assert findings
    assert any(item.invariant == "I1" for item in findings)
    assert blob.read_bytes() == corrupted
    assert blob.read_bytes() != original


def test_lineage_gap_is_i2(tmp_path: Path) -> None:
    ctx, snapshot, reconciler = _audit(tmp_path)
    bean_id = seal_input(ctx, snapshot, "note.text@1")
    missing = "sha256:" + "ab" * 32
    # record_lineage 对不存在的输入抛 BEAN_NOT_FOUND。悬挂证明只能在外键关闭时写入。
    conn = sqlite3.connect(ctx.db.path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO lineage (job_id, direction, port, ordinal, bean_id) VALUES (?, 'out', 'note', 0, ?)",
            ("j-0001", bean_id),
        )
        conn.execute(
            "INSERT INTO lineage (job_id, direction, port, ordinal, bean_id) VALUES (?, 'in', 'note', 0, ?)",
            ("j-0001", missing),
        )
        conn.commit()
    finally:
        conn.close()
    findings = reconciler.audit_once(timeout_s=5)
    assert findings
    assert any(item.invariant == "I2" and item.detail.get("missing_input") == missing for item in findings)


def test_unsealed_input_is_i3(tmp_path: Path) -> None:
    ctx, snapshot, reconciler = _audit(tmp_path)
    draft = ctx.warehouse.add(
        BeanDraft(type=snapshot.type_key("note.text@1").split(":", 1)[1], meta={}, files={})
    )
    assert draft.sealed is False
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO jobs (id, station, epoch, state, attempt, inputs, params, outputs, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "j-unsealed",
                snapshot.key("Station", "upper@1"),
                snapshot.epoch,
                "RUNNING",
                1,
                json.dumps({"note": [draft.id]}),
                "{}",
                "{}",
                "2026-01-01T00:00:00.000Z",
                "2026-01-01T00:00:00.000Z",
            ),
        )
    findings = reconciler.audit_once(timeout_s=5)
    assert findings
    assert any(item.invariant == "I3" and item.detail.get("bean") == draft.id for item in findings)
    assert snapshot.key("Station", "upper@1") in reconciler.holds()


def test_git_ref_restored(tmp_path: Path) -> None:
    ctx, _snapshot, reconciler = _audit(tmp_path)
    repo = ctx.root
    _git(repo, "init")
    _git(repo, "config", "user.email", "acceptance@example.com")
    _git(repo, "config", "user.name", "acceptance")
    (repo / "README").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "A")
    commit_a = _git(repo, "rev-parse", "HEAD")
    (repo / "README").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "B")
    commit_b = _git(repo, "rev-parse", "HEAD")
    ctx.db.query(
        "INSERT INTO epoch_pointers (name, commit) VALUES (?, ?)",
        ("active", commit_a),
    )
    _git(repo, "update-ref", "refs/factory/active", commit_b)
    assert _git(repo, "rev-parse", "refs/factory/active") == commit_b
    findings = reconciler.audit_once(timeout_s=5)
    assert findings
    assert _git(repo, "rev-parse", "refs/factory/active") == commit_a
    stored = ctx.db.query("SELECT commit FROM epoch_pointers WHERE name = ?", ("active",))
    assert stored[0]["commit"] == commit_a


def test_missing_image_does_not_pull(tmp_path: Path) -> None:
    ctx, _snapshot, reconciler = _audit(tmp_path)
    log = tmp_path / "docker-argv.txt"
    fake = tmp_path / "docker-fake.py"
    fake.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"Path({str(log)!r}).write_text(' '.join(sys.argv), encoding='utf-8')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    launcher = tmp_path / "docker.cmd"
    launcher.write_text(f'@echo off\r\npython "{fake}" %*\r\n', encoding="utf-8")
    (tmp_path / "docker").write_text(f'#!/bin/sh\nexec python {fake} "$@"\n', encoding="utf-8", newline="\n")
    os.environ["PATH"] = str(tmp_path) + os.pathsep + os.environ.get("PATH", "")
    try:
        findings = reconciler.audit_once(timeout_s=5)
    finally:
        os.environ["PATH"] = os.pathsep.join(
            part for part in os.environ.get("PATH", "").split(os.pathsep) if part != str(tmp_path)
        )
    image = [item for item in findings if item.detail.get("kind") == "image"]
    assert image
    assert all(item.state.value == "wake" for item in image)
    assert log.is_file()
    recorded = log.read_text(encoding="utf-8")
    assert recorded.strip()
    assert "pull" not in recorded.split()


def test_secret_values_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, snapshot, reconciler = _audit(tmp_path)
    monkeypatch.setenv("ACCEPTANCE_SECRET", CANARY)
    bean_id = seal_input(ctx, snapshot, "note.text@1")
    blob = ctx.warehouse.blob_path(ctx.warehouse.get(bean_id).payload[0].digest)
    blob.write_bytes(blob.read_bytes()[:-1] + b"\x01")
    findings = reconciler.audit_once(timeout_s=5)
    assert any(item.invariant == "I1" for item in findings)
    assert CANARY not in json.dumps([item.detail for item in findings], ensure_ascii=False)


def test_baseline_breach_holds(tmp_path: Path) -> None:
    ctx, snapshot, reconciler = _audit(tmp_path)
    station = snapshot.key("Station", "upper@1")
    observed = "2026-01-01T00:00:00.000Z"
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO jobs (id, station, epoch, state, attempt, inputs, params, outputs, cost_usd, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("j-drift", station, snapshot.epoch, "SEALED", 1, "{}", "{}", "{}", 50.0, observed, observed),
        )
    first = reconciler.audit_once(timeout_s=5)
    assert first
    assert station in reconciler.holds()
    assert any(item.invariant == "drift" and item.state.value == "held" for item in first)
    with ctx.db.tx() as conn:
        conn.execute("UPDATE jobs SET cost_usd = ? WHERE id = ?", (0.1, "j-drift"))
    reconciler.audit_once(timeout_s=5)
    assert station not in reconciler.holds()
    kinds = [row["kind"] for row in ctx.db.query("SELECT kind FROM events")]
    assert "audit.release" in kinds


def test_no_psi_without_limit(tmp_path: Path) -> None:
    ctx, snapshot, reconciler = _audit(tmp_path)
    bean_id = seal_input(ctx, snapshot, "note.text@1")
    blob_path = ctx.warehouse.blob_path(ctx.warehouse.get(bean_id).payload[0].digest)
    blob_path.write_bytes(blob_path.read_bytes()[:-1] + b"\x02")
    findings = reconciler.audit_once(timeout_s=5)
    assert any(item.invariant == "I1" for item in findings)
    blob = json.dumps([item.detail for item in findings])
    assert "0.2" not in blob
    assert not any("psi" in json.dumps(item.detail).lower() for item in findings)


def test_unrestorable_wakes(tmp_path: Path) -> None:
    ctx, snapshot, reconciler = _audit(tmp_path)
    bean_id = seal_input(ctx, snapshot, "note.text@1")
    blob = ctx.warehouse.blob_path(ctx.warehouse.get(bean_id).payload[0].digest)
    blob.write_bytes(blob.read_bytes()[:-1] + b"\x00")
    from mao.foreman import WakeQueue

    findings = reconciler.audit_once(timeout_s=5)
    assert any(item.invariant == "I1" for item in findings)
    tickets = [item for item in WakeQueue(ctx).list() if item.signal == "invariant"]
    assert tickets
    assert set(tickets[0].payload) == {"id"}


def test_hold_blocks_submit(tmp_path: Path) -> None:
    from mao.runs import LineRunner, NodeState, RunState

    ctx, snapshot, runner, _clock = open_factory(tmp_path, FIXTURE)
    reconciler = Reconciler(ctx, EpochStore(ctx))
    station = snapshot.key("Station", "upper@1")
    note = seal_input(ctx, snapshot, "note.text@1")
    ctx.db.query(
        "INSERT INTO station_holds (station, finding_id, at) VALUES (?, ?, ?)",
        (station, "au-0001", "2026-01-01T00:00:00.000Z"),
    )
    lines = LineRunner(ctx, snapshot, runner)
    run = lines.start_run("note-trigger@1", {"note": note})
    submitted = lines.tick(run.id)
    nodes = lines.nodes(run.id)
    assert nodes
    assert submitted == []
    assert all(node.state == NodeState.WAITING for node in nodes)
    assert station in reconciler.holds()
    assert lines.get(run.id).state != RunState.FAILED


def test_i14_compares_tables(tmp_path: Path) -> None:
    ctx, snapshot, reconciler = _audit(tmp_path)
    epoch = EpochStore(ctx).current()
    stored = [dict(item) for item in snapshot.result.resolutions]
    assert stored
    stored[0] = {**stored[0], "chain": ["tampered-converter@1"], "origin": "precomputed"}
    frozen = ctx.root / "epochs" / epoch / "resolutions.json"
    frozen.parent.mkdir(parents=True, exist_ok=True)
    frozen.write_text(json.dumps(stored), encoding="utf-8")
    before = frozen.read_text(encoding="utf-8")
    findings = reconciler.audit_once(timeout_s=5)
    assert findings
    assert any(item.invariant == "I14" for item in findings)
    assert frozen.read_text(encoding="utf-8") == before
    assert "tampered-converter@1" in frozen.read_text(encoding="utf-8")
    assert not (ctx.root / "epochs" / epoch / "resolution-runtime.json").exists()
