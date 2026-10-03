import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_real_stdio_mcp_roundtrip(tmp_path):
    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "dao.mcp_server", "--db", str(tmp_path / "mcp.db")],
            env=dict(os.environ),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                names = {tool.name for tool in listing.tools}
                assert {
                    "read_state",
                    "project_state",
                    "decide",
                    "propose_action",
                    "execute_action",
                    "usage",
                } <= names
                assert "adjudicate_action" not in names
                result = await session.call_tool("read_state", {"branch": "main"})
                assert not result.isError
                head_before = json.loads(result.content[0].text)["head"]
                audit_before = await session.read_resource("dao://audit")
                projected = await session.call_tool("project_state", {"branch": "main"})
                assert not projected.isError
                projection = projected.structuredContent
                assert projection["branch"] == "main"
                assert projection["head"] == head_before
                assert "event_seq" in projection
                assert isinstance(projection["claims"], list)
                assert "coverage" in projection
                narrative = await session.read_resource("dao://projection/main")
                assert narrative.contents[0].text == projection["text"]
                head_after = await session.call_tool("read_state", {"branch": "main"})
                assert json.loads(head_after.content[0].text)["head"] == head_before
                audit_after = await session.read_resource("dao://audit")
                assert audit_after.contents == audit_before.contents
                fork = await session.call_tool("branch_state", {"name": "experiment"})
                assert not fork.isError
                integrity = await session.call_tool("verify_integrity", {})
                assert not integrity.isError
                resources = await session.list_resources()
                assert "dao://branches" in {str(r.uri) for r in resources.resources}

    asyncio.run(run())
