from __future__ import annotations

import re
import threading
import urllib.request
from pathlib import Path

from mao.context import Context
from mao.sandbox import LocalProvider
from mao.snapshot import Snapshot
from mao.webapp.server import WebServer

FIXTURE = Path("spec/examples/registry")


def test_frontend_is_react_production_bundle_and_has_local_only_shell(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    with Context.open(root, provider=LocalProvider(root)) as ctx:
        server = WebServer(ctx, snapshot=Snapshot.load(FIXTURE))
        port = server.start(0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                html = response.read().decode("utf-8")
            script_match = re.search(r'<script type="module" crossorigin src="([^"]+\.js)">', html)
            style_match = re.search(r'<link rel="stylesheet" crossorigin href="([^"]+\.css)">', html)
            assert script_match
            assert style_match
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{script_match.group(1)}", timeout=5
            ) as response:
                bundle = response.read().decode("utf-8")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{style_match.group(1)}", timeout=5
            ) as response:
                css = response.read().decode("utf-8")
        finally:
            server.close()

    assert 'id="root"' in html
    assert 'type="module"' in html
    assert "/api/web/bootstrap" in bundle
    assert "FACTORY" in bundle
    assert "WORKAREA" in bundle
    assert "登录" not in html + bundle + css
    assert "租户" not in html + bundle + css
    assert "邀请" not in html + bundle + css
    assert "#e9eef1" in css


def test_frontend_source_is_componentized_and_does_not_embed_mock_graph_or_secrets() -> None:
    source = Path("frontend/src/App.tsx").read_text(encoding="utf-8")
    package = Path("frontend/package.json").read_text(encoding="utf-8")
    assert "ReactFlow" in source
    assert "localStorage" in source
    assert "internal_steps" in source
    assert "second canvas" not in source.lower()
    assert "wa-vision" not in source
    assert "OpenAI" not in source
    assert "ANTHROPIC_API_KEY" not in source
    assert "MODEL_API_KEY" not in source
    assert '"react"' in package
    assert '"@xyflow/react"' in package
