"""冷检查点失败时不发布半成品。"""

from __future__ import annotations

from pathlib import Path

from mao.errors import FactoryError
from mao.packages import create_checkpoint
from tests.acceptance.m16.asset_world import World


def test_unclassified_file_cleans_partial_checkpoint(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.workspace().mkdir(parents=True)
    (world.workspace() / "notes.db").write_text("unclassified", encoding="utf-8")
    root = world.factory_asset()
    dest = tmp_path / "checkpoint"
    try:
        create_checkpoint(world.ctx, dest, factory_asset=root["id"])
    except FactoryError as exc:
        assert exc.code == "ASSET_STATE_UNDECLARED"
    else:
        raise AssertionError("未分类文件不应形成检查点")
    assert not dest.exists()
    assert not Path(str(dest) + ".part").exists()
    assert (world.workspace() / "notes.db").read_text(encoding="utf-8") == "unclassified"
