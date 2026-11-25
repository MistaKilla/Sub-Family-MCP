"""MCP tool surface, exercised through the SDK's in-memory client."""

from __future__ import annotations

from mcp import Client
from tests.conftest import json_content

from sub_family.server import create_server


async def test_tools_are_registered_with_descriptions(service) -> None:
    svc, _ = service
    server = create_server(svc)
    async with Client(server) as client:
        tools = await client.list_tools()
    names = {tool.name for tool in tools.tools}
    assert {
        "list_family",
        "family_status",
        "ask_family",
        "ask_family_swarm",
        "list_sessions",
        "session_history",
        "reset_session",
    } <= names
    by_name = {tool.name: tool for tool in tools.tools}
    assert "untrusted_cloud" in (by_name["ask_family"].description or "")
    assert "vision" in (by_name["ask_family"].description or "")


async def test_list_family_tool(service) -> None:
    svc, _ = service
    server = create_server(svc)
    async with Client(server) as client:
        result = await client.call_tool("list_family", {})
    payload = json_content(result)
    assert payload["count"] == 2
    assert {member["name"] for member in payload["members"]} == {"lilbro", "big_brother"}
    assert any(member["vision"] for member in payload["members"])


async def test_ask_family_tool(service) -> None:
    svc, provider = service
    server = create_server(svc)
    async with Client(server) as client:
        result = await client.call_tool(
            "ask_family", {"member": "lilbro", "prompt": "hi", "session_id": "mcp-1"}
        )
    payload = json_content(result)
    assert payload["ok"] is True
    assert payload["content"] == "fake answer:hi"
    assert payload["session_id"] == "mcp-1"
    assert provider.calls


async def test_unknown_member_is_a_tool_error(service) -> None:
    svc, _ = service
    server = create_server(svc)
    async with Client(server) as client:
        result = await client.call_tool("ask_family", {"member": "ghost", "prompt": "hi"})
    assert result.is_error
    assert "Available members" in result.content[0].text


async def test_swarm_tool(service) -> None:
    svc, _ = service
    server = create_server(svc)
    async with Client(server) as client:
        result = await client.call_tool(
            "ask_family_swarm", {"member": "lilbro", "tasks": ["x", "y"]}
        )
    payload = json_content(result)
    assert payload["summary"] == {"total": 2, "succeeded": 2, "failed": 0}


async def test_session_tools(service) -> None:
    svc, _ = service
    await svc.ask("lilbro", "one", session_id="sess")
    server = create_server(svc)
    async with Client(server) as client:
        listed = json_content(await client.call_tool("list_sessions", {}))
        history = json_content(
            await client.call_tool("session_history", {"member": "lilbro", "session_id": "sess"})
        )
        removed = json_content(
            await client.call_tool("reset_session", {"member": "lilbro", "session_id": "sess"})
        )
    assert listed["count"] == 1
    assert history["messages"][0]["content"] == "one"
    assert removed["messages"] == 2


async def test_family_status_unknown_member_is_tool_error(service) -> None:
    svc, _ = service
    server = create_server(svc)
    async with Client(server) as client:
        result = await client.call_tool("family_status", {"member": "ghost"})
    assert result.is_error
    assert "Available members" in result.content[0].text


async def test_family_status_tool(service) -> None:
    svc, _ = service
    server = create_server(svc)
    async with Client(server) as client:
        result = await client.call_tool("family_status", {"member": "lilbro"})
    payload = json_content(result)
    assert payload["members"][0]["name"] == "lilbro"
    assert payload["members"][0]["ok"] is True
