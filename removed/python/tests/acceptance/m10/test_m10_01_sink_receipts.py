"""M10-01 验收：Sink 在生产路径上取得、发布并核验回执（spec/system/wiring.md 5.1，connectors.md 5.3–5.4）。

全部经 `Runner.run` 或 `factory` 命令行执行，不直接调用 ReceiptStore 的写方法。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mao.connectors")

from tests.acceptance.m10.wiring_world import Cli, World  # noqa: E402


def _kinds(w: World, job_id: str) -> list[str]:
    return [kind for kind, _ in w.events(job_id) if kind.startswith(("sink.", "receipt."))]


def test_sink_publishes_and_verifies(tmp_path: Path) -> None:
    w = World(tmp_path)
    note = w.note("hello factory\n")
    job = w.run("publish-local@1", note)
    assert job.state.value == "SEALED", job.error
    assert job.outputs == {} and job.attempt == 1
    [receipt] = w.receipts(job.id)
    assert receipt.status.value == "verified"
    assert receipt.target == str(w.pub)
    assert receipt.locator == {"path": str(w.pub / "note.txt")}
    assert receipt.verify_method == "local-file-check" and receipt.verified_at
    assert _kinds(w, job.id) == ["sink.claimed", "sink.published", "sink.verified"]
    assert (w.pub / "note.txt").read_text(encoding="utf-8") == "hello factory\n"


def test_duplicate_publish_is_suppressed(tmp_path: Path) -> None:
    w = World(tmp_path)
    note = w.note()
    first = w.run("publish-local@1", note)
    assert first.state.value == "SEALED", first.error
    stamp = (w.pub / "note.txt").stat().st_mtime_ns
    second = w.run("publish-local@1", note)
    assert second.state.value == "SEALED", second.error
    assert second.outputs == {}
    assert _kinds(w, second.id) == ["sink.duplicate"]
    assert (w.pub / "note.txt").stat().st_mtime_ns == stamp
    assert [item.status.value for item in w.receipts()] == ["verified"]


def test_line_rerun_succeeds(tmp_path: Path) -> None:
    cli = Cli(tmp_path)
    note = cli.note()
    first = cli.json("line", "run", "note-publish@1", "--in", f"note={note}")
    second = cli.json("line", "run", "note-publish@1", "--in", f"note={note}")
    assert first["state"] == "SUCCEEDED" and second["state"] == "SUCCEEDED", (first["error"], second["error"])
    publish = cli.nodes(second["id"])["publish"]
    assert publish["state"] == "DONE"
    kinds = [
        event["kind"]
        for event in cli.json("events", "--limit", "500")
        if event["job_id"] == publish["job_id"]
    ]
    assert "sink.duplicate" in kinds
    [receipt] = [event for event in cli.json("events", "--kind", "sink.verified", "--limit", "500")]
    assert receipt["job_id"] == cli.nodes(first["id"])["publish"]["job_id"]


def test_receipt_from_recipe_template(tmp_path: Path) -> None:
    w = World(tmp_path)
    job = w.run("sink-template@1", w.note())
    assert job.state.value == "SEALED", job.error
    [receipt] = w.receipts(job.id)
    assert receipt.status.value == "verified"
    assert receipt.target == str(w.pub)
    assert receipt.locator == {"path": f"{w.pub}/note.txt", "kind": "local"}


def test_receipt_missing_is_dead(tmp_path: Path) -> None:
    w = World(tmp_path)
    job = w.run("sink-plain@1", w.note())
    assert job.state.value == "DEAD"
    assert job.error["code"] == "RECEIPT_MISSING" and job.attempt == 1
    [receipt] = w.receipts(job.id)
    assert receipt.status.value == "claimed"
    assert _kinds(w, job.id) == ["sink.claimed"]


def test_receipt_invalid_is_dead(tmp_path: Path) -> None:
    w = World(tmp_path)
    job = w.run("sink-broken@1", w.note())
    assert job.state.value == "DEAD"
    assert job.error["code"] == "RECEIPT_INVALID"
    assert "target" in job.error["detail"]["problems"]
    [receipt] = w.receipts(job.id)
    assert receipt.status.value == "claimed"
    assert (receipt.verify_detail or "").startswith("receipt.json 格式错误")


def test_unverified_receipt_is_dead(tmp_path: Path) -> None:
    w = World(tmp_path)
    job = w.run("sink-bad@1", w.note())
    assert job.state.value == "DEAD"
    assert job.error["code"] == "RECEIPT_UNVERIFIED"
    [receipt] = w.receipts(job.id)
    assert receipt.status.value == "failed"
    assert job.error["detail"]["receipt"] == receipt.id
    assert _kinds(w, job.id) == ["sink.claimed", "sink.published", "sink.verify_failed"]


def test_verify_facility_error_keeps_published(tmp_path: Path) -> None:
    w = World(tmp_path)
    job = w.run("sink-crashverify@1", w.note())
    assert job.state.value == "DEAD"
    assert job.error["code"] == "RECEIPT_VERIFY_ERROR"
    [receipt] = w.receipts(job.id)
    assert receipt.status.value == "published"
    assert _kinds(w, job.id) == ["sink.claimed", "sink.published"]
    assert (w.pub / "note.txt").is_file()


def test_sink_timeout_and_refusal_are_outcome_unknown(tmp_path: Path) -> None:
    w = World(tmp_path)
    note = w.note()
    slow = w.run("sink-slow@1", note)
    assert slow.state.value == "DEAD" and slow.attempt == 1
    assert slow.error["code"] == "SINK_OUTCOME_UNKNOWN"
    assert "timeout_s" in slow.error["detail"]["cause"]
    refused = w.run("sink-refused@1", note)
    assert refused.state.value == "DEAD" and refused.attempt == 1
    assert refused.error["code"] == "SINK_OUTCOME_UNKNOWN"
    assert refused.error["detail"]["cause"] == {"reason": "remote refused"}
    assert [item.status.value for item in w.receipts()] == ["claimed", "claimed"]


def test_factoryctl_receipt(tmp_path: Path) -> None:
    w = World(tmp_path)
    note = w.note()
    job = w.run("sink-ctl@1", note)
    assert job.state.value == "SEALED", job.error
    [receipt] = w.receipts(job.id)
    target = w.pub / "note.txt"
    assert receipt.status.value == "verified"
    assert receipt.target == str(w.pub) and receipt.locator == {"path": str(target)}
    assert [item["bean"] for item in receipt.items] == [note]
    assert receipt.items[0]["locator"] == str(target) and receipt.items[0]["digest"].startswith("sha256:")

    script = w.ctx.root / "tools" / "factory" / "bin" / "factoryctl"
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    env = dict(os.environ, FACTORY_CTL=str(ctl))
    missing = subprocess.run(
        [sys.executable, str(script), "receipt", "--locator", "a=b"], env=env, capture_output=True
    )
    assert missing.returncode == 2 and not (ctl / "receipt.json").exists()
    malformed = subprocess.run(
        [sys.executable, str(script), "receipt", "--target", "t", "--locator", "no-equals"],
        env=env,
        capture_output=True,
    )
    assert malformed.returncode == 2
