"""邮箱里的 MCP 请求。"""

from __future__ import annotations

from typing import Any

from mao.mcp import McpServer


class McpCtlHandler:
    def __init__(self, server: McpServer) -> None:
        self.server = server

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        rid = request.get("id")
        if request.get("op") != "mcp":
            return {
                "id": rid,
                "ok": False,
                "result": None,
                "error": {"code": "CTL_REQUEST_INVALID", "message": "工位通道不接受这个操作"},
            }
        args = request.get("args") if isinstance(request.get("args"), dict) else {}
        message = self.server.handle(args.get("message"))
        return {"id": rid, "ok": True, "result": {"message": message}, "error": None}
