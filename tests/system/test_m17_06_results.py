"""M17-06：同一请求键不重复开跑，结果查询走真实 ResultStore。"""

from __future__ import annotations

from pathlib import Path

from mao.errors import FactoryError
from mao.results import ResultStore
from tests.acceptance.m13.agent_world import World


def test_request_key_returns_the_same_run(tmp_path: Path) -> None:
    world = World(tmp_path)
    try:
        store = ResultStore(world.ctx, world.epochs)
        note = world.note()
        first = store.start("scout", "note-trigger@1", {"note": note}, request_key="req-1")
        second = store.start("scout", "note-trigger@1", {"note": note}, request_key="req-1")
        assert first["id"] == second["id"]
        body = store.get(first["id"])
        assert body["id"] == first["id"]
        assert body["line"].startswith("Line:")
        try:
            store.start("scout", "note-trigger@1", {"note": world.note("other\n")}, request_key="req-1")
        except FactoryError as exc:
            assert exc.code == "RUN_REQUEST_CONFLICT"
        else:
            raise AssertionError("changed inputs reused the request key")
    finally:
        world.ctx.close()
