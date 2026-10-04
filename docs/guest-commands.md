# Guest shell commands and visible Windows desktop alerts

This guide covers two different jobs:

1. Run a command in a UTM guest and await its stdout, stderr, and exit status.
2. Show a Windows alert in the **logged-in user's desktop**, rather than in the guest-agent service session.

The second workflow was verified in a running Windows VM through the MCP tools, and the user confirmed seeing the alert.

## Prerequisites

- macOS, UTM 4.6+, and a running VM using the **QEMU backend**.
- QEMU Guest Agent installed, running, and connected to UTM.
- The agent must allow `guest-exec` and `guest-exec-status`.
- For desktop alerts, a user must already be logged in to the Windows console desktop. Task Scheduler and Windows PowerShell must be available.

Apple Virtualization guests, including macOS and Linux guests, do not support this native execution route. SSH is not implemented by these tools.

### Windows agent setup

Follow [UTM's Windows Guest Tools instructions](https://docs.getutm.app/guest-support/windows/):

1. Start the VM. In its UTM toolbar, click the CD icon and choose **Install Windows Guest Tools…**.
2. Inside Windows, open the mounted **UTM** CD and run `spice-guest-tools-xxx.exe`.
3. Check that the **QEMU Guest Agent** component/service is installed. Display or clipboard support alone does not prove the agent is running.
4. In administrator PowerShell inside Windows, check the service:

```powershell
Get-Service QEMU-GA
# If the service exists but is stopped:
Start-Service QEMU-GA
```

If the service is absent, install the QEMU Guest Agent component matching your guest architecture. If it is running but UTM still cannot reach it, check the guest-agent serial channel/configuration in UTM and the guest tools installation.

## 1. Await a Linux shell command

Call `run_vm_command` with these MCP arguments:

```json
{
  "name": "Ubuntu",
  "command": "uname -a && printf 'hello\\n'",
  "timeout": 60
}
```

The default executable is `/bin/sh`, with `shell_args: ["-c"]`. Shell syntax such as pipes, redirects, and `cd /path && command` runs **inside the guest**, not on the Mac.

Pass guest variables and text input when needed:

```json
{
  "name": "Ubuntu",
  "command": "printf '%s\\n' \"$GREETING\" && cat",
  "environment": {"GREETING": "hello from MCP"},
  "stdin": "text sent to guest stdin\n",
  "timeout": 60
}
```

## 2. Await a Windows command

Specify the Windows shell and its command flag explicitly:

```json
{
  "name": "Windows",
  "shell": "cmd.exe",
  "shell_args": ["/c"],
  "command": "ver && echo hello",
  "timeout": 60
}
```

Successful launch is not the same as successful command completion. Inspect `exited` and `exit_code`, not just the MCP error flag.

## 3. PowerShell with robust quoting and Unicode

For nontrivial PowerShell, use **`-EncodedCommand`**. Its payload is Base64 of **UTF-16LE**, not UTF-8. This avoids nested quoting across Python/JSON, AppleScript, QEMU Guest Agent, and Windows argument parsing. Encoded commands are not encryption.

Generate an MCP argument object on the Mac from this checkout:

```bash
uv run python - <<'PY'
import base64
import json

script = """
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
[pscustomobject]@{
    Identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    SessionId = [Diagnostics.Process]::GetCurrentProcess().SessionId
} | ConvertTo-Json
"""
arguments = {
    "name": "Windows",
    "shell": "powershell.exe",
    "shell_args": ["-NoProfile", "-NonInteractive", "-EncodedCommand"],
    "command": base64.b64encode(script.encode("utf-16le")).decode("ascii"),
    "timeout": 60,
}
print(json.dumps(arguments, indent=2))
PY
```

Pass the printed object to `run_vm_command`. This snippet **generates arguments only**, it does not execute the command. The runnable alert example below opens a real MCP connection and calls the tool for you.

`[Console]::OutputEncoding` makes PowerShell text suitable for this server's UTF-8 decoding. Native Windows commands may still emit their own OEM code page. PowerShell can also write progress records as CLIXML to stderr. `stderr` being nonempty does not, by itself, mean the process failed.

## 4. Background launch and awaiting later

Launch without waiting:

```json
{
  "name": "Ubuntu",
  "command": "sleep 5 && printf 'finished\\n'",
  "wait": false
}
```

Keep the returned `command_id`, then call `get_vm_command_result` **on the same running MCP server**:

```json
{
  "command_id": "<returned command_id>",
  "wait": true,
  "timeout": 120
}
```

Use `wait: false` on the result tool for a single status poll. `wait` defaults to `true` for launch and `false` for polling.

| Result field | Meaning |
|---|---|
| `command_id` | Server-local handle for polling |
| `name`, `pid` | VM name and guest process ID |
| `exited` | Whether the process has completed |
| `exit_code` | Normal exit status, or null when unavailable |
| `signal_code` | Guest signal/exception status, or null when unavailable. Some guest agents report zero for no signal |
| `stdout`, `stderr` | Captured UTF-8 text, available after completion |
| `stdout_truncated`, `stderr_truncated` | Whether this server's per-stream byte cap shortened output |
| `timed_out` | The current wait reached its deadline |

Important behavior:

- **A wait timeout does not kill the guest process.** Poll the same handle rather than launching the command again.
- Timeouts accept 1 to 600 seconds per wait. Launch and transport calls have their own timeouts.
- No live stdout/stderr streaming is available through UTM's guest-agent API. Running snapshots have empty output.
- `max_output_bytes` caps each returned stream: 1 MiB by default, maximum 16 MiB. The guest agent can have its own capture limit, which UTM does not expose through these flags.
- Handles disappear when the MCP server exits. Starting a new stdio server for each poll will not recover a previous handle.
- Terminal results are cached for at least an hour while the server runs. At most 128 commands, including cached completions, are retained. Poll outstanding commands to mark them complete. Expired completions are removed on a subsequent launch.

## 5. Show the alert on the logged-in Windows desktop

### Why a direct MessageBox can be invisible

QEMU Guest Agent commonly runs as **SYSTEM in Session 0**. Your logged-in desktop runs in another session, typically Session 1. A successful guest command can therefore create a dialog you cannot see.

Do not rely on this direct service-session command to display interactive UI:

```powershell
Add-Type -AssemblyName PresentationFramework
[System.Windows.MessageBox]::Show('Your alert message here', 'Alert Title', 'Ok', 'Exclamation')
```

The script can stay running because the modal message box is waiting for someone to click OK in an inaccessible session. A timeout is not proof that the dialog appeared on your desktop.

### Working approach: an interactive-token scheduled task

From the guest-agent process:

1. Resolve the currently logged-in console user.
2. Create a uniquely named task with `LogonType Interactive` and `RunLevel Limited`.
3. Start a separate PowerShell process as that user with `-STA`, using an encoded alert script.
4. Confirm that Task Scheduler started the task.
5. Remove the temporary registration. The already-running dialog remains until the user dismisses it.

The key guest-side PowerShell operations are:

```powershell
$user = (Get-CimInstance Win32_ComputerSystem).UserName
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
# $alert64 is UTF-16LE/Base64 of the MessageBox script above.
$action = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument "-NoProfile -STA -WindowStyle Hidden -EncodedCommand $alert64"
# Use a unique name, start it, verify startup, then unregister in try/finally.
```

These lines illustrate the mechanism, not a complete standalone script. Use the runnable example for registration, startup checking, encoding, and cleanup.

### Runnable MCP example

From the repository root on your Mac:

```bash
uv run examples/windows_desktop_alert.py --vm Windows
```

Customize the visible message and title:

```bash
uv run examples/windows_desktop_alert.py \
  --vm Windows \
  --message "Your alert message here" \
  --title "Alert Title"
```

The [complete example](../examples/windows_desktop_alert.py):

- Starts this checkout's real `mcp-utm` stdio server and invokes **`run_vm_command`**.
- Encodes both the dispatcher and the alert as UTF-16LE/Base64.
- Uses the existing user's interactive token, without requesting a password.
- Creates a one-off, limited-privilege task with **no recurring trigger**.
- Checks startup and removes its own uniquely named task registration.
- Keeps the same MCP server alive if it needs to await the dispatcher again.
- Prints the dispatcher PID/exit status and JSON describing the selected user/session and cleanup.

**The MCP result belongs to the dispatcher, not the dialog process.** Exit code 0 means the task was dispatched and its registration cleaned up. It does not mean the user clicked OK, and it does not capture the alert's button result. Verify visibility in the Windows desktop. This recipe targets a single logged-in console user, not an arbitrary RDP session or a specific user in a multi-user environment.

`TaskLastResult: 267009` (`0x41301`) means the task is currently running, not that it failed. It is normal alongside `TaskStateBeforeCleanup: "Running"` while a modal alert waits for OK.

On normal guest dispatcher completion or a PowerShell error, the `finally` block attempts cleanup. Cancelling the host MCP request does not terminate the guest or force immediate cleanup. If the guest crashes or the process is force-killed, inspect Task Scheduler for a leftover task named `mcp-utm-alert-<random-id>` and remove only the task created by that invocation. Do not broadly delete unrelated tasks.

## Troubleshooting and safety

| Symptom | Action |
|---|---|
| “QEMU guest agent is not running or not installed” | Install/start the agent and check its connection to UTM |
| Backend unsupported | Use a QEMU-backed guest. Apple Virtualization needs another transport |
| Command launched but no visible alert | Use the interactive task recipe, not the service's Session 0 |
| No logged-in console desktop user | Log into the Windows console desktop first |
| Task registration denied | The guest-agent identity needs permission to register/start the task. Do not change passwords or weaken security policies |
| Garbled native Windows output | Prefer PowerShell UTF-8 output or explicitly configure the command's output encoding |
| Unknown `command_id` | Keep the original server alive and use its returned handle |

Normal guest commands run with the guest-agent identity, often root/SYSTEM. Only run trusted commands. The desktop task itself runs as the logged-in user with limited privileges. No keystroke injection, desktop clicking, or host PowerShell is used by this recipe.

## Reproduce the command-result acceptance test

This test uses a **real MCP stdio client and the real guest**, without mocks:

```bash
MCP_UTM_TEST_VM=Windows MCP_UTM_TEST_SHELL=cmd.exe \
  uv run --extra test pytest tests/test_guest_integration.py -q
```

It checks detached launch, awaiting stdout/stderr, an intentional guest exit code of 7, and repeat cached polling. It does not display a message box.
