"""Awaitable guest shell execution using UTM's QEMU Guest Agent bridge.

QGA only exposes captured output when a process exits, not live output.
Completed results are cached because guest-exec-status consumes them.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import time
import uuid
from dataclasses import dataclass, field

from . import applescript as utm

_MAX_JOBS = 128
_RESULT_TTL = 3600
_POLL_INTERVAL = 0.25


@dataclass
class _Job:
    name: str
    pid: int
    max_output_bytes: int
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    result: dict | None = None
    completed_at: float | None = None


_jobs: dict[str, _Job] = {}
_start_lock = asyncio.Lock()


def _text(value: str) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise ValueError("Expected text without NUL characters")
    return '"' + utm._esc(value) + '"'


def _list(values: list[str]) -> str:
    return "{" + ", ".join(_text(value) for value in values) + "}"


def _timeout(timeout: int) -> None:
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 600:
        raise ValueError("timeout must be an integer between 1 and 600 seconds")


def _snapshot(command_id: str, job: _Job) -> dict[str, object]:
    return {
        "command_id": command_id, "name": job.name, "pid": job.pid,
        "exited": False, "exit_code": None, "signal_code": None,
        "stdout": "", "stderr": "", "stdout_truncated": False,
        "stderr_truncated": False, "timed_out": False,
    }


def _decode(data: str, limit: int) -> tuple[str, bool]:
    raw = base64.b64decode(data, validate=True)
    return raw[:limit].decode("utf-8", errors="replace"), len(raw) > limit


async def _fetch(command_id: str, job: _Job) -> dict[str, object]:
    # Serialize even concurrent callers: fetching a terminal QGA result consumes it.
    async with job.lock:
        if job.result is not None:
            return dict(job.result)
        script = f'''
        tell application "UTM"
            set vm to virtual machine named {_text(job.name)}
            set res to get result of (guest process id {job.pid} of vm)
            if not (exited of res) then return "running"
            set exitValue to "-"
            set signalValue to "-"
            try
                if exit code of res is not missing value then set exitValue to (exit code of res) as text
            end try
            try
                if signal code of res is not missing value then set signalValue to (signal code of res) as text
            end try
            return "done" & linefeed & exitValue & linefeed & signalValue & linefeed & (output data of res) & linefeed & (error data of res) & linefeed & "END"
        end tell
        '''
        raw = await asyncio.to_thread(utm._run, script)
        result = _snapshot(command_id, job)
        if raw == "running":
            return result
        parts = raw.split("\n")
        try:
            if len(parts) != 6 or parts[0] != "done" or parts[5] != "END":
                raise ValueError("Invalid result frame")
            result["exit_code"] = None if parts[1] == "-" else int(parts[1])
            result["signal_code"] = None if parts[2] == "-" else int(parts[2])
            result["stdout"], result["stdout_truncated"] = _decode(parts[3], job.max_output_bytes)
            result["stderr"], result["stderr_truncated"] = _decode(parts[4], job.max_output_bytes)
        except (ValueError, binascii.Error) as exc:
            raise RuntimeError("Malformed UTM guest execution result") from exc
        result["exited"] = True
        job.result = result
        job.completed_at = time.monotonic()
        return dict(result)


async def get_vm_command_result(
    command_id: str, wait: bool = False, timeout: int = 60,
) -> dict[str, object]:
    """Poll or await a command started by this MCP server.

    Results remain available for at least one hour while the server runs.
    A timeout stops waiting only, it does not terminate the guest process.
    QEMU Guest Agent supplies stdout/stderr only after completion, not live.
    """
    _timeout(timeout)
    if not isinstance(wait, bool):
        raise ValueError("wait must be a boolean")
    if not isinstance(command_id, str) or command_id not in _jobs:
        raise ValueError("Unknown command_id (handles are local to this server and may expire)")
    job = _jobs[command_id]
    deadline = time.monotonic() + timeout
    while True:
        # Cache a consumed result even if the MCP request is cancelled mid-fetch.
        task = asyncio.create_task(_fetch(command_id, job))
        # Retrieve background exceptions when a caller is cancelled.
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        try:
            result = await asyncio.wait_for(
                asyncio.shield(task), timeout=max(0, deadline - time.monotonic()),
            )
        except TimeoutError:
            return {**_snapshot(command_id, job), "timed_out": True}
        if result["exited"] or not wait:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {**result, "timed_out": True}
        await asyncio.sleep(min(_POLL_INTERVAL, remaining))


async def run_vm_command(
    name: str,
    command: str,
    shell: str = "/bin/sh",
    shell_args: list[str] | None = None,
    environment: dict[str, str] | None = None,
    stdin: str | None = None,
    wait: bool = True,
    timeout: int = 60,
    max_output_bytes: int = 1048576,
) -> dict[str, object]:
    """Run a shell command inside a running UTM guest, not on the host.

    Requires QEMU Guest Agent (not supported by Apple VF macOS guests).
    Defaults to /bin/sh -c. For Windows use shell='cmd.exe', shell_args=['/c'],
    or shell='powershell.exe', shell_args=['-NoProfile', '-Command'].
    environment is a mapping of guest variable names to values. stdin is text.
    wait=False returns a command_id immediately. Otherwise await completion up
    to timeout (1..600 seconds), then poll get_vm_command_result with the handle.
    Timeout/cancellation does not kill the guest process. Output is UTF-8 with
    replacement for invalid bytes, capped per stream at max_output_bytes
    (1..16777216). QGA itself may also limit output. Commands run with guest
    agent privileges, commonly root/SYSTEM. No live output streaming is available.
    """
    utm._validate_vm_name(name)
    _timeout(timeout)
    if not isinstance(wait, bool):
        raise ValueError("wait must be a boolean")
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or not 1 <= max_output_bytes <= 16777216:
        raise ValueError("max_output_bytes must be an integer between 1 and 16777216")
    if not command or not shell:
        raise ValueError("command and shell must not be empty")
    args = ["-c"] if shell_args is None else shell_args
    if not isinstance(args, list):
        raise ValueError("shell_args must be a list of strings")
    execute = f"execute vm at {_text(shell)} with arguments {_list([*args, command])} with output capturing"
    if environment is not None:
        if not isinstance(environment, dict):
            raise ValueError("environment must be a mapping of names to strings")
        entries = []
        for key, value in environment.items():
            if not isinstance(key, str) or not key or "=" in key or "\0" in key:
                raise ValueError("Invalid environment variable name")
            _text(value)
            entries.append(f"{key}={value}")
        execute += f" with environment {_list(entries)}"
    if stdin is not None:
        execute += f" using input {_text(stdin)}"
    script = f'''
    tell application "UTM"
        set vm to virtual machine named {_text(name)}
        set proc to {execute}
        return id of proc
    end tell
    '''
    async with _start_lock:
        now = time.monotonic()
        for handle, job in list(_jobs.items()):
            if job.completed_at is not None and now - job.completed_at > _RESULT_TTL:
                del _jobs[handle]
        if len(_jobs) >= _MAX_JOBS:
            raise RuntimeError("Command registry is full. Poll outstanding commands and wait for cached results to expire.")
        if await asyncio.to_thread(utm.get_vm_status, name) != "started":
            raise ValueError("VM must be running before executing commands")
        try:
            pid = int(await asyncio.to_thread(utm._run, script))
            if pid <= 0:
                raise ValueError("Invalid PID")
        except ValueError as exc:
            raise RuntimeError("UTM returned an invalid guest process ID") from exc
        command_id = str(uuid.uuid4())
        job = _Job(name, pid, max_output_bytes)
        _jobs[command_id] = job
    if not wait:
        return _snapshot(command_id, job)
    return await get_vm_command_result(command_id, wait=True, timeout=timeout)
