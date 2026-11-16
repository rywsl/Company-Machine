"""M17-07 target protocol: real assets, scoped work, and durable cycles.

The M16 assets and learning implementations, followed by M17 objectives, must
exist before this module's tests can execute. The final gate forbids skips.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from tests.acceptance.m13.agent_world import tool_result
from tests.acceptance.m16.asset_world import World, error_code


@pytest.fixture
def world(tmp_path: Path):
    pytest.importorskip("mao.assets")
    pytest.importorskip("mao.learning")
    pytest.importorskip("mao.objectives")
    w = World(tmp_path)
    w.config["continuity"] = {"enabled": True}
    w.save_config()
    try:
        yield w
    finally:
        w.ctx.close()


def _store(w):
    from mao.objectives import ObjectiveStore

    return ObjectiveStore(w.ctx, w.epochs)


def _objective(w, *, total_usd: float = 10.0, daily_usd: float = 3.0, expires_s: int = 172800):
    from mao.base import utc_iso

    bean = w.note("one\ntwo\n")
    contract = w.asset(
        key="contract.factory-notes",
        kind="contract",
        subject="factory",
        data={
            "registry": [],
            "files": [],
            "cases": [
                {
                    "name": "normal",
                    "kind": "job",
                    "subject": "station:upper@1",
                    "tags": ["normal"],
                    "uses_target": True,
                    "assert": 'out.state == "SEALED" && out.outputs.note[0].facts.lines >= 2',
                }
            ],
        },
    )
    w.learning.authorize(contract["id"], by="ada")
    asset = w.asset(
        key="objective.notes",
        kind="objective",
        subject="factory",
        beans=[bean],
        requires=[contract["id"]],
        data={
            "intent": "持续发现需要整理的笔记并产出有证据的结果",
            "observation_scope": "本工厂已登记且可追溯的笔记材料",
            "contract": contract["id"],
        },
    )
    grant = {
        "agents": ["scout"],
        "lines": [w.snapshot.key("Line", "note-trigger@1")],
        "total_usd": total_usd,
        "daily_usd": daily_usd,
        "every_s": 60,
        "expires_at": utc_iso(w.clock.now() + timedelta(seconds=expires_s)),
        "max_cycle_s": 300,
    }
    return asset, grant, bean


def _authorize(w, *, total_usd: float = 10.0, daily_usd: float = 3.0, expires_s: int = 172800):
    asset, grant, bean = _objective(w, total_usd=total_usd, daily_usd=daily_usd, expires_s=expires_s)
    objective = _store(w).authorize(asset["id"], grant, by="ada")
    return objective, asset, grant, bean


def _cycle(w, objective_id: str) -> str:
    # Both immediate and first-interval scheduling reach a due boundary here.
    w.clock.advance(60)
    ids = _store(w).collect()
    assert len(ids) == 1, ids
    assert _store(w).get(objective_id)["active_cycle"] == ids[0]
    return ids[0]


def _plan(w, cycle_id: str, bean: str, *, disposition: str = "work", agent: str = "scout", line=None):
    source_bean = w.note("material observed after cycle creation\n")
    observation = w.asset(
        key="observation." + cycle_id,
        kind="dataset",
        subject="factory",
        beans=[source_bean],
        summary="本周期真实登记并封存的观察材料",
    )
    if line is None:
        line = w.snapshot.key("Line", "note-trigger@1")
    data = {
        "cycle_id": cycle_id,
        "observations": [{"asset_id": observation["id"], "note": "发现需要整理的材料"}],
        "candidates": [{"id": "c1", "reason": "材料需要整理", "evidence": [observation["id"]]}],
        "selected": ["c1"] if disposition == "work" else [],
        "disposition": disposition,
        "tasks": (
            [{"id": "t1", "candidate": "c1", "agent": agent, "line": line, "inputs": {"note": bean}}]
            if disposition == "work"
            else []
        ),
        "review_after_s": 120,
        "reason": "本周期按新观察决定下一步" if disposition == "work" else "暂无新工作，两分钟后再观察",
    }
    [objective] = w.rows(
        "SELECT o.asset_id FROM objectives o JOIN objective_cycles c ON c.objective_id = o.id WHERE c.id = ?",
        (cycle_id,),
    )
    plan = w.asset(
        key="plan." + cycle_id,
        kind="objective_plan",
        subject="factory",
        beans=[bean],
        requires=[objective["asset_id"], observation["id"]],
        data=data,
    )
    return plan, observation


def _foreman_call(w, name: str, arguments: dict):
    """Exercise MCP with a real lease and the cycle mail bound to a running turn.

    This is a trust-boundary fixture, not a claim of a full AgentHost cycle.
    """
    from mao.base import canonical_json, utc_iso
    from mao.durable import LeaseStore
    from mao.mcp import McpServer, Session

    cycle_id = arguments["cycle_id"]
    turns = getattr(w, "_m17_foreman_turns", {})
    if cycle_id not in turns:
        lease_store = LeaseStore(w.ctx)
        lease = lease_store.acquire_fenced("agents", owner=w.ctx.owner)
        assert lease is not None
        leases = getattr(w, "_m17_fenced_leases", {})
        leases[cycle_id] = lease
        w._m17_fenced_leases = leases
        notices = [
            m for m in w.messages("foreman") if m.kind == "objective_cycle" and cycle_id in json.dumps(m.body)
        ]
        assert len(notices) == 1
        turn_id = w.ctx.ids.new("at")
        now = utc_iso(w.clock.now())
        [cycle] = w.rows("SELECT objective_id FROM objective_cycles WHERE id = ?", (cycle_id,))
        [origin] = w.rows(
            "SELECT objective_id, cycle_id, task_id, parent_kind, parent_id FROM execution_origins "
            "WHERE kind = 'agent_message' AND execution_id = ?",
            (notices[0].id,),
        )
        assert (
            origin["objective_id"],
            origin["cycle_id"],
            origin["task_id"],
            origin["parent_kind"],
            origin["parent_id"],
        ) == (cycle["objective_id"], cycle_id, None, "cycle", cycle_id)
        with w.ctx.db.tx() as conn:
            conn.execute(
                "INSERT INTO agent_turns "
                "(id, agent, conversation, number, kind, session, state, messages, started_at, owner, "
                "lease_generation, objective_id, cycle_id, task_id) "
                "VALUES (?, 'foreman', 1, 1, 'work', NULL, 'running', ?, ?, ?, ?, ?, ?, NULL)",
                (
                    turn_id,
                    canonical_json([notices[0].id]),
                    now,
                    w.ctx.owner,
                    lease.generation,
                    cycle["objective_id"],
                    cycle_id,
                ),
            )
            conn.execute(
                "UPDATE agent_messages SET state = 'delivered', turn = ? WHERE id = ?",
                (turn_id, notices[0].id),
            )
            conn.execute(
                "INSERT INTO execution_origins "
                "(kind, execution_id, objective_id, cycle_id, task_id, parent_kind, parent_id) "
                "VALUES ('turn', ?, ?, ?, NULL, 'agent_message', ?)",
                (turn_id, cycle["objective_id"], cycle_id, notices[0].id),
            )
        turns[cycle_id] = (turn_id, w.ctx.owner, lease.generation, cycle["objective_id"])
        w._m17_foreman_turns = turns
    turn_id, owner, generation, objective_id = turns[cycle_id]
    server = McpServer(
        w.ctx,
        w.epochs,
        Session(
            role="foreman",
            actor="fake-agent@1",
            turn_id=turn_id,
            lease_owner=owner,
            lease_generation=generation,
            objective_id=objective_id,
            cycle_id=cycle_id,
        ),
        pipeline=w.pipeline(),
    )
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "m17-test", "version": "1"},
            },
        }
    )
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
            },
        }
    )
    return tool_result(reply)


def _foreman(w, name: str, arguments: dict):
    value, error = _foreman_call(w, name, arguments)
    assert error is None, (name, error)
    return value


def _commit(w, cycle_id: str, plan: dict):
    return _foreman(w, "objective_commit_plan", {"cycle_id": cycle_id, "plan": plan["id"]})


def _dispatch(w, cycle_id: str, task_id: str = "t1"):
    result = _foreman(w, "objective_dispatch", {"cycle_id": cycle_id, "task_id": task_id})
    assert result["cycle_id"] == cycle_id and result["task_id"] == task_id
    assert result["message_id"]
    return result


def _bind_worker(w, cycle_id: str, dispatch: dict, *, agent: str = "scout") -> str:
    """Install the server-created task delivery as a worker running-turn context."""
    from mao.base import canonical_json, utc_iso

    turns = getattr(w, "_m17_worker_turns", {})
    key = (cycle_id, dispatch["task_id"], agent)
    if key in turns:
        return turns[key]
    lease = w._m17_fenced_leases[cycle_id]
    assert lease.owner == w.ctx.owner
    assert [m.id for m in w.messages(agent) if m.id == dispatch["message_id"]] == [dispatch["message_id"]]
    turn_id = w.ctx.ids.new("at")
    [cycle] = w.rows("SELECT objective_id FROM objective_cycles WHERE id = ?", (cycle_id,))
    [origin] = w.rows(
        "SELECT objective_id, cycle_id, task_id, parent_kind, parent_id FROM execution_origins "
        "WHERE kind = 'agent_message' AND execution_id = ?",
        (dispatch["message_id"],),
    )
    assert (
        origin["objective_id"],
        origin["cycle_id"],
        origin["task_id"],
        origin["parent_kind"],
        origin["parent_id"],
    ) == (cycle["objective_id"], cycle_id, dispatch["task_id"], "task", cycle_id + "/" + dispatch["task_id"])
    with w.ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO agent_turns "
            "(id, agent, conversation, number, kind, session, state, messages, started_at, owner, "
            "lease_generation, objective_id, cycle_id, task_id) "
            "VALUES (?, ?, 1, 1, 'work', NULL, 'running', ?, ?, ?, ?, ?, ?, ?)",
            (
                turn_id,
                agent,
                canonical_json([dispatch["message_id"]]),
                utc_iso(w.clock.now()),
                w.ctx.owner,
                lease.generation,
                cycle["objective_id"],
                cycle_id,
                dispatch["task_id"],
            ),
        )
        conn.execute(
            "UPDATE agent_messages SET state = 'delivered', turn = ? WHERE id = ?",
            (turn_id, dispatch["message_id"]),
        )
        conn.execute(
            "INSERT INTO execution_origins "
            "(kind, execution_id, objective_id, cycle_id, task_id, parent_kind, parent_id) "
            "VALUES ('turn', ?, ?, ?, ?, 'agent_message', ?)",
            (turn_id, cycle["objective_id"], cycle_id, dispatch["task_id"], dispatch["message_id"]),
        )
    turns[key] = turn_id
    w._m17_worker_turns = turns
    return turn_id


def _start(w, cycle_id: str, bean: str, *, task_id: str = "t1", key: str = "req-1", agent: str = "scout"):
    from mao.mcp import McpServer, Session

    arguments = {
        "line": w.snapshot.key("Line", "note-trigger@1"),
        "inputs": {"note": bean},
        "request_key": key,
        "cycle_id": cycle_id,
        "task_id": task_id,
    }
    if agent != "scout":
        # A different worker must not forge a dispatch by passing its identifiers.
        return tool_result(w.mcp("run_start", arguments, agent=agent))
    dispatched = getattr(w, "_m17_dispatches", {}).get((cycle_id, task_id))
    if dispatched is None:
        dispatched = _dispatch(w, cycle_id, task_id)
        dispatches = getattr(w, "_m17_dispatches", {})
        dispatches[(cycle_id, task_id)] = dispatched
        w._m17_dispatches = dispatches
    turn_id = _bind_worker(w, cycle_id, dispatched, agent=agent)
    lease = w._m17_fenced_leases[cycle_id]
    assert lease.owner == w.ctx.owner
    [cycle] = w.rows("SELECT objective_id FROM objective_cycles WHERE id = ?", (cycle_id,))
    server = McpServer(
        w.ctx,
        w.epochs,
        Session(
            role="agent",
            actor="fake-agent@1",
            agent=agent,
            turn_id=turn_id,
            lease_owner=lease.owner,
            lease_generation=lease.generation,
            objective_id=cycle["objective_id"],
            cycle_id=cycle_id,
            task_id=task_id,
        ),
        pipeline=w.pipeline(),
    )
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "m17-test", "version": "1"},
            },
        }
    )
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "run_start",
                "arguments": arguments,
            },
        }
    )
    return tool_result(reply)


def _bind_dispatched_task(w, cycle_id: str, task_id: str = "t1"):
    dispatched = _dispatch(w, cycle_id, task_id)
    assert _dispatch(w, cycle_id, task_id)["message_id"] == dispatched["message_id"]
    w._m17_dispatches = {(cycle_id, task_id): dispatched}
    _bind_worker(w, cycle_id, dispatched)
    return dispatched


def _finish_run(w, run_id: str):
    from mao.runner import open_runner
    from mao.runs import InlineExecutor, open_line_runner

    runner = open_runner(w.ctx, w.snapshot)
    lines = open_line_runner(w.ctx, w.snapshot, runner=runner, cache=False)
    run = lines.run_to_completion(run_id, InlineExecutor(runner))
    assert run.state.value == "SUCCEEDED", run.error
    nodes = lines.nodes(run_id)
    assert len(nodes) == 1 and nodes[0].job_id and not nodes[0].cache_hit
    return nodes[0].job_id


def test_enable_requires_existing_assets_agents_and_resident_foreman(world) -> None:
    """Target autonomy is opt-in and rejects missing required factory facilities."""
    from mao.mcp import McpServer, Session

    asset, grant, _bean = _objective(world)
    original = json.loads(json.dumps(world.config))
    mutations = (
        lambda config: config.pop("assets"),
        lambda config: config.pop("agents"),
        lambda config: config.pop("governance"),
        lambda config: config["foreman"].update({"mode": "session"}),
    )
    for change in mutations:
        world.config = json.loads(json.dumps(original))
        change(world.config)
        world.save_config()
        with error_code("CONTINUITY_CONFIG_INVALID"):
            _store(world).authorize(asset["id"], grant, by="ada")
    world.config = json.loads(json.dumps(original))
    world.config.pop("continuity")
    world.save_config()
    session = Session(role="foreman", actor="fake-agent@1")
    assert not any(
        t["name"].startswith("objective_") for t in McpServer(world.ctx, world.epochs, session).tools()
    )
    world.config = original
    world.save_config()
    objective = _store(world).authorize(asset["id"], grant, by="ada")
    assert objective["asset_id"] == asset["id"]


def test_human_authorizes_one_fixed_version_and_roles_are_scoped(world, tmp_path: Path) -> None:
    """A grant cannot silently change and agents cannot acquire management tools."""
    from mao.mcp import McpServer, Session

    asset, grant, _bean = _objective(world)
    path = tmp_path / "grant.json"
    path.write_text(json.dumps(grant), encoding="utf-8")
    code, first, err = world.cli("objective", "authorize", asset["id"], "--grant", str(path), "--by", "ada")
    assert code == 0, (first, err)
    from mao.base import utc_iso

    assert first["state"] == "active" and first["next_due_at"] == utc_iso(world.clock.now())
    again = _store(world).authorize(asset["id"], grant, by="ada")
    assert again["id"] == first["id"] and again["grant"] == grant
    with error_code("OBJECTIVE_STATE_INVALID"):
        _store(world).authorize(asset["id"], {**grant, "total_usd": grant["total_usd"] + 1}, by="ada")
    assert _store(world).get(first["id"])["grant"] == grant
    for role, agent in (("foreman", None), ("agent", "scout"), ("reviewer", None)):
        session = Session(role=role, actor="fake-agent@1", agent=agent)
        names = {tool["name"] for tool in McpServer(world.ctx, world.epochs, session).tools()}
        assert {"objective_get", "objective_list"} <= names
        assert not ({"objective_authorize", "objective_resume", "objective_revoke"} & names)
        assert ("objective_commit_plan" in names) is (role == "foreman")
        assert ("objective_evaluate" in names) is (role == "foreman")
        assert ("objective_dispatch" in names) is (role == "foreman")


def test_due_cycle_and_mail_are_unique_after_context_restart(world) -> None:
    """Repeated tick and reopened DB preserve the same cycle and single delivery."""
    from mao.context import Context
    from mao.governance.epochs import EpochStore
    from mao.sandbox import LocalProvider
    from tests.fakes.fakelib import local_commands

    objective, _asset, _grant, _bean = _authorize(world)
    cycle_id = _cycle(world, objective["id"])
    assert _store(world).collect() == []
    original_ids = world.ctx.ids
    world.ctx.close()
    provider = LocalProvider(world.root, commands=local_commands(), clock=world.clock)
    world.ctx = Context.open(world.root, provider=provider, clock=world.clock, ids=original_ids)
    world.epochs = EpochStore(world.ctx)
    world.snapshot = world.epochs.materialize(world.epochs.current())
    assert _store(world).collect() == []
    assert _store(world).get(objective["id"])["active_cycle"] == cycle_id
    rows = world.rows(
        "SELECT id, number, state FROM objective_cycles WHERE objective_id = ?", (objective["id"],)
    )
    assert [(r["id"], r["number"], r["state"]) for r in rows] == [(cycle_id, 1, "discovering")]
    notices = [m for m in world.messages("foreman") if m.kind == "objective_cycle"]
    assert len(notices) == 1 and cycle_id in json.dumps(notices[0].body)


def test_idle_cycle_reobserves_only_after_review_interval(world) -> None:
    """An honest idle result closes the slot and schedules another observation."""
    from mao.base import utc_iso

    objective, _asset, _grant, bean = _authorize(world)
    cycle_id = _cycle(world, objective["id"])
    plan, _observation = _plan(world, cycle_id, bean, disposition="idle")
    finished = _commit(world, cycle_id, plan)
    assert finished["state"] == "idle" and finished["plan_asset"] == plan["id"]
    target = _store(world).get(objective["id"])
    assert target["active_cycle"] is None
    assert target["next_due_at"] == utc_iso(world.clock.now() + timedelta(seconds=120))
    world.clock.advance(119)
    assert _store(world).collect() == []
    world.clock.advance(1)
    [next_id] = _store(world).collect()
    assert next_id != cycle_id
    [row] = world.rows("SELECT number, state FROM objective_cycles WHERE id = ?", (next_id,))
    assert (row["number"], row["state"]) == (2, "discovering")


def test_commit_plan_requires_real_observation_and_selected_candidate(world) -> None:
    """A missing observation cannot become an executing plan."""
    objective, _asset, _grant, bean = _authorize(world)
    cycle_id = _cycle(world, objective["id"])
    valid, observation = _plan(world, cycle_id, bean)
    bad_data = dict(valid["document"]["data"])
    bad_data["observations"] = []
    invalid = world.asset(
        key="invalid.plan",
        kind="objective_plan",
        subject="factory",
        beans=[bean],
        requires=valid["document"]["requires"],
        data=bad_data,
    )
    _value, error = _foreman_call(
        world, "objective_commit_plan", {"cycle_id": cycle_id, "plan": invalid["id"]}
    )
    assert error is not None and error["code"] == "OBJECTIVE_INVALID", error
    [cycle] = world.rows("SELECT state, plan_asset FROM objective_cycles WHERE id = ?", (cycle_id,))
    assert cycle["state"] == "discovering" and cycle["plan_asset"] is None
    accepted = _commit(world, cycle_id, valid)
    assert accepted["state"] == "executing" and accepted["plan_asset"] == valid["id"]
    assert _commit(world, cycle_id, valid) == accepted
    assert world.assets.get(observation["id"])["released"] is False


def test_scope_is_enforced_for_plan_worker_and_legacy_run(world) -> None:
    """An ungranted worker cannot submit work for a plan or gain legacy line rights."""
    objective, _asset, _grant, bean = _authorize(world)
    cycle_id = _cycle(world, objective["id"])
    bad, _observation = _plan(world, cycle_id, bean, agent="helper")
    _value, error = _foreman_call(world, "objective_commit_plan", {"cycle_id": cycle_id, "plan": bad["id"]})
    assert error is not None and error["code"] == "OBJECTIVE_FORBIDDEN", error
    assert world.rows("SELECT id FROM runs") == []
    good, _observation = _plan(world, cycle_id, bean)
    assert _commit(world, cycle_id, good)["state"] == "executing"
    _value, error = _start(world, cycle_id, bean, agent="helper")
    assert error is not None and error["code"] == "OBJECTIVE_FORBIDDEN", error
    wrong_input = world.note("different source material\n")
    _value, error = _start(world, cycle_id, wrong_input, agent="scout")
    assert error is not None and error["code"] == "OBJECTIVE_FORBIDDEN", error
    _value, error = tool_result(
        world.mcp("run_start", {"line": "note-trigger@1", "inputs": {"note": bean}}, agent="helper")
    )
    assert error is not None and error["code"] == "AGENT_LINE_FORBIDDEN", error
    assert world.rows("SELECT id FROM runs") == []


def test_revoke_stops_new_tasks_but_preserves_existing_state(world) -> None:
    """Revocation prevents new task starts and later cycles without deleting the plan."""
    objective, _asset, _grant, bean = _authorize(world)
    cycle_id = _cycle(world, objective["id"])
    plan, _observation = _plan(world, cycle_id, bean)
    assert _commit(world, cycle_id, plan)["state"] == "executing"
    _bind_dispatched_task(world, cycle_id)
    revoked = _store(world).revoke(objective["id"], reason="授权撤回", by="ada")
    assert revoked["state"] == "revoked"
    _value, error = _start(world, cycle_id, bean)
    assert error is not None and error["code"] in {"OBJECTIVE_FORBIDDEN", "OBJECTIVE_STATE_INVALID"}, error
    world.clock.advance(600)
    assert _store(world).collect() == [] and world.rows("SELECT id FROM runs") == []
    assert world.assets.get(plan["id"])["id"] == plan["id"]


def test_expiry_stops_an_undispatched_task(world) -> None:
    """Expiry is checked when an already planned worker tries to start."""
    objective, _asset, _grant, bean = _authorize(world, expires_s=70)
    cycle_id = _cycle(world, objective["id"])
    plan, _observation = _plan(world, cycle_id, bean)
    assert _commit(world, cycle_id, plan)["state"] == "executing"
    _bind_dispatched_task(world, cycle_id)
    world.clock.advance(11)
    _value, error = _start(world, cycle_id, bean)
    assert error is not None and error["code"] in {"OBJECTIVE_FORBIDDEN", "OBJECTIVE_STATE_INVALID"}, error
    assert world.rows("SELECT id FROM runs") == []
    assert _store(world).collect() == []
    assert _store(world).get(objective["id"])["state"] == "expired"


def test_real_task_evidence_satisfies_frozen_contract_once(world) -> None:
    """Only the real task run and its fresh job can complete this cycle."""
    objective, _asset, _grant, bean = _authorize(world)
    cycle_id = _cycle(world, objective["id"])
    plan, _observation = _plan(world, cycle_id, bean)
    assert _commit(world, cycle_id, plan)["state"] == "executing"
    run, error = _start(world, cycle_id, bean)
    assert error is None and run["id"], error
    again, error = _start(world, cycle_id, bean)
    assert error is None and again["id"] == run["id"]
    assert len(world.rows("SELECT id FROM runs")) == 1
    job_id = _finish_run(world, run["id"])
    proof = {"normal": {"kind": "job", "id": job_id}}
    finished = _foreman(world, "objective_evaluate", {"cycle_id": cycle_id, "evidence": proof})
    assert finished["state"] == "completed" and finished["evidence"] == proof
    assert finished["checks"] and all(check["passed"] for check in finished["checks"])
    assert {check["case"] for check in finished["checks"]} == {"normal"}
    repeated = _foreman(world, "objective_evaluate", {"cycle_id": cycle_id, "evidence": proof})
    assert repeated == finished
    assert _store(world).get(objective["id"])["active_cycle"] is None


def test_evaluate_rejects_old_job_and_self_report(world) -> None:
    """Old work and a claimed passed flag cannot stand in for cycle evidence."""
    objective, _asset, _grant, bean = _authorize(world)
    old_job = world.job(bean)
    assert old_job.state.value == "SEALED"
    cycle_id = _cycle(world, objective["id"])
    plan, _observation = _plan(world, cycle_id, bean)
    assert _commit(world, cycle_id, plan)["state"] == "executing"
    for proof in (
        {"normal": {"kind": "job", "id": old_job.id}},
        {"normal": {"kind": "job", "id": old_job.id, "passed": "true"}},
    ):
        _value, error = _foreman_call(world, "objective_evaluate", {"cycle_id": cycle_id, "evidence": proof})
        assert error is not None and error["code"] == "OBJECTIVE_EVIDENCE_INVALID", error
        [row] = world.rows("SELECT state FROM objective_cycles WHERE id = ?", (cycle_id,))
        assert row["state"] == "executing"


@pytest.mark.parametrize("total_usd,automatically_resumes", [(3.0, True), (1.0, False)])
def test_real_foreman_turn_budget_pauses_and_daily_reset_is_bounded(
    world, total_usd: float, automatically_resumes: bool
) -> None:
    """A real AgentHost turn with a fake CLI cost exhausts the configured grant."""
    import threading
    import time

    objective, _asset, _grant, bean = _authorize(world, total_usd=total_usd, daily_usd=1.0)
    cycle_id = _cycle(world, objective["id"])
    plan, _observation = _plan(world, cycle_id, bean, disposition="idle")
    world.script(
        {
            "cost_usd": 1.0,
            "default": [
                {
                    "do": "mcp",
                    "calls": [
                        {
                            "name": "objective_commit_plan",
                            "arguments": {"cycle_id": cycle_id, "plan": plan["id"]},
                        }
                    ],
                }
            ],
        }
    )
    host = world.host()
    host.pause("scout")
    host.pause("helper")
    stop = threading.Event()
    worker = threading.Thread(target=host.serve, args=(stop,), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 15
        turn = None
        while time.monotonic() < deadline:
            turns = host.turns("foreman")
            if turns and turns[-1].state != "running":
                turn = turns[-1]
                break
            time.sleep(0.05)
    finally:
        stop.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert turn is not None and turn.state == "ok" and turn.cost_usd == pytest.approx(1.0), turn
    [cycle] = world.rows("SELECT state, plan_asset FROM objective_cycles WHERE id = ?", (cycle_id,))
    assert (cycle["state"], cycle["plan_asset"]) == ("idle", plan["id"])
    assert _store(world).collect() == []
    paused = _store(world).get(objective["id"])
    assert paused["state"] == "paused"
    assert paused["spent_usd"] == pytest.approx(1.0)
    assert paused["reserved_usd"] == pytest.approx(0.0)
    assert _store(world).collect() == []
    world.clock.advance(24 * 3600 + 1)
    new_cycles = _store(world).collect()
    after = _store(world).get(objective["id"])
    if automatically_resumes:
        assert after["state"] == "active" and len(new_cycles) == 1
    else:
        assert after["state"] == "paused" and new_cycles == []


def test_foreman_write_requires_live_cycle_turn_and_lease(world) -> None:
    """The foreman role alone cannot forge a cycle turn or write after lease loss."""
    from mao.durable import LeaseStore
    from tests.acceptance.m15.talk_world import foreman_call

    objective, _asset, _grant, bean = _authorize(world)
    cycle_id = _cycle(world, objective["id"])
    plan, _observation = _plan(world, cycle_id, bean)
    _value, error = foreman_call(world, "objective_commit_plan", {"cycle_id": cycle_id, "plan": plan["id"]})
    assert error is not None and error["code"] == "OBJECTIVE_FORBIDDEN", error
    assert _commit(world, cycle_id, plan)["state"] == "executing"
    assert LeaseStore(world.ctx).release_fenced(world._m17_fenced_leases[cycle_id])
    _value, error = _foreman_call(world, "objective_dispatch", {"cycle_id": cycle_id, "task_id": "t1"})
    assert error is not None and error["code"] == "LEASE_LOST", error
    assert world.messages("scout") == []
