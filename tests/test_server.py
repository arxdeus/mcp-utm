"""Tests for the MCP server tool layer."""

from unittest.mock import patch
import asyncio
import base64
import pytest

from mcp_utm.server import mcp
from mcp_utm.applescript import VMInfo, VMConfig, DriveInfo
from mcp_utm import commands
from mcp.server.mcpserver.exceptions import ToolError


class TestToolsRegistered:
    def test_tool_count(self):
        tools = mcp._tool_manager.list_tools()
        assert len(tools) == 24

    def test_expected_tools(self):
        names = {t.name for t in mcp._tool_manager.list_tools()}
        expected = {
            "list_vms", "get_vm", "clone_vm", "start_vm", "stop_vm", "delete_vm",
            "suspend_vm", "wait_for_vm", "get_vm_ip", "set_vm_network",
            "set_vm_resources", "rename_vm", "set_vm_display", "list_vm_shares",
            "add_vm_share", "remove_vm_share", "set_vm_shares", "list_vm_drives",
            "attach_drive", "export_vm", "import_vm", "get_serial_port",
            "run_vm_command", "get_vm_command_result",
        }
        assert expected == names


class TestGuestCommandDispatch:
    def test_agent_failure_is_actionable_tool_error(self):
        with patch.object(commands.utm, "get_vm_status", return_value="started"), \
             patch.object(commands.utm, "_run", side_effect=RuntimeError("QEMU guest agent is not running")):
            with pytest.raises(ToolError, match="QEMU guest agent is not running"):
                asyncio.run(mcp.call_tool("run_vm_command", {"name": "Windows", "command": "echo hello"}))

    def test_unknown_handle_is_actionable_tool_error(self):
        with pytest.raises(ToolError, match="Unknown command_id"):
            asyncio.run(mcp.call_tool("get_vm_command_result", {"command_id": "missing"}))

    def test_awaited_result_through_mcp(self):
        async def scenario():
            result = await mcp.call_tool("run_vm_command", {
                "name": "Ubuntu", "command": "printf hello",
            })
            assert not result.is_error
            assert result.structured_content["stdout"] == "hello"
            assert result.structured_content["exit_code"] == 0
            assert result.structured_content["exited"] is True

        output = base64.b64encode(b"hello").decode()
        with patch.dict(commands._jobs, {}, clear=True), \
             patch.object(commands.utm, "get_vm_status", return_value="started"), \
             patch.object(commands.utm, "_run", side_effect=["123", f"done\n0\n-\n{output}\n\nEND"]):
            asyncio.run(scenario())

    def test_detached_and_polled_through_mcp(self):
        async def scenario():
            started = await mcp.call_tool("run_vm_command", {
                "name": "Windows", "command": "exit /b 7",
                "shell": "cmd.exe", "shell_args": ["/c"], "wait": False,
            })
            assert started.structured_content["exited"] is False
            handle = started.structured_content["command_id"]
            completed = await mcp.call_tool("get_vm_command_result", {"command_id": handle})
            assert completed.structured_content["exit_code"] == 7
            assert completed.structured_content["exited"] is True
            cached = await mcp.call_tool("get_vm_command_result", {"command_id": handle})
            assert cached.structured_content == completed.structured_content

        with patch.dict(commands._jobs, {}, clear=True), \
             patch.object(commands.utm, "get_vm_status", return_value="started"), \
             patch.object(commands.utm, "_run", side_effect=["123", "done\n7\n-\n\n\nEND"]) as run:
            asyncio.run(scenario())
            assert run.call_count == 2
