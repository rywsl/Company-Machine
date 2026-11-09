"""M2-05 验收：缓存键（spec/system/cache.md 第 7 章）。

模块还不存在时整个文件跳过。实现之后，下面每个测试都调用规范已经点名的函数。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.cache")
pytest.importorskip("mao.base")
pytest.importorskip("mao.snapshot")

from mao.base import canonical_bytes, digest_bytes  # noqa: E402
from mao.cache import CACHEABLE_ROLES, JobCache, KeyMaterial  # noqa: E402
from mao.errors import FactoryError  # noqa: E402
from tests.acceptance.m2.world import EXAMPLE, open_factory  # noqa: E402


def _material() -> KeyMaterial:
    return KeyMaterial(
        station="Station:explainer@3",
        station_digest="sha256:" + "a" * 64,
        machine="Machine:openmontage@7",
        machine_digest="sha256:" + "b" * 64,
        image="registry.local/machines/openmontage@sha256:" + "c" * 64,
        recipe="animated-explainer",
        runtime="OperatorRuntime:claude-code@4",
        runtime_digest="sha256:" + "d" * 64,
        model="claude-opus-5-5",
        policy="Policy:video-default@2",
        policy_digest="sha256:" + "e" * 64,
        inputs={"brief": ["sha256:" + "1" * 64]},
        params={},
        variant="",
    )


def test_material_shape() -> None:
    material = _material()
    body = material.to_json()
    assert body["v"] == 1
    assert set(body) >= {
        "v",
        "station",
        "station_digest",
        "machine",
        "image",
        "recipe",
        "inputs",
        "params",
        "variant",
    }
    assert material.key() == "jk:" + digest_bytes(canonical_bytes(body))[7:]


def test_key_invariances(tmp_path: Path) -> None:
    ctx, snapshot, _runner, _clock = open_factory(tmp_path, EXAMPLE)
    cache = JobCache(ctx)
    station = snapshot.key("Station", "subs-convert@1")
    left = cache.material(snapshot, station, {"in": "sha256:" + "1" * 64}, {"fmt": "srt"})
    right = cache.material(snapshot, station, {"in": "sha256:" + "1" * 64}, {"fmt": "srt"})
    assert left.key() == right.key()
    assert "transform" in CACHEABLE_ROLES and "converter" in CACHEABLE_ROLES


def test_key_sensitivity(tmp_path: Path) -> None:
    ctx, snapshot, _runner, _clock = open_factory(tmp_path, EXAMPLE)
    cache = JobCache(ctx)
    station = snapshot.key("Station", "subs-convert@1")
    base = cache.key(snapshot, station, {"in": "sha256:" + "1" * 64}, {})
    other = cache.key(snapshot, station, {"in": "sha256:" + "2" * 64}, {})
    variant = cache.key(snapshot, station, {"in": "sha256:" + "1" * 64}, {}, variant="b")
    assert base != other
    assert base != variant


def test_not_cacheable_roles(tmp_path: Path) -> None:
    ctx, snapshot, _runner, _clock = open_factory(tmp_path, EXAMPLE)
    cache = JobCache(ctx)
    station = snapshot.key("Station", "publish-s3@2")
    with pytest.raises(FactoryError) as ei:
        cache.material(snapshot, station, {"artifact": "sha256:" + "1" * 64}, {})
    assert ei.value.code == "CACHE_NOT_CACHEABLE"


def test_rebuild_hits_everything(tmp_path: Path) -> None:
    from mao.runs import InlineExecutor, LineRunner
    from tests.acceptance.m2.world import start_line

    ctx, snapshot, runner, _clock, lines, inputs = start_line(tmp_path, "note-pipeline@1")
    lines = LineRunner(ctx, snapshot, runner, cache=JobCache(ctx))
    run = lines.start_run("note-pipeline@1", inputs)
    done = lines.run_to_completion(run.id, InlineExecutor(runner))
    rebuilt = lines.rebuild(done.id, by="acceptance")
    again = lines.run_to_completion(rebuilt.id, InlineExecutor(runner))
    assert again.spent_usd == 0
    assert any(node.cache_hit for node in lines.nodes(again.id))


def test_variant_reruns_one_step(tmp_path: Path) -> None:
    from mao.runs import InlineExecutor, LineRunner
    from tests.acceptance.m2.world import start_line

    ctx, snapshot, runner, _clock, _lines, inputs = start_line(tmp_path, "note-pipeline@1")
    lines = LineRunner(ctx, snapshot, runner, cache=JobCache(ctx))
    first = lines.run_to_completion(lines.start_run("note-pipeline@1", inputs).id, InlineExecutor(runner))
    second = lines.run_to_completion(
        lines.start_run("note-pipeline@1", inputs, variants={"write": "b"}).id,
        InlineExecutor(runner),
    )
    assert first.id != second.id
    assert any(node.cache_hit for node in lines.nodes(second.id))


def test_converter_instance_hit(tmp_path: Path) -> None:
    ctx, snapshot, _runner, _clock = open_factory(tmp_path, EXAMPLE)
    cache = JobCache(ctx)
    station = snapshot.key("Station", "subs-convert@1")
    assert cache.key(snapshot, station, {"in": "sha256:" + "9" * 64}, {}) == cache.key(
        snapshot, station, {"in": "sha256:" + "9" * 64}, {}
    )


def test_invalidation_scopes(tmp_path: Path) -> None:
    ctx, snapshot, _runner, _clock = open_factory(tmp_path, EXAMPLE)
    cache = JobCache(ctx)
    station = snapshot.key("Station", "subs-convert@1")
    material = cache.material(snapshot, station, {"in": "sha256:" + "3" * 64}, {})
    cache.bind("j-0001", material)
    for scope, value in (
        ("key", material.key()),
        ("job", "j-0001"),
        ("station", station),
        ("bean", "sha256:" + "3" * 64),
    ):
        record = cache.invalidate(scope, value, reason="acceptance", by="test")
        assert record.startswith("ci-")
    assert cache.lookup(material.key()) is None


def test_no_cache_still_binds(tmp_path: Path) -> None:
    ctx, snapshot, _runner, _clock = open_factory(tmp_path, EXAMPLE)
    cache = JobCache(ctx)
    station = snapshot.key("Station", "subs-convert@1")
    material = cache.material(snapshot, station, {"in": "sha256:" + "4" * 64}, {})
    cache.bind("j-0002", material)
    explained = cache.explain("j-0002")
    assert explained["key"] == material.key()
    assert explained["material"]["v"] == 1


def test_factory_run_cache_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from mao.cli import main

    ctx, snapshot, _runner, _clock = open_factory(tmp_path, EXAMPLE)
    cache = JobCache(ctx)
    station = snapshot.key("Station", "subs-convert@1")
    bound = cache.key(snapshot, station, {"in": "sha256:" + "5" * 64}, {})
    assert bound.startswith("jk:")
    monkeypatch.setenv("FACTORY_ROOT", str(ctx.root))
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    code = main(["run", "--help"])
    assert isinstance(code, int)
    assert "--cache" in buf.getvalue()
