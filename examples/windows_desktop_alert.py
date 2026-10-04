"""Show a Windows desktop alert through mcp-utm, without passwords or GUI typing.

Run from the repository: uv run examples/windows_desktop_alert.py --vm Windows
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def encode_powershell(script: str) -> str:
    """PowerShell -EncodedCommand expects Base64 of UTF-16LE, not UTF-8."""
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def ps_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def desktop_script(message: str, title: str) -> str:
    alert = (
        "Add-Type -AssemblyName PresentationFramework; "
        f"[System.Windows.MessageBox]::Show({ps_literal(message)}, "
        f"{ps_literal(title)}, 'Ok', 'Exclamation') | Out-Null"
    )
    alert64 = encode_powershell(alert)
    return r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$user = (Get-CimInstance Win32_ComputerSystem).UserName
if (-not $user) { throw 'No logged-in console desktop user was found' }
$desktopSessions = @(Get-Process explorer -ErrorAction Stop | Select-Object -ExpandProperty SessionId -Unique)
$taskName = 'mcp-utm-alert-' + [guid]::NewGuid().ToString('N')
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument '-NoProfile -STA -WindowStyle Hidden -EncodedCommand ALERT64'
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$registered = $false
try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings | Out-Null
    $registered = $true
    $startedAt = Get-Date
    Start-ScheduledTask -TaskName $taskName
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        $task = Get-ScheduledTask -TaskName $taskName
        $info = Get-ScheduledTaskInfo -TaskName $taskName
        $started = $task.State.ToString() -eq 'Running' -or $info.LastRunTime -ge $startedAt.AddSeconds(-2)
    } while (-not $started -and [DateTime]::UtcNow -lt $deadline)
    if (-not $started) { throw 'The interactive task did not start within 30 seconds' }
    $state = $task.State.ToString()
    $lastResult = $info.LastTaskResult
    if ($state -ne 'Running' -and $lastResult -ne 0) {
        throw ('Interactive task failed with result ' + $lastResult)
    }
    # Deleting registration does not dismiss the already-running message box.
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    $registered = $false
    [pscustomobject]@{
        DesktopUser = $user
        ExplorerSessionIds = $desktopSessions
        TaskStateBeforeCleanup = $state
        TaskLastResult = $lastResult
        TemporaryTaskRegistrationRemoved = $true
    } | ConvertTo-Json -Depth 4
} finally {
    if ($registered) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
}
'''.replace("ALERT64", alert64)


async def show_alert(vm: str, message: str, title: str) -> None:
    # This launches this checkout's real MCP entry point, not a host PowerShell.
    server = StdioServerParameters(command=sys.executable, args=["-m", "mcp_utm"])
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=150) as client:
            await client.initialize()
            result = await client.call_tool("run_vm_command", {
                "name": vm,
                "shell": "powershell.exe",
                "shell_args": ["-NoProfile", "-NonInteractive", "-EncodedCommand"],
                "command": encode_powershell(desktop_script(message, title)),
                "timeout": 120,
            })
            if result.is_error:
                raise RuntimeError("\n".join(item.text for item in result.content if hasattr(item, "text")))
            data = result.structured_content
            if data is None:
                raise RuntimeError("MCP returned no structured command result")
            print(json.dumps(data, ensure_ascii=False, indent=2))
            if not data["exited"]:
                # Keep the same server alive: handles are server-local.
                followup = await client.call_tool("get_vm_command_result", {
                    "command_id": data["command_id"], "wait": True, "timeout": 120,
                })
                if followup.is_error:
                    raise RuntimeError(str(followup.content))
                data = followup.structured_content
                print(json.dumps(data, ensure_ascii=False, indent=2))
                if data is None or not data["exited"]:
                    raise RuntimeError("Dispatcher still running. The guest process was not terminated.")
            if data["exit_code"] != 0:
                raise RuntimeError(data["stderr"] or f"Guest dispatcher exited with {data['exit_code']}")
            print("Interactive alert dispatched. Check the logged-in Windows desktop.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", default="Windows", help="Registered UTM VM name")
    parser.add_argument("--message", default="Your alert message here")
    parser.add_argument("--title", default="Alert Title")
    args = parser.parse_args()
    asyncio.run(show_alert(args.vm, args.message, args.title))


if __name__ == "__main__":
    main()
