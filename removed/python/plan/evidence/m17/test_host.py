"""M17 baseline diagnostics: the assertions describe the required safe behavior.

These are not M17 acceptance tests. On the audited c538768 baseline, failures
are intentional evidence of identified lifecycle and delivery defects.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from tests.acceptance.m13.agent_world import World
from tests.fakes.fakelib import FAKE_AGENT_COMMAND, local_commands


def _successful_command() -> list[str]:
    # Avoid a host-side script under /tmp: LocalProvider rewrites that prefix.
    code = (
        'import json; print(json.dumps({"type": "result", "subtype": "success", '
        '"is_error": False, "session_id": "stable-session"}))'
    )
    return [sys.executable, "-c", code]


def test_cli_serve_starts_configured_resident_host(tmp_path: Path) -> None:
    """M13 5.7: the ordinary CLI must actually consume configured agent mail."""
    w = World(tmp_path)
    cfg = json.loads((w.root / "factory.json").read_text(encoding="utf-8"))
    cfg["serve"] = {
        "tick_s": 0.05,
        "max_parallel_jobs": 2,
        "audit_every_s": None,
        "canary_every_s": None,
        "docker_timeout_s": 60,
        "supervise": False,
        "model_port": 0,
    }
    (w.root / "factory.json").write_text(json.dumps(cfg), encoding="utf-8")
    from tests.acceptance.m12.instance_world import SCHEDULER

    (w.root / "scheduler.json").write_text(json.dumps(SCHEDULER), encoding="utf-8")
    mapping = tmp_path / "commands.json"
    command_map = local_commands()
    command_map[FAKE_AGENT_COMMAND] = _successful_command()
    mapping.write_text(json.dumps(command_map), encoding="utf-8")
    message = w.post("helper", "one external input")
    cmd = [
        sys.executable,
        "-m",
        "mao",
        "--root",
        str(w.root),
        "--provider",
        "local",
        "--local-commands",
        str(mapping),
        "serve",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if proc.poll() is not None or w.messages("helper")[0].state == "done":
                break
            time.sleep(0.05)
        state = w.messages("helper")[0].state
        lease = w.rows("SELECT resource FROM leases WHERE resource IN ('serve', 'agents') ORDER BY resource")
        evidence = {
            "message": message.id,
            "state": state,
            "leases": [r["resource"] for r in lease],
            "process_exit": proc.poll(),
        }
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=5)
        w.ctx.close()
    assert state == "done", (evidence, stdout[-1000:], stderr[-1000:])


def test_cli_launch_error_does_not_strand_delivered_mail(tmp_path: Path) -> None:
    """A missing operator executable must leave the message retryable."""
    w = World(tmp_path)
    host = w.host()
    message = w.post("helper", "complete this after a transient launcher failure")
    commands = w.ctx.provider.commands
    original = _successful_command()
    commands[FAKE_AGENT_COMMAND] = [str(tmp_path / "missing-executable")]
    try:
        try:
            host.run_once("helper")
        except OSError:
            pass
    finally:
        commands[FAKE_AGENT_COMMAND] = original
    next_turn = host.run_once("helper")
    mailbox = {m.id: m.state for m in w.messages("helper")}
    turns = [(t.state, t.error) for t in host.turns("helper")]
    evidence = {"message_state": mailbox[message.id], "turns": turns, "retry_executed": next_turn is not None}
    w.ctx.close()
    assert next_turn is not None and next_turn.state == "ok", evidence


def test_taken_wake_survives_mailbox_write_failure(tmp_path: Path, monkeypatch) -> None:
    """A crash between claiming a wake and enqueuing it must not lose the wake."""
    from mao.agents import Mailbox
    from mao.foreman import WakeQueue

    w = World(tmp_path, foreman={"mode": "resident"})
    host = w.host()
    queue = WakeQueue(w.ctx)
    wake = queue.file("other", "needs-attention", {"id": "needs-attention"})
    original = Mailbox.post
    injected = False

    def fail_once(self, agent, kind, source, body, **kwargs):
        nonlocal injected
        if agent == "foreman" and kind == "wake" and not injected:
            injected = True
            raise OSError("injected mailbox write failure")
        return original(self, agent, kind, source, body, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Mailbox, "post", fail_once)
        try:
            host.collect()
        except OSError:
            pass
    host.recover()
    host.collect()
    matching = [m for m in w.messages("foreman") if m.source == "wake:" + wake.id]
    evidence = {"wake_state": queue.get(wake.id).status, "matching_mail": len(matching)}
    w.ctx.close()
    assert len(matching) == 1, evidence


def test_schedule_post_is_not_duplicated_across_interruption(tmp_path: Path, monkeypatch) -> None:
    """A crash after durable mail insertion must not replay the same schedule occurrence."""
    from mao.agents import Mailbox

    w = World(tmp_path)
    host = w.host()
    original = Mailbox.post
    injected = False

    def fail_after_commit(self, agent, kind, source, body, **kwargs):
        nonlocal injected
        message = original(self, agent, kind, source, body, **kwargs)
        if agent == "scout" and kind == "schedule" and not injected:
            injected = True
            raise OSError("injected interruption after mail commit")
        return message

    with monkeypatch.context() as patch:
        patch.setattr(Mailbox, "post", fail_after_commit)
        try:
            host.collect()
        except OSError:
            pass
    host.recover()
    host.collect()
    matching = [m for m in w.messages("scout") if m.kind == "schedule" and m.source == "schedule:0"]
    evidence = {"same_occurrence_mail_count": len(matching), "message_ids": [m.id for m in matching]}
    w.ctx.close()
    assert len(matching) == 1, evidence


def test_lost_agents_lease_stops_host(tmp_path: Path, monkeypatch) -> None:
    """Lease loss must fence the old host before another owner takes over."""
    import mao.durable as durable

    w = World(tmp_path)
    host = w.host()
    host.pause("scout")
    host.pause("helper")
    host.config = replace(host.config, poll_s=0.02)
    real_keeper = durable.LeaseKeeper

    class FastKeeper(real_keeper):
        def __init__(self, store, resource, *, owner):
            super().__init__(store, resource, owner=owner, ttl_s=0.06)

    monkeypatch.setattr(durable, "LeaseKeeper", FastKeeper)
    stop = threading.Event()
    worker = threading.Thread(target=host.serve, args=(stop,), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and durable.LeaseStore(w.ctx).valid("agents") is None:
            time.sleep(0.01)
        assert durable.LeaseStore(w.ctx).valid("agents") is not None
        with w.ctx.db.tx() as conn:
            conn.execute("DELETE FROM leases WHERE resource = 'agents'")
        worker.join(timeout=0.6)
        still_active = worker.is_alive()
    finally:
        stop.set()
        worker.join(timeout=5)
        w.ctx.close()
    assert not still_active, {"host_alive_after_lease_removed": still_active}


def test_empty_successful_cli_output_is_not_completed_work(tmp_path: Path) -> None:
    """A CLI that emits no result/session cannot substantiate completed agent work."""
    w = World(tmp_path)
    host = w.host()
    message = w.post("helper", "complete the assigned work")
    w.ctx.provider.commands[FAKE_AGENT_COMMAND] = ["/usr/bin/true"]
    turn = host.run_once("helper")
    mailbox = {m.id: m.state for m in w.messages("helper")}
    evidence = {"turn_state": turn.state, "session": turn.session, "message_state": mailbox[message.id]}
    w.ctx.close()
    assert turn.state != "ok" and mailbox[message.id] != "done", evidence


def test_wake_claim_is_exclusive_under_concurrency(tmp_path: Path, monkeypatch) -> None:
    """Two claimants must never both obtain the same wake."""
    from mao.foreman import WakeQueue

    w = World(tmp_path)
    queue = WakeQueue(w.ctx)
    wake = queue.file("other", "only-once", {"id": "only-once"})
    original_query = w.ctx.db.query
    barrier = threading.Barrier(2)
    outcomes = []
    errors = []

    def synchronized_query(sql, params=()):
        rows = original_query(sql, params)
        if sql.startswith("SELECT * FROM wakes WHERE status = 'open'"):
            barrier.wait(timeout=3)
        return rows

    def claim(name):
        try:
            result = queue.next(by=name)
            outcomes.append(None if result is None else result.id)
        except Exception as exc:
            errors.append(repr(exc))

    with monkeypatch.context() as patch:
        patch.setattr(w.ctx.db, "query", synchronized_query)
        threads = [threading.Thread(target=claim, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
    evidence = {"wake": wake.id, "claims": outcomes, "errors": errors}
    w.ctx.close()
    assert not errors and outcomes.count(wake.id) == 1, evidence


def test_local_agent_diagnostic_fixture_can_complete_mail(tmp_path: Path) -> None:
    """Positive control: this fixture can execute and record a valid CLI turn."""
    w = World(tmp_path)
    w.ctx.provider.commands[FAKE_AGENT_COMMAND] = _successful_command()
    host = w.host()
    message = w.post("helper", "positive control")
    turn = host.run_once("helper")
    mail = {m.id: m.state for m in w.messages("helper")}
    evidence = {"state": turn.state, "session": turn.session, "mail": mail[message.id]}
    w.ctx.close()
    assert evidence == {"state": "ok", "session": "stable-session", "mail": "done"}


def test_batch_order_matches_chronological_prompt(tmp_path: Path) -> None:
    """The task explicitly promises the messages are in chronological order."""
    w = World(tmp_path)
    host = w.host()
    original_ids = w.ctx.ids

    class OutOfOrderIds:
        def __init__(self):
            self.messages = iter(("am-zzzzzzzzzz", "am-aaaaaaaaaa"))

        def new(self, prefix):
            return next(self.messages) if prefix == "am" else original_ids.new(prefix)

    w.ctx.ids = OutOfOrderIds()
    first = w.post("helper", "first: start the work")
    w.clock.advance(1)
    second = w.post("helper", "second: cancel the work")
    got = host._pending("helper", 20)
    evidence = {"created_order": [first.id, second.id], "delivered_order": [m.id for m in got]}
    w.ctx.close()
    assert [m.id for m in got] == [first.id, second.id], evidence
