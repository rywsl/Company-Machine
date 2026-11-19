from __future__ import annotations

import threading
import urllib.request
from pathlib import Path

from mao.context import Context
from mao.sandbox import LocalProvider
from mao.snapshot import Snapshot
from mao.webapp.server import WebServer

FIXTURE = Path("spec/examples/registry")


def test_frontend_is_api_hydrated_and_has_local_only_shell(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    with Context.open(root, provider=LocalProvider(root)) as ctx:
        server = WebServer(ctx, snapshot=Snapshot.load(FIXTURE))
        port = server.start(0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                html = response.read().decode("utf-8")
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/app.js", timeout=5) as response:
                app = response.read().decode("utf-8")
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/styles.css", timeout=5) as response:
                css = response.read().decode("utf-8")
        finally:
            server.close()

    assert 'id="app"' in html
    assert 'src="/app.js"' in html
    assert "/api/web/bootstrap" in app
    assert "/api/web/graph?scope=workarea" in app
    assert "localStorage" in app
    assert "FACTORY" in app
    assert "WORKAREA" in app
    assert "登录" not in html + app + css
    assert "租户" not in html + app + css
    assert "邀请" not in html + app + css
    assert "#edf1f4" in css


def test_frontend_does_not_embed_mock_graph_or_secret_values() -> None:
    app = Path("src/mao/webapp/static/app.js").read_text(encoding="utf-8")
    assert "wa-vision" not in app
    assert "OpenAI" not in app
    assert "ANTHROPIC_API_KEY" not in app
    assert "MODEL_API_KEY" not in app
