"""M1-05 单元：mao.ctl（spec/system/factoryctl.md 5.4）。模块缺失时整文件跳过。"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mao.ctl")

from mao.ctl import CtlServer  # noqa: E402


class _Handler:
    def handle(self, request: dict) -> dict:
        return {"ok": True, "id": request["id"], "result": {"echo": request.get("op")}}


def test_poll_empty_is_zero(tmp_path) -> None:
    server = CtlServer(tmp_path, _Handler())
    assert server.poll_once() == 0


def test_stop_without_start_returns(tmp_path) -> None:
    CtlServer(tmp_path, _Handler()).stop()


def test_start_twice_raises(tmp_path) -> None:
    server = CtlServer(tmp_path, _Handler())
    server.start(interval_s=10)
    try:
        with pytest.raises(RuntimeError):
            server.start()
    finally:
        server.stop()


def test_invalid_request_is_rejected(tmp_path) -> None:
    (tmp_path / "requests").mkdir()
    (tmp_path / "requests" / "bad.json").write_text("{", encoding="utf-8")
    server = CtlServer(tmp_path, _Handler())
    assert server.poll_once() == 1
    body = json.loads((tmp_path / "responses" / "bad.json").read_text(encoding="utf-8"))
    assert body["ok"] is False
    assert body["error"]["code"] == "CTL_REQUEST_INVALID"


def test_valid_request_is_answered(tmp_path) -> None:
    (tmp_path / "requests").mkdir()
    (tmp_path / "requests" / "r1.json").write_text(json.dumps({"id": "r1", "op": "log"}), encoding="utf-8")
    server = CtlServer(tmp_path, _Handler())
    assert server.poll_once() == 1
    body = json.loads((tmp_path / "responses" / "r1.json").read_text(encoding="utf-8"))
    assert body["ok"] is True
    assert body["result"]["echo"] == "log"
