"""Opt-in real UTM guest smoke test. Does not install or modify guest tools."""

import asyncio
import os

import pytest

from mcp_utm.commands import run_vm_command, get_vm_command_result


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
        launched = await run_vm_command(
            name, command, shell=shell, shell_args=["/c"] if windows else ["-c"], wait=False,
        )
        completed = await get_vm_command_result(launched["command_id"], wait=True, timeout=30)
        assert completed["exited"] is True
        assert completed["exit_code"] == 7
        assert "mcp-utm-live-stdout" in completed["stdout"]
        assert "mcp-utm-live-stderr" in completed["stderr"]
        assert await get_vm_command_result(launched["command_id"]) == completed

    asyncio.run(scenario())
