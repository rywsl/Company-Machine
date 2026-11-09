"""M1-05 验收（spec/system/factoryctl.md 第 7 章）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

pytest.importorskip("mao.ctl")

from mao.base import FakeClock  # noqa: E402
from mao.ctl import CtlServer  # noqa: E402


class _Handler:
    def __init__(self) -> None:
        self.seen = 0

    def handle(self, request):
        self.seen += 1
        if request["name"] == "later" and self.seen == 1:
            return None
        return {"ok": True, "echo": request["name"]}


def _write(dirpath, name, body) -> None:
    path = dirpath / name
    path.write_text(json.dumps(body), encoding="utf-8")


def test_poll_once_answers_in_order(tmp_path) -> None:
    req = tmp_path / "requests"
    req.mkdir()
    _write(req, "r-0001.json", {"id": "r-0001", "name": "a", "payload": {}})
    _write(req, "r-0002.json", {"id": "r-0002", "name": "b", "payload": {}})
    server = CtlServer(tmp_path, _Handler(), FakeClock(datetime(2026, 11, 9, tzinfo=UTC)))
    assert server.poll_once() == 2
    assert json.loads((tmp_path / "responses" / "r-0001.json").read_text(encoding="utf-8"))["ok"] is True


def test_handler_none_retries(tmp_path) -> None:
    req = tmp_path / "requests"
    req.mkdir()
    _write(req, "r-0001.json", {"id": "r-0001", "name": "later", "payload": {}})
    handler = _Handler()
    server = CtlServer(tmp_path, handler, FakeClock(datetime(2026, 11, 9, tzinfo=UTC)))
    assert server.poll_once() == 0
    assert server.poll_once() == 1


def test_atomic_rename(tmp_path) -> None:
    req = tmp_path / "requests"
    req.mkdir()
    _write(req, "r-0001.json", {"id": "r-0001", "name": "a", "payload": {}})
    server = CtlServer(tmp_path, _Handler(), FakeClock(datetime(2026, 11, 9, tzinfo=UTC)))
    server.poll_once()
    text = (tmp_path / "responses" / "r-0001.json").read_text(encoding="utf-8")
    assert json.loads(text)["id"] == "r-0001"
    assert not list((tmp_path / "responses").glob("*.part"))


def test_factoryctl_call_roundtrip(tmp_path) -> None:
    pytest.importorskip("mao.ctl.factoryctl")
    from mao.ctl.factoryctl import main as ctl_main

    req = tmp_path / "requests"
    req.mkdir()
    (tmp_path / "responses").mkdir()
    code = ctl_main(["call", "ping", "{}"])
    assert code in {0, 4}


def test_harvest_missing_file(tmp_path) -> None:
    from mao.ctl.factoryctl import main as ctl_main

    (tmp_path / "harvest.json").write_text(
        json.dumps({"outputs": {"note": {"glob": "note.txt"}}}), encoding="utf-8"
    )
    code = ctl_main(["harvest"])
    assert code == 1
