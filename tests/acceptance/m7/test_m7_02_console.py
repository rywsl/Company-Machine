"""M7-02 验收：本机只读 Web 控制台（spec/system/console.md 第 7 章）。"""

from __future__ import annotations

import http.client
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlencode

import pytest

pytest.importorskip("mao.console")

from tests.acceptance.m2.world import seal_input  # noqa: E402
from tests.acceptance.m3.gov_world import open_world  # noqa: E402

TOKEN = "t" * 40
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class Console:
    def __init__(self, tmp: Path) -> None:
        from mao.console import ConsoleServer

        self.w = open_world(tmp)
        self.server = ConsoleServer(self.w.ctx, token=TOKEN, epochs=self.w.epochs)
        self.port = self.server.start(port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        cookie: str | None = None,
        host: str | None = None,
        form: dict | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if cookie:
            headers["Cookie"] = f"mao_console={cookie}"
        body = None
        if form is not None:
            body = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def get_json(self, path: str):
        status, _headers, body = self.request("GET", path, token=TOKEN)
        assert status == 200, (path, status, body[:200])
        return json.loads(body)

    def close(self) -> None:
        self.server.close()
        self.thread.join(5)


@pytest.fixture
def console(tmp_path: Path):
    c = Console(tmp_path)
    try:
        yield c
    finally:
        c.close()


def _cli_json(root: Path, capsys, *args: str):
    from mao.cli import main

    capsys.readouterr()
    assert main(["--root", str(root), "--json", *args]) in (0, 1)
    return json.loads(capsys.readouterr().out)


def _session(ctx, session_id: str) -> None:
    ctx.db.query(
        "INSERT INTO foreman_sessions (id, wake_id, role, runtime, actor, model, epoch, state, cost_usd, started_at) "
        "VALUES (?, 'wk-0001', 'foreman', 'OperatorRuntime:fake-agent@1', 'fake-agent@1', 'm', 'x', 'ok', 0.1, "
        "'2026-11-10T00:00:00.000Z')",
        (session_id,),
    )


def test_token_file(tmp_path: Path) -> None:
    from mao.console import load_token
    from mao.errors import FactoryError

    root = tmp_path / "root"
    root.mkdir()
    token = load_token(root)
    path = root / "console.token"
    assert path.read_text(encoding="utf-8").strip() == token
    assert len(token) >= 32
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert load_token(root) == token
    path.write_text("short", encoding="utf-8")
    with pytest.raises(FactoryError) as err:
        load_token(root)
    assert err.value.code == "CONSOLE_TOKEN_INVALID"


def test_requires_token(console: Console) -> None:
    status, headers, body = console.request("GET", "/api/status")
    assert status == 401 and json.loads(body) == {"error": "unauthorized"}
    assert headers["www-authenticate"].startswith("Bearer")
    status, headers, _ = console.request("GET", "/")
    assert status == 303 and headers["location"] == "/login"
    assert console.request("GET", "/api/status", token="x" * 40)[0] == 401
    assert console.request("GET", f"/api/status?token={TOKEN}")[0] == 401
    assert console.request("GET", "/metrics")[0] == 401
    assert console.request("GET", "/api/status", token=TOKEN)[0] == 200
    status, _, body = console.request("GET", "/healthz")
    assert (status, body) == (200, b"ok")


def test_login_cookie(console: Console) -> None:
    status, headers, _ = console.request("POST", "/login", form={"token": TOKEN})
    assert status == 303 and headers["location"] == "/"
    cookie = headers["set-cookie"]
    assert cookie.startswith(f"mao_console={TOKEN}")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    status, headers, body = console.request("GET", "/", cookie=TOKEN)
    assert status == 200 and headers["content-type"] == "text/html; charset=utf-8"
    wrong = "wrong-" + "z" * 40
    status, _, body = console.request("POST", "/login", form={"token": wrong})
    assert status == 401
    assert wrong.encode() not in body


def test_read_only(console: Console) -> None:
    def counts() -> tuple[int, int]:
        db = console.w.ctx.db
        return (
            db.query("SELECT COUNT(*) AS n FROM events")[0]["n"],
            db.query("SELECT COUNT(*) AS n FROM wakes")[0]["n"],
        )

    before = counts()
    for method in ("PUT", "DELETE", "PATCH", "POST"):
        status, headers, _ = console.request(method, "/api/status", token=TOKEN)
        assert status == 405, method
        assert headers["allow"] == "GET"
    status, headers, _ = console.request("DELETE", "/login")
    assert status == 405 and headers["allow"] == "GET, POST"
    for path in ("/", "/api/status", "/wakes", "/events", "/metrics"):
        assert console.request("GET", path, token=TOKEN)[0] == 200
    assert counts() == before


def test_security_headers_and_host(console: Console) -> None:
    for path, token, expected in (
        ("/api/status", TOKEN, 200),
        ("/api/status", None, 401),
        ("/jobs/j-none", TOKEN, 404),
    ):
        status, headers, _ = console.request("GET", path, token=token)
        assert status == expected, path
        for name, value in HEADERS.items():
            assert headers[name.lower()] == value, (path, name)
    status, headers, _ = console.request(
        "GET", "/api/status", token=TOKEN, host=f"evil.example:{console.port}"
    )
    assert status == 421
    assert headers["x-frame-options"] == "DENY"
    assert console.request("GET", "/api/status", token=TOKEN, host=f"localhost:{console.port}")[0] == 200


def test_html_escaped(console: Console) -> None:
    from mao.foreman import WakeQueue

    WakeQueue(console.w.ctx).file("other", "<script>alert(1)</script>", {"id": "x"})
    status, _, body = console.request("GET", "/wakes", token=TOKEN)
    text = body.decode("utf-8")
    assert status == 200
    assert "<script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text


def test_api_matches_cli(console: Console, capsys) -> None:
    from mao.foreman import WakeQueue

    queue = WakeQueue(console.w.ctx)
    queue.file("other", "a", {"id": "a"})
    queue.done(queue.file("other", "b", {"id": "b"}).id, by="supervisor")
    _session(console.w.ctx, "fs-0001")
    root = console.w.ctx.root
    api = console.get_json("/api/status")
    cli = _cli_json(root, capsys, "status")
    api.pop("at")
    cli.pop("at")
    assert api == cli
    assert console.get_json("/api/wakes") == _cli_json(root, capsys, "foreman", "wakes")
    assert console.get_json("/api/wakes?status=open") == _cli_json(
        root, capsys, "foreman", "wakes", "--status", "open"
    )
    assert console.get_json("/api/sessions") == _cli_json(root, capsys, "foreman", "sessions")


def test_pages_render(console: Console) -> None:
    from mao.runner import Runner
    from mao.runs import LineRunner

    w = console.w
    runner = Runner(w.ctx, w.snapshot)
    note = seal_input(w.ctx, w.snapshot, "note.text@1")
    job = runner.run(runner.submit(w.snapshot.key("Station", "upper@1"), {"note": note}, {}).id)
    assert job.state.value == "SEALED"
    [out] = job.outputs["note"]
    run = LineRunner(w.ctx, w.snapshot, runner).start_run("note-trigger@1", {"note": note})
    _session(w.ctx, "fs-0001")
    html_pages = [
        "/",
        "/runs",
        f"/runs/{run.id}",
        f"/jobs/{job.id}",
        f"/beans/{out}",
        "/changesets",
        "/wakes",
        "/sessions",
        "/sessions/fs-0001",
        "/approvals",
        "/events",
        "/login",
    ]
    for path in html_pages:
        status, headers, body = console.request("GET", path, token=TOKEN)
        assert status == 200, (path, body[:300])
        assert headers["content-type"] == "text/html; charset=utf-8", path
    status, headers, body = console.request("GET", "/metrics", token=TOKEN)
    assert status == 200
    assert headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert b"# TYPE mao_jobs gauge" in body
    assert console.get_json(f"/api/jobs/{job.id}")["id"] == job.id
    assert console.get_json(f"/api/runs/{run.id}")["id"] == run.id
    for path in ("/jobs/j-none", "/sessions/fs-9999", "/api/jobs/j-none", "/api/sessions/fs-9999"):
        assert console.request("GET", path, token=TOKEN)[0] == 404, path
