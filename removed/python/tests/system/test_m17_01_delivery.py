"""M17-01：围栏租约、持久顺序和稳定效果键。"""

from __future__ import annotations

import threading
from pathlib import Path

from mao.context import Context
from mao.delivery import DeliveryStore
from mao.durable import LeaseStore
from mao.errors import FactoryError
from mao.foreman import WakeQueue
from tests.acceptance.m13.agent_world import World


def test_old_token_cannot_renew_or_release_newer_generation(tmp_path: Path) -> None:
    world = World(tmp_path)
    store = LeaseStore(world.ctx)
    first = store.acquire_fenced("agents", owner="host-a", ttl_s=10)
    assert first is not None and first.generation == 1
    world.clock.advance(11)
    second = store.acquire_fenced("agents", owner="host-a", ttl_s=10)
    assert second is not None and second.generation == first.generation + 1
    assert store.renew_fenced(first, ttl_s=10) is False
    assert store.release_fenced(first) is False
    assert store.renew_fenced(second, ttl_s=10) is True
    assert store.release_fenced(second) is True
    assert store.valid("agents") is None
    world.ctx.close()


def test_expired_same_owner_reacquire_bumps_generation(tmp_path: Path) -> None:
    world = World(tmp_path)
    store = LeaseStore(world.ctx)
    held = store.acquire_fenced("box", owner=world.ctx.owner, ttl_s=5)
    assert held is not None
    world.clock.advance(6)
    again = store.acquire_fenced("box", owner=world.ctx.owner, ttl_s=5)
    assert again is not None and again.generation == held.generation + 1
    try:
        store.guard("box", owner=held.owner, generation=held.generation)
    except FactoryError as exc:
        assert exc.code == "LEASE_LOST"
    else:
        raise AssertionError("old generation still guarded")
    world.ctx.close()


def test_wake_claim_is_exclusive_across_two_connections(tmp_path: Path) -> None:
    world = World(tmp_path)
    wake = WakeQueue(world.ctx).file("other", "two-conn", {"id": "two-conn"})
    other = Context.open(world.root, provider=world.ctx.provider, clock=world.clock, ids=world.ctx.ids)
    barrier = threading.Barrier(2)
    outcomes: list[str | None] = []
    errors: list[str] = []

    def claim(ctx: Context) -> None:
        try:
            barrier.wait(timeout=3)
            got = WakeQueue(ctx).next(by=ctx.owner)
            outcomes.append(None if got is None else got.id)
        except Exception as exc:
            errors.append(repr(exc))

    threads = [
        threading.Thread(target=claim, args=(world.ctx,)),
        threading.Thread(target=claim, args=(other,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    evidence = {"wake": wake.id, "claims": outcomes, "errors": errors}
    other.close()
    world.ctx.close()
    assert not errors and outcomes.count(wake.id) == 1, evidence


def test_effect_key_replay_does_not_create_another_outbox_row(tmp_path: Path) -> None:
    world = World(tmp_path)
    store = DeliveryStore(world.ctx)
    body = {
        "agent": "helper",
        "kind": "message",
        "source": "run:r-1:terminal",
        "body": {"text": "done"},
        "delivery_source": "run:r-1:terminal",
    }
    with world.ctx.db.tx() as conn:
        first = store.enqueue(conn, kind="agent_mail", effect_key="mail/helper/run:r-1:terminal", body=body)
        second = store.enqueue(conn, kind="agent_mail", effect_key="mail/helper/run:r-1:terminal", body=body)
    assert first == second
    assert store.drain() == [first]
    assert store.drain() == []
    mail = [item for item in world.messages("helper") if item.source == "run:r-1:terminal"]
    assert len(mail) == 1
    world.ctx.close()
