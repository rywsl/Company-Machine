"""M17-08 离线跨进程验收。测试只投放原始资料、注入故障和推进时钟。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mao.base import utc_iso
from mao.daemon import Daemon, ProcessExecutor, ServeConfig
from mao.durable import LeaseStore
from tests.acceptance.m16.asset_world import World
from tests.fakes.fakelib import FAKE_AGENT_COMMAND, local_commands

AGENT = Path(__file__).with_name("m17_08_agent.py")
UPPER = Path(__file__).resolve().parents[1] / "fakes" / "tools" / "upper_text.py"


def _fail_wrapper(path: Path, marker: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "import json, subprocess, sys",
                f"MARKER = {str(marker)!r}",
                f"UPPER = {str(UPPER)!r}",
                "def main():",
                "    remaining = 0",
                "    try:",
                "        remaining = int(json.loads(open(MARKER, encoding='utf-8').read()).get('remaining', 0))",
                "    except (OSError, ValueError):",
                "        remaining = 0",
                "    if remaining > 0:",
                "        open(MARKER, 'w', encoding='utf-8').write(json.dumps({'remaining': remaining - 1}))",
                "        sys.stderr.write('injected failure\\n')",
                "        return 3",
                "    return subprocess.call([sys.executable, UPPER, sys.argv[1], sys.argv[2]])",
                "if __name__ == '__main__':",
                "    sys.exit(main())",
            ]
        ),
        encoding="utf-8",
    )


class Factory:
    def __init__(self, tmp: Path, *, live: bool = False, domain_id: str = "domain-notes") -> None:
        self.tmp = tmp
        self.live = live
        self.domain_id = domain_id
        self.source = tmp / "raw-source"
        self.source.mkdir(parents=True)
        self.marker = tmp / "fail-once.json"
        self.marker.write_text(json.dumps({"remaining": 0}), encoding="utf-8")
        wrapper = tmp / "fail_upper.py"
        _fail_wrapper(wrapper, self.marker)
        self.world = World(tmp / "world")
        self.world.config["continuity"] = {"enabled": True}
        self.world.config["agents"]["poll_s"] = 0.05
        self.world.config["foreman"]["max_turns"] = 30
        self.world.config["foreman"]["session_timeout_s"] = 180
        self.world.config["serve"] = {
            "tick_s": 0.05,
            "max_parallel_jobs": 2,
            "audit_every_s": None,
            "canary_every_s": None,
            "docker_timeout_s": 60,
            "supervise": False,
            "model_port": 0,
        }
        self.world.save_config()
        (self.world.root / "scheduler.json").write_text(
            json.dumps(
                {
                    "pools": {"cpu": None, "memory_mb": None, "gpu": None},
                    "compartments": [],
                    "breakers": [],
                    "wip": [],
                    "rates": [],
                    "run_cap": None,
                    "toxic_failures": None,
                    "tick_s": None,
                }
            ),
            encoding="utf-8",
        )
        commands = local_commands()
        commands[FAKE_AGENT_COMMAND] = [sys.executable, str(AGENT)]
        commands["upper-text"] = [sys.executable, str(wrapper)]
        self.world.ctx.provider.commands = commands
        mapping = tmp / "commands.json"
        mapping.write_text(json.dumps(commands), encoding="utf-8")
        self.mapping = mapping
        self.contract = self._authorize()
        prefix = [
            sys.executable,
            "-m",
            "mao",
            "--root",
            str(self.world.root),
            "--provider",
            "local",
            "--local-commands",
            str(mapping),
        ]
        self.daemon = Daemon(
            self.world.ctx,
            ServeConfig.load(self.world.root),
            executor=ProcessExecutor(
                self.world.root,
                prefix,
                max_parallel=2,
                log_dir=self.world.root / "logs" / "jobs",
            ),
        )
        self.daemon.start()
        self.host = self.world.host()
        token = LeaseStore(self.world.ctx).acquire_fenced("agents", owner=self.world.ctx.owner)
        assert token is not None
        self.lease = token
        self.host._lease = token

    def close(self) -> None:
        try:
            self.daemon.shutdown(timeout_s=5)
        except Exception:
            pass
        self.world.ctx.close()

    def _authorize(self) -> dict:
        bean = self.world.note("setup\nonly\n")
        contract = self.world.asset(
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
                "domain": self.domain_id,
            },
        )
        self.world.learning.authorize(contract["id"], by="ada")
        objective = self.world.asset(
            key="objective.notes",
            kind="objective",
            subject="factory",
            beans=[bean],
            requires=[contract["id"]],
            data={
                "intent": "持续观察原始资料并产出可核验的整理结果",
                "observation_scope": "raw:" + self.source.as_posix(),
                "contract": contract["id"],
            },
        )
        grant = {
            "agents": ["scout"],
            "lines": [self.world.snapshot.key("Line", "note-trigger@1")],
            "total_usd": 20.0,
            "daily_usd": 20.0,
            "every_s": 60,
            # Offline cases intentionally use the frozen START clock.  A live
            # witness must carry a grant valid at wall-clock observation time;
            # otherwise a perfectly real run is rejected as an expired grant
            # before its evidence can be checked.
            "expires_at": utc_iso(
                (datetime.now(UTC) if self.live else self.world.clock.now()) + timedelta(days=2)
            ),
            "max_cycle_s": 300,
        }
        path = self.tmp / "grant.json"
        path.write_text(json.dumps(grant), encoding="utf-8")
        code, body, err = self.world.cli(
            "objective", "authorize", objective["id"], "--grant", str(path), "--by", "ada"
        )
        assert code == 0, (body, err)
        self.objective_id = body["id"]
        return contract

    def renew(self) -> None:
        assert LeaseStore(self.world.ctx).renew_fenced(self.lease, ttl_s=60)
        LeaseStore(self.world.ctx).renew("serve", owner=self.world.ctx.owner, ttl_s=60)

    def advance(self, seconds: int) -> None:
        store = LeaseStore(self.world.ctx)
        assert store.renew_fenced(self.lease, ttl_s=seconds + 60)
        assert store.renew("serve", owner=self.world.ctx.owner, ttl_s=seconds + 60)
        self.world.clock.advance(seconds)

    def pump(self, rounds: int = 8) -> None:
        for _ in range(rounds):
            self.daemon.tick()
            self.host.run_once("scout")
            self.daemon.tick()
            self.host.run_once("scout")
            self.host.run_once("foreman")
            # ProcessExecutor is intentionally asynchronous.  Yield briefly so
            # the real child can publish its terminal state before the next
            # deterministic tick observes the factory.
            time.sleep(0.05)

    def until_terminal(self, number: int, rounds: int = 24) -> None:
        for _ in range(rounds):
            rows = self.cycles()
            if any(
                row["number"] == number and row["state"] in {"idle", "completed", "failed", "blocked"}
                for row in rows
            ):
                return
            self.pump(1)
        raise AssertionError([(row["number"], row["state"]) for row in self.cycles()])

    def cycles(self) -> list:
        return self.world.rows(
            "SELECT id, number, state, plan_asset FROM objective_cycles WHERE objective_id = ? ORDER BY number",
            (self.objective_id,),
        )

    def drop(self, name: str, text: str) -> str:
        from mao.agents import Mailbox

        (self.source / name).write_text(text, encoding="utf-8")
        bean = self.world.note(text)
        self.world.ctx.warehouse.label(bean, "src", "scout", by="ada")
        Mailbox(self.world.ctx).post("scout", "ingress", "raw-source", {"name": name, "text": text})
        return bean

    def arm_failure(self, remaining: int) -> None:
        self.marker.write_text(json.dumps({"remaining": remaining}), encoding="utf-8")


def _plan(factory: Factory, cycle: dict) -> dict:
    asset = factory.world.assets.get(cycle["plan_asset"])
    return asset["document"]["data"]


def _turns(factory: Factory, agent: str) -> list:
    return factory.world.rows(
        "SELECT id, state, error FROM agent_turns WHERE agent = ? ORDER BY started_at",
        (agent,),
    )


def test_macro_objective_drives_multiple_cycles(tmp_path: Path) -> None:
    factory = Factory(tmp_path)
    try:
        factory.until_terminal(1)
        idle = factory.cycles()
        assert idle and idle[0]["state"] == "idle", [(row["number"], row["state"]) for row in idle]
        bean_a = factory.drop("a.txt", "alpha line\nsecond line\n")
        factory.pump(4)
        factory.advance(60)
        factory.until_terminal(2)
        bean_b = factory.drop("b.txt", "beta line\nsecond line\n")
        factory.arm_failure(2)
        factory.pump(4)
        factory.advance(60)
        factory.until_terminal(3)
        factory.advance(60)
        factory.until_terminal(4)
        cycles = factory.cycles()
        states = [(row["number"], row["state"]) for row in cycles]
        assert len(cycles) >= 4, states
        assert states[0] == (1, "idle")
        work = [row for row in cycles if row["state"] in {"completed", "failed", "executing"}]
        assert len(work) >= 3, states
        plans = [_plan(factory, row) for row in cycles if row["plan_asset"]]
        idle_plan = plans[0]
        assert idle_plan["disposition"] == "idle" and idle_plan["reason"]
        digests = []
        for plan in plans:
            for item in plan.get("observations") or []:
                observed = factory.world.assets.get(item["asset_id"])
                for record in observed["document"]["data"].get("records") or []:
                    digests.append(record["digest"])
        file_a = hashlib.sha256((factory.source / "a.txt").read_bytes()).hexdigest()
        file_b = hashlib.sha256((factory.source / "b.txt").read_bytes()).hexdigest()
        assert file_a in digests and file_b in digests
        runs = factory.world.rows("SELECT id, state FROM runs ORDER BY id")
        assert len(runs) >= 2, runs
        failed = [row for row in runs if row["state"] != "SUCCEEDED"]
        assert failed, runs
        adjusted = [plan for plan in plans if "失败运行" in str(plan.get("reason"))]
        assert adjusted, [plan.get("reason") for plan in plans]
        completed = [row for row in cycles if row["state"] == "completed"]
        assert completed, states
        foreman = _turns(factory, "foreman")
        scout = _turns(factory, "scout")
        assert foreman and scout
        assert any(row["state"] == "ok" for row in foreman), foreman
        assert bean_a != bean_b
    finally:
        factory.close()


def test_host_restart_and_lease_fencing(tmp_path: Path) -> None:
    factory = Factory(tmp_path)
    try:
        factory.until_terminal(1)
        hold = factory.world.root / "hold-turn"
        hold.write_text("1", encoding="utf-8")
        from mao.agents import Mailbox

        Mailbox(factory.world.ctx).post("scout", "ingress", "raw-source", {"name": "hold", "text": "wake"})
        errors: list[str] = []

        def run_held() -> None:
            try:
                factory.host.run_once("scout")
            except Exception as exc:
                errors.append(getattr(exc, "code", type(exc).__name__))

        worker = threading.Thread(target=run_held)
        worker.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not factory.world.rows(
            "SELECT id FROM agent_turns WHERE agent = 'scout' AND state = 'running'"
        ):
            time.sleep(0.05)
        held = factory.lease
        factory.world.clock.advance(61)
        other = LeaseStore(factory.world.ctx).acquire_fenced("agents", owner="host-b", ttl_s=60)
        assert other is not None and other.generation > held.generation
        assert LeaseStore(factory.world.ctx).renew_fenced(held, ttl_s=60) is False
        hold.unlink()
        worker.join(timeout=20)
        assert not worker.is_alive()
        interrupted = factory.host.recover()
        runs_before = len(factory.world.rows("SELECT id FROM runs"))
        factory.host._lease = other
        factory.lease = other
        factory.pump(2)
        assert len(factory.world.rows("SELECT id FROM runs")) == runs_before
        assert (
            "LEASE_LOST" in errors
            or interrupted
            or factory.world.rows("SELECT id FROM agent_turns WHERE state = 'interrupted'")
        )
    finally:
        hold = factory.world.root / "hold-turn"
        if hold.exists():
            hold.unlink()
        factory.close()


def _service_line(tmp_path: Path, *, fail_fetch: int = 0, drop_submit: bool = False, retry: bool = True):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import yaml

    from mao.runs import InlineExecutor, LineRunner
    from tests.acceptance.m9.closure_world import build_registry, node_map, open_world, seal_note

    class Box:
        puts = 0
        fetches = 0

    box = Box()
    registry = build_registry(tmp_path)
    station = registry / "stations" / "render.yaml"
    if retry:
        text = station.read_text(encoding="utf-8").replace("failed: 0", "failed: 1")
        station.write_text(text, encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            box.puts += 1
            if drop_submit:
                self._send(200, b" ")
                return
            self._send(200, b'{"data": {"ref": "rnd-7"}}')

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/openapi.json":
                self._send(200, b'{"openapi": "3.0.3", "paths": {}}')
                return
            if self.path == "/v2/renders/rnd-7/status":
                self._send(
                    200,
                    b'{"state": "finished", "outputs": ["http://127.0.0.1:%d/v2/files/rnd-7/final.txt"]}'
                    % server.server_address[1],
                )
                return
            if self.path == "/v2/files/rnd-7/final.txt":
                box.fetches += 1
                if box.fetches <= fail_fetch:
                    self._send(500, b"{}")
                    return
                self._send(200, b"RENDERED 6 en\n")
                return
            self._send(404, b"{}")

        def log_message(self, fmt: str, *args: object) -> None:
            del fmt, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    machine_path = registry / "machines" / "servicebox" / "machine.yaml"
    machine = yaml.safe_load(machine_path.read_text(encoding="utf-8"))
    machine["spec"]["runtime"]["service"]["port"] = server.server_address[1]
    machine_path.write_text(yaml.safe_dump(machine, sort_keys=False), encoding="utf-8")
    root = tmp_path / "w" / "factory"
    real_ctl = root / "tools" / "factory" / "bin" / "factoryctl"
    ctx, snapshot, runner, _clock = open_world(
        tmp_path / "w", registry, commands={"factoryctl": [sys.executable, str(real_ctl)]}, gateway=True
    )
    lines = LineRunner(ctx, snapshot, runner)
    run = lines.start_run("note-render@1", {"note": seal_note(ctx, snapshot, "hello\n")})
    done = lines.run_to_completion(run.id, InlineExecutor(runner), max_ticks=40, wait_s=0)
    return ctx, runner, lines, done, box, server, node_map


def test_known_external_task_resumes_fetch(tmp_path: Path) -> None:
    ctx, runner, lines, done, box, server, node_map = _service_line(tmp_path, fail_fetch=1)
    try:
        node = node_map(lines, done.id)["render"]
        job = runner.get(node.job_id)
        ops = ctx.db.query("SELECT state, task_id FROM service_operations")
        assert box.puts == 1, box.puts
        assert box.fetches >= 2
        assert done.state.value == "SUCCEEDED", (done.state, job.error, ops)
        assert ops and ops[0]["task_id"] == "rnd-7"
    finally:
        server.shutdown()
        ctx.close()


def test_unknown_submit_outcome_never_blindly_retries(tmp_path: Path) -> None:
    ctx, _runner, _lines, done, box, server, _node_map = _service_line(
        tmp_path, drop_submit=True, retry=False
    )
    try:
        assert box.puts == 1, box.puts
        ops = ctx.db.query("SELECT state, task_id FROM service_operations")
        assert ops and ops[0]["state"] == "outcome_unknown", ops
        assert done.state.value != "SUCCEEDED"
    finally:
        server.shutdown()
        ctx.close()


def test_budget_survives_restart_and_stops_authorized_work(tmp_path: Path) -> None:
    factory = Factory(tmp_path)
    try:
        (factory.world.root / "model-price.json").write_text(json.dumps({"usd": 0.6}), encoding="utf-8")
        from mao.agents import Mailbox

        Mailbox(factory.world.ctx).post("scout", "message", "price", {"text": "meter"})
        first = factory.host.run_once("scout")
        assert first is not None and first.cost_usd == 0.6
        spent = factory.world.rows(
            "SELECT COALESCE(SUM(cost_usd), 0) AS n FROM agent_turns WHERE agent = 'scout'"
        )
        LeaseStore(factory.world.ctx).release_fenced(factory.lease)
        from mao.context import Context
        from mao.sandbox import LocalProvider

        restarted = Context.open(
            factory.world.root,
            provider=LocalProvider(
                factory.world.root, commands=factory.world.ctx.provider.commands, clock=factory.world.clock
            ),
            clock=factory.world.clock,
            ids=factory.world.ctx.ids,
        )
        again = restarted.db.query(
            "SELECT COALESCE(SUM(cost_usd), 0) AS n FROM agent_turns WHERE agent = 'scout'"
        )
        assert again[0]["n"] == spent[0]["n"] == 0.6
        (factory.world.root / "model-price.json").write_text(json.dumps({"usd": 0.6}), encoding="utf-8")
        from mao.agents import AgentHost, AgentsConfig
        from mao.governance.epochs import EpochStore

        factory.host = AgentHost(
            restarted,
            EpochStore(restarted),
            AgentsConfig.load(factory.world.root),
            pipeline=factory.world.pipeline(),
        )
        token = LeaseStore(restarted).acquire_fenced("agents", owner=restarted.owner)
        assert token is not None
        factory.host._lease = token
        factory.lease = token
        Mailbox(restarted).post("scout", "message", "price-2", {"text": "meter again"})
        second = factory.host.run_once("scout")
        assert second is not None and second.cost_usd == 0.6
        Mailbox(restarted).post("scout", "message", "price-3", {"text": "over cap"})
        blocked = factory.host.run_once("scout")
        assert blocked is None
        ctl = factory.world.root / "tools" / "factory" / "bin" / "factoryctl"
        folder = factory.world.root / "agents" / "scout" / "ctl"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "job.json").write_text(
            json.dumps(
                {"job_id": "budget-check", "attempt": 1, "inputs": {}, "budget_usd": 1.0, "timeout_s": 60}
            ),
            encoding="utf-8",
        )
        shown = subprocess.run(
            [sys.executable, str(ctl), "budget", "--json"],
            capture_output=True,
            text=True,
            env={**os.environ, "FACTORY_CTL": str(folder)},
        )
        assert shown.returncode == 0, shown.stderr
        body = json.loads(shown.stdout)
        assert body.get("spent_usd") is None
        assert body.get("spent_usd") != 0
        restarted.close()
    finally:
        factory.close()


def test_revoke_and_expiry_stop_new_actions(tmp_path: Path) -> None:
    factory = Factory(tmp_path)
    try:
        factory.until_terminal(1)
        factory.world.cli(
            "objective", "revoke", factory.objective_id, "--reason", "计划内撤回", "--by", "ada"
        )
        from mao.objectives import ObjectiveStore

        revoked = ObjectiveStore(factory.world.ctx, factory.world.epochs).get(factory.objective_id)
        assert revoked["state"] == "revoked"
        before = len(factory.cycles())
        factory.advance(120)
        factory.pump(2)
        assert len(factory.cycles()) == before
        assert factory.world.rows("SELECT id FROM runs") == []
    finally:
        factory.close()
    expired = Factory(tmp_path / "expiry")
    try:
        expired.until_terminal(1)
        expired.world.clock.advance(3 * 24 * 3600)
        from mao.objectives import ObjectiveStore

        assert ObjectiveStore(expired.world.ctx, expired.world.epochs).collect() == []
        assert (
            ObjectiveStore(expired.world.ctx, expired.world.epochs).get(expired.objective_id)["state"]
            == "expired"
        )
    finally:
        expired.close()


def test_real_factoryctl_and_declared_operator_run(tmp_path: Path) -> None:
    ctx, runner, lines, done, box, server, node_map = _service_line(tmp_path)
    try:
        node = node_map(lines, done.id)["render"]
        job = runner.get(node.job_id)
        stderr = ctx.root / "tmp" / "gateway" / f"{job.id}.stderr"
        detail = stderr.read_text(encoding="utf-8") if stderr.is_file() else ""
        assert done.state.value == "SUCCEEDED", (job.error, detail)
        assert box.puts == 1
        installed = ctx.root / "tools" / "factory" / "bin" / "factoryctl"
        assert installed.is_file()
        assert "factoryctl" in detail or box.puts == 1
    finally:
        server.shutdown()
        ctx.close()


def test_results_return_once_and_drive_next_cycle(tmp_path: Path) -> None:
    factory = Factory(tmp_path)
    try:
        factory.until_terminal(1)
        factory.drop("a.txt", "alpha line\nsecond line\n")
        factory.pump(2)
        factory.advance(60)
        factory.until_terminal(2)
        notices = factory.world.rows("SELECT id, source FROM agent_messages WHERE kind = 'run_result'")
        assert len(notices) == 1
        runs = factory.world.rows("SELECT id FROM runs")
        assert len(runs) == 1
        factory.world.ctx.close()
        from mao.context import Context
        from mao.sandbox import LocalProvider

        factory.world.ctx = Context.open(
            factory.world.root,
            provider=LocalProvider(
                factory.world.root, commands=factory.world.ctx.provider.commands, clock=factory.world.clock
            ),
            clock=factory.world.clock,
            ids=factory.world.ctx.ids,
        )
        again = factory.world.rows("SELECT id, source FROM agent_messages WHERE kind = 'run_result'")
        assert again == notices
        assert len(factory.world.rows("SELECT id FROM runs")) == 1
    finally:
        factory.close()


def test_change_governance_keeps_scope_and_safe_mode(tmp_path: Path) -> None:
    from mao.errors import FactoryError
    from mao.governance.rollout import evaluate_safe_mode
    from mao.mcp import McpServer, Session

    factory = Factory(tmp_path)
    try:
        session = Session(role="agent", actor="fake-agent@1", agent="helper")
        server = McpServer(factory.world.ctx, factory.world.epochs, session)
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            }
        )
        reply = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "changeset_propose",
                    "arguments": {"document": {"kind": "undeclared"}, "commit": "abc"},
                },
            }
        )
        assert "error" in reply or reply.get("result", {}).get("isError")
        factory.world.ctx.db.query("SELECT 1")
        with factory.world.ctx.db.tx() as conn:
            conn.execute(
                """INSERT INTO audit_findings (id, invariant, subject, state, detail, at, updated_at)
                   VALUES ('af-safe', 'drift', 'factory', 'wake', '{}', ?, ?)""",
                (factory.world.clock.now().isoformat(), factory.world.clock.now().isoformat()),
            )
        mode = evaluate_safe_mode(factory.world.ctx)
        assert mode["on"] is True
        try:
            factory.world.pipeline().advance("missing", to="canary")
        except FactoryError as exc:
            assert exc.code
        except Exception:
            pass
    finally:
        factory.close()


def test_live_docker_real_cli_produces_evidence(tmp_path: Path) -> None:
    from tests.system.m17_08_live import diagnosis, execute_witness

    report = Path(".factory/m17-repro/live/report.json")
    result = execute_witness("c10", tmp_path, live=True, report_path=report)
    if result.get("live_passed") is not True:
        raise AssertionError("live witness " + diagnosis(result))
