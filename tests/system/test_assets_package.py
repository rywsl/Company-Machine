"""移交包在绑定缺失时不安装半成品。"""

from __future__ import annotations

from pathlib import Path

from mao.errors import FactoryError
from mao.packages import create_checkpoint, export_package, import_package
from tests.acceptance.m16.asset_world import World


def test_missing_binding_does_not_leave_a_buyer_root(tmp_path: Path) -> None:
    world = World(tmp_path)
    registry = world.registry_asset()
    root = world.factory_asset([registry["id"]])
    checkpoint = tmp_path / "checkpoint"
    create_checkpoint(world.ctx, checkpoint, factory_asset=root["id"])
    package = tmp_path / "package"
    export_package(checkpoint, package, release=world.release(root, registry))
    buyer = tmp_path / "buyer"
    try:
        import_package(package, buyer, bindings={"resources": {}, "factory_config": {}})
    except FactoryError as exc:
        assert exc.code == "PACKAGE_BINDING_REQUIRED"
    else:
        raise AssertionError("缺绑定不应安装")
    assert not buyer.exists()
    assert not buyer.with_name(buyer.name + ".part").exists()
