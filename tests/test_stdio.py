"""Exercise the real server entry point and MCP stdio protocol without mocks."""

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_command_tools_over_real_stdio():
    async def scenario():
        server = StdioServerParameters(command=sys.executable, args=["-m", "mcp_utm"])
        async with stdio_client(server) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=20) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert len(tools.tools) == 24
                assert {"run_vm_command", "get_vm_command_result"} <= {tool.name for tool in tools.tools}
                # Validation occurs before contacting UTM, so this runs on any OS.
                invalid = await client.call_tool("run_vm_command", {
                    "name": "Ubuntu", "command": "echo hello", "timeout": 0,
                })
                assert invalid.is_error
                assert "timeout must be" in invalid.content[0].text
                unknown = await client.call_tool("get_vm_command_result", {"command_id": "missing"})
                assert unknown.is_error
                assert "Unknown command_id" in unknown.content[0].text
                assert len((await client.list_tools()).tools) == 24

    asyncio.run(scenario())
