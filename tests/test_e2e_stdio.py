"""End-to-end test: a real stdio server process talking to a mock model."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from mcp import Client, StdioServerParameters

from tests.conftest import json_content


class MockModelHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible endpoint that echoes the last user message."""

    requests: ClassVar[list[dict[str, Any]]] = []

    def log_message(self, *args: object) -> None:  # keep test output clean
        return

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.endswith("/models"):
            self._send(200, {"data": [{"id": "mock-model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        MockModelHandler.requests.append(payload)
        user_messages = [m for m in payload["messages"] if m["role"] == "user"]
        text = user_messages[-1]["content"] if user_messages else ""
        if isinstance(text, list):
            text = " ".join(part.get("text", "") for part in text if isinstance(part, dict))
        self._send(
            200,
            {
                "model": payload["model"],
                "choices": [{"message": {"content": f"mock:{text}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


@pytest.fixture
def mock_model() -> str:
    MockModelHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.e2e
async def test_stdio_roundtrip_and_session_persistence(mock_model: str, tmp_path: Path) -> None:
    config = tmp_path / "family.yaml"
    config.write_text(
        f"""\
version: 1
members:
  mock:
    base_url: "{mock_model}"
    model: "mock-model"
    trust: "safe_local"
    capabilities: ["chat"]
    max_clones: 2
sessions:
  db_path: "{tmp_path / "sessions.db"}"
""",
        encoding="utf-8",
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sub_family.cli", "serve", "--config", str(config)],
    )

    async with Client(params, read_timeout_seconds=20) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert "ask_family" in tools
        result = await client.call_tool(
            "ask_family", {"member": "mock", "prompt": "first", "session_id": "e2e"}
        )
        assert json_content(result)["content"] == "mock:first"

    # A fresh server process must replay the persisted session history.
    async with Client(params, read_timeout_seconds=20) as client:
        result = await client.call_tool(
            "ask_family", {"member": "mock", "prompt": "second", "session_id": "e2e"}
        )
        assert json_content(result)["content"] == "mock:second"
        status = json_content(await client.call_tool("family_status", {}))
        assert status["members"][0]["ok"] is True

    assert len(MockModelHandler.requests) == 2
    second_messages = MockModelHandler.requests[1]["messages"]
    assert [m["role"] for m in second_messages] == ["user", "assistant", "user"]
    assert second_messages[0]["content"] == "first"
