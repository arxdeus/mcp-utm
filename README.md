# mcp-utm

MCP server for managing [UTM](https://mac.getutm.app/) virtual machines on macOS via AppleScript.

Provides 24 tools for cloning, configuring, controlling UTM VMs, and awaiting guest shell commands. Includes proper MAC address randomization for Apple Virtualization Framework clones, which enables concurrent VMs with unique network identities.

## Install

```bash
# PyPI
uvx mcp-utm

# Or install globally
uv tool install mcp-utm
pip install mcp-utm
```

## Claude Code config

```json
{
  "mcpServers": {
    "utm": {
      "command": "uvx",
      "args": ["mcp-utm"]
    }
  }
}
```

## Requirements

- **macOS** (uses AppleScript / `osascript`)
- **UTM 4.6+** ([download](https://mac.getutm.app/) or `brew install --cask utm`)
- **Python 3.11+**

## Tools

### Lifecycle
| Tool | Description |
|------|-------------|
| `list_vms` | List all registered VMs with status |
| `get_vm` | Get status and configuration of a VM |
| `clone_vm` | Clone a template with unique random MAC |
| `start_vm` | Start a stopped or suspended VM |
| `stop_vm` | Stop a running VM (graceful or force) |
| `delete_vm` | Delete a VM permanently |

### State
| Tool | Description |
|------|-------------|
| `suspend_vm` | Suspend a running VM to memory |
| `wait_for_vm` | Poll until VM reaches a target status |

### Networking
| Tool | Description |
|------|-------------|
| `get_vm_ip` | Discover VM IP via ARP table |
| `set_vm_network` | Update MAC address or network mode |

### Configuration
| Tool | Description |
|------|-------------|
| `set_vm_resources` | Update memory and CPU cores |
| `rename_vm` | Rename a VM |
| `set_vm_display` | Toggle dynamic resolution |

### Directory Shares (VirtioFS)
| Tool | Description |
|------|-------------|
| `list_vm_shares` | List shared directories |
| `add_vm_share` | Add a host directory share |
| `remove_vm_share` | Remove a directory share |
| `set_vm_shares` | Replace all shares |

### Drives
| Tool | Description |
|------|-------------|
| `list_vm_drives` | List attached drives |
| `attach_drive` | Attach an ISO or disk image |

### Portability
| Tool | Description |
|------|-------------|
| `export_vm` | Export VM to a `.utm` file |
| `import_vm` | Import VM from a `.utm` file |

### Console
| Tool | Description |
|------|-------------|
| `get_serial_port` | Get serial port address for console access |

### Guest commands
| Tool | Description |
|------|-------------|
| `run_vm_command` | Execute a guest shell command and await completion, or return a pollable handle |
| `get_vm_command_result` | Poll or await a previously started command, including after a wait timeout |

## Running shell commands

Guest execution uses UTM's native [`execute` and `get result` API](https://docs.getutm.app/scripting/reference/), not host shell execution or simulated typing. The VM must use the **QEMU backend** and be running with **QEMU Guest Agent installed, running, and connected to UTM**. On Debian/Ubuntu guests, install `qemu-guest-agent` and enable/start its service. On Windows, install the QEMU Guest Agent service from the guest tools package. Some agent configurations disable `guest-exec` or `guest-exec-status`, which must be allowed.

**Apple Virtualization guests (both macOS and Linux) do not support this native guest-agent route.** Those guests need another transport such as SSH, which these tools do not currently implement. Merely installing SPICE display tools does not guarantee the QEMU Guest Agent is running. See the [UTM scripting examples](https://docs.getutm.app/scripting/cheat-sheet/#execute-commands).

Pass these arguments to `run_vm_command` for a Linux guest:

```json
{
  "name": "Ubuntu",
  "command": "uname -a && printf 'hello\\n'",
  "timeout": 60
}
```

The default shell is `/bin/sh` with `shell_args: ["-c"]`. Commands can use pipelines, redirects, or `cd` to select a working directory. For Windows, specify the shell and its command flag explicitly:

```json
{
  "name": "Windows",
  "command": "ver && echo hello",
  "shell": "cmd.exe",
  "shell_args": ["/c"],
  "timeout": 60
}
```

PowerShell is also supported with `shell: "powershell.exe"` and `shell_args: ["-NoProfile", "-Command"]`. Optional `environment` supplies a mapping of guest environment variables, and `stdin` supplies text input.

The result contains `command_id`, `name`, guest `pid`, `exited`, `exit_code`, `signal_code`, `stdout`, `stderr`, `stdout_truncated`, `stderr_truncated`, and `timed_out`. Nonzero guest exit codes are returned normally rather than raised as transport errors. Output preserves whitespace and is decoded as UTF-8, replacing invalid bytes. `max_output_bytes` caps each returned stream (default 1 MiB, maximum 16 MiB). These truncation flags describe the server's cap only. The guest agent may impose its own capture limit that UTM's API does not expose.

For longer work, use `wait: false` to receive the handle after launch, then call `get_vm_command_result`:

```json
{
  "command_id": "<command_id from run_vm_command>",
  "wait": true,
  "timeout": 120
}
```

- `wait` defaults to `true` for launching and `false` for result polling. Timeouts accept 1 to 600 seconds per wait, measured after launch. UTM launch and status calls have their own transport timeout.
- **Timeout or request cancellation does not kill the guest process.** A wait timeout returns `timed_out: true` and the handle, so you can await again without re-running the command.
- Waiting is asynchronous and does not block the MCP event loop. **Live output streaming is not available:** QEMU Guest Agent supplies captured stdout/stderr only after the process exits. Running snapshots therefore have empty output and null exit/signal codes.
- Handles belong to one MCP server process and are lost on restart. Completed results are cached for at least an hour, avoiding repeat reads of the guest agent's consumable results. The registry holds at most 128 commands, including cached completions. Poll outstanding commands to mark them complete. Expired completions are removed when another command launches.
- Commands run with guest-agent privileges, commonly **root or SYSTEM**. Only run trusted commands. These tools do not automatically install agents, start VMs, or change authentication.

## Development

```bash
uv run --extra test pytest
```

Unit tests mock UTM so they do not require a guest VM. Actual guest execution requires the setup above.

After configuring the agent, run the opt-in live smoke test (prints test output and returns exit code 7 inside the guest):

```bash
MCP_UTM_TEST_VM=Ubuntu uv run --extra test pytest tests/test_guest_integration.py
# Windows guest
MCP_UTM_TEST_VM=Windows MCP_UTM_TEST_SHELL=cmd.exe uv run --extra test pytest tests/test_guest_integration.py
```

## How MAC randomization works

Apple's Virtualization Framework ignores `MacAddress` changes written directly to `config.plist` — UTM caches the config in memory. This server uses AppleScript's `update configuration` command which properly updates UTM's internal state, giving each clone a unique MAC and therefore a unique IP on the `192.168.64.0/24` subnet.

## License

MIT
