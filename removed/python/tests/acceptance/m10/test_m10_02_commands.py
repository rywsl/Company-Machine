"""M10-02 验收：`factory receipt`、`factory source run`、`factory run`（spec/system/wiring.md 5.2）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.cli")

from tests.acceptance.m10.wiring_world import Cli  # noqa: E402


def _published(cli: Cli) -> tuple[str, dict]:
    note = cli.note()
    run = cli.json("line", "run", "note-publish@1", "--in", f"note={note}")
    assert run["state"] == "SUCCEEDED", run["error"]
    [receipt] = cli.json("receipt", "ls")
    return note, receipt


def test_receipt_ls_and_show(tmp_path: Path) -> None:
    cli = Cli(tmp_path)
    _note, receipt = _published(cli)
    assert receipt["status"] == "verified"
    assert receipt["station"] == "Station:publish-local@1" and receipt["station_name"] == "publish-local"
    assert cli.json("receipt", "ls", "--status", "claimed") == []
    assert [item["id"] for item in cli.json("receipt", "ls", "--station", "publish-local")] == [receipt["id"]]
    assert [item["id"] for item in cli.json("receipt", "ls", "--station", "publish-local@1")] == [
        receipt["id"]
    ]
    assert cli.json("receipt", "ls", "--station", "sink-plain") == []
    shown = cli.json("receipt", "show", receipt["id"])
    assert shown["id"] == receipt["id"] and shown["target"] == str(cli.pub)
    assert shown["locator"] == {"path": str(cli.pub / "note.txt")}


def test_receipt_verify_detects_deleted_target(tmp_path: Path) -> None:
    cli = Cli(tmp_path)
    _note, receipt = _published(cli)
    target = cli.pub / "note.txt"
    data = target.read_bytes()
    target.unlink()
    code, _out, err = cli("receipt", "verify", receipt["id"])
    assert code == 1, err
    assert cli.json("receipt", "show", receipt["id"])["status"] == "failed"
    assert [event["data"]["receipt"] for event in cli.json("events", "--kind", "sink.verify_failed")] == [
        receipt["id"]
    ]
    target.write_bytes(data)
    again = cli.json("receipt", "verify", receipt["id"])
    assert again["status"] == "verified"


def test_receipt_verify_rejects_claimed(tmp_path: Path) -> None:
    cli = Cli(tmp_path)
    note = cli.note()
    code, out, err = cli("run", "sink-plain@1", "--input", f"note={note}", "--param", f"dir={cli.pub}")
    assert code == 1, err
    assert json.loads(out)["job"]["error"]["code"] == "RECEIPT_MISSING"
    [receipt] = cli.json("receipt", "ls", "--station", "sink-plain")
    assert receipt["status"] == "claimed"
    code, error = cli.fails("receipt", "verify", receipt["id"])
    assert code == 1 and "RECEIPT_STATE_INVALID" in error


def test_receipt_void_allows_republish(tmp_path: Path) -> None:
    cli = Cli(tmp_path)
    note, receipt = _published(cli)
    voided = cli.json("receipt", "void", receipt["id"], "--by", "human:acceptance", "--reason", "redo")
    assert voided["status"] == "void"
    [event] = cli.json("events", "--kind", "receipt.voided")
    assert event["data"] == {"receipt": receipt["id"], "by": "human:acceptance", "reason": "redo"}
    run = cli.json("line", "run", "note-publish@1", "--in", f"note={note}")
    assert run["state"] == "SUCCEEDED", run["error"]
    statuses = sorted(item["status"] for item in cli.json("receipt", "ls", "--station", "publish-local"))
    assert statuses == ["verified", "void"]


def test_receipt_command_errors(tmp_path: Path) -> None:
    cli = Cli(tmp_path)
    code, error = cli.fails("receipt", "show", "rc-9999")
    assert code == 1 and "RECEIPT_NOT_FOUND" in error
    code, error = cli.fails("receipt", "verify", "rc-9999")
    assert code == 1 and "RECEIPT_NOT_FOUND" in error
    code, _out, _err = cli("receipt", "void", "rc-9999", "--reason", "x")
    assert code == 2


def test_source_run_ingests(tmp_path: Path) -> None:
    cli = Cli(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.txt").write_text("from outside\n", encoding="utf-8")
    first = cli.json("source", "run", "pull-notes@1", "--param", f"from_dir={inbox}")
    assert first["state"] == "SEALED", first["error"]
    [bean] = first["outputs"]["note"]
    [ingested] = cli.json("events", "--kind", "source.ingested")
    assert ingested["data"]["job"] == first["id"] and ingested["data"]["outputs"] == {"note": [bean]}
    [sealed] = [
        event for event in cli.json("events", "--kind", "bean.sealed") if event["data"]["bean"] == bean
    ]
    assert sealed["data"]["external"] is True
    second = cli.json("source", "run", "pull-notes@1", "--param", f"from_dir={inbox}")
    assert second["id"] != first["id"] and second["outputs"] == {"note": [bean]}


def test_source_run_requires_source_role(tmp_path: Path) -> None:
    cli = Cli(tmp_path)
    code, error = cli.fails("source", "run", "upper@1")
    assert code == 1 and "ROLE_MISMATCH" in error
    code, _out, _err = cli("source", "run", "pull-notes@1", "--param", "no-equals")
    assert code == 2


def test_run_reuses_cache_only_when_asked(tmp_path: Path) -> None:
    cli = Cli(tmp_path)
    note = cli.note()
    first = cli.json("run", "upper@1", "--input", f"note={note}")
    assert first["cache_hit"] is False and first["job"]["state"] == "SEALED"
    hit = cli.json("run", "upper@1", "--input", f"note={note}", "--cache")
    assert hit["cache_hit"] is True and hit["job"]["id"] == first["job"]["id"]
    fresh = cli.json("run", "upper@1", "--input", f"note={note}")
    assert fresh["cache_hit"] is False and fresh["job"]["id"] != first["job"]["id"]
    other = cli.json("run", "upper@1", "--input", f"note={note}", "--cache", "--variant", "b")
    assert other["cache_hit"] is False


def test_run_params_and_exit_codes(tmp_path: Path) -> None:
    cli = Cli(tmp_path)
    note = cli.note()
    done = cli.json(
        "run", "publish-local@1", "--input", f"note={note}", "--param", f"dir={cli.pub}", "--cache"
    )
    assert done["job"]["state"] == "SEALED" and done["cache_hit"] is False
    assert (cli.pub / "note.txt").is_file()
    code, out, _err = cli(
        "run", "sink-plain@1", "--input", f"note={note}", "--param", f"dir={tmp_path / 'other'}"
    )
    assert code == 1 and json.loads(out)["job"]["state"] == "DEAD"
    assert cli("run")[0] == 2
    assert cli("run", "upper@1", "--param", "no-equals")[0] == 2
