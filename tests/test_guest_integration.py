"""Opt-in real UTM guest smoke test. Does not install or modify guest tools."""

import asyncio
import os
import sys

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.skipif(not os.environ.get("MCP_UTM_TEST_VM"), reason="Set MCP_UTM_TEST_VM for real guest execution")
def test_real_guest_execution():
    name = os.environ["MCP_UTM_TEST_VM"]
    shell = os.environ.get("MCP_UTM_TEST_SHELL", "/bin/sh")
    windows = shell.lower() == "cmd.exe"
    command = (
        "echo mcp-utm-live-stdout & echo mcp-utm-live-stderr 1>&2 & exit /b 7"
        if windows else
        "printf 'mcp-utm-live-stdout\\n'; printf 'mcp-utm-live-stderr\\n' >&2; exit 7"
    )

    async def scenario():
        server = StdioServerParameters(command=sys.executable, args=["-m", "mcp_utm"])
        async with stdio_client(server) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=40) as client:
                await client.initialize()
                started = await client.call_tool("run_vm_command", {
                    "name": name, "command": command, "shell": shell,
                    "shell_args": ["/c"] if windows else ["-c"], "wait": False,
                })
                assert not started.is_error, started.content
                handle = started.structured_content["command_id"]
                result = await client.call_tool("get_vm_command_result", {
                    "command_id": handle, "wait": True, "timeout": 30,
                })
                assert not result.is_error, result.content
                completed = result.structured_content
                assert completed["exited"] is True
                assert completed["exit_code"] == 7
                assert "mcp-utm-live-stdout" in completed["stdout"]
                assert "mcp-utm-live-stderr" in completed["stderr"]
                cached = await client.call_tool("get_vm_command_result", {"command_id": handle})
                assert cached.structured_content == completed

    asyncio.run(scenario())
